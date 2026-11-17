import uuid
from datetime import datetime, timezone, timedelta

from botocore.exceptions import ClientError
from aws_lambda_powertools import Logger, Tracer, Metrics
from aws_lambda_powertools.metrics import MetricUnit
from aws_lambda_powertools.utilities.typing import LambdaContext

# Configuración común
from common import config
from common.logging_utils import log_with_context
from common.aws_utils import assume_role
from common.account_config import get_managed_accounts
from common.audit import put_audit_event, update_retention_state

SERVICE_NAME = "RdsRetention"
MANAGED_SNAPSHOT_TAGS = {
    "CreatedBy": "RdsBackupLambda",
    "Service": "RdsBackup",
}

logger = Logger(service=SERVICE_NAME)
tracer = Tracer(service=SERVICE_NAME)
metrics = Metrics(namespace="RdsBackup", service=SERVICE_NAME)

is_cold_start = True


def get_all_snapshots(rds_client):
    snapshots = rds_client.describe_db_snapshots(SnapshotType="manual")["DBSnapshots"]
    log_with_context(f"Se encontraron {len(snapshots)} snapshots manuales", step="list_snapshots")
    return snapshots


def is_managed_snapshot(tags):
    tag_values = {tag.get("Key"): tag.get("Value") for tag in tags}
    return all(
        tag_values.get(key) == value
        for key, value in MANAGED_SNAPSHOT_TAGS.items()
    )


def get_snapshot_db_identifier(snapshot):
    return (
        snapshot.get("DBInstanceIdentifier")
        or snapshot.get("DBClusterIdentifier")
        or snapshot["DBSnapshotIdentifier"]
    )


def audit_retention_result(
    *,
    account,
    rds,
    snapshot,
    event_type,
    status,
    message,
    error=None,
    skip_reason=None,
    operation_id=None,
):
    db_identifier = get_snapshot_db_identifier(snapshot)
    snapshot_id = snapshot["DBSnapshotIdentifier"]
    put_audit_event(
        event_type=event_type,
        account_id=account["account_id"],
        account_name=account["account_name"],
        region=rds.meta.region_name,
        db_identifier=db_identifier,
        snapshot_id=snapshot_id,
        status=status,
        source=SERVICE_NAME,
        message=message,
        error=error,
        skip_reason=skip_reason,
        operation_id=operation_id,
    )
    update_retention_state(
        account_id=account["account_id"],
        account_name=account["account_name"],
        region=rds.meta.region_name,
        db_identifier=db_identifier,
        status=status,
        snapshot_id=snapshot_id if event_type == "SNAPSHOT_DELETED" else None,
        error=error,
        operation_id=operation_id,
    )


def delete_snapshot_if_expired(rds, snapshot, cutoff_date, retention_days, account, operation_id):
    snapshot_id = snapshot["DBSnapshotIdentifier"]
    creation_time = snapshot["SnapshotCreateTime"]

    try:
        tags_response = rds.list_tags_for_resource(ResourceName=snapshot["DBSnapshotArn"])
        tags = tags_response.get("TagList", [])
        tag_summary = {tag['Key']: tag['Value'] for tag in tags if tag['Key'] in ['CreatedBy', 'Service', 'AccountId']}
        log_with_context(f"Etiquetas encontradas para snapshot '{snapshot_id}'", step="tag_check", tag_summary=tag_summary)

        if not is_managed_snapshot(tags):
            log_with_context(
                f"Snapshot '{snapshot_id}' no eliminado (no fue creado por RdsBackup)",
                step="skip_snapshot",
                tag_summary=tag_summary
            )
            snapshot["Tags"] = tags
            audit_retention_result(
                account=account,
                rds=rds,
                snapshot=snapshot,
                event_type="SNAPSHOT_SKIPPED",
                status="SKIPPED",
                message="Snapshot manual no administrado",
                skip_reason="unmanaged",
                operation_id=operation_id,
            )
            return False, snapshot_id, creation_time, retention_days, "snapshot manual no administrado"

        if any(tag.get("Key") == "keep" and tag.get("Value", "").lower() == "true" for tag in tags):
            log_with_context(f"Snapshot '{snapshot_id}' no eliminado (etiqueta keep=true)", step="skip_snapshot")
            snapshot["Tags"] = tags
            audit_retention_result(
                account=account,
                rds=rds,
                snapshot=snapshot,
                event_type="SNAPSHOT_SKIPPED",
                status="SKIPPED",
                message="Snapshot protegido por keep=true",
                skip_reason="keep_tag",
                operation_id=operation_id,
            )
            return False, snapshot_id, creation_time, retention_days, "etiqueta keep=true"

    except ClientError as e:
        log_with_context(f"No se pudieron obtener etiquetas para '{snapshot_id}': {e}", level="warning", step="tag_check")
        metrics.add_metric(name="SnapshotErrors", unit=MetricUnit.Count, value=1)
        audit_retention_result(
            account=account,
            rds=rds,
            snapshot=snapshot,
            event_type="RETENTION_FAILED",
            status="ERROR",
            message="Error al obtener etiquetas del snapshot",
            error=str(e),
            operation_id=operation_id,
        )
        return False, snapshot_id, creation_time, retention_days, "error al obtener etiquetas"

    if creation_time < cutoff_date:
        try:
            log_with_context(
                f"Snapshot eliminado por retención: {snapshot_id} (creado el {creation_time}, retención: {retention_days} días)",
                step="delete_snapshot",
                retention_days=retention_days
            )
            rds.delete_db_snapshot(DBSnapshotIdentifier=snapshot_id)
            metrics.add_metric(name="SnapshotsDeleted", unit=MetricUnit.Count, value=1)
            audit_retention_result(
                account=account,
                rds=rds,
                snapshot=snapshot,
                event_type="SNAPSHOT_DELETED",
                status="SUCCESS",
                message="Snapshot eliminado por política de retención",
                operation_id=operation_id,
            )
            return True, snapshot_id, creation_time, retention_days, "eliminado"
        except ClientError as e:
            log_with_context(f"No se pudo eliminar snapshot '{snapshot_id}': {e}", level="error", step="delete_snapshot")
            metrics.add_metric(name="SnapshotErrors", unit=MetricUnit.Count, value=1)
            audit_retention_result(
                account=account,
                rds=rds,
                snapshot=snapshot,
                event_type="RETENTION_FAILED",
                status="ERROR",
                message="Error al eliminar snapshot",
                error=str(e),
                operation_id=operation_id,
            )
            return False, snapshot_id, creation_time, retention_days, "error al eliminar"

    audit_retention_result(
        account=account,
        rds=rds,
        snapshot=snapshot,
        event_type="SNAPSHOT_SKIPPED",
        status="SKIPPED",
        message="Snapshot no vencido",
        skip_reason="not_expired",
        operation_id=operation_id,
    )
    return False, snapshot_id, creation_time, retention_days, "no vencido"


@logger.inject_lambda_context
@metrics.log_metrics
@tracer.capture_lambda_handler
def main(event: dict, context: LambdaContext):
    global is_cold_start
    operation_id = str(uuid.uuid4())

    log_with_context(
        "Inicio ejecución lambda",
        step="start",
        function_name=context.function_name,
        memory_limit=context.memory_limit_in_mb,
        request_id=context.aws_request_id,
        cold_start=is_cold_start
    )
    is_cold_start = False

    role_name = config.ROLE_NAME

    try:
        accounts = get_managed_accounts(feature="retention")
    except Exception as e:
        log_with_context(f"Error leyendo configuración: {e}", level="error", step="load_config")
        return {"status": "error", "message": "No se pudo leer configuración"}

    snapshot_deleted_summary = {}
    snapshot_skipped_summary = {}
    total_deleted = total_skipped = 0

    for account in accounts:
        account_id = account["account_id"]
        account_name = account["account_name"]
        retention_days = account["retention_days"]
        cutoff_date = datetime.now(timezone.utc) - timedelta(days=retention_days)

        try:
            rds = assume_role(account_id, role_name, session_name="RdsRetentionSession")
        except ClientError as e:
            code = e.response.get("Error", {}).get("Code")
            level = "warning" if code == "AccessDenied" else "error"
            log_with_context(f"Error al asumir rol en {account_id}: {e}", level=level, step="assume_role")
            metric_name = "MissingRetentionRole" if code == "AccessDenied" else "SnapshotErrors"
            metrics.add_metric(name=metric_name, unit=MetricUnit.Count, value=1)
            put_audit_event(
                event_type="ASSUME_ROLE_FAILED",
                account_id=account_id,
                account_name=account_name,
                status="ERROR",
                source=SERVICE_NAME,
                message=str(e),
                error_code=code,
                operation_id=operation_id,
            )
            continue

        try:
            snapshots = get_all_snapshots(rds)
        except ClientError as e:
            log_with_context(f"Error al listar snapshots: {e}", level="error", step="list_snapshots")
            metrics.add_metric(name="SnapshotErrors", unit=MetricUnit.Count, value=1)
            put_audit_event(
                event_type="LIST_SNAPSHOTS_FAILED",
                account_id=account_id,
                account_name=account_name,
                region=rds.meta.region_name,
                status="ERROR",
                source=SERVICE_NAME,
                message=str(e),
                operation_id=operation_id,
            )
            continue

        deleted, skipped = [], []
        for snapshot in snapshots:
            result, snapshot_id, creation_time, days, motivo = delete_snapshot_if_expired(
                rds,
                snapshot,
                cutoff_date,
                retention_days,
                account,
                operation_id,
            )
            info = {
                "id": snapshot_id,
                "fecha": creation_time.strftime("%Y-%m-%d"),
                "retencion_dias": days
            }
            if result:
                deleted.append(info)
            else:
                info["motivo"] = motivo
                skipped.append(info)

        snapshot_deleted_summary[account_id] = deleted
        snapshot_skipped_summary[account_id] = skipped
        total_deleted += len(deleted)
        total_skipped += len(skipped)

        log_with_context(f"Snapshots omitidos en cuenta {account_id}: {len(skipped)}", step="summary")
        log_with_context(f"Snapshots eliminados en cuenta {account_id}: {len(deleted)}", step="summary")

    log_with_context(
        "Resumen final de snapshots eliminados",
        step="summary",
        total_deleted=total_deleted,
        total_skipped=total_skipped,
        snapshot_deleted_summary=snapshot_deleted_summary,
        snapshot_skipped_summary=snapshot_skipped_summary,
        param_name=config.CONFIG_PARAM_NAME,
        cold_start=is_cold_start,
        function_name=context.function_name,
        function_memory_size=str(context.memory_limit_in_mb),
        function_arn=context.invoked_function_arn,
        function_request_id=context.aws_request_id,
        service=SERVICE_NAME,
        xray_trace_id=context.aws_request_id
    )

    return {"status": "completed", "operation_id": operation_id}
