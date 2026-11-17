import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "lambda"))

from common import account_config


def test_get_managed_accounts_supports_legacy_retention_shape(monkeypatch):
    monkeypatch.setattr(account_config, "get_retention_config", lambda: {
        "default_retention_days": 7,
        "accounts": {
            "210000000006": 6,
        },
    })

    accounts = account_config.get_managed_accounts(feature="backup")

    assert accounts == [{
        "account_id": "210000000006",
        "account_name": "210000000006",
        "enabled": True,
        "backup_enabled": True,
        "retention_enabled": True,
        "retention_days": 6,
    }]


def test_get_managed_accounts_supports_account_name_and_feature_flags(monkeypatch):
    monkeypatch.setattr(account_config, "get_retention_config", lambda: {
        "default_retention_days": 7,
        "accounts": {
            "111111111111": {
                "account_name": "app-a",
                "backup_enabled": True,
                "retention_enabled": False,
                "retention_days": 3,
            },
            "222222222222": {
                "account_name": "app-d",
                "enabled": False,
                "retention_days": 5,
            },
        },
    })

    backup_accounts = account_config.get_managed_accounts(feature="backup")
    retention_accounts = account_config.get_managed_accounts(feature="retention")

    assert backup_accounts[0]["account_id"] == "111111111111"
    assert backup_accounts[0]["account_name"] == "app-a"
    assert backup_accounts[0]["retention_days"] == 3
    assert retention_accounts == []
