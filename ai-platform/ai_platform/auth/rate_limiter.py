"""
Sliding window rate limiter using DynamoDB atomic counters.
Two windows: per-minute and per-day.
Uses DynamoDB TTL to auto-expire counters — no cleanup Lambda needed.
"""
from __future__ import annotations

import asyncio
import logging
import time

import boto3
from botocore.exceptions import ClientError
from fastapi import HTTPException

from ..auth.authenticator import CallerIdentity
from ..config.settings import get_settings

logger = logging.getLogger(__name__)


class RateLimiter:
    """
    DynamoDB table schema (ai-platform-rate-limits):
      PK: counter_key  (e.g. "rpm:caller_123:1709123456" — minute bucket)
      Attrs: count (Number), ttl (Number — Unix epoch, used by DynamoDB TTL)
    """

    def __init__(self) -> None:
        settings = get_settings()
        self._table = boto3.resource("dynamodb", region_name=settings.aws_region).Table(
            settings.rate_limit_table
        )

    async def check_and_increment(self, caller: CallerIdentity) -> None:
        now = int(time.time())
        minute_bucket = now // 60
        day_bucket = now // 86400

        rpm_key = f"rpm:{caller.caller_id}:{minute_bucket}"
        rpd_key = f"rpd:{caller.caller_id}:{day_bucket}"

        try:
            # Per-minute check
            rpm_count = await self._increment(rpm_key, ttl=now + 120)  # expire after 2 min
            if rpm_count > caller.rpm_limit:
                logger.warning(
                    "rate_limit_exceeded",
                    extra={"caller_id": caller.caller_id, "window": "rpm", "count": rpm_count},
                )
                raise HTTPException(
                    status_code=429,
                    detail=f"Rate limit exceeded: {caller.rpm_limit} requests/minute",
                    headers={"Retry-After": "60"},
                )

            # Per-day check
            rpd_count = await self._increment(rpd_key, ttl=now + 90000)  # expire after ~25h
            if rpd_count > caller.rpd_limit:
                logger.warning(
                    "rate_limit_exceeded",
                    extra={"caller_id": caller.caller_id, "window": "rpd", "count": rpd_count},
                )
                raise HTTPException(
                    status_code=429,
                    detail=f"Daily rate limit exceeded: {caller.rpd_limit} requests/day",
                    headers={"Retry-After": "86400"},
                )

        except HTTPException:
            raise
        except Exception as exc:
            # On any AWS error (incl. no credentials locally), allow the request
            logger.error("rate_limiter_error", extra={"error": str(exc)})

    def _increment_sync(self, key: str, ttl: int) -> int:
        """Sync DynamoDB call — run via executor to avoid blocking the event loop."""
        response = self._table.update_item(
            Key={"counter_key": key},
            UpdateExpression="ADD #c :one SET #ttl = if_not_exists(#ttl, :ttl)",
            ExpressionAttributeNames={"#c": "count", "#ttl": "ttl"},
            ExpressionAttributeValues={":one": 1, ":ttl": ttl},
            ReturnValues="UPDATED_NEW",
        )
        return int(response["Attributes"]["count"])

    async def _increment(self, key: str, ttl: int) -> int:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self._increment_sync, key, ttl)
