import time
import uuid
import boto3
from botocore.exceptions import ClientError
from aws_lambda_powertools import Logger, Tracer, Metrics
from aws_lambda_powertools.metrics import MetricUnit
from aws_lambda_powertools.utilities.typing import LambdaContext

# Módulos compartidos
from common.logging_utils import log_with_context
from common.aws_utils import assume_role
from common.account_config import get_managed_accounts
from common.audit import put_audit_event, update_backup_state
from common import config

# ✅ Nombre específico para esta Lambda
SERVICE_NAME = "RdsBackup"
SNAPSHOT_TAGS = {
    "CreatedBy": "RdsBackupLambda",
    "Service": SERVICE_NAME,
}

# Inicialización de Powertools
logger = Logger(service=SERVICE_NAME)
tracer = Tracer(service=SERVICE_NAME)
metrics = Metrics(namespace="RdsBackup", service=SERVICE_NAME)


# Lista todas las instancias RDS manuales en la cuenta actual
def get_all_db_instances(rds_client):
    dbs = rds_client.describe_db_instances()["DBInstances"]
    log_with_context(f"Se encontraron {len(dbs)} instancias RDS", step="list_dbs")
    return dbs


# Intenta crear un snapshot para una instancia específica y lo etiqueta
def create_snapshot_for_db(rds, db_id, account, operation_id):
    account_id = account["account_id"]
    account_name = account["account_name"]
    region = rds.meta.region_name
    snapshot_id = f"{db_id}-{int(time.time())}"
    entry = {"id": snapshot_id, "db": db_id}
    tags = [
        {"Key": key, "Value": value}
        for key, value in SNAPSHOT_TAGS.items()
    ]
    tags.append({"Key": "AccountId", "Value": account_id})

    try:
        rds.create_db_snapshot(
            DBInstanceIdentifier=db_id,
            DBSnapshotIdentifier=snapshot_id,
            Tags=tags
        )
        metrics.add_metric(name="SnapshotsCreatedTotal", unit=MetricUnit.Count, value=1)
        log_with_context(f"Snapshot creado: {snapshot_id}", step="create_snapshot", db=db_id)
        put_audit_event(
            event_type="BACKUP_CREATED",
            account_id=account_id,
            account_name=account_name,
            region=region,
            db_identifier=db_id,
            snapshot_id=snapshot_id,
            status="SUCCESS",
            source=SERVICE_NAME,
            message="Snapshot creado correctamente",
            operation_id=operation_id,
        )
        update_backup_state(
            account_id=account_id,
            account_name=account_name,
            region=region,
            db_identifier=db_id,
            status="SUCCESS",
            snapshot_id=snapshot_id,
            operation_id=operation_id,
        )

        # Espera a que el snapshot sea visible antes de etiquetarlo
        time.sleep(3)
        snapshot_info = rds.describe_db_snapshots(DBSnapshotIdentifier=snapshot_id)["DBSnapshots"][0]
        snapshot_arn = snapshot_info["DBSnapshotArn"]

        # Etiqueta el snapshot creado
        rds.add_tags_to_resource(
            ResourceName=snapshot_arn,
            Tags=tags
        )
        log_with_context(f"Etiquetas agregadas a: {snapshot_id}", step="tag_snapshot")

    except ClientError as e:
        code = e.response.get("Error", {}).get("Code")
        if code == "InvalidDBInstanceState":
            entry["motivo"] = "instancia no disponible"
            log_with_context(f"Instancia no disponible para snapshot: {db_id}", level="warning", step="skip_snapshot")
            metrics.add_metric(name="SkippedInvalidState", unit=MetricUnit.Count, value=1)
            put_audit_event(
                event_type="BACKUP_SKIPPED",
                account_id=account_id,
                account_name=account_name,
                region=region,
                db_identifier=db_id,
                snapshot_id=snapshot_id,
                status="SKIPPED",
                source=SERVICE_NAME,
                message="Instancia no disponible para snapshot",
                operation_id=operation_id,
            )
            update_backup_state(
                account_id=account_id,
                account_name=account_name,
                region=region,
                db_identifier=db_id,
                status="SKIPPED",
                error="instancia no disponible",
                operation_id=operation_id,
            )
        else:
            entry["motivo"] = f"error inesperado: {code}"
            log_with_context(f"Error creando snapshot: {db_id}, {e}", level="error", step="create_snapshot")
            metrics.add_metric(name="SnapshotErrors", unit=MetricUnit.Count, value=1)
            put_audit_event(
                event_type="BACKUP_FAILED",
                account_id=account_id,
                account_name=account_name,
                region=region,
                db_identifier=db_id,
                snapshot_id=snapshot_id,
                status="ERROR",
                source=SERVICE_NAME,
                message=str(e),
                error_code=code,
                operation_id=operation_id,
            )
            update_backup_state(
                account_id=account_id,
                account_name=account_name,
                region=region,
                db_identifier=db_id,
                status="ERROR",
                error=str(e),
                operation_id=operation_id,
            )
    return entry


@logger.inject_lambda_context
@metrics.log_metrics
@tracer.capture_lambda_handler
def main(event: dict, context: LambdaContext):
    operation_id = str(uuid.uuid4())
    # Configuración centralizada
    role_name = config.ROLE_NAME

    snapshot_summary = {}

    try:
        accounts = get_managed_accounts(feature="backup")
    except Exception as e:
        log_with_context(f"Error al leer el parámetro SSM: {e}", level="error", step="load_config")
        return {"status": "error", "message": "No se pudo leer configuración de cuentas"}

    for account in accounts:
        account_id = account["account_id"]
        account_name = account["account_name"]
        try:
            rds = assume_role(account_id, role_name, session_name="RdsBackupSession")
        except ClientError as e:
            code = e.response.get("Error", {}).get("Code")
            level = "warning" if code == "AccessDenied" else "error"
            log_with_context(f"Error al asumir rol en cuenta {account_id}: {e}", level=level, step="assume_role")
            metric_name = "MissingBackupRole" if code == "AccessDenied" else "SnapshotErrors"
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
            dbs = get_all_db_instances(rds)
        except ClientError as e:
            log_with_context(f"No se pudieron listar instancias RDS: {e}", level="error", step="list_dbs")
            metrics.add_metric(name="SnapshotErrors", unit=MetricUnit.Count, value=1)
            put_audit_event(
                event_type="LIST_DBS_FAILED",
                account_id=account_id,
                account_name=account_name,
                region=rds.meta.region_name,
                status="ERROR",
                source=SERVICE_NAME,
                message=str(e),
                operation_id=operation_id,
            )
            continue

        snapshot_result = []
        for db in dbs:
            db_id = db["DBInstanceIdentifier"]
            snapshot_result.append(create_snapshot_for_db(rds, db_id, account, operation_id))

        snapshot_summary[account_id] = snapshot_result

    log_with_context("Resumen final de snapshots creados", step="summary", snapshot_summary=snapshot_summary)

    return {
        "status": "completed",
        "operation_id": operation_id,
        "snapshots_created": snapshot_summary
    }
