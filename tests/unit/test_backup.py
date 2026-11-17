import importlib
import os
import sys
from pathlib import Path


os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AUDIT_EVENTS_TABLE_NAME", "events")
os.environ.setdefault("AUDIT_STATE_TABLE_NAME", "state")
os.environ.setdefault("POWERTOOLS_TRACE_DISABLED", "true")
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "lambda"))

handler = importlib.import_module("backup.handler")


class FakeRds:
    class meta:
        region_name = "us-east-1"

    def __init__(self):
        self.created = []

    def create_db_snapshot(self, **kwargs):
        self.created.append(kwargs)

    def describe_db_snapshots(self, **kwargs):
        raise AssertionError("no debe volver a consultar el snapshot")

    def add_tags_to_resource(self, **kwargs):
        raise AssertionError("no debe volver a etiquetar el snapshot")


def test_create_snapshot_tags_on_creation_and_audits_success(monkeypatch):
    events = []
    states = []
    monkeypatch.setattr(handler, "put_audit_event", lambda **kwargs: events.append(kwargs))
    monkeypatch.setattr(handler, "update_backup_state", lambda **kwargs: states.append(kwargs))
    rds = FakeRds()

    entry = handler.create_snapshot_for_db(
        rds,
        "db-1",
        {"account_id": "111111111111", "account_name": "app-a"},
        "op-1",
    )

    assert "motivo" not in entry
    assert rds.created[0]["DBInstanceIdentifier"] == "db-1"
    assert {"Key": "CreatedBy", "Value": "RdsBackupLambda"} in rds.created[0]["Tags"]
    assert {"Key": "Service", "Value": "RdsBackup"} in rds.created[0]["Tags"]
    assert [event["event_type"] for event in events] == ["BACKUP_CREATED"]
    assert [state["status"] for state in states] == ["SUCCESS"]
