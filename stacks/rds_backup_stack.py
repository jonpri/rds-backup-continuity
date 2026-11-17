from aws_cdk import (
    Stack,
    Duration,
    aws_iam as iam,
    aws_logs as logs,
    aws_scheduler as scheduler,
)
from aws_cdk.aws_lambda_python_alpha import PythonFunction
from aws_cdk.aws_lambda import Runtime
from constructs import Construct
from stacks import config  # configuración de infraestructura

class RdsBackupStack(Stack):

    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        backup_role = iam.Role(
            self,
            "RdsBackupLambdaExecutionRole",
            role_name=config.BACKUP_LAMBDA_EXECUTION_ROLE_NAME,
            assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"),
            managed_policies=[
                iam.ManagedPolicy.from_aws_managed_policy_name(
                    "service-role/AWSLambdaBasicExecutionRole"
                )
            ],
        )

        # Lambda que ejecuta el backup de RDS
        lambda_fn = PythonFunction(
            self, "RdsBackupLambda",
            entry="lambda",                  # ✅ empaqueta backup + common
            index="backup/handler.py",       # ✅ punto de entrada dentro del entry
            handler="main",
            runtime=Runtime.PYTHON_3_12,
            function_name=config.BACKUP_LAMBDA_FUNCTION_NAME,
            environment={
                "SSM_PARAM_CONFIG": config.CONFIG_PARAM_NAME,
                "ROLE_NAME": config.ROLE_NAME,
                "POWERTOOLS_SERVICE_NAME": "RdsBackup",
                "ASSUME_ROLE_EXTERNAL_ID": config.ASSUME_ROLE_EXTERNAL_ID,
                "AUDIT_EVENTS_TABLE_NAME": config.AUDIT_EVENTS_TABLE_NAME,
                "AUDIT_STATE_TABLE_NAME": config.AUDIT_STATE_TABLE_NAME,
            },
            timeout=Duration.minutes(5),
            log_retention=logs.RetentionDays.ONE_WEEK,
            role=backup_role,
        )

        # Permiso para leer desde SSM Parameter Store
        lambda_fn.add_to_role_policy(iam.PolicyStatement(
            actions=["ssm:GetParameter"],
            resources=[
                f"arn:aws:ssm:{self.region}:{self.account}:parameter{config.CONFIG_PARAM_NAME}",
            ]
        ))

        lambda_fn.add_to_role_policy(iam.PolicyStatement(
            actions=["dynamodb:PutItem"],
            resources=[
                f"arn:aws:dynamodb:{self.region}:{self.account}:table/{config.AUDIT_EVENTS_TABLE_NAME}"
            ]
        ))

        lambda_fn.add_to_role_policy(iam.PolicyStatement(
            actions=["dynamodb:UpdateItem"],
            resources=[
                f"arn:aws:dynamodb:{self.region}:{self.account}:table/{config.AUDIT_STATE_TABLE_NAME}"
            ]
        ))

        # Permisos para asumir roles cross-account
        for role_arn in config.CROSS_ACCOUNT_ROLES:
            lambda_fn.add_to_role_policy(iam.PolicyStatement(
                actions=["sts:AssumeRole"],
                resources=[role_arn],
                conditions={
                    "StringEquals": {
                        "sts:ExternalId": config.ASSUME_ROLE_EXTERNAL_ID
                    }
                },
            ))

        # Permiso para publicar métricas personalizadas en CloudWatch
        lambda_fn.add_to_role_policy(iam.PolicyStatement(
            actions=["cloudwatch:PutMetricData"],
            resources=["*"]
        ))

        scheduler_role = iam.Role(
            self,
            "RdsBackupSchedulerRole",
            assumed_by=iam.ServicePrincipal("scheduler.amazonaws.com"),
        )
        scheduler_role.add_to_policy(iam.PolicyStatement(
            actions=["lambda:InvokeFunction"],
            resources=[lambda_fn.function_arn],
        ))

        # Crear schedules con zona horaria local desde configuración
        for idx, cron in enumerate(config.CRON_SCHEDULES):
            scheduler.CfnSchedule(
                self,
                f"RdsBackupSchedule{idx}",
                flexible_time_window=scheduler.CfnSchedule.FlexibleTimeWindowProperty(
                    mode="OFF",
                ),
                name=f"rds-backup-{config.ENVIRONMENT}-{idx}",
                schedule_expression=f"cron({cron['minute']} {cron['hour']} * * ? *)",
                schedule_expression_timezone=config.SCHEDULE_TIMEZONE,
                target=scheduler.CfnSchedule.TargetProperty(
                    arn=lambda_fn.function_arn,
                    role_arn=scheduler_role.role_arn,
                ),
            )
