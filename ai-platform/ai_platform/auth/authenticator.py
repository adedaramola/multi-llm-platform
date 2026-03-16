"""
API key authentication.
Keys are stored in DynamoDB with per-key rate limit config and caller metadata.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
from dataclasses import dataclass

import boto3
from botocore.exceptions import ClientError
from fastapi import HTTPException, Request

from ..config.settings import get_settings

logger = logging.getLogger(__name__)


@dataclass
class CallerIdentity:
    caller_id: str
    app_name: str
    rpm_limit: int
    rpd_limit: int
    active: bool


class Authenticator:
    """
    DynamoDB table schema (ai-platform-api-keys):
      PK: key_hash (SHA256 of the raw API key)
      Attrs: caller_id, app_name, rpm_limit, rpd_limit, active, created_at
    """

    def __init__(self) -> None:
        settings = get_settings()
        self._table = boto3.resource("dynamodb", region_name=settings.aws_region).Table(
            settings.api_keys_table
        )
        self._settings = settings

    def _hash_key(self, raw_key: str) -> str:
        return hashlib.sha256(raw_key.encode()).hexdigest()

    async def authenticate(self, raw_key: str) -> CallerIdentity:
        # Dev bypass — no DynamoDB required locally
        if self._settings.environment == "dev":
            return CallerIdentity(
                caller_id="dev-user",
                app_name="local-dev",
                rpm_limit=1000,
                rpd_limit=100_000,
                active=True,
            )

        key_hash = self._hash_key(raw_key)
        try:
            loop = asyncio.get_event_loop()
            response = await loop.run_in_executor(
                None, lambda: self._table.get_item(Key={"key_hash": key_hash})
            )
            item = response.get("Item")
        except ClientError as exc:
            logger.error("dynamo_auth_error", extra={"error": str(exc)})
            raise HTTPException(status_code=503, detail="Auth service unavailable")

        if not item:
            raise HTTPException(status_code=401, detail="Invalid API key")

        if not item.get("active", True):
            raise HTTPException(status_code=401, detail="API key revoked")

        return CallerIdentity(
            caller_id=item["caller_id"],
            app_name=item.get("app_name", "unknown"),
            rpm_limit=int(item.get("rpm_limit", self._settings.default_rpm)),
            rpd_limit=int(item.get("rpd_limit", self._settings.default_rpd)),
            active=True,
        )


async def get_caller_identity(request: Request) -> CallerIdentity:
    """FastAPI dependency — extracts and validates the Bearer token."""
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing or malformed Authorization header")

    raw_key = auth_header.removeprefix("Bearer ").strip()
    if not raw_key:
        raise HTTPException(status_code=401, detail="Empty API key")

    authenticator: Authenticator = request.app.state.authenticator
    return await authenticator.authenticate(raw_key)
