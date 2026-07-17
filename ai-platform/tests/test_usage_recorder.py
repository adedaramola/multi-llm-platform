"""
Unit tests for accounting/usage_recorder.py using a fake DynamoDB table.
"""
from __future__ import annotations

import asyncio
import datetime

from ai_platform.accounting.usage_recorder import UsageRecorder


class FakeTable:
    def __init__(self, query_pages: list[list[dict]] | None = None, fail: bool = False) -> None:
        self.updates: list[dict] = []
        self.queries: list[dict] = []
        self._pages = list(query_pages or [])
        self._fail = fail

    def update_item(self, **kwargs) -> None:
        if self._fail:
            raise RuntimeError("dynamo down")
        self.updates.append(kwargs)

    def query(self, **kwargs) -> dict:
        self.queries.append(kwargs)
        page = self._pages.pop(0) if self._pages else []
        response: dict = {"Items": page}
        if self._pages:
            response["LastEvaluatedKey"] = {"caller_id": "x", "usage_date": "y"}
        return response


def test_record_writes_atomic_increments():
    table = FakeTable()
    recorder = UsageRecorder(table=table)

    asyncio.run(
        recorder.record(
            "caller-1",
            input_tokens=1000,
            output_tokens=500,
            estimated_cost_usd=0.0105,
        )
    )

    assert len(table.updates) == 1
    update = table.updates[0]
    assert update["Key"]["caller_id"] == "caller-1"
    today = datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%d")
    assert update["Key"]["usage_date"] == today
    values = update["ExpressionAttributeValues"]
    assert values[":one"] == 1
    assert values[":ch"] == 0
    assert values[":inp"] == 1000
    assert values[":out"] == 500
    assert values[":cost"] == 10500  # 0.0105 USD in integer micro-USD


def test_record_cache_hit_increments_cache_counter():
    table = FakeTable()
    recorder = UsageRecorder(table=table)

    asyncio.run(recorder.record("caller-1", cache_hit=True))

    values = table.updates[0]["ExpressionAttributeValues"]
    assert values[":ch"] == 1
    assert values[":inp"] == 0
    assert values[":cost"] == 0


def test_record_never_raises_on_store_failure():
    recorder = UsageRecorder(table=FakeTable(fail=True))
    asyncio.run(recorder.record("caller-1", input_tokens=5))  # must not raise


def test_month_summary_aggregates_across_pages():
    table = FakeTable(
        query_pages=[
            [
                {
                    "request_count": 10,
                    "cache_hits": 2,
                    "input_tokens": 1000,
                    "output_tokens": 400,
                    "cost_microusd": 7000,
                }
            ],
            [
                {
                    "request_count": 5,
                    "cache_hits": 1,
                    "input_tokens": 500,
                    "output_tokens": 200,
                    "cost_microusd": 3500,
                }
            ],
        ]
    )
    recorder = UsageRecorder(table=table)

    summary = asyncio.run(recorder.month_summary("caller-1"))

    assert len(table.queries) == 2  # paginated
    assert summary["month"] == datetime.datetime.now(datetime.UTC).strftime("%Y-%m")
    assert summary["request_count"] == 15
    assert summary["cache_hits"] == 3
    assert summary["input_tokens"] == 1500
    assert summary["output_tokens"] == 600
    assert summary["estimated_cost_usd"] == 0.0105


def test_month_summary_empty_when_no_usage():
    recorder = UsageRecorder(table=FakeTable())
    summary = asyncio.run(recorder.month_summary("caller-1"))
    assert summary["request_count"] == 0
    assert summary["estimated_cost_usd"] == 0.0
