import json
import boto3
from typing import Dict
from common import config

ssm = boto3.client("ssm")


def get_retention_config() -> Dict:
    """
    Lee desde SSM el JSON de configuración operativa.
    Retorna un diccionario como:
    {
        "default_retention_days": 7,
        "accounts": {
            "111122223333": 3,
            "444455556666": 4
        }
    }
    """
    response = ssm.get_parameter(Name=config.CONFIG_PARAM_NAME)
    return json.loads(response["Parameter"]["Value"])
