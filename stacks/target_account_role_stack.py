from aws_cdk import (
    Stack,
    aws_iam as iam,
)
from constructs import Construct
from stacks import config


class TargetAccountRoleStack(Stack):

    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        role = iam.Role(
            self,
            "RdsBackupCrossAccountRole",
            role_name=config.ROLE_NAME,
            assumed_by=iam.PrincipalWithConditions(
                iam.AccountPrincipal(config.CENTRAL_ACCOUNT_ID),
                {
                    "StringEquals": {
                        "sts:ExternalId": config.ASSUME_ROLE_EXTERNAL_ID,
                    },
                    "ArnEquals": {
                        "aws:PrincipalArn": [
                            config.BACKUP_LAMBDA_EXECUTION_ROLE_ARN,
                            config.RETENTION_LAMBDA_EXECUTION_ROLE_ARN,
                        ],
                    },
                },
            ),
            description="Rol asumido por la cuenta central para backup y retención de snapshots RDS",
        )

        role.add_to_policy(iam.PolicyStatement(
            actions=[
                "rds:DescribeDBInstances",
                "rds:DescribeDBSnapshots",
                "rds:CreateDBSnapshot",
                "rds:AddTagsToResource",
                "rds:ListTagsForResource",
                "rds:DeleteDBSnapshot",
            ],
            resources=["*"],
        ))
