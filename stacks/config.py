import json
import os
from pathlib import Path

# Proyecto
PROJECT_NAME = "RdsBackup"
ENVIRONMENT = os.environ.get("RDS_BACKUP_ENV", "qa")

# Nombre del rol asumido en cada cuenta administrada(hija)
ROLE_NAME = "RdsBackupCrossAccountRole"
CENTRAL_ACCOUNT_ID = os.environ.get("RDS_BACKUP_CENTRAL_ACCOUNT_ID", "123456789012")
ASSUME_ROLE_EXTERNAL_ID = f"rds-backup-{CENTRAL_ACCOUNT_ID}"
BACKUP_LAMBDA_EXECUTION_ROLE_NAME = "RdsBackupLambdaExecutionRole"
RETENTION_LAMBDA_EXECUTION_ROLE_NAME = "RdsRetentionLambdaExecutionRole"
BACKUP_LAMBDA_EXECUTION_ROLE_ARN = (
    f"arn:aws:iam::{CENTRAL_ACCOUNT_ID}:role/{BACKUP_LAMBDA_EXECUTION_ROLE_NAME}"
)
RETENTION_LAMBDA_EXECUTION_ROLE_ARN = (
    f"arn:aws:iam::{CENTRAL_ACCOUNT_ID}:role/{RETENTION_LAMBDA_EXECUTION_ROLE_NAME}"
)

# Nombre del parámetro canónico en SSM
CONFIG_PARAM_NAME = "/rds-backup/config"

# Tablas de auditoría
AUDIT_EVENTS_TABLE_NAME = "rds-backup-audit-events"
AUDIT_STATE_TABLE_NAME = "rds-backup-audit-state"
AUDIT_API_NAME = "rds-backup-audit-api"
DASHBOARD_NAME = "rds-backup-operations"
DASHBOARD_DEFAULT_RANGE = "-PT3H"
DASHBOARD_PERIOD_MINUTES = 5
BACKUP_LAMBDA_FUNCTION_NAME = "rds-backup-runner"
RETENTION_LAMBDA_FUNCTION_NAME = "rds-backup-retention"
AUDIT_API_LAMBDA_FUNCTION_NAME = "rds-backup-audit-api"

CONFIG_DIR = Path(__file__).resolve().parents[1] / "config"
ACCOUNTS_CONFIG_FILE = Path(
    os.environ.get(
        "RDS_BACKUP_ACCOUNTS_CONFIG",
        CONFIG_DIR / f"accounts.{ENVIRONMENT}.json",
    )
)


def load_accounts_config(config_path=ACCOUNTS_CONFIG_FILE):
    with Path(config_path).open(encoding="utf-8") as config_file:
        return json.load(config_file)


# Configuración operativa de cuentas. Este valor se publica en SSM.
BACKUP_CONFIG = load_accounts_config()

ACCOUNTS_STRING = ",".join(BACKUP_CONFIG["accounts"].keys())

CROSS_ACCOUNT_ROLES = [
    f"arn:aws:iam::{acc}:role/{ROLE_NAME}"
    for acc in BACKUP_CONFIG["accounts"].keys()
]
# Horarios orientados a mejorar el RPO durante la jornada operativa.
# Con backups a las 12:00 y 16:00:
# - Si ocurre un error a las 15:30, se puede volver al backup de las 12:00.
# - Si ocurre un error a las 18:30, se puede volver al backup de las 16:00.
# Esto reduce la perdida potencial de datos frente a respaldar solo al inicio
# o al final del dia.
CRON_SCHEDULES = [
    {"hour": "12", "minute": "0"},
    {"hour": "16", "minute": "0"}
]

CRON_SCHEDULE_RETENTION = {
    "minute": "0",
    "hour": "00"
}

SCHEDULE_TIMEZONE = "America/Santiago"
