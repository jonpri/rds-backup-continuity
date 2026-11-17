from common.retention_utils import get_retention_config


def normalize_account_config(account_id, raw_config, default_retention_days):
    if isinstance(raw_config, dict):
        account = dict(raw_config)
    else:
        account = {"retention_days": raw_config}

    account["account_id"] = str(account_id)
    account.setdefault("account_name", str(account_id))
    account.setdefault("enabled", True)
    account.setdefault("backup_enabled", True)
    account.setdefault("retention_enabled", True)
    account.setdefault("retention_days", default_retention_days)
    return account


def get_managed_accounts(feature=None):
    config = get_retention_config()
    default_retention_days = config.get("default_retention_days", 7)
    accounts = []

    for account_id, raw_config in config.get("accounts", {}).items():
        account = normalize_account_config(
            account_id,
            raw_config,
            default_retention_days,
        )

        if not account.get("enabled", True):
            continue

        if feature and not account.get(f"{feature}_enabled", True):
            continue

        accounts.append(account)

    return accounts
