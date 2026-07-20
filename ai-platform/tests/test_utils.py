"""Tests for shared Secrets Manager helpers."""

from __future__ import annotations

import json
from unittest.mock import Mock, patch

from ai_platform.utils import fetch_secret, fetch_secret_value


def _client_returning(secret: str) -> Mock:
    client = Mock()
    client.get_secret_value.return_value = {"SecretString": secret}
    return client


def test_fetch_secret_value_preserves_structured_secret():
    secret = json.dumps(
        {
            "username": "platform_admin",
            "password": "secret",
            "host": "aurora.internal",
            "port": 5432,
            "dbname": "ai_platform",
        }
    )
    client = _client_returning(secret)

    with patch("ai_platform.utils.boto3.client", return_value=client):
        assert fetch_secret_value("rds-secret-arn") == secret

    client.get_secret_value.assert_called_once_with(SecretId="rds-secret-arn")


def test_fetch_secret_extracts_provider_api_key():
    client = _client_returning(json.dumps({"api_key": "provider-key"}))

    with patch("ai_platform.utils.boto3.client", return_value=client):
        assert fetch_secret("provider-secret-arn") == "provider-key"


def test_fetch_secret_returns_plaintext_unchanged():
    client = _client_returning("plain-secret")

    with patch("ai_platform.utils.boto3.client", return_value=client):
        assert fetch_secret("plain-secret-arn") == "plain-secret"
