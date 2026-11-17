import uuid
from datetime import datetime, timezone

import boto3
from botocore.exceptions import ClientError

from common import config
from common.logging_utils import log_with_context


dynamodb = boto3.resource("dynamodb")
events_table = dynamodb.Table(config.AUDIT_EVENTS_TABLE_NAME)
state_table = dynamodb.Table(config.AUDIT_STATE_TABLE_NAME)


def utc_now_iso():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _resource_id(details):
    return (
        details.get("snapshot_id")
        or details.get("db_identifier")
        or details.get("account_id")
        or "unknown"
    )


def put_audit_event(
    *,
    event_type,
    account_id,
    account_name,
    status,
    source,
    region=None,
    message=None,
    timestamp=None,
    **details,
):
    timestamp = timestamp or utc_now_iso()
    resource_id = _resource_id(details)
    item = {
        "pk": f"ACCOUNT#{account_id}",
        "sk": f"{timestamp}#{event_type}#{resource_id}",
        "gsi1pk": f"ACCOUNT_NAME#{account_name}",
        "gsi1sk": f"{timestamp}#{event_type}#{resource_id}",
        "event_id": str(uuid.uuid4()),
        "event_type": event_type,
        "timestamp": timestamp,
        "account_id": str(account_id),
        "account_name": str(account_name),
        "status": status,
        "source": source,
    }

    if region:
        item["region"] = region
    if message:
        item["message"] = message

    item.update({key: value for key, value in details.items() if value is not None})

    try:
        events_table.put_item(Item=item)
    except ClientError as exc:
        log_with_context(
            f"No se pudo escribir evento de auditoría: {exc}",
            level="warning",
            step="audit_event",
            event_type=event_type,
            account_id=account_id,
        )


def update_backup_state(
    *,
    account_id,
    account_name,
    region,
    db_identifier,
    status,
    snapshot_id=None,
    error=None,
    operation_id=None,
    timestamp=None,
):
    timestamp = timestamp or utc_now_iso()
    _update_state(
        account_id=account_id,
        account_name=account_name,
        region=region,
        db_identifier=db_identifier,
        updates={
            "last_backup_status": status,
            "last_backup_at": timestamp,
            "last_snapshot_id": snapshot_id,
            "last_error": error,
            "last_backup_operation_id": operation_id,
        },
    )


def update_retention_state(
    *,
    account_id,
    account_name,
    region,
    db_identifier,
    status,
    snapshot_id=None,
    error=None,
    operation_id=None,
    timestamp=None,
):
    timestamp = timestamp or utc_now_iso()
    _update_state(
        account_id=account_id,
        account_name=account_name,
        region=region,
        db_identifier=db_identifier,
        updates={
            "last_retention_status": status,
            "last_retention_at": timestamp,
            "last_deleted_snapshot_id": snapshot_id,
            "last_retention_error": error,
            "last_retention_operation_id": operation_id,
        },
    )


def _update_state(*, account_id, account_name, region, db_identifier, updates):
    key = {
        "pk": f"ACCOUNT#{account_id}",
        "sk": f"DB#{region}#{db_identifier}",
    }
    item_updates = {
        "account_id": str(account_id),
        "account_name": str(account_name),
        "region": region,
        "db_identifier": db_identifier,
        "gsi1pk": f"ACCOUNT_NAME#{account_name}",
        "gsi1sk": f"DB#{region}#{db_identifier}",
        "updated_at": utc_now_iso(),
    }
    item_updates.update(updates)

    expression_names = {}
    expression_values = {}
    set_expressions = []

    for idx, (name, value) in enumerate(item_updates.items()):
        name_key = f"#n{idx}"
        value_key = f":v{idx}"
        expression_names[name_key] = name
        expression_values[value_key] = value
        set_expressions.append(f"{name_key} = {value_key}")

    try:
        state_table.update_item(
            Key=key,
            UpdateExpression="SET " + ", ".join(set_expressions),
            ExpressionAttributeNames=expression_names,
            ExpressionAttributeValues=expression_values,
        )
    except ClientError as exc:
        log_with_context(
            f"No se pudo actualizar estado de auditoría: {exc}",
            level="warning",
            step="audit_state",
            account_id=account_id,
            db_identifier=db_identifier,
        )
