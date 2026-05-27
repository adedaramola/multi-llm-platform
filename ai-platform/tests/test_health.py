"""
Unit tests for router/health.py — local circuit-breaker behaviour.
"""
from ai_platform.config.settings import Settings
from ai_platform.router.health import ProviderHealthRegistry


class FakeTable:
    def __init__(self) -> None:
        self.items: dict[str, dict] = {}

    def scan(self, **kwargs):
        return {
            "Items": [
                {"provider_name": name, "status": item["status"]}
                for name, item in self.items.items()
            ]
        }

    def update_item(self, Key, UpdateExpression, ExpressionAttributeNames, ExpressionAttributeValues):
        provider_name = Key["provider_name"]
        item = self.items.get(
            provider_name,
            {"provider_name": provider_name, "consecutive_failures": 0},
        )
        item["consecutive_failures"] = item.get("consecutive_failures", 0) + int(
            ExpressionAttributeValues[":one"]
        )
        item["status"] = ExpressionAttributeValues[":status"]
        item["updated_at"] = ExpressionAttributeValues[":ts"]
        self.items[provider_name] = item
        return {"Attributes": item}

    def put_item(self, Item):
        self.items[Item["provider_name"]] = Item


def test_circuit_breaker_opens_after_threshold_and_recovers_after_cooldown():
    now = [1_000.0]
    table = FakeTable()
    settings = Settings(
        health_table="test-health",
        circuit_breaker_failure_threshold=2,
        circuit_breaker_cooldown_seconds=30,
    )
    registry = ProviderHealthRegistry(
        table=table,
        settings=settings,
        time_fn=lambda: now[0],
    )

    assert registry.is_healthy("anthropic-sonnet")

    registry.mark_failure("anthropic-sonnet")
    assert registry.is_healthy("anthropic-sonnet")

    registry.mark_failure("anthropic-sonnet")
    assert not registry.is_healthy("anthropic-sonnet")

    now[0] += 31
    assert registry.is_healthy("anthropic-sonnet")


def test_mark_success_closes_half_open_circuit():
    now = [2_000.0]
    table = FakeTable()
    settings = Settings(
        health_table="test-health",
        circuit_breaker_failure_threshold=1,
        circuit_breaker_cooldown_seconds=10,
    )
    registry = ProviderHealthRegistry(
        table=table,
        settings=settings,
        time_fn=lambda: now[0],
    )

    registry.mark_failure("openai-gpt4o")
    assert not registry.is_healthy("openai-gpt4o")

    now[0] += 11
    assert registry.is_healthy("openai-gpt4o")

    registry.mark_success("openai-gpt4o")
    assert registry.is_healthy("openai-gpt4o")
