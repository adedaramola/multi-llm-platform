"""
Provider health registry backed by DynamoDB.
Health status is updated by a background Lambda (scheduled every 2 minutes).
The gateway reads health flags; it does NOT perform health checks inline.
"""
from __future__ import annotations

import time

import boto3
from botocore.exceptions import ClientError

from ..config.settings import get_settings


class ProviderHealthRegistry:
    """
    Reads provider health flags from DynamoDB.
    Format: { "provider_name": "healthy" | "degraded" | "unhealthy" }

    DynamoDB table: ai-platform-provider-health
      PK: provider_name (String)
      SK: (none)
      Attrs: status (String), updated_at (Number), consecutive_failures (Number)
    """

    def __init__(self, table=None, settings=None, time_fn=None) -> None:
        settings = settings or get_settings()
        self._settings = settings
        self._table = table or boto3.resource(
            "dynamodb", region_name=settings.aws_region
        ).Table(settings.health_table)
        self._time_fn = time_fn or time.time
        self._cache: dict[str, str] = {}
        self._consecutive_failures: dict[str, int] = {}
        self._local_state: dict[str, str] = {}
        self._open_until: dict[str, float] = {}

    def refresh(self) -> None:
        """Pull all provider statuses in a single scan (small table, cheap)."""
        try:
            response = self._table.scan(ProjectionExpression="provider_name, #s",
                                        ExpressionAttributeNames={"#s": "status"})
            self._cache = {
                item["provider_name"]: item["status"]
                for item in response.get("Items", [])
            }
        except Exception:
            # On any error (incl. NoCredentialsError locally), assume all healthy
            self._cache = {}

    def is_healthy(self, provider_name: str) -> bool:
        state = self._local_state.get(provider_name)
        if state == "open":
            if self._open_until.get(provider_name, 0) > self._time_fn():
                return False
            self._local_state[provider_name] = "half_open"
            return True
        if state == "half_open":
            return True

        status = self._cache.get(provider_name, "healthy")
        return status in ("healthy", "degraded")

    def mark_failure(self, provider_name: str) -> None:
        """Increment failure counter. Called by gateway on provider exception."""
        failures = self._consecutive_failures.get(provider_name, 0) + 1
        self._consecutive_failures[provider_name] = failures

        opened = failures >= self._settings.circuit_breaker_failure_threshold
        status = "unhealthy" if opened else "degraded"
        if opened:
            self._local_state[provider_name] = "open"
            self._open_until[provider_name] = (
                self._time_fn() + self._settings.circuit_breaker_cooldown_seconds
            )
        self._cache[provider_name] = status

        try:
            self._table.update_item(
                Key={"provider_name": provider_name},
                UpdateExpression=(
                    "ADD consecutive_failures :one "
                    "SET #s = :status, updated_at = :ts"
                ),
                ExpressionAttributeNames={"#s": "status"},
                ExpressionAttributeValues={
                    ":one": 1,
                    ":status": status,
                    ":ts": int(self._time_fn()),
                },
            )
        except Exception:
            pass  # non-critical path

    def mark_success(self, provider_name: str) -> None:
        """Reset failure counter on successful call."""
        self._consecutive_failures.pop(provider_name, None)
        self._local_state.pop(provider_name, None)
        self._open_until.pop(provider_name, None)
        self._cache[provider_name] = "healthy"

        try:
            self._table.put_item(
                Item={
                    "provider_name": provider_name,
                    "status": "healthy",
                    "consecutive_failures": 0,
                    "updated_at": int(self._time_fn()),
                }
            )
        except ClientError:
            pass


_registry: ProviderHealthRegistry | None = None


def get_health_registry() -> ProviderHealthRegistry:
    global _registry
    if _registry is None:
        _registry = ProviderHealthRegistry()
        _registry.refresh()
    return _registry
