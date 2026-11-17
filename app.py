#!/usr/bin/env python3
import aws_cdk as cdk

from stacks.rds_backup_stack import RdsBackupStack
from stacks.observability_stack import ObservabilityStack
from stacks.retention_stack import RdsRetentionStack
from stacks.audit_stack import AuditStack
from stacks.audit_api_stack import AuditApiStack
from stacks.target_account_role_stack import TargetAccountRoleStack

app = cdk.App()

# Stack para tablas de auditoría
audit_stack = AuditStack(app, "RdsAuditStack")

# Stack para Lambda que realiza limpieza
retention_stack = RdsRetentionStack(app, "RdsRetentionStack")
retention_stack.add_dependency(audit_stack)

# Stack para Lambda que realiza backups
backup_stack = RdsBackupStack(app, "RdsBackupStack")
backup_stack.add_dependency(audit_stack)
backup_stack.add_dependency(retention_stack)

# Stack para API de consulta de auditoría
audit_api_stack = AuditApiStack(app, "RdsAuditApiStack")
audit_api_stack.add_dependency(audit_stack)

# Stack para dashboard y alarmas
ObservabilityStack(app, "RdsObservabilityStack")

# Stack para rol cross-account en cuentas destino.
# Desplegar este stack solo autenticado contra la cuenta child.
TargetAccountRoleStack(app, "RdsTargetAccountRoleStack")

app.synth()
