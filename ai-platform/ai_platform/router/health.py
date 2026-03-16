"""
Provider health registry backed by DynamoDB.
Health status is updated by a background Lambda (scheduled every 2 minutes).
The gateway reads health flags; it does NOT perform health checks inline.
"""
from __future__ import annotations

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

    def __init__(self) -> None:
        settings = get_settings()
        self._table = boto3.resource("dynamodb", region_name=settings.aws_region).Table(
            settings.health_table
        )
        self._cache: dict[str, str] = {}

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
        status = self._cache.get(provider_name, "healthy")
        return status in ("healthy", "degraded")

    def mark_failure(self, provider_name: str) -> None:
        """Increment failure counter. Called by gateway on provider exception."""
        try:
            self._table.update_item(
                Key={"provider_name": provider_name},
                UpdateExpression=(
                    "ADD consecutive_failures :one "
                    "SET #s = if_not_exists(#s, :healthy), updated_at = :ts"
                ),
                ExpressionAttributeNames={"#s": "status"},
                ExpressionAttributeValues={
                    ":one": 1,
                    ":healthy": "healthy",
                    ":ts": int(__import__("time").time()),
                },
            )
        except Exception:
            pass  # non-critical path

    def mark_success(self, provider_name: str) -> None:
        """Reset failure counter on successful call."""
        try:
            self._table.put_item(Item={
                "provider_name": provider_name,
                "status": "healthy",
                "consecutive_failures": 0,
                "updated_at": int(__import__("time").time()),
            })
        except ClientError:
            pass


_registry: ProviderHealthRegistry | None = None


def get_health_registry() -> ProviderHealthRegistry:
    global _registry
    if _registry is None:
        _registry = ProviderHealthRegistry()
        _registry.refresh()
    return _registry
