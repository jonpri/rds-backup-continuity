import aws_cdk as core
import aws_cdk.assertions as assertions
import json

from stacks import config
from stacks.audit_api_stack import AuditApiStack
from stacks.audit_stack import AuditStack
from stacks.observability_stack import ObservabilityStack
from stacks.rds_backup_stack import RdsBackupStack
from stacks.retention_stack import RdsRetentionStack
from stacks.target_account_role_stack import TargetAccountRoleStack


def test_qa_accounts_config_contains_configured_pilot_accounts():
    qa_config = config.load_accounts_config(config.CONFIG_DIR / "accounts.qa.json")

    assert qa_config == {
        "default_retention_days": 7,
        "accounts": {
            "210000000001": {
                "account_name": "app-a",
                "enabled": True,
                "backup_enabled": True,
                "retention_enabled": True,
                "retention_days": 3,
            },
            "210000000002": {
                "account_name": "app-b",
                "enabled": True,
                "backup_enabled": True,
                "retention_enabled": True,
                "retention_days": 3,
            },
            "210000000003": {
                "account_name": "app-c",
                "enabled": True,
                "backup_enabled": True,
                "retention_enabled": True,
                "retention_days": 3,
            },
        },
    }


def test_backup_stack_creates_configured_schedules_and_lambda_env():
    app = core.App()
    stack = RdsBackupStack(app, "RdsBackupStack")
    template = assertions.Template.from_stack(stack)

    template.resource_count_is("AWS::Scheduler::Schedule", len(config.CRON_SCHEDULES))
    for schedule in config.CRON_SCHEDULES:
        template.has_resource_properties("AWS::Scheduler::Schedule", {
            "ScheduleExpression": f"cron({schedule['minute']} {schedule['hour']} * * ? *)",
            "ScheduleExpressionTimezone": config.SCHEDULE_TIMEZONE,
            "FlexibleTimeWindow": {
                "Mode": "OFF",
            },
        })
    template.has_resource_properties("AWS::IAM::Role", {
        "AssumeRolePolicyDocument": {
            "Statement": assertions.Match.array_with([
                assertions.Match.object_like({
                    "Principal": {"Service": "scheduler.amazonaws.com"},
                    "Action": "sts:AssumeRole",
                })
            ])
        },
    })
    template.has_resource_properties("AWS::IAM::Policy", {
        "PolicyDocument": {
            "Statement": assertions.Match.array_with([
                assertions.Match.object_like({
                    "Action": "lambda:InvokeFunction",
                })
            ])
        }
    })

    template.has_resource_properties("AWS::Lambda::Function", {
        "FunctionName": config.BACKUP_LAMBDA_FUNCTION_NAME,
        "Environment": {
            "Variables": assertions.Match.object_like({
                "SSM_PARAM_CONFIG": config.CONFIG_PARAM_NAME,
                "ROLE_NAME": config.ROLE_NAME,
                "POWERTOOLS_SERVICE_NAME": "RdsBackup",
                "ASSUME_ROLE_EXTERNAL_ID": config.ASSUME_ROLE_EXTERNAL_ID,
                "AUDIT_EVENTS_TABLE_NAME": config.AUDIT_EVENTS_TABLE_NAME,
                "AUDIT_STATE_TABLE_NAME": config.AUDIT_STATE_TABLE_NAME,
            })
        }
    })
    template.has_resource_properties("AWS::IAM::Role", {
        "RoleName": config.BACKUP_LAMBDA_EXECUTION_ROLE_NAME,
        "AssumeRolePolicyDocument": {
            "Statement": assertions.Match.array_with([
                assertions.Match.object_like({
                    "Principal": {"Service": "lambda.amazonaws.com"},
                    "Action": "sts:AssumeRole",
                })
            ])
        },
    })


def test_backup_stack_can_assume_configured_cross_account_roles():
    app = core.App()
    stack = RdsBackupStack(app, "RdsBackupStack")
    template = assertions.Template.from_stack(stack)

    for role_arn in config.CROSS_ACCOUNT_ROLES:
        template.has_resource_properties("AWS::IAM::Policy", {
            "PolicyDocument": {
                "Statement": assertions.Match.array_with([
                    assertions.Match.object_like({
                        "Action": "sts:AssumeRole",
                        "Resource": role_arn,
                        "Condition": {
                            "StringEquals": {
                                "sts:ExternalId": config.ASSUME_ROLE_EXTERNAL_ID,
                            }
                        },
                    })
                ])
            }
        })


def test_retention_stack_creates_config_parameter_and_schedule():
    app = core.App()
    stack = RdsRetentionStack(app, "RdsRetentionStack")
    template = assertions.Template.from_stack(stack)

    template.has_resource_properties("AWS::SSM::Parameter", {
        "Name": config.CONFIG_PARAM_NAME,
        "Type": "String",
    })
    template.resource_count_is("AWS::Events::Rule", 0)
    template.has_resource_properties("AWS::Scheduler::Schedule", {
        "Name": f"rds-retention-{config.ENVIRONMENT}",
        "ScheduleExpression": (
            f"cron({config.CRON_SCHEDULE_RETENTION['minute']} "
            f"{config.CRON_SCHEDULE_RETENTION['hour']} * * ? *)"
        ),
        "ScheduleExpressionTimezone": config.SCHEDULE_TIMEZONE,
        "FlexibleTimeWindow": {
            "Mode": "OFF",
        },
    })
    template.has_resource_properties("AWS::IAM::Role", {
        "AssumeRolePolicyDocument": {
            "Statement": assertions.Match.array_with([
                assertions.Match.object_like({
                    "Principal": {"Service": "scheduler.amazonaws.com"},
                    "Action": "sts:AssumeRole",
                })
            ])
        },
    })
    template.has_resource_properties("AWS::IAM::Policy", {
        "PolicyDocument": {
            "Statement": assertions.Match.array_with([
                assertions.Match.object_like({
                    "Action": "lambda:InvokeFunction",
                })
            ])
        }
    })
    template.has_resource_properties("AWS::Lambda::Function", {
        "FunctionName": config.RETENTION_LAMBDA_FUNCTION_NAME,
        "Environment": {
            "Variables": assertions.Match.object_like({
                "SSM_PARAM_CONFIG": config.CONFIG_PARAM_NAME,
                "ROLE_NAME": config.ROLE_NAME,
                "POWERTOOLS_SERVICE_NAME": "RdsRetention",
                "ASSUME_ROLE_EXTERNAL_ID": config.ASSUME_ROLE_EXTERNAL_ID,
                "AUDIT_EVENTS_TABLE_NAME": config.AUDIT_EVENTS_TABLE_NAME,
                "AUDIT_STATE_TABLE_NAME": config.AUDIT_STATE_TABLE_NAME,
            })
        }
    })
    template.has_resource_properties("AWS::IAM::Role", {
        "RoleName": config.RETENTION_LAMBDA_EXECUTION_ROLE_NAME,
        "AssumeRolePolicyDocument": {
            "Statement": assertions.Match.array_with([
                assertions.Match.object_like({
                    "Principal": {"Service": "lambda.amazonaws.com"},
                    "Action": "sts:AssumeRole",
                })
            ])
        },
    })


def test_observability_stack_creates_dashboard():
    app = core.App()
    stack = ObservabilityStack(app, "RdsObservabilityStack")
    template = assertions.Template.from_stack(stack)

    template.resource_count_is("AWS::CloudWatch::Dashboard", 1)
    template.has_resource_properties("AWS::CloudWatch::Dashboard", {
        "DashboardName": config.DASHBOARD_NAME,
    })
    dashboard = template.find_resources("AWS::CloudWatch::Dashboard")
    dashboard_body = next(iter(dashboard.values()))["Properties"]["DashboardBody"]
    dashboard_body_text = json.dumps(dashboard_body)
    assert "RdsBackup" in dashboard_body_text
    assert "RdsBackupV2" not in dashboard_body_text
    assert "RdsSnapshot" not in dashboard_body_text
    assert f'\\"start\\":\\"{config.DASHBOARD_DEFAULT_RANGE}\\"' in dashboard_body_text
    assert f'\\"period\\":{config.DASHBOARD_PERIOD_MINUTES * 60}' in dashboard_body_text


def test_audit_stack_creates_events_and_state_tables():
    app = core.App()
    stack = AuditStack(app, "RdsAuditStack")
    template = assertions.Template.from_stack(stack)

    template.resource_count_is("AWS::DynamoDB::Table", 2)
    for table_name in [
        config.AUDIT_EVENTS_TABLE_NAME,
        config.AUDIT_STATE_TABLE_NAME,
    ]:
        template.has_resource_properties("AWS::DynamoDB::Table", {
            "TableName": table_name,
            "BillingMode": "PAY_PER_REQUEST",
            "KeySchema": assertions.Match.array_with([
                assertions.Match.object_like({"AttributeName": "pk", "KeyType": "HASH"}),
                assertions.Match.object_like({"AttributeName": "sk", "KeyType": "RANGE"}),
            ]),
            "GlobalSecondaryIndexes": assertions.Match.array_with([
                assertions.Match.object_like({
                    "IndexName": "gsi1",
                    "KeySchema": assertions.Match.array_with([
                        assertions.Match.object_like({"AttributeName": "gsi1pk", "KeyType": "HASH"}),
                        assertions.Match.object_like({"AttributeName": "gsi1sk", "KeyType": "RANGE"}),
                    ]),
                })
            ]),
        })

    template.has_resource_properties("AWS::DynamoDB::Table", {
        "TableName": config.AUDIT_EVENTS_TABLE_NAME,
        "PointInTimeRecoverySpecification": {
            "PointInTimeRecoveryEnabled": True,
        },
    })

    tables = template.find_resources("AWS::DynamoDB::Table")
    state_table = next(
        table
        for table in tables.values()
        if table["Properties"]["TableName"] == config.AUDIT_STATE_TABLE_NAME
    )
    assert "PointInTimeRecoverySpecification" not in state_table["Properties"]


def test_audit_api_stack_creates_gateway_lambda_and_read_policies():
    app = core.App()
    stack = AuditApiStack(app, "RdsAuditApiStack")
    template = assertions.Template.from_stack(stack)

    template.resource_count_is("AWS::ApiGateway::RestApi", 1)
    template.resource_count_is("AWS::ApiGateway::ApiKey", 1)
    template.resource_count_is("AWS::ApiGateway::UsagePlan", 1)
    template.resource_count_is("AWS::ApiGateway::UsagePlanKey", 1)
    template.has_resource_properties("AWS::ApiGateway::RestApi", {
        "Name": config.AUDIT_API_NAME,
    })
    template.has_resource_properties("AWS::ApiGateway::Method", {
        "HttpMethod": "GET",
        "ApiKeyRequired": True,
    })
    template.has_resource_properties("AWS::Lambda::Function", {
        "FunctionName": config.AUDIT_API_LAMBDA_FUNCTION_NAME,
        "Environment": {
            "Variables": assertions.Match.object_like({
                "AUDIT_EVENTS_TABLE_NAME": config.AUDIT_EVENTS_TABLE_NAME,
                "AUDIT_STATE_TABLE_NAME": config.AUDIT_STATE_TABLE_NAME,
            })
        }
    })
    template.has_resource_properties("AWS::IAM::Policy", {
        "PolicyDocument": {
            "Statement": assertions.Match.array_with([
                assertions.Match.object_like({
                    "Action": [
                        "dynamodb:GetItem",
                        "dynamodb:Query",
                        "dynamodb:Scan",
                    ],
                })
            ])
        }
    })
    for path_part in [
        "audit",
        "summary",
        "state",
        "events",
        "diagnostics",
        "health",
        "retention",
        "operation",
        "account",
        "databases",
        "database",
        "restore-candidates",
        "{operation_id}",
        "{account_name}",
        "{region}",
        "{db_identifier}",
    ]:
        template.has_resource_properties("AWS::ApiGateway::Resource", {
            "PathPart": path_part,
        })


def test_target_account_role_stack_creates_cross_account_rds_role():
    app = core.App()
    stack = TargetAccountRoleStack(app, "RdsTargetAccountRoleStack")
    template = assertions.Template.from_stack(stack)

    template.has_resource_properties("AWS::IAM::Role", {
        "RoleName": config.ROLE_NAME,
        "AssumeRolePolicyDocument": {
            "Statement": assertions.Match.array_with([
                assertions.Match.object_like({
                    "Principal": {
                        "AWS": assertions.Match.object_like({
                            "Fn::Join": assertions.Match.array_with([
                                assertions.Match.array_with([
                                    assertions.Match.string_like_regexp(config.CENTRAL_ACCOUNT_ID)
                                ])
                            ])
                        }),
                    },
                    "Action": "sts:AssumeRole",
                    "Condition": {
                        "ArnEquals": {
                            "aws:PrincipalArn": [
                                config.BACKUP_LAMBDA_EXECUTION_ROLE_ARN,
                                config.RETENTION_LAMBDA_EXECUTION_ROLE_ARN,
                            ],
                        },
                        "StringEquals": {
                            "sts:ExternalId": config.ASSUME_ROLE_EXTERNAL_ID,
                        }
                    },
                })
            ])
        },
    })
    role_template = str(template.to_json())
    assert config.BACKUP_LAMBDA_EXECUTION_ROLE_NAME in role_template
    assert config.RETENTION_LAMBDA_EXECUTION_ROLE_NAME in role_template
    assert config.CENTRAL_ACCOUNT_ID in role_template

    template.has_resource_properties("AWS::IAM::Policy", {
        "PolicyDocument": {
            "Statement": assertions.Match.array_with([
                assertions.Match.object_like({
                    "Action": assertions.Match.array_with([
                        "rds:DescribeDBInstances",
                        "rds:CreateDBSnapshot",
                        "rds:DeleteDBSnapshot",
                    ]),
                    "Resource": "*",
                })
            ])
        }
    })
