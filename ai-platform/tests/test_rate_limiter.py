"""Tests for production rate-limit enforcement and dependency failures."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from ai_platform.auth.authenticator import CallerIdentity
from ai_platform.auth.rate_limiter import RateLimiter
from fastapi import HTTPException


def _limiter(*, fail_open: bool = False) -> RateLimiter:
    limiter = object.__new__(RateLimiter)
    limiter._fail_open = fail_open
    return limiter


def _caller() -> CallerIdentity:
    return CallerIdentity("caller", "app", rpm_limit=2, rpd_limit=3, active=True)


@pytest.mark.asyncio
async def test_limits_allow_request_below_both_quotas():
    limiter = _limiter()
    limiter._increment = AsyncMock(side_effect=[1, 1])
    await limiter.check_and_increment(_caller())


@pytest.mark.asyncio
async def test_minute_limit_returns_429():
    limiter = _limiter()
    limiter._increment = AsyncMock(return_value=3)
    with pytest.raises(HTTPException) as exc_info:
        await limiter.check_and_increment(_caller())
    assert exc_info.value.status_code == 429
    assert exc_info.value.headers["Retry-After"] == "60"


@pytest.mark.asyncio
async def test_dependency_failure_is_closed_by_default():
    limiter = _limiter()
    limiter._increment = AsyncMock(side_effect=RuntimeError("DynamoDB unavailable"))
    with pytest.raises(HTTPException) as exc_info:
        await limiter.check_and_increment(_caller())
    assert exc_info.value.status_code == 503


@pytest.mark.asyncio
async def test_dependency_failure_can_be_configured_fail_open():
    limiter = _limiter(fail_open=True)
    limiter._increment = AsyncMock(side_effect=RuntimeError("DynamoDB unavailable"))
    await limiter.check_and_increment(_caller())
