import json
from aws_cdk import (
    Stack,
    Duration,
    aws_iam as iam,
    aws_ssm as ssm,
    RemovalPolicy,
    aws_scheduler as scheduler,
)
from constructs import Construct
from stacks import config
from aws_cdk.aws_lambda_python_alpha import PythonFunction
from aws_cdk.aws_lambda import Runtime


class RdsRetentionStack(Stack):

    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        config_param_name = config.CONFIG_PARAM_NAME
        role_name = config.ROLE_NAME

        # Convertir BACKUP_CONFIG a JSON ordenado y legible
        backup_config_json = json.dumps(config.BACKUP_CONFIG, indent=2, sort_keys=True)

        retention_role = iam.Role(
            self,
            "RdsRetentionLambdaExecutionRole",
            role_name=config.RETENTION_LAMBDA_EXECUTION_ROLE_NAME,
            assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"),
            managed_policies=[
                iam.ManagedPolicy.from_aws_managed_policy_name(
                    "service-role/AWSLambdaBasicExecutionRole"
                )
            ],
        )

        # Lambda Function para ejecutar limpieza de snapshots antiguos
        retention_fn = PythonFunction(
            self, "RdsRetentionLambda",
            entry="lambda",  # ✅ Corregido
            runtime=Runtime.PYTHON_3_11,
            index="retention/handler.py",
            handler="main",
            function_name=config.RETENTION_LAMBDA_FUNCTION_NAME,
            environment={
                "ROLE_NAME": role_name,
                "POWERTOOLS_SERVICE_NAME": "RdsRetention",
                "SSM_PARAM_CONFIG": config_param_name,
                "ASSUME_ROLE_EXTERNAL_ID": config.ASSUME_ROLE_EXTERNAL_ID,
                "AUDIT_EVENTS_TABLE_NAME": config.AUDIT_EVENTS_TABLE_NAME,
                "AUDIT_STATE_TABLE_NAME": config.AUDIT_STATE_TABLE_NAME,
            },
            timeout=Duration.minutes(5),
            role=retention_role,
        )

        # Permiso para leer el parámetro desde SSM
        retention_fn.add_to_role_policy(iam.PolicyStatement(
            actions=["ssm:GetParameter"],
            resources=[
                f"arn:aws:ssm:{self.region}:{self.account}:parameter{config_param_name}"
            ]
        ))

        # Permiso para asumir roles cross-account
        retention_fn.add_to_role_policy(iam.PolicyStatement(
            actions=["sts:AssumeRole"],
            resources=config.CROSS_ACCOUNT_ROLES,
            conditions={
                "StringEquals": {
                    "sts:ExternalId": config.ASSUME_ROLE_EXTERNAL_ID
                }
            },
        ))

        # Permiso para publicar métricas personalizadas
        retention_fn.add_to_role_policy(iam.PolicyStatement(
            actions=["cloudwatch:PutMetricData"],
            resources=["*"]
        ))

        retention_fn.add_to_role_policy(iam.PolicyStatement(
            actions=["dynamodb:PutItem"],
            resources=[
                f"arn:aws:dynamodb:{self.region}:{self.account}:table/{config.AUDIT_EVENTS_TABLE_NAME}"
            ]
        ))

        retention_fn.add_to_role_policy(iam.PolicyStatement(
            actions=["dynamodb:UpdateItem"],
            resources=[
                f"arn:aws:dynamodb:{self.region}:{self.account}:table/{config.AUDIT_STATE_TABLE_NAME}"
            ]
        ))

        # Crear o actualizar el parámetro SSM con la configuración operativa
        param = ssm.StringParameter(
            self, "BackupConfigParameter",
            parameter_name=config_param_name,
            string_value=backup_config_json,
            description="Configuración operativa por cuenta para backups y retención RDS",
            simple_name=False
        )

        param.apply_removal_policy(RemovalPolicy.RETAIN)

        self.retention_lambda = retention_fn

        scheduler_role = iam.Role(
            self,
            "RdsRetentionSchedulerRole",
            assumed_by=iam.ServicePrincipal("scheduler.amazonaws.com"),
        )
        scheduler_role.add_to_policy(iam.PolicyStatement(
            actions=["lambda:InvokeFunction"],
            resources=[retention_fn.function_arn],
        ))

        # Schedule diario con zona horaria local desde configuración.
        scheduler.CfnSchedule(
            self,
            "RdsRetentionSchedule",
            flexible_time_window=scheduler.CfnSchedule.FlexibleTimeWindowProperty(
                mode="OFF",
            ),
            name=f"rds-retention-{config.ENVIRONMENT}",
            schedule_expression=(
                f"cron({config.CRON_SCHEDULE_RETENTION['minute']} "
                f"{config.CRON_SCHEDULE_RETENTION['hour']} * * ? *)"
            ),
            schedule_expression_timezone=config.SCHEDULE_TIMEZONE,
            target=scheduler.CfnSchedule.TargetProperty(
                arn=retention_fn.function_arn,
                role_arn=scheduler_role.role_arn,
            ),
        )
