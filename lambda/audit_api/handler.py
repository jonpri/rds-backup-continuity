import base64
import json
import os
from datetime import datetime, timezone
from decimal import Decimal

import boto3
from boto3.dynamodb.conditions import Attr, Key
from botocore.exceptions import ClientError


EVENTS_TABLE_NAME = os.environ["AUDIT_EVENTS_TABLE_NAME"]
STATE_TABLE_NAME = os.environ["AUDIT_STATE_TABLE_NAME"]
DEFAULT_LIMIT = 50
MAX_LIMIT = 200
HIDDEN_KEYS = {"pk", "sk", "gsi1pk", "gsi1sk"}

dynamodb = boto3.resource("dynamodb")
events_table = dynamodb.Table(EVENTS_TABLE_NAME)
state_table = dynamodb.Table(STATE_TABLE_NAME)


class ApiError(Exception):
    def __init__(self, status_code, code, message):
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message


def main(event, context):
    try:
        path = event.get("path", "/")
        method = event.get("httpMethod", "GET")
        query = event.get("queryStringParameters") or {}

        if method != "GET":
            raise ApiError(405, "METHOD_NOT_ALLOWED", "Solo se permite GET")

        if path == "/audit/diagnostics/health":
            body = get_health_diagnostic(query)
        elif path == "/audit/diagnostics/retention":
            body = get_retention_diagnostic(query)
        elif path.startswith("/audit/diagnostics/operation/"):
            operation_id = path.removeprefix("/audit/diagnostics/operation/").strip("/")
            body = get_operation_diagnostic(operation_id, query)
        elif path.startswith("/audit/diagnostics/databases/"):
            account_name = path.removeprefix("/audit/diagnostics/databases/").strip("/")
            body = get_databases_diagnostic(account_name, query)
        elif path.startswith("/audit/diagnostics/account/"):
            account_name = path.removeprefix("/audit/diagnostics/account/").strip("/")
            body = get_account_diagnostic(account_name, query)
        elif path.startswith("/audit/diagnostics/database/"):
            body = get_database_diagnostic(path, query)
        elif path.startswith("/audit/diagnostics/restore-candidates/"):
            body = get_restore_candidates(path, query)
        elif path == "/audit/summary":
            body = get_summary(query)
        elif path == "/audit/state":
            body = list_state(query)
        elif path.startswith("/audit/state/"):
            body = get_state_path(path, query)
        elif path == "/audit/events":
            body = list_events(query)
        elif path.startswith("/audit/events/"):
            account_name = path.removeprefix("/audit/events/").strip("/")
            body = list_events({**query, "account_name": account_name})
        else:
            raise ApiError(404, "NOT_FOUND", "Ruta no encontrada")

        return response(200, body)
    except ApiError as exc:
        return error_response(exc.status_code, exc.code, exc.message, event)
    except ClientError as exc:
        return error_response(500, "INTERNAL_ERROR", str(exc), event)
    except Exception as exc:
        return error_response(500, "INTERNAL_ERROR", str(exc), event)


def get_state_path(path, query):
    parts = path.removeprefix("/audit/state/").strip("/").split("/")
    if len(parts) == 1:
        return list_state({**query, "account_name": parts[0]})
    if len(parts) == 3:
        account_name, region, db_identifier = parts
        items = collect_state({
            "account_name": account_name,
            "region": region,
            "db_identifier": db_identifier,
        })
        if not items:
            raise ApiError(404, "NOT_FOUND", "Estado no encontrado")
        return items[0]
    raise ApiError(404, "NOT_FOUND", "Ruta no encontrada")


STATE_FILTERS = {"region", "db_identifier", "last_backup_status"}
EVENT_FILTERS = {
    "event_type",
    "status",
    "region",
    "db_identifier",
    "snapshot_id",
    "skip_reason",
    "operation_id",
}


def build_state_request(query):
    account_id = query.get("account_id")
    account_name = query.get("account_name")

    if account_id:
        return {"KeyConditionExpression": Key("pk").eq(f"ACCOUNT#{account_id}")}
    if account_name:
        return {
            "IndexName": "gsi1",
            "KeyConditionExpression": Key("gsi1pk").eq(f"ACCOUNT_NAME#{account_name}"),
        }
    return {}


def list_state(query):
    request = build_state_request(query)
    request["Limit"] = parse_limit(query.get("limit"))
    exclusive_start_key = decode_next_token(query.get("next_token"))
    if exclusive_start_key:
        request["ExclusiveStartKey"] = exclusive_start_key

    result = run_request(state_table, request)
    items = apply_item_filters(result.get("Items", []), query, allowed_filters=STATE_FILTERS)

    return {
        "items": [sanitize_state_item(item) for item in items],
        "next_token": encode_next_token(result.get("LastEvaluatedKey")),
    }


def collect_state(query):
    # Lee todas las páginas: los diagnósticos necesitan el estado completo.
    items = iterate_pages(state_table, build_state_request(query), query, STATE_FILTERS)
    return [sanitize_state_item(item) for item in items]


def build_events_request(query):
    from_value = query.get("from")
    to_value = query.get("to")
    validate_iso8601(from_value, "from")
    validate_iso8601(to_value, "to")

    account_id = query.get("account_id")
    account_name = query.get("account_name")

    if account_id:
        return {
            "KeyConditionExpression": event_key_condition("pk", f"ACCOUNT#{account_id}", "sk", from_value, to_value),
            "ScanIndexForward": False,
        }
    if account_name:
        return {
            "IndexName": "gsi1",
            "KeyConditionExpression": event_key_condition("gsi1pk", f"ACCOUNT_NAME#{account_name}", "gsi1sk", from_value, to_value),
            "ScanIndexForward": False,
        }

    # Sin cuenta no hay clave de rango: la ventana se aplica como filtro del scan.
    request = {}
    time_filter = timestamp_filter(from_value, to_value)
    if time_filter is not None:
        request["FilterExpression"] = time_filter
    return request


def list_events(query):
    request = build_events_request(query)
    request["Limit"] = parse_limit(query.get("limit"))
    exclusive_start_key = decode_next_token(query.get("next_token"))
    if exclusive_start_key:
        request["ExclusiveStartKey"] = exclusive_start_key

    result = run_request(events_table, request)
    items = apply_item_filters(result.get("Items", []), query, allowed_filters=EVENT_FILTERS)
    items.sort(key=lambda item: item.get("timestamp", ""), reverse=True)

    return {
        "items": [sanitize_item(item) for item in items],
        "next_token": encode_next_token(result.get("LastEvaluatedKey")),
    }


def collect_events(query):
    # Lee todas las páginas: los resúmenes no pueden depender de una sola página.
    items = iterate_pages(events_table, build_events_request(query), query, EVENT_FILTERS)
    items.sort(key=lambda item: item.get("timestamp", ""), reverse=True)
    return [sanitize_item(item) for item in items]


def run_request(table, request):
    if "KeyConditionExpression" in request:
        return table.query(**request)
    return table.scan(**request)


def iterate_pages(table, request, query, allowed_filters):
    items = []
    while True:
        result = run_request(table, request)
        items.extend(apply_item_filters(result.get("Items", []), query, allowed_filters))
        last_evaluated_key = result.get("LastEvaluatedKey")
        if not last_evaluated_key:
            return items
        request = {**request, "ExclusiveStartKey": last_evaluated_key}


def state_scope(query):
    return {
        key: value
        for key, value in query.items()
        if key in {"account_id", "account_name", "region"}
    }


def get_summary(query):
    return build_summary(query, collect_events(query), collect_state(state_scope(query)))


def build_summary(query, events, state):
    backups = {"success": 0, "skipped": 0, "error": 0}
    retention = {
        "deleted": 0,
        "skipped": 0,
        "skipped_unmanaged": 0,
        "skipped_not_expired": 0,
        "skipped_keep_tag": 0,
        "skipped_unknown": 0,
        "error": 0,
    }
    last_event_at = None

    for item in events:
        event_type = item.get("event_type")
        status = item.get("status")
        timestamp = item.get("timestamp")
        if timestamp and (last_event_at is None or timestamp > last_event_at):
            last_event_at = timestamp

        if event_type == "BACKUP_CREATED":
            backups["success"] += 1
        elif event_type == "BACKUP_SKIPPED":
            backups["skipped"] += 1
        elif event_type in {"BACKUP_FAILED", "ASSUME_ROLE_FAILED", "LIST_DBS_FAILED"} or status == "ERROR":
            backups["error"] += 1

        if event_type == "SNAPSHOT_DELETED":
            retention["deleted"] += 1
        elif event_type == "SNAPSHOT_SKIPPED":
            retention["skipped"] += 1
            retention[get_retention_skip_summary_key(item)] += 1
        elif event_type in {"RETENTION_FAILED", "LIST_SNAPSHOTS_FAILED"}:
            retention["error"] += 1

    database_counts = get_database_counts(state)

    return {
        "from": query.get("from"),
        "to": query.get("to"),
        "accounts": len({item.get("account_id") for item in state if item.get("account_id")}),
        "accounts_with_backup_state": len({
            item.get("account_id")
            for item in state
            if item.get("account_id") and item.get("coverage", {}).get("has_backup_state")
        }),
        "databases": len(state),
        **database_counts,
        "backups": backups,
        "retention": retention,
        "last_event_at": last_event_at,
    }


def get_health_diagnostic(query):
    events = collect_events(query)
    state = collect_state(state_scope(query))
    summary = build_summary(query, events, state)
    errors = [event for event in events if event.get("status") == "ERROR"]
    active_errors, resolved_errors = split_active_and_resolved_errors(errors, state)

    return {
        "status": classify_health(summary, active_errors),
        "summary": summary,
        "database_health": summarize_database_health(state),
        "error_health": {
            "active": len(active_errors),
            "resolved": len(resolved_errors),
            "active_accounts": summarize_accounts(active_errors),
            "resolved_accounts": summarize_accounts(resolved_errors),
        },
        "priorities": build_health_priorities(summary, active_errors),
        "affected_accounts": summarize_accounts(active_errors),
        "affected_databases": summarize_databases(active_errors),
        "recommended_actions": build_recommended_actions(active_errors, summary),
    }


def get_account_diagnostic(account_name, query):
    if not account_name:
        raise ApiError(404, "NOT_FOUND", "Ruta no encontrada")

    account_query = {**query, "account_name": account_name}
    events = collect_events(account_query)
    state = collect_state({"account_name": account_name})
    summary = build_summary(account_query, events, state)
    errors = [event for event in events if event.get("status") == "ERROR"]

    return {
        "account_name": account_name,
        "status": classify_health(summary, errors),
        "summary": summary,
        "databases": summarize_database_state(state),
        "errors": group_errors(errors),
        "recommended_actions": build_recommended_actions(errors, summary),
    }


def get_databases_diagnostic(account_name, query):
    if not account_name:
        raise ApiError(404, "NOT_FOUND", "Ruta no encontrada")

    state = collect_state({"account_name": account_name})
    items = [build_database_inventory_item(item) for item in state]
    items.sort(key=lambda item: (item.get("region") or "", item.get("db_identifier") or ""))

    return {
        "account_name": account_name,
        "databases": len(items),
        "healthy": len([item for item in items if item["status"] == "healthy"]),
        "degraded": len([item for item in items if item["status"] == "degraded"]),
        "warning": len([item for item in items if item["status"] == "warning"]),
        "retention_only": len([item for item in items if item["status"] == "retention-only"]),
        "unknown": len([item for item in items if item["status"] == "unknown"]),
        "items": items,
    }


def get_operation_diagnostic(operation_id, query):
    if not operation_id:
        raise ApiError(404, "NOT_FOUND", "Ruta no encontrada")

    events = collect_events({**query, "operation_id": operation_id})
    if not events:
        raise ApiError(404, "NOT_FOUND", "Operación no encontrada")

    events.sort(key=lambda item: item.get("timestamp", ""))
    errors = [event for event in events if event.get("status") == "ERROR"]
    accounts = sorted({event.get("account_name") for event in events if event.get("account_name")})
    databases = sorted({event.get("db_identifier") for event in events if event.get("db_identifier")})

    return {
        "operation_id": operation_id,
        "status": "failed" if errors else "completed",
        "source": events[0].get("source"),
        "started_at": events[0].get("timestamp"),
        "finished_at": events[-1].get("timestamp"),
        "accounts": len(accounts),
        "account_names": accounts,
        "databases": len(databases),
        "event_counts": count_events(events),
        "errors": group_errors(errors),
        "events": events,
    }


def get_database_diagnostic(path, query):
    parts = path.removeprefix("/audit/diagnostics/database/").strip("/").split("/")
    if len(parts) != 3:
        raise ApiError(404, "NOT_FOUND", "Ruta no encontrada")

    account_name, region, db_identifier = parts
    state_items = collect_state({
        "account_name": account_name,
        "region": region,
        "db_identifier": db_identifier,
    })
    state = state_items[0] if state_items else None
    events = collect_events({
        **query,
        "account_name": account_name,
        "region": region,
        "db_identifier": db_identifier,
    })
    errors = [event for event in events if event.get("status") == "ERROR"]

    return {
        "account_name": account_name,
        "region": region,
        "db_identifier": db_identifier,
        "status": classify_database_state(state, errors),
        "state": state,
        "events": events[:MAX_LIMIT],
        "recommended_actions": build_recommended_actions(errors, {}),
    }


def get_retention_diagnostic(query):
    events = collect_events(query)
    summary = build_summary(query, events, collect_state(state_scope(query)))
    samples = {
        reason: [
            event
            for event in events
            if event.get("event_type") == "SNAPSHOT_SKIPPED" and event.get("skip_reason") == reason
        ][:10]
        for reason in ("unmanaged", "not_expired", "keep_tag")
    }

    return {
        "retention": summary.get("retention", {}),
        "samples": samples,
        "interpretation": interpret_retention_skips(summary.get("retention", {})),
    }


def get_restore_candidates(path, query):
    parts = path.removeprefix("/audit/diagnostics/restore-candidates/").strip("/").split("/")
    if len(parts) != 3:
        raise ApiError(404, "NOT_FOUND", "Ruta no encontrada")

    account_name, region, db_identifier = parts
    events = collect_events({
        **query,
        "account_name": account_name,
        "region": region,
        "db_identifier": db_identifier,
    })
    # Un snapshot eliminado por retención ya no sirve para restaurar.
    deleted_snapshot_ids = {
        event.get("snapshot_id")
        for event in events
        if event.get("event_type") == "SNAPSHOT_DELETED"
    }

    candidates = [
        {
            "snapshot_id": event.get("snapshot_id"),
            "created_at": event.get("timestamp"),
            "account_name": event.get("account_name"),
            "account_id": event.get("account_id"),
            "region": event.get("region"),
            "db_identifier": event.get("db_identifier"),
        }
        for event in events
        if event.get("event_type") == "BACKUP_CREATED"
        and event.get("status") == "SUCCESS"
        and event.get("snapshot_id") not in deleted_snapshot_ids
    ]
    candidates.sort(key=lambda item: item.get("created_at") or "", reverse=True)

    return {"items": candidates}


def classify_health(summary, errors):
    if any(event.get("event_type") in {"ASSUME_ROLE_FAILED", "LIST_DBS_FAILED"} for event in errors):
        return "critical"
    if errors:
        return "degraded"
    if summary.get("databases", 0) and summary.get("accounts_with_backup_state", 0) == 0:
        return "observing-retention-only"
    return "healthy"


def split_active_and_resolved_errors(errors, state_items):
    latest_success_by_account = {}

    for item in state_items:
        account_id = item.get("account_id")
        if not account_id:
            continue

        success_timestamps = []
        if item.get("last_backup_status") == "SUCCESS" and item.get("last_backup_at"):
            success_timestamps.append(item["last_backup_at"])
        if item.get("last_retention_status") in {"SUCCESS", "SKIPPED"} and item.get("last_retention_at"):
            success_timestamps.append(item["last_retention_at"])

        if not success_timestamps:
            continue

        latest_success = max(success_timestamps)
        current_success = latest_success_by_account.get(account_id)
        if current_success is None or latest_success > current_success:
            latest_success_by_account[account_id] = latest_success

    active_errors = []
    resolved_errors = []
    for error in errors:
        account_id = error.get("account_id")
        timestamp = error.get("timestamp")
        latest_success = latest_success_by_account.get(account_id)

        if timestamp and latest_success and latest_success > timestamp:
            resolved_errors.append(error)
        else:
            active_errors.append(error)

    return active_errors, resolved_errors


def classify_database_state(state, errors):
    if errors:
        return "degraded"
    if not state:
        return "unknown"
    if state.get("last_backup_status") == "SUCCESS":
        return "healthy"
    if state.get("last_backup_status") == "ERROR":
        return "degraded"
    coverage = state.get("coverage", {})
    if coverage.get("has_retention_state") and not coverage.get("has_backup_state"):
        return "retention-only"
    return "unknown"


def build_database_inventory_item(item):
    backup = {
        "health": classify_backup_health(item),
        "status": item.get("last_backup_status"),
        "last_at": item.get("last_backup_at"),
        "snapshot_id": item.get("last_snapshot_id"),
        "error": item.get("last_error"),
    }
    retention = {
        "health": classify_retention_health(item),
        "status": item.get("last_retention_status"),
        "last_at": item.get("last_retention_at"),
        "deleted_snapshot_id": item.get("last_deleted_snapshot_id"),
        "error": item.get("last_retention_error"),
    }

    return {
        "account_id": item.get("account_id"),
        "account_name": item.get("account_name"),
        "region": item.get("region"),
        "db_identifier": item.get("db_identifier"),
        "status": combine_database_health(item, backup["health"], retention["health"]),
        "backup": backup,
        "retention": retention,
        "coverage": item.get("coverage", {}),
        "last_activity_source": item.get("last_activity_source"),
        "updated_at": item.get("updated_at"),
    }


def summarize_database_health(state_items):
    items = [build_database_inventory_item(item) for item in state_items]
    counts = {
        "total": len(items),
        "healthy": 0,
        "degraded": 0,
        "warning": 0,
        "retention_only": 0,
        "unknown": 0,
    }

    for item in items:
        status = item.get("status") or "unknown"
        counts[status.replace("-", "_")] = counts.get(status.replace("-", "_"), 0) + 1

    return {
        **counts,
        "findings": [
            item
            for item in items
            if item.get("status") != "healthy"
        ][:10],
    }


def count_events(events):
    counts = {}
    for event in events:
        event_type = event.get("event_type", "unknown")
        status = event.get("status", "unknown").lower()
        if event_type not in counts:
            counts[event_type] = {}
        counts[event_type][status] = counts[event_type].get(status, 0) + 1
    return counts


def classify_backup_health(item):
    status = item.get("last_backup_status")
    if status == "SUCCESS":
        return "healthy"
    if status == "ERROR":
        return "degraded"
    if status == "SKIPPED":
        return "warning"
    return "unknown"


def classify_retention_health(item):
    status = item.get("last_retention_status")
    if status in {"SUCCESS", "SKIPPED"}:
        return "healthy"
    if status == "ERROR":
        return "degraded"
    return "unknown"


def combine_database_health(item, backup_health, retention_health):
    if backup_health == "degraded" or retention_health == "degraded":
        return "degraded"
    if backup_health == "warning":
        return "warning"
    coverage = item.get("coverage", {})
    if coverage.get("has_retention_state") and not coverage.get("has_backup_state"):
        return "retention-only"
    if backup_health == "healthy":
        return "healthy"
    return "unknown"


def build_health_priorities(summary, errors):
    priorities = []
    event_types = {event.get("event_type") for event in errors}

    if "ASSUME_ROLE_FAILED" in event_types:
        priorities.append("Validar trust policy, ExternalId y despliegue del rol RdsBackupCrossAccountRole.")
    if "LIST_DBS_FAILED" in event_types:
        priorities.append("Validar permisos rds:DescribeDBInstances en la cuenta administrada.")
    if "BACKUP_FAILED" in event_types:
        priorities.append("Revisar errores de snapshot por base de datos y estado de la instancia RDS.")
    if summary.get("databases_retention_only", 0):
        priorities.append("Ejecutar o esperar el runner de backup para completar cobertura de backup state.")
    if summary.get("retention", {}).get("skipped_unmanaged", 0):
        priorities.append("Revisar snapshots manuales no administrados; retention los omite por diseno.")

    return priorities


def build_recommended_actions(errors, summary):
    actions = []
    event_types = {event.get("event_type") for event in errors}

    if "ASSUME_ROLE_FAILED" in event_types:
        actions.append({
            "priority": "high",
            "action": "Validar que el rol RdsBackupCrossAccountRole exista en la cuenta hija y permita AssumeRole desde los roles centrales con el ExternalId esperado.",
        })
    if "LIST_DBS_FAILED" in event_types:
        actions.append({
            "priority": "high",
            "action": "Validar permisos RDS de lectura en el rol cross-account.",
        })
    if "BACKUP_FAILED" in event_types:
        actions.append({
            "priority": "medium",
            "action": "Revisar el mensaje del evento BACKUP_FAILED, estado de la instancia, cuotas de snapshots y permisos KMS si aplica.",
        })
    if summary.get("databases_retention_only", 0):
        actions.append({
            "priority": "medium",
            "action": "Confirmar que el scheduler de backup ya ejecuto despues del despliegue productivo.",
        })

    return actions


def summarize_accounts(events):
    grouped = {}
    for event in events:
        account = event.get("account_name") or event.get("account_id") or "unknown"
        if account not in grouped:
            grouped[account] = {"count": 0, "event_types": {}}
        grouped[account]["count"] += 1
        event_type = event.get("event_type", "unknown")
        grouped[account]["event_types"][event_type] = grouped[account]["event_types"].get(event_type, 0) + 1

    return [
        {"account": account, **values}
        for account, values in sorted(grouped.items())
    ]


def summarize_databases(events):
    grouped = {}
    for event in events:
        db_identifier = event.get("db_identifier")
        if not db_identifier:
            continue
        if db_identifier not in grouped:
            grouped[db_identifier] = {"count": 0, "event_types": {}}
        grouped[db_identifier]["count"] += 1
        event_type = event.get("event_type", "unknown")
        grouped[db_identifier]["event_types"][event_type] = grouped[db_identifier]["event_types"].get(event_type, 0) + 1

    return [
        {"db_identifier": db_identifier, **values}
        for db_identifier, values in sorted(grouped.items())
    ]


def summarize_database_state(items):
    coverage_counts = {
        "with_backup_state": 0,
        "retention_only": 0,
        "without_observed_state": 0,
    }
    backup_status = {}

    for item in items:
        coverage = item.get("coverage", {})
        if coverage.get("has_backup_state"):
            coverage_counts["with_backup_state"] += 1
        elif coverage.get("has_retention_state"):
            coverage_counts["retention_only"] += 1
        else:
            coverage_counts["without_observed_state"] += 1

        status = item.get("last_backup_status", "UNKNOWN")
        backup_status[status] = backup_status.get(status, 0) + 1

    return {
        "total": len(items),
        "coverage": coverage_counts,
        "backup_status": backup_status,
        "items": items,
    }


def group_errors(errors):
    grouped = {}
    for event in errors:
        event_type = event.get("event_type", "unknown")
        if event_type not in grouped:
            grouped[event_type] = {"count": 0, "accounts": {}, "messages": {}}
        grouped[event_type]["count"] += 1

        account = event.get("account_name") or event.get("account_id") or "unknown"
        grouped[event_type]["accounts"][account] = grouped[event_type]["accounts"].get(account, 0) + 1

        message = event.get("message") or event.get("error") or event.get("error_code")
        if message:
            grouped[event_type]["messages"][message] = grouped[event_type]["messages"].get(message, 0) + 1

    return [
        {"event_type": event_type, **values}
        for event_type, values in sorted(grouped.items())
    ]


def interpret_retention_skips(retention):
    return {
        "unmanaged": "Snapshots manuales sin tags de la solucion; retention los omite para no borrar respaldos humanos.",
        "not_expired": "Snapshots administrados que aun estan dentro de la ventana de retencion.",
        "keep_tag": "Snapshots protegidos explicitamente con keep=true.",
        "unknown": "Eventos antiguos o sin motivo clasificado; revisar muestras para confirmar.",
        "counts": {
            "unmanaged": retention.get("skipped_unmanaged", 0),
            "not_expired": retention.get("skipped_not_expired", 0),
            "keep_tag": retention.get("skipped_keep_tag", 0),
            "unknown": retention.get("skipped_unknown", 0),
        },
    }


def event_key_condition(partition_key, partition_value, sort_key, from_value, to_value):
    condition = Key(partition_key).eq(partition_value)
    if from_value and to_value:
        return condition & Key(sort_key).between(from_value, to_value)
    if from_value:
        return condition & Key(sort_key).gte(from_value)
    if to_value:
        return condition & Key(sort_key).lte(to_value)
    return condition


def timestamp_filter(from_value, to_value):
    if from_value and to_value:
        return Attr("timestamp").between(from_value, to_value)
    if from_value:
        return Attr("timestamp").gte(from_value)
    if to_value:
        return Attr("timestamp").lte(to_value)
    return None


def apply_item_filters(items, query, allowed_filters):
    filtered = items
    for key in allowed_filters:
        value = query.get(key)
        if value is not None:
            filtered = [item for item in filtered if str(item.get(key)) == value]
    return filtered


def get_database_counts(state_items):
    with_backup_state = 0
    retention_only = 0
    without_observed_state = 0

    for item in state_items:
        coverage = item.get("coverage", {})
        has_backup_state = coverage.get("has_backup_state", False)
        has_retention_state = coverage.get("has_retention_state", False)

        if has_backup_state:
            with_backup_state += 1
        elif has_retention_state:
            retention_only += 1
        else:
            without_observed_state += 1

    return {
        "databases_with_backup_state": with_backup_state,
        "databases_retention_only": retention_only,
        "databases_without_observed_state": without_observed_state,
    }


def get_retention_skip_summary_key(item):
    skip_reason = item.get("skip_reason")
    if not skip_reason:
        skip_reason = infer_retention_skip_reason(item.get("message"))

    return {
        "unmanaged": "skipped_unmanaged",
        "not_expired": "skipped_not_expired",
        "keep_tag": "skipped_keep_tag",
    }.get(skip_reason, "skipped_unknown")


def infer_retention_skip_reason(message):
    if message == "Snapshot manual no administrado":
        return "unmanaged"
    if message == "Snapshot no vencido":
        return "not_expired"
    if message == "Snapshot protegido por keep=true":
        return "keep_tag"
    return "unknown"


def parse_limit(raw_limit):
    if raw_limit is None:
        return DEFAULT_LIMIT
    try:
        limit = int(raw_limit)
    except ValueError as exc:
        raise ApiError(400, "INVALID_FILTER", "El parámetro limit debe ser numérico") from exc
    if limit < 1 or limit > MAX_LIMIT:
        raise ApiError(400, "INVALID_FILTER", f"El parámetro limit debe estar entre 1 y {MAX_LIMIT}")
    return limit


def validate_iso8601(value, field_name):
    if not value:
        return
    try:
        normalized = value.replace("Z", "+00:00")
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise ApiError(400, "INVALID_FILTER", f"El parámetro {field_name} debe usar ISO-8601 UTC") from exc
    if parsed.tzinfo is None:
        raise ApiError(400, "INVALID_FILTER", f"El parámetro {field_name} debe incluir timezone UTC")


def encode_next_token(last_evaluated_key):
    if not last_evaluated_key:
        return None
    payload = json.dumps(to_jsonable(last_evaluated_key), separators=(",", ":"))
    return base64.urlsafe_b64encode(payload.encode("utf-8")).decode("utf-8")


def decode_next_token(token):
    if not token:
        return None
    try:
        payload = base64.urlsafe_b64decode(token.encode("utf-8")).decode("utf-8")
        return json.loads(payload)
    except Exception as exc:
        raise ApiError(400, "INVALID_FILTER", "El parámetro next_token no es válido") from exc


def sanitize_item(item):
    return {
        key: to_jsonable(value)
        for key, value in item.items()
        if key not in HIDDEN_KEYS
    }


def sanitize_state_item(item):
    sanitized = sanitize_item(item)
    has_backup_state = "last_backup_at" in sanitized or "last_backup_status" in sanitized
    has_retention_state = "last_retention_at" in sanitized or "last_retention_status" in sanitized
    sanitized["coverage"] = {
        "has_backup_state": has_backup_state,
        "has_retention_state": has_retention_state,
    }
    sanitized["last_activity_source"] = get_last_activity_source(sanitized)
    return sanitized


def get_last_activity_source(item):
    last_backup_at = item.get("last_backup_at")
    last_retention_at = item.get("last_retention_at")

    if last_backup_at and last_retention_at:
        if last_backup_at > last_retention_at:
            return "RdsBackup"
        return "RdsRetention"
    if last_backup_at:
        return "RdsBackup"
    if last_retention_at:
        return "RdsRetention"
    return None


def to_jsonable(value):
    if isinstance(value, Decimal):
        if value % 1 == 0:
            return int(value)
        return float(value)
    if isinstance(value, list):
        return [to_jsonable(item) for item in value]
    if isinstance(value, dict):
        return {key: to_jsonable(item) for key, item in value.items()}
    return value


def response(status_code, body):
    return {
        "statusCode": status_code,
        "headers": {
            "Content-Type": "application/json",
        },
        "body": json.dumps(to_jsonable(body)),
    }


def error_response(status_code, code, message, event):
    request_context = event.get("requestContext") or {}
    request_id = request_context.get("requestId")
    return response(status_code, {
        "error": {
            "code": code,
            "message": message,
            "request_id": request_id,
        }
    })
