"""
Unit tests for router/router.py — mocks providers and health registry.
"""
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ai_platform.models.schemas import BudgetHint, InferenceRequest, RequestMetadata
from ai_platform.providers.base import ProviderConfig, ProviderResponse
from ai_platform.router.router import LLMRouter


def _make_provider(name: str, tier: str, healthy: bool = True) -> MagicMock:
    cfg = ProviderConfig(
        name=name,
        model_id=f"test/{name}",
        tier=tier,
        cost_per_input_token=0.001 / 1_000_000,
        cost_per_output_token=0.002 / 1_000_000,
        max_tokens_limit=4096,
    )
    provider = MagicMock()
    provider.name = name
    provider.tier = tier
    provider.config = cfg
    provider.complete = AsyncMock(return_value=ProviderResponse(
        content="test response",
        input_tokens=10,
        output_tokens=5,
        model_id=f"test/{name}",
        provider_name=name,
    ))
    return provider


def _req(content: str = "hello", budget: BudgetHint = BudgetHint.STANDARD) -> InferenceRequest:
    return InferenceRequest(
        messages=[{"role": "user", "content": content}],
        metadata=RequestMetadata(budget=budget),
    )


@pytest.fixture
def providers():
    return {
        "low":  [_make_provider("cheap-model", "low")],
        "mid":  [_make_provider("mid-model", "mid")],
        "high": [_make_provider("expensive-model", "high")],
    }


@pytest.fixture
def router(providers):
    return LLMRouter(providers)


# ── Routing behaviour ──────────────────────────────────────────────────────────

class TestLLMRouter:
    def test_low_budget_routes_to_low_tier(self, router, providers):
        with patch("ai_platform.router.router.get_health_registry") as mock_reg:
            mock_reg.return_value.is_healthy.return_value = True
            result = asyncio.run(router.route(_req(budget=BudgetHint.LOW)))
        providers["low"][0].complete.assert_called_once()
        assert result.provider_name == "cheap-model"

    def test_simple_prompt_routes_to_low_tier(self, router, providers):
        with patch("ai_platform.router.router.get_health_registry") as mock_reg:
            mock_reg.return_value.is_healthy.return_value = True
            result = asyncio.run(router.route(_req("hi")))
        assert result.provider_name == "cheap-model"

    def test_falls_back_when_provider_unhealthy(self, providers):
        with patch("ai_platform.router.router.get_health_registry") as mock_reg:
            # low tier is unhealthy, mid is healthy
            def is_healthy(name):
                return name != "cheap-model"
            mock_reg.return_value.is_healthy.side_effect = is_healthy

            router = LLMRouter(providers)
            result = asyncio.run(router.route(_req(budget=BudgetHint.LOW)))

        # Should fall back up the chain to mid
        assert result.provider_name == "mid-model"

    def test_raises_when_all_providers_fail(self, providers):
        # Make all providers raise exceptions
        for tier_list in providers.values():
            for p in tier_list:
                p.complete = AsyncMock(side_effect=Exception("provider down"))

        with patch("ai_platform.router.router.get_health_registry") as mock_reg:
            mock_reg.return_value.is_healthy.return_value = True
            router = LLMRouter(providers)
            with pytest.raises(RuntimeError, match="All providers exhausted"):
                asyncio.run(router.route(_req()))

    def test_on_provider_selected_callback_fires(self, router, providers):
        selected = []

        def capture(name, tier):
            selected.append((name, tier))

        with patch("ai_platform.router.router.get_health_registry") as mock_reg:
            mock_reg.return_value.is_healthy.return_value = True
            asyncio.run(router.route(_req(budget=BudgetHint.LOW), on_provider_selected=capture))

        assert len(selected) == 1
        assert selected[0][0] == "cheap-model"
        assert selected[0][1] == "low"
