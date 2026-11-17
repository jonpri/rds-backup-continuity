import boto3
from common import config
from common.logging_utils import log_with_context

sts = boto3.client("sts")

# Asume el rol configurado en la cuenta destino para obtener acceso temporal
def assume_role(account_id: str, role_name: str, session_name: str = "CrossAccountSession") -> boto3.client:
    role_arn = f"arn:aws:iam::{account_id}:role/{role_name}"
    log_with_context(f"Asumiendo rol: {role_arn}", step="assume_role")
    creds = sts.assume_role(
        RoleArn=role_arn,
        RoleSessionName=session_name,
        ExternalId=config.ASSUME_ROLE_EXTERNAL_ID,
    )
    return boto3.client(
        "rds",
        aws_access_key_id=creds["Credentials"]["AccessKeyId"],
        aws_secret_access_key=creds["Credentials"]["SecretAccessKey"],
        aws_session_token=creds["Credentials"]["SessionToken"]
    )
