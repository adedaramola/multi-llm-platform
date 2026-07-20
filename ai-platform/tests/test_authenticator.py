"""Tests for production API-key authentication behavior."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from ai_platform.auth.authenticator import Authenticator
from botocore.exceptions import ClientError, EndpointConnectionError
from fastapi import HTTPException


def _authenticator(item: dict | None = None, error: Exception | None = None) -> Authenticator:
    authenticator = object.__new__(Authenticator)
    table = MagicMock()
    if error:
        table.get_item.side_effect = error
    else:
        table.get_item.return_value = {"Item": item} if item else {}
    authenticator._table = table
    authenticator._settings = SimpleNamespace(
        environment="production",
        default_rpm=60,
        default_rpd=5000,
    )
    return authenticator


@pytest.mark.asyncio
async def test_valid_key_returns_caller_identity():
    authenticator = _authenticator(
        {
            "caller_id": "caller-1",
            "app_name": "integration-test",
            "rpm_limit": 10,
            "rpd_limit": 100,
            "active": True,
        }
    )

    caller = await authenticator.authenticate("raw-key")

    assert caller.caller_id == "caller-1"
    expected_hash = authenticator._hash_key("raw-key")
    authenticator._table.get_item.assert_called_once_with(Key={"key_hash": expected_hash})


@pytest.mark.asyncio
async def test_missing_key_is_rejected():
    with pytest.raises(HTTPException) as exc_info:
        await _authenticator().authenticate("missing")
    assert exc_info.value.status_code == 401


@pytest.mark.asyncio
async def test_revoked_key_is_rejected():
    with pytest.raises(HTTPException) as exc_info:
        await _authenticator({"caller_id": "revoked", "active": False}).authenticate("key")
    assert exc_info.value.status_code == 401


@pytest.mark.asyncio
async def test_dynamodb_failure_returns_service_unavailable():
    error = ClientError({"Error": {"Code": "InternalError", "Message": "failed"}}, "GetItem")
    with pytest.raises(HTTPException) as exc_info:
        await _authenticator(error=error).authenticate("key")
    assert exc_info.value.status_code == 503


@pytest.mark.asyncio
async def test_dynamodb_connection_failure_returns_service_unavailable():
    error = EndpointConnectionError(endpoint_url="https://dynamodb.us-east-1.amazonaws.com")
    with pytest.raises(HTTPException) as exc_info:
        await _authenticator(error=error).authenticate("key")
    assert exc_info.value.status_code == 503
