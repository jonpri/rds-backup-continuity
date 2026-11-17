from aws_cdk import (
    Stack,
    aws_cloudwatch as cw,
    Tags,
    Duration
)
from constructs import Construct
from stacks import config


class ObservabilityStack(Stack):
    """
    Stack que crea métricas y dashboard para monitorear
        el proceso de backup de RDS y la Lambda de retención,
        usando el namespace 'RdsBackup'.
    """

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        env=None,
        **kwargs
    ) -> None:
        super().__init__(scope, construct_id, env=env, **kwargs)

        # Etiquetas globales
        env_tag_value = env.environment if env and getattr(env, "environment", None) else config.ENVIRONMENT
        Tags.of(self).add("Project", config.PROJECT_NAME)
        Tags.of(self).add("Environment", env_tag_value)

        period = Duration.minutes(config.DASHBOARD_PERIOD_MINUTES)

        # Métricas para RdsSnapshot (ya existentes)
        metric_snapshots_created = cw.Metric(
            namespace="RdsBackup",
            metric_name="SnapshotsCreatedTotal",
            dimensions_map={"service": "RdsBackup"},
            period=period,
            statistic="Sum"
        )
        metric_skipped_invalid_state = cw.Metric(
            namespace="RdsBackup",
            metric_name="SkippedInvalidState",
            dimensions_map={"service": "RdsBackup"},
            period=period,
            statistic="Sum"
        )
        metric_snapshot_errors = cw.Metric(
            namespace="RdsBackup",
            metric_name="SnapshotErrors",
            dimensions_map={"service": "RdsBackup"},
            period=period,
            statistic="Sum"
        )
        metric_missing_backup_role = cw.Metric(
            namespace="RdsBackup",
            metric_name="MissingBackupRole",
            dimensions_map={"service": "RdsBackup"},
            period=period,
            statistic="Sum"
        )

        # Métricas para Lambda Retention (nuevas)
        metric_snapshots_deleted = cw.Metric(
            namespace="RdsBackup",
            metric_name="SnapshotsDeleted",
            dimensions_map={"service": "RdsRetention"},
            period=period,
            statistic="Sum"
        )
        metric_retention_errors = cw.Metric(
            namespace="RdsBackup",
            metric_name="SnapshotErrors",
            dimensions_map={"service": "RdsRetention"},
            period=period,
            statistic="Sum"
        )
        metric_missing_retention_role = cw.Metric(
            namespace="RdsBackup",
            metric_name="MissingRetentionRole",
            dimensions_map={"service": "RdsRetention"},
            period=period,
            statistic="Sum"
        )

        # Dashboard con widgets para snapshots y retención
        dashboard = cw.Dashboard(
            self,
            "RdsBackupDashboard",
            dashboard_name=config.DASHBOARD_NAME,
            start=config.DASHBOARD_DEFAULT_RANGE,
        )
        dashboard.add_widgets(
            cw.GraphWidget(
                title="Snapshots Creados (Total)",
                left=[metric_snapshots_created],
                width=12,
                period=period,
            ),
            cw.GraphWidget(
                title="Snapshots Omitidos (Estado inválido)",
                left=[metric_skipped_invalid_state],
                width=12,
                period=period,
            ),
            cw.GraphWidget(
                title="Errores al Crear Snapshots",
                left=[metric_snapshot_errors],
                width=12,
                period=period,
            ),
            cw.GraphWidget(
                title="Cuentas sin Rol de Backup",
                left=[metric_missing_backup_role],
                width=12,
                period=period,
            ),

            # Widgets nuevos para Lambda Retention
            cw.GraphWidget(
                title="Snapshots Eliminados por Retención",
                left=[metric_snapshots_deleted],
                width=12,
                period=period,
            ),
            cw.GraphWidget(
                title="Errores en Lambda Retention",
                left=[metric_retention_errors],
                width=12,
                period=period,
            ),
            cw.GraphWidget(
                title="Cuentas sin Rol de Retención",
                left=[metric_missing_retention_role],
                width=12,
                period=period,
            )
        )
