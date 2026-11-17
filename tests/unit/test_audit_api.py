import importlib
import json
import os
import sys
from pathlib import Path


os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AUDIT_EVENTS_TABLE_NAME", "events")
os.environ.setdefault("AUDIT_STATE_TABLE_NAME", "state")
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "lambda"))

handler = importlib.import_module("audit_api.handler")


class FakeTable:
    def __init__(self, items=None):
        self.items = items or []
        self.last_request = None

    def query(self, **kwargs):
        self.last_request = kwargs
        return {"Items": self.items}

    def scan(self, **kwargs):
        self.last_request = kwargs
        return {"Items": self.items}


def api_event(path, query=None):
    return {
        "httpMethod": "GET",
        "path": path,
        "queryStringParameters": query or {},
        "requestContext": {"requestId": "req-1"},
    }


def body(response):
    return json.loads(response["body"])


def test_state_response_hides_dynamodb_keys(monkeypatch):
    fake_state = FakeTable([{
        "pk": "ACCOUNT#210000000001",
        "sk": "DB#us-east-1#db-prod",
        "gsi1pk": "ACCOUNT_NAME#app-a",
        "gsi1sk": "DB#us-east-1#db-prod",
        "account_id": "210000000001",
        "account_name": "app-a",
        "region": "us-east-1",
        "db_identifier": "db-prod",
        "last_backup_status": "SUCCESS",
        "last_backup_at": "2026-06-13T03:00:00Z",
        "last_retention_status": "SKIPPED",
        "last_retention_at": "2026-06-13T04:00:00Z",
    }])
    monkeypatch.setattr(handler, "state_table", fake_state)

    response = handler.main(api_event("/audit/state", {"account_name": "app-a"}), None)

    assert response["statusCode"] == 200
    item = body(response)["items"][0]
    assert item["account_name"] == "app-a"
    assert "pk" not in item
    assert "sk" not in item
    assert "gsi1pk" not in item
    assert "gsi1sk" not in item
    assert item["coverage"] == {
        "has_backup_state": True,
        "has_retention_state": True,
    }
    assert item["last_activity_source"] == "RdsRetention"


def test_state_response_marks_retention_only_items(monkeypatch):
    fake_state = FakeTable([{
        "account_id": "210000000001",
        "account_name": "app-a",
        "region": "us-east-1",
        "db_identifier": "db-prod",
        "last_retention_status": "SKIPPED",
        "last_retention_at": "2026-06-13T04:00:00Z",
    }])
    monkeypatch.setattr(handler, "state_table", fake_state)

    response = handler.main(api_event("/audit/state", {"account_name": "app-a"}), None)

    assert response["statusCode"] == 200
    item = body(response)["items"][0]
    assert item["coverage"] == {
        "has_backup_state": False,
        "has_retention_state": True,
    }
    assert item["last_activity_source"] == "RdsRetention"


def test_events_path_account_name_uses_alias_filter(monkeypatch):
    fake_events = FakeTable([{
        "account_id": "210000000001",
        "account_name": "app-a",
        "event_type": "BACKUP_CREATED",
        "timestamp": "2026-06-10T17:00:00Z",
        "status": "SUCCESS",
    }])
    monkeypatch.setattr(handler, "events_table", fake_events)

    response = handler.main(api_event("/audit/events/app-a"), None)

    assert response["statusCode"] == 200
    assert body(response)["items"][0]["account_name"] == "app-a"
    assert fake_events.last_request["IndexName"] == "gsi1"


def test_summary_breaks_down_retention_skipped_reasons(monkeypatch):
    fake_events = FakeTable([
        {
            "event_type": "SNAPSHOT_SKIPPED",
            "timestamp": "2026-06-16T00:00:01Z",
            "status": "SKIPPED",
            "skip_reason": "unmanaged",
        },
        {
            "event_type": "SNAPSHOT_SKIPPED",
            "timestamp": "2026-06-16T00:00:02Z",
            "status": "SKIPPED",
            "skip_reason": "not_expired",
        },
        {
            "event_type": "SNAPSHOT_SKIPPED",
            "timestamp": "2026-06-16T00:00:03Z",
            "status": "SKIPPED",
            "skip_reason": "keep_tag",
        },
        {
            "event_type": "SNAPSHOT_SKIPPED",
            "timestamp": "2026-06-16T00:00:04Z",
            "status": "SKIPPED",
            "message": "Snapshot manual no administrado",
        },
        {
            "event_type": "SNAPSHOT_SKIPPED",
            "timestamp": "2026-06-16T00:00:05Z",
            "status": "SKIPPED",
            "message": "motivo legado desconocido",
        },
    ])
    fake_state = FakeTable([{
        "account_id": "210000000001",
        "account_name": "app-a",
        "region": "us-east-1",
        "db_identifier": "db-prod",
    }])
    monkeypatch.setattr(handler, "events_table", fake_events)
    monkeypatch.setattr(handler, "state_table", fake_state)

    response = handler.main(api_event("/audit/summary"), None)

    assert response["statusCode"] == 200
    retention = body(response)["retention"]
    assert retention["skipped"] == 5
    assert retention["skipped_unmanaged"] == 2
    assert retention["skipped_not_expired"] == 1
    assert retention["skipped_keep_tag"] == 1
    assert retention["skipped_unknown"] == 1


def test_summary_breaks_down_database_state_coverage(monkeypatch):
    fake_events = FakeTable([])
    fake_state = FakeTable([
        {
            "account_id": "111111111111",
            "account_name": "app-a",
            "region": "us-east-1",
            "db_identifier": "db-with-backup",
            "last_backup_status": "SUCCESS",
            "last_backup_at": "2026-06-16T12:00:00Z",
            "last_retention_status": "SKIPPED",
            "last_retention_at": "2026-06-17T04:00:00Z",
        },
        {
            "account_id": "222222222222",
            "account_name": "app-b",
            "region": "us-east-1",
            "db_identifier": "db-retention-only",
            "last_retention_status": "SKIPPED",
            "last_retention_at": "2026-06-17T04:00:00Z",
        },
        {
            "account_id": "333333333333",
            "account_name": "app-c",
            "region": "us-east-1",
            "db_identifier": "db-without-observed-state",
        },
    ])
    monkeypatch.setattr(handler, "events_table", fake_events)
    monkeypatch.setattr(handler, "state_table", fake_state)

    response = handler.main(api_event("/audit/summary"), None)

    assert response["statusCode"] == 200
    summary = body(response)
    assert summary["accounts"] == 3
    assert summary["accounts_with_backup_state"] == 1
    assert summary["databases"] == 3
    assert summary["databases_with_backup_state"] == 1
    assert summary["databases_retention_only"] == 1
    assert summary["databases_without_observed_state"] == 1


def test_health_diagnostic_prioritizes_account_level_errors(monkeypatch):
    fake_events = FakeTable([{
        "account_id": "210000000005",
        "account_name": "app-d",
        "event_type": "ASSUME_ROLE_FAILED",
        "timestamp": "2026-06-17T04:00:00Z",
        "status": "ERROR",
        "message": "AccessDenied",
    }])
    fake_state = FakeTable([{
        "account_id": "210000000004",
        "account_name": "app-a",
        "region": "us-east-1",
        "db_identifier": "db-prod",
        "last_retention_status": "SKIPPED",
        "last_retention_at": "2026-06-17T04:00:00Z",
    }])
    monkeypatch.setattr(handler, "events_table", fake_events)
    monkeypatch.setattr(handler, "state_table", fake_state)

    response = handler.main(api_event("/audit/diagnostics/health"), None)

    assert response["statusCode"] == 200
    result = body(response)
    assert result["status"] == "critical"
    assert result["database_health"]["total"] == 1
    assert result["database_health"]["retention_only"] == 1
    assert result["database_health"]["findings"][0]["db_identifier"] == "db-prod"
    assert result["error_health"]["active"] == 1
    assert result["error_health"]["resolved"] == 0
    assert result["affected_accounts"] == [{
        "account": "app-d",
        "count": 1,
        "event_types": {"ASSUME_ROLE_FAILED": 1},
    }]
    assert result["recommended_actions"][0]["priority"] == "high"


def test_health_diagnostic_marks_old_errors_as_resolved_after_later_success(monkeypatch):
    fake_events = FakeTable([{
        "account_id": "210000000005",
        "account_name": "app-d",
        "event_type": "ASSUME_ROLE_FAILED",
        "timestamp": "2026-06-17T04:00:00Z",
        "status": "ERROR",
        "message": "AccessDenied",
    }])
    fake_state = FakeTable([{
        "account_id": "210000000005",
        "account_name": "app-d",
        "region": "us-east-1",
        "db_identifier": "db-app-d-prod",
        "last_backup_status": "SUCCESS",
        "last_backup_at": "2026-06-17T20:00:00Z",
        "last_snapshot_id": "db-app-d-prod-1781726400",
    }])
    monkeypatch.setattr(handler, "events_table", fake_events)
    monkeypatch.setattr(handler, "state_table", fake_state)

    response = handler.main(api_event("/audit/diagnostics/health"), None)

    assert response["statusCode"] == 200
    result = body(response)
    assert result["status"] == "healthy"
    assert result["summary"]["backups"]["error"] == 1
    assert result["error_health"]["active"] == 0
    assert result["error_health"]["resolved"] == 1
    assert result["affected_accounts"] == []
    assert result["error_health"]["resolved_accounts"] == [{
        "account": "app-d",
        "count": 1,
        "event_types": {"ASSUME_ROLE_FAILED": 1},
    }]


def test_account_diagnostic_groups_state_and_errors(monkeypatch):
    fake_events = FakeTable([{
        "account_id": "210000000004",
        "account_name": "app-a",
        "event_type": "BACKUP_FAILED",
        "timestamp": "2026-06-17T12:00:00Z",
        "status": "ERROR",
        "db_identifier": "db-error",
        "message": "snapshot quota exceeded",
    }])
    fake_state = FakeTable([
        {
            "account_id": "210000000004",
            "account_name": "app-a",
            "region": "us-east-1",
            "db_identifier": "db-ok",
            "last_backup_status": "SUCCESS",
            "last_backup_at": "2026-06-17T12:00:00Z",
        },
        {
            "account_id": "210000000004",
            "account_name": "app-a",
            "region": "us-east-1",
            "db_identifier": "db-retention-only",
            "last_retention_status": "SKIPPED",
            "last_retention_at": "2026-06-17T04:00:00Z",
        },
    ])
    monkeypatch.setattr(handler, "events_table", fake_events)
    monkeypatch.setattr(handler, "state_table", fake_state)

    response = handler.main(api_event("/audit/diagnostics/account/app-a"), None)

    assert response["statusCode"] == 200
    result = body(response)
    assert result["status"] == "degraded"
    assert result["databases"]["coverage"] == {
        "with_backup_state": 1,
        "retention_only": 1,
        "without_observed_state": 0,
    }
    assert result["errors"][0]["event_type"] == "BACKUP_FAILED"


def test_databases_diagnostic_returns_db_inventory_for_account(monkeypatch):
    fake_state = FakeTable([
        {
            "account_id": "210000000004",
            "account_name": "app-a",
            "region": "us-east-1",
            "db_identifier": "db-ok",
            "last_backup_status": "SUCCESS",
            "last_backup_at": "2026-06-17T12:00:00Z",
            "last_snapshot_id": "snapshot-ok",
            "last_retention_status": "SKIPPED",
            "last_retention_at": "2026-06-17T04:00:00Z",
            "updated_at": "2026-06-17T12:00:01Z",
        },
        {
            "account_id": "210000000004",
            "account_name": "app-a",
            "region": "us-east-1",
            "db_identifier": "db-retention-only",
            "last_retention_status": "SKIPPED",
            "last_retention_at": "2026-06-17T04:00:00Z",
        },
    ])
    monkeypatch.setattr(handler, "state_table", fake_state)

    response = handler.main(api_event("/audit/diagnostics/databases/app-a"), None)

    assert response["statusCode"] == 200
    result = body(response)
    assert result["account_name"] == "app-a"
    assert result["databases"] == 2
    assert result["healthy"] == 1
    assert result["retention_only"] == 1
    assert result["items"][0]["db_identifier"] == "db-ok"
    assert result["items"][0]["backup"] == {
        "health": "healthy",
        "status": "SUCCESS",
        "last_at": "2026-06-17T12:00:00Z",
        "snapshot_id": "snapshot-ok",
        "error": None,
    }


def test_retention_diagnostic_returns_interpretation_and_samples(monkeypatch):
    fake_events = FakeTable([
        {
            "event_type": "SNAPSHOT_SKIPPED",
            "timestamp": "2026-06-17T04:00:00Z",
            "status": "SKIPPED",
            "skip_reason": "unmanaged",
            "snapshot_id": "manual-snapshot",
        },
        {
            "event_type": "SNAPSHOT_SKIPPED",
            "timestamp": "2026-06-17T04:00:01Z",
            "status": "SKIPPED",
            "skip_reason": "not_expired",
            "snapshot_id": "managed-snapshot",
        },
    ])
    fake_state = FakeTable([])
    monkeypatch.setattr(handler, "events_table", fake_events)
    monkeypatch.setattr(handler, "state_table", fake_state)

    response = handler.main(api_event("/audit/diagnostics/retention"), None)

    assert response["statusCode"] == 200
    result = body(response)
    assert result["retention"]["skipped_unmanaged"] == 1
    assert result["retention"]["skipped_not_expired"] == 1
    assert result["samples"]["unmanaged"][0]["snapshot_id"] == "manual-snapshot"
    assert result["interpretation"]["counts"]["not_expired"] == 1


def test_restore_candidates_returns_successful_backups_sorted(monkeypatch):
    fake_events = FakeTable([
        {
            "account_id": "210000000004",
            "account_name": "app-a",
            "event_type": "BACKUP_CREATED",
            "timestamp": "2026-06-17T12:00:00Z",
            "status": "SUCCESS",
            "region": "us-east-1",
            "db_identifier": "db-prod",
            "snapshot_id": "snap-old",
        },
        {
            "account_id": "210000000004",
            "account_name": "app-a",
            "event_type": "BACKUP_CREATED",
            "timestamp": "2026-06-17T16:00:00Z",
            "status": "SUCCESS",
            "region": "us-east-1",
            "db_identifier": "db-prod",
            "snapshot_id": "snap-new",
        },
    ])
    monkeypatch.setattr(handler, "events_table", fake_events)

    response = handler.main(
        api_event("/audit/diagnostics/restore-candidates/app-a/us-east-1/db-prod"),
        None,
    )

    assert response["statusCode"] == 200
    assert [item["snapshot_id"] for item in body(response)["items"]] == ["snap-new", "snap-old"]


def test_operation_diagnostic_groups_events_by_operation_id(monkeypatch):
    operation_id = "987b1570-dcca-4a74-bcf9-2b835a698d3f"
    fake_events = FakeTable([
        {
            "operation_id": operation_id,
            "account_id": "210000000004",
            "account_name": "app-a",
            "event_type": "BACKUP_CREATED",
            "timestamp": "2026-06-18T12:00:01Z",
            "status": "SUCCESS",
            "source": "RdsBackup",
            "region": "us-east-1",
            "db_identifier": "db-app-a-prod",
            "snapshot_id": "snapshot-1",
        },
        {
            "operation_id": operation_id,
            "account_id": "210000000005",
            "account_name": "app-d",
            "event_type": "ASSUME_ROLE_FAILED",
            "timestamp": "2026-06-18T12:00:02Z",
            "status": "ERROR",
            "source": "RdsBackup",
            "message": "AccessDenied",
        },
        {
            "operation_id": "other-operation",
            "account_id": "210000000007",
            "account_name": "app-c",
            "event_type": "BACKUP_CREATED",
            "timestamp": "2026-06-18T12:00:03Z",
            "status": "SUCCESS",
            "source": "RdsBackup",
        },
    ])
    monkeypatch.setattr(handler, "events_table", fake_events)

    response = handler.main(api_event(f"/audit/diagnostics/operation/{operation_id}"), None)

    assert response["statusCode"] == 200
    result = body(response)
    assert result["operation_id"] == operation_id
    assert result["status"] == "failed"
    assert result["accounts"] == 2
    assert result["databases"] == 1
    assert result["event_counts"] == {
        "ASSUME_ROLE_FAILED": {"error": 1},
        "BACKUP_CREATED": {"success": 1},
    }
    assert result["errors"][0]["event_type"] == "ASSUME_ROLE_FAILED"
    assert len(result["events"]) == 2


def test_invalid_limit_returns_400():
    response = handler.main(api_event("/audit/events", {"limit": "abc"}), None)

    assert response["statusCode"] == 400
    assert body(response)["error"]["code"] == "INVALID_FILTER"


class PagedFakeTable:
    def __init__(self, pages):
        self.pages = pages
        self.requests = []

    def _page(self, kwargs):
        self.requests.append(kwargs)
        index = kwargs.get("ExclusiveStartKey", {}).get("page", 0)
        result = {"Items": self.pages[index]}
        if index + 1 < len(self.pages):
            result["LastEvaluatedKey"] = {"page": index + 1}
        return result

    def query(self, **kwargs):
        return self._page(kwargs)

    def scan(self, **kwargs):
        return self._page(kwargs)


def backup_event(timestamp, snapshot_id="db-prod-1"):
    return {
        "account_id": "111111111111",
        "account_name": "app-a",
        "region": "us-east-1",
        "db_identifier": "db-prod",
        "event_type": "BACKUP_CREATED",
        "status": "SUCCESS",
        "snapshot_id": snapshot_id,
        "timestamp": timestamp,
    }


def test_summary_reads_every_events_page(monkeypatch):
    fake_events = PagedFakeTable([
        [backup_event("2026-11-10T16:00:00Z")],
        [backup_event("2026-11-11T16:00:00Z")],
        [backup_event("2026-11-12T16:00:00Z")],
    ])
    monkeypatch.setattr(handler, "events_table", fake_events)
    monkeypatch.setattr(handler, "state_table", FakeTable([]))

    response = handler.main(api_event("/audit/summary"), None)

    assert response["statusCode"] == 200
    assert body(response)["backups"]["success"] == 3
    assert len(fake_events.requests) == 3


def test_global_events_scan_applies_time_window(monkeypatch):
    fake_events = FakeTable([])
    monkeypatch.setattr(handler, "events_table", fake_events)

    response = handler.main(api_event("/audit/events", {
        "from": "2026-11-10T00:00:00Z",
        "to": "2026-11-17T23:59:59Z",
    }), None)

    assert response["statusCode"] == 200
    assert "FilterExpression" in fake_events.last_request
    assert "KeyConditionExpression" not in fake_events.last_request


def test_health_detects_errors_beyond_first_page(monkeypatch):
    failure = {
        "account_id": "111111111111",
        "account_name": "app-a",
        "event_type": "LIST_DBS_FAILED",
        "status": "ERROR",
        "timestamp": "2026-11-12T16:00:00Z",
    }
    fake_events = PagedFakeTable([
        [backup_event("2026-11-10T16:00:00Z")],
        [failure],
    ])
    monkeypatch.setattr(handler, "events_table", fake_events)
    monkeypatch.setattr(handler, "state_table", FakeTable([]))

    response = handler.main(api_event("/audit/diagnostics/health"), None)

    assert response["statusCode"] == 200
    result = body(response)
    assert result["status"] == "critical"
    assert result["error_health"]["active"] == 1


def test_restore_candidates_exclude_deleted_snapshots(monkeypatch):
    fake_events = PagedFakeTable([
        [backup_event("2026-11-01T16:00:00Z", "db-prod-old")],
        [
            backup_event("2026-11-16T16:00:00Z", "db-prod-new"),
            {
                **backup_event("2026-11-09T03:00:00Z", "db-prod-old"),
                "event_type": "SNAPSHOT_DELETED",
            },
        ],
    ])
    monkeypatch.setattr(handler, "events_table", fake_events)

    response = handler.main(api_event("/audit/diagnostics/restore-candidates/app-a/us-east-1/db-prod"), None)

    assert response["statusCode"] == 200
    assert [item["snapshot_id"] for item in body(response)["items"]] == ["db-prod-new"]


def test_state_path_finds_database_on_later_page(monkeypatch):
    fake_state = PagedFakeTable([
        [{"account_name": "app-a", "region": "us-east-1", "db_identifier": "db-other"}],
        [{"account_name": "app-a", "region": "us-east-1", "db_identifier": "db-prod"}],
    ])
    monkeypatch.setattr(handler, "state_table", fake_state)

    response = handler.main(api_event("/audit/state/app-a/us-east-1/db-prod"), None)

    assert response["statusCode"] == 200
    assert body(response)["db_identifier"] == "db-prod"
