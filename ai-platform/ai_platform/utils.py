"""
Shared utilities used by both the gateway and the health checker Lambda.
"""
from __future__ import annotations

import json
import logging

import boto3

logger = logging.getLogger(__name__)


def fetch_secret(arn: str) -> str:
    """
    Fetch a plaintext or JSON secret from Secrets Manager.
    If the secret value is a JSON object, returns the first value found.
    """
    client = boto3.client("secretsmanager")
    resp = client.get_secret_value(SecretId=arn)
    value = resp.get("SecretString", "")
    try:
        parsed = json.loads(value)
        if isinstance(parsed, dict):
            return next(iter(parsed.values()))
    except (json.JSONDecodeError, StopIteration):
        pass
    return value
