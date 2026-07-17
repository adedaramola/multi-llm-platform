"""
Per-caller usage accounting backed by DynamoDB.
One atomic UpdateItem per successful request, bucketed by caller and UTC day.
Costs are stored as integer micro-USD to avoid float drift in atomic ADDs.
"""
from __future__ import annotations

import asyncio
import datetime
import logging

import boto3
from boto3.dynamodb.conditions import Key

from ..config.settings import get_settings

logger = logging.getLogger(__name__)

MICRO_USD = 1_000_000


class UsageRecorder:
    """
    DynamoDB table schema (ai-platform-usage):
      PK: caller_id  (S)
      SK: usage_date (S, "YYYY-MM-DD" in UTC)
      Attrs: request_count, cache_hits, input_tokens, output_tokens,
             cost_microusd — all Numbers, incremented atomically via ADD.
    """

    def __init__(self, table=None) -> None:
        settings = get_settings()
        self._table = table or boto3.resource("dynamodb", region_name=settings.aws_region).Table(
            settings.usage_table
        )

    @staticmethod
    def _now() -> datetime.datetime:
        return datetime.datetime.now(datetime.UTC)

    def _record_sync(
        self,
        caller_id: str,
        input_tokens: int,
        output_tokens: int,
        cost_microusd: int,
        cache_hit: bool,
    ) -> None:
        self._table.update_item(
            Key={"caller_id": caller_id, "usage_date": self._now().strftime("%Y-%m-%d")},
            UpdateExpression=(
                "ADD request_count :one, cache_hits :ch, input_tokens :inp, "
                "output_tokens :out, cost_microusd :cost"
            ),
            ExpressionAttributeValues={
                ":one": 1,
                ":ch": 1 if cache_hit else 0,
                ":inp": input_tokens,
                ":out": output_tokens,
                ":cost": cost_microusd,
            },
        )

    async def record(
        self,
        caller_id: str,
        *,
        input_tokens: int = 0,
        output_tokens: int = 0,
        estimated_cost_usd: float = 0.0,
        cache_hit: bool = False,
    ) -> None:
        """Fire-safe: accounting must never fail the request it accounts for."""
        cost_microusd = int(round(estimated_cost_usd * MICRO_USD))
        try:
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(
                None,
                lambda: self._record_sync(
                    caller_id, input_tokens, output_tokens, cost_microusd, cache_hit
                ),
            )
        except Exception as exc:
            logger.warning("usage_record_failed", extra={"error": str(exc), "caller_id": caller_id})

    def _query_month_sync(self, caller_id: str, month_prefix: str) -> list[dict]:
        items: list[dict] = []
        kwargs: dict = {
            "KeyConditionExpression": (
                Key("caller_id").eq(caller_id) & Key("usage_date").begins_with(month_prefix)
            ),
        }
        while True:
            response = self._table.query(**kwargs)
            items.extend(response.get("Items", []))
            last_key = response.get("LastEvaluatedKey")
            if not last_key:
                return items
            kwargs["ExclusiveStartKey"] = last_key

    async def month_summary(self, caller_id: str) -> dict:
        """Aggregate the caller's current UTC month. Raises on DynamoDB errors."""
        month = self._now().strftime("%Y-%m")
        loop = asyncio.get_running_loop()
        items = await loop.run_in_executor(None, self._query_month_sync, caller_id, month)

        totals = {
            "request_count": 0,
            "cache_hits": 0,
            "input_tokens": 0,
            "output_tokens": 0,
            "cost_microusd": 0,
        }
        for item in items:
            for field in totals:
                totals[field] += int(item.get(field, 0))

        return {
            "month": month,
            "request_count": totals["request_count"],
            "cache_hits": totals["cache_hits"],
            "input_tokens": totals["input_tokens"],
            "output_tokens": totals["output_tokens"],
            "estimated_cost_usd": totals["cost_microusd"] / MICRO_USD,
        }
