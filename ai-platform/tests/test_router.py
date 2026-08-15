"""
Unit tests for router/router.py — mocks providers and health registry.
"""
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from ai_platform.models.schemas import BudgetHint, InferenceRequest, RequestMetadata
from ai_platform.providers.base import ProviderConfig, ProviderResponse
from ai_platform.router.router import LLMRouter, StreamInterruptedError


async def _stream_from_chunks(*chunks: str, delay: float = 0.0):
    for chunk in chunks:
        if delay:
            await asyncio.sleep(delay)
        yield chunk


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
    provider.cost_per_token = (
        cfg.cost_per_input_token + cfg.cost_per_output_token
    ) / 2
    provider.complete = AsyncMock(return_value=ProviderResponse(
        content="test response",
        input_tokens=10,
        output_tokens=5,
        model_id=f"test/{name}",
        provider_name=name,
    ))
    provider.stream = MagicMock(side_effect=lambda **kwargs: _stream_from_chunks("test response"))
    return provider


def _req(
    content: str = "hello",
    budget: BudgetHint = BudgetHint.STANDARD,
    latency_sla_ms: int = 5000,
) -> InferenceRequest:
    return InferenceRequest(
        messages=[{"role": "user", "content": content}],
        metadata=RequestMetadata(budget=budget, latency_sla_ms=latency_sla_ms),
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

    def test_stream_timeout_falls_back_to_next_provider(self, providers):
        async def slow_stream(**kwargs):
            await asyncio.sleep(0.6)
            yield "too slow"

        async def fast_stream(**kwargs):
            yield "mid response"

        providers["low"][0].stream = MagicMock(side_effect=slow_stream)
        providers["mid"][0].stream = MagicMock(side_effect=fast_stream)

        with patch("ai_platform.router.router.get_health_registry") as mock_reg:
            mock_reg.return_value.is_healthy.return_value = True
            router = LLMRouter(providers)
            chunks = asyncio.run(_collect_stream(router, _req(latency_sla_ms=500)))

        assert chunks == ["mid response"]
        providers["low"][0].stream.assert_called_once()
        providers["mid"][0].stream.assert_called_once()

    def test_stream_error_before_first_chunk_falls_back(self, providers):
        async def broken_stream(**kwargs):
            raise ConnectionError("refused")
            yield  # pragma: no cover — makes this an async generator

        async def good_stream(**kwargs):
            yield "mid response"

        providers["low"][0].stream = MagicMock(side_effect=broken_stream)
        providers["mid"][0].stream = MagicMock(side_effect=good_stream)

        with patch("ai_platform.router.router.get_health_registry") as mock_reg:
            mock_reg.return_value.is_healthy.return_value = True
            router = LLMRouter(providers)
            chunks = asyncio.run(_collect_stream(router, _req(budget=BudgetHint.LOW)))

        assert chunks == ["mid response"]

    def test_stream_error_mid_stream_raises_without_fallback(self, providers):
        async def dying_stream(**kwargs):
            yield "partial-1"
            yield "partial-2"
            raise ConnectionError("connection dropped")

        providers["low"][0].stream = MagicMock(side_effect=dying_stream)

        with patch("ai_platform.router.router.get_health_registry") as mock_reg:
            mock_reg.return_value.is_healthy.return_value = True
            router = LLMRouter(providers)
            chunks, interrupted = asyncio.run(
                _collect_stream_until_error(router, _req(budget=BudgetHint.LOW))
            )

        # Chunks already sent are preserved; no second provider is tried —
        # restarting would duplicate the partial output at the client.
        assert chunks == ["partial-1", "partial-2"]
        assert interrupted is True
        providers["mid"][0].stream.assert_not_called()
        providers["high"][0].stream.assert_not_called()

    def test_stream_timeout_mid_stream_raises_without_fallback(self, providers):
        async def stalling_stream(**kwargs):
            yield "partial-1"
            await asyncio.sleep(5)
            yield "never delivered"

        providers["low"][0].stream = MagicMock(side_effect=stalling_stream)

        with patch("ai_platform.router.router.get_health_registry") as mock_reg:
            mock_reg.return_value.is_healthy.return_value = True
            router = LLMRouter(providers)
            chunks, interrupted = asyncio.run(
                _collect_stream_until_error(
                    router, _req(budget=BudgetHint.LOW, latency_sla_ms=500)
                )
            )

        assert chunks == ["partial-1"]
        assert interrupted is True
        providers["mid"][0].stream.assert_not_called()


# ── model_preference resolution ─────────────────────────────────────────────

class TestFindPreferredProvider:
    """openai-gpt4o-mini is a prefix-superstring of openai-gpt4o — an exact
    match for "openai-gpt4o" must win even though the mini variant is
    iterated first and also substring-matches."""

    @pytest.fixture
    def providers(self):
        return {
            "mid": [_make_provider("openai-gpt4o-mini", "mid")],
            "high": [_make_provider("openai-gpt4o", "high")],
        }

    def test_exact_name_match_wins_over_substring_match(self, providers):
        with patch("ai_platform.router.router.get_health_registry") as mock_reg:
            mock_reg.return_value.is_healthy.return_value = True
            router = LLMRouter(providers)
            result = router._find_preferred_provider("openai-gpt4o")
        assert result.name == "openai-gpt4o"

    def test_falls_back_to_substring_match_when_no_exact_match(self, providers):
        with patch("ai_platform.router.router.get_health_registry") as mock_reg:
            mock_reg.return_value.is_healthy.return_value = True
            router = LLMRouter(providers)
            result = router._find_preferred_provider("gpt4o-mini")
        assert result.name == "openai-gpt4o-mini"

    def test_returns_none_when_no_match(self, providers):
        with patch("ai_platform.router.router.get_health_registry") as mock_reg:
            mock_reg.return_value.is_healthy.return_value = True
            router = LLMRouter(providers)
            assert router._find_preferred_provider("nonexistent-model") is None

    def test_skips_unhealthy_exact_match_for_healthy_substring_match(self, providers):
        with patch("ai_platform.router.router.get_health_registry") as mock_reg:
            mock_reg.return_value.is_healthy.side_effect = lambda name: name != "openai-gpt4o"
            router = LLMRouter(providers)
            result = router._find_preferred_provider("openai-gpt4o")
        assert result.name == "openai-gpt4o-mini"


async def _collect_stream(router: LLMRouter, request: InferenceRequest) -> list[str]:
    return [chunk async for chunk in router.route_stream(request)]


async def _collect_stream_until_error(
    router: LLMRouter, request: InferenceRequest
) -> tuple[list[str], bool]:
    """Collect chunks, returning (chunks, was_interrupted_mid_stream)."""
    chunks: list[str] = []
    try:
        async for chunk in router.route_stream(request):
            chunks.append(chunk)
    except StreamInterruptedError:
        return chunks, True
    return chunks, False
