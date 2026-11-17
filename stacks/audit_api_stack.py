from aws_cdk import (
    Duration,
    Stack,
    aws_apigateway as apigateway,
    aws_iam as iam,
    aws_logs as logs,
)
from aws_cdk.aws_lambda import Runtime
from aws_cdk.aws_lambda_python_alpha import PythonFunction
from constructs import Construct
from stacks import config


class AuditApiStack(Stack):

    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        audit_api_fn = PythonFunction(
            self,
            "RdsBackupAuditApiLambda",
            entry="lambda",
            index="audit_api/handler.py",
            handler="main",
            runtime=Runtime.PYTHON_3_12,
            function_name=config.AUDIT_API_LAMBDA_FUNCTION_NAME,
            environment={
                "AUDIT_EVENTS_TABLE_NAME": config.AUDIT_EVENTS_TABLE_NAME,
                "AUDIT_STATE_TABLE_NAME": config.AUDIT_STATE_TABLE_NAME,
            },
            timeout=Duration.seconds(30),
            log_retention=logs.RetentionDays.ONE_WEEK,
        )

        audit_api_fn.add_to_role_policy(iam.PolicyStatement(
            actions=[
                "dynamodb:GetItem",
                "dynamodb:Query",
                "dynamodb:Scan",
            ],
            resources=[
                f"arn:aws:dynamodb:{self.region}:{self.account}:table/{config.AUDIT_EVENTS_TABLE_NAME}",
                f"arn:aws:dynamodb:{self.region}:{self.account}:table/{config.AUDIT_EVENTS_TABLE_NAME}/index/*",
                f"arn:aws:dynamodb:{self.region}:{self.account}:table/{config.AUDIT_STATE_TABLE_NAME}",
                f"arn:aws:dynamodb:{self.region}:{self.account}:table/{config.AUDIT_STATE_TABLE_NAME}/index/*",
            ],
        ))

        api = apigateway.RestApi(
            self,
            "RdsBackupAuditApi",
            rest_api_name=config.AUDIT_API_NAME,
            deploy_options=apigateway.StageOptions(
                stage_name=config.ENVIRONMENT,
                metrics_enabled=True,
                logging_level=apigateway.MethodLoggingLevel.INFO,
            ),
        )

        integration = apigateway.LambdaIntegration(audit_api_fn, proxy=True)

        api_key = api.add_api_key(
            "RdsBackupAuditApiKey",
            api_key_name=f"{config.AUDIT_API_NAME}-{config.ENVIRONMENT}",
        )
        usage_plan = api.add_usage_plan(
            "RdsBackupAuditUsagePlan",
            name=f"{config.AUDIT_API_NAME}-{config.ENVIRONMENT}",
            api_stages=[
                apigateway.UsagePlanPerApiStage(
                    api=api,
                    stage=api.deployment_stage,
                )
            ],
        )
        usage_plan.add_api_key(api_key)

        audit = api.root.add_resource("audit")
        audit.add_resource("summary").add_method("GET", integration, api_key_required=True)

        diagnostics = audit.add_resource("diagnostics")
        diagnostics.add_resource("health").add_method("GET", integration, api_key_required=True)
        diagnostics.add_resource("retention").add_method("GET", integration, api_key_required=True)
        diagnostics.add_resource("operation").add_resource("{operation_id}").add_method(
            "GET",
            integration,
            api_key_required=True,
        )
        diagnostics.add_resource("databases").add_resource("{account_name}").add_method(
            "GET",
            integration,
            api_key_required=True,
        )
        diagnostics.add_resource("account").add_resource("{account_name}").add_method(
            "GET",
            integration,
            api_key_required=True,
        )
        diagnostics_database = diagnostics.add_resource("database")
        diagnostics_database_account = diagnostics_database.add_resource("{account_name}")
        diagnostics_database_region = diagnostics_database_account.add_resource("{region}")
        diagnostics_database_region.add_resource("{db_identifier}").add_method(
            "GET",
            integration,
            api_key_required=True,
        )
        diagnostics_restore = diagnostics.add_resource("restore-candidates")
        diagnostics_restore_account = diagnostics_restore.add_resource("{account_name}")
        diagnostics_restore_region = diagnostics_restore_account.add_resource("{region}")
        diagnostics_restore_region.add_resource("{db_identifier}").add_method(
            "GET",
            integration,
            api_key_required=True,
        )

        state = audit.add_resource("state")
        state.add_method("GET", integration, api_key_required=True)
        state_account_name = state.add_resource("{account_name}")
        state_account_name.add_method("GET", integration, api_key_required=True)
        state_region = state_account_name.add_resource("{region}")
        state_region.add_resource("{db_identifier}").add_method("GET", integration, api_key_required=True)

        events = audit.add_resource("events")
        events.add_method("GET", integration, api_key_required=True)
        events.add_resource("{account_name}").add_method("GET", integration, api_key_required=True)
