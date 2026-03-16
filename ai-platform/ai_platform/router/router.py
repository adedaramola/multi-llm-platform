"""
Cost-aware LLM router.
Selects the cheapest healthy provider in the appropriate tier,
with automatic fallback to the next tier on failure.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Callable

from ..models.schemas import InferenceRequest
from ..providers.base import BaseProvider, ProviderResponse
from .health import get_health_registry
from .policies import estimate_complexity, select_tier

logger = logging.getLogger(__name__)


class LLMRouter:
    def __init__(self, providers_by_tier: dict[str, list[BaseProvider]]) -> None:
        """
        providers_by_tier: {
            "low":  [BedrockTitanLite, ClaudeHaiku, ...],
            "mid":  [ClaudeSonnet, GPT4oMini, ...],
            "high": [ClaudeOpus, GPT4o, ...],
        }
        Each list is ordered by priority (ascending). Router picks first healthy one.
        """
        self._tiers = providers_by_tier
        self._fallback_order = ["low", "mid", "high"]

    def _get_candidates(self, tier: str) -> list[BaseProvider]:
        registry = get_health_registry()
        candidates = self._tiers.get(tier, [])
        healthy = [p for p in candidates if registry.is_healthy(p.name)]
        # Sort by cost_per_token ascending, then priority ascending
        return sorted(healthy, key=lambda p: (p.cost_per_token, p.config.priority))

    async def route(
        self,
        request: InferenceRequest,
        on_provider_selected: Callable[[str, str], None] | None = None,
    ) -> ProviderResponse:
        """
        Main routing entry point.
        Tries providers in tier order. Falls back down/up tiers if all in tier fail.
        """
        complexity = estimate_complexity(request)
        target_tier = select_tier(complexity, request.metadata.budget)

        logger.info(
            "routing_decision",
            extra={
                "complexity": complexity,
                "target_tier": target_tier,
                "budget": request.metadata.budget,
                "message_count": len(request.messages),
            },
        )

        # Build fallback chain: start at target tier, then try others
        tier_order = self._build_fallback_chain(target_tier)
        messages = [m.model_dump() for m in request.messages]
        registry = get_health_registry()
        last_error: Exception | None = None

        for tier in tier_order:
            candidates = self._get_candidates(tier)
            for provider in candidates:
                if on_provider_selected:
                    on_provider_selected(provider.name, tier)
                try:
                    response = await asyncio.wait_for(
                        provider.complete(
                            messages=messages,
                            max_tokens=request.max_tokens,
                            temperature=request.temperature,
                        ),
                        timeout=float(
                            request.metadata.latency_sla_ms / 1000
                        ),
                    )
                    registry.mark_success(provider.name)
                    return response
                except asyncio.TimeoutError:
                    logger.warning("provider_timeout", extra={"provider": provider.name})
                    registry.mark_failure(provider.name)
                    last_error = asyncio.TimeoutError(f"{provider.name} timed out")
                except Exception as exc:
                    logger.warning(
                        "provider_error",
                        extra={"provider": provider.name, "error": str(exc)},
                    )
                    registry.mark_failure(provider.name)
                    last_error = exc

        raise RuntimeError(
            f"All providers exhausted. Last error: {last_error}"
        ) from last_error

    def _build_fallback_chain(self, target_tier: str) -> list[str]:
        """
        Start at target tier. If it fails, try tiers in this order:
          low → low, mid, high
          mid → mid, low, high
          high → high, mid, low
        """
        others = [t for t in self._fallback_order if t != target_tier]
        return [target_tier] + others
