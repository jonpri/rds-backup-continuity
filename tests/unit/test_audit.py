import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "lambda"))

from common import audit


class FakeTable:
    def __init__(self):
        self.items = []
        self.updates = []

    def put_item(self, Item):
        self.items.append(Item)

    def update_item(self, **kwargs):
        self.updates.append(kwargs)


def test_put_audit_event_builds_account_and_account_name_keys(monkeypatch):
    fake_events = FakeTable()
    monkeypatch.setattr(audit, "events_table", fake_events)
    monkeypatch.setattr(audit, "utc_now_iso", lambda: "2026-06-10T17:00:00Z")

    audit.put_audit_event(
        event_type="BACKUP_CREATED",
        account_id="210000000006",
        account_name="app-a",
        region="us-east-1",
        db_identifier="db-prod",
        snapshot_id="db-prod-123",
        status="SUCCESS",
        source="RdsBackup",
        message="ok",
        operation_id="987b1570-dcca-4a74-bcf9-2b835a698d3f",
    )

    item = fake_events.items[0]
    assert item["pk"] == "ACCOUNT#210000000006"
    assert item["sk"] == "2026-06-10T17:00:00Z#BACKUP_CREATED#db-prod-123"
    assert item["gsi1pk"] == "ACCOUNT_NAME#app-a"
    assert item["gsi1sk"] == "2026-06-10T17:00:00Z#BACKUP_CREATED#db-prod-123"
    assert item["account_name"] == "app-a"
    assert item["operation_id"] == "987b1570-dcca-4a74-bcf9-2b835a698d3f"


def test_update_backup_state_uses_db_key_and_account_name_gsi(monkeypatch):
    fake_state = FakeTable()
    monkeypatch.setattr(audit, "state_table", fake_state)
    monkeypatch.setattr(audit, "utc_now_iso", lambda: "2026-06-10T17:00:00Z")

    audit.update_backup_state(
        account_id="210000000006",
        account_name="app-a",
        region="us-east-1",
        db_identifier="db-prod",
        status="SUCCESS",
        snapshot_id="db-prod-123",
        operation_id="987b1570-dcca-4a74-bcf9-2b835a698d3f",
    )

    update = fake_state.updates[0]
    assert update["Key"] == {
        "pk": "ACCOUNT#210000000006",
        "sk": "DB#us-east-1#db-prod",
    }
    values = update["ExpressionAttributeValues"]
    assert "ACCOUNT_NAME#app-a" in values.values()
    assert "DB#us-east-1#db-prod" in values.values()
    assert "SUCCESS" in values.values()
    assert "987b1570-dcca-4a74-bcf9-2b835a698d3f" in values.values()
