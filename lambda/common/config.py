import os

# Parámetros de entorno (definidos en el stack CDK)
CONFIG_PARAM_NAME = os.environ.get("SSM_PARAM_CONFIG", "/rds-backup/config")
ROLE_NAME = os.environ.get("ROLE_NAME", "RdsBackupCrossAccountRole")
ASSUME_ROLE_EXTERNAL_ID = os.environ.get("ASSUME_ROLE_EXTERNAL_ID", "rds-backup-123456789012")
AUDIT_EVENTS_TABLE_NAME = os.environ.get("AUDIT_EVENTS_TABLE_NAME", "rds-backup-audit-events")
AUDIT_STATE_TABLE_NAME = os.environ.get("AUDIT_STATE_TABLE_NAME", "rds-backup-audit-state")

# Metadata común para métricas y trazabilidad
PROJECT_NAME = "RdsBackup"
ENVIRONMENT = "dev"

# Se recomienda que cada Lambda defina su propio SERVICE_NAME en su handler
