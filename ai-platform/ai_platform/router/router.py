"""
Cost-aware LLM router.
Selects the cheapest healthy provider in the appropriate tier,
with automatic fallback to the next tier on failure.
"""
from __future__ import annotations

import asyncio
import logging
from typing import AsyncIterator, Callable

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

    def _find_preferred_provider(self, model_preference: str) -> BaseProvider | None:
        """
        Return the first healthy provider whose name or model_id matches
        model_preference (case-insensitive substring match).
        """
        registry = get_health_registry()
        pref = model_preference.lower()
        for providers in self._tiers.values():
            for p in providers:
                if (pref in p.name.lower() or pref in p.config.model_id.lower()) and registry.is_healthy(p.name):
                    return p
        return None

    async def route(
        self,
        request: InferenceRequest,
        on_provider_selected: Callable[[str, str], None] | None = None,
    ) -> ProviderResponse:
        """
        Main routing entry point.
        If model_preference is set, pins to that provider first (falls back to
        normal tier routing if the preferred provider is unavailable).
        Otherwise tries providers in complexity-based tier order.
        """
        complexity = estimate_complexity(request)
        target_tier = select_tier(complexity, request.metadata.budget)

        logger.info(
            "routing_decision",
            extra={
                "complexity": complexity,
                "target_tier": target_tier,
                "budget": request.metadata.budget,
                "model_preference": request.model_preference,
                "message_count": len(request.messages),
            },
        )

        messages = [m.model_dump() for m in request.messages]
        registry = get_health_registry()
        timeout = float(request.metadata.latency_sla_ms / 1000)
        last_error: Exception | None = None

        # ── Preferred provider pin ────────────────────────────────────────────
        if request.model_preference:
            preferred = self._find_preferred_provider(request.model_preference)
            if preferred:
                if on_provider_selected:
                    on_provider_selected(preferred.name, preferred.tier)
                try:
                    response = await asyncio.wait_for(
                        preferred.complete(
                            messages=messages,
                            max_tokens=request.max_tokens,
                            temperature=request.temperature,
                        ),
                        timeout=timeout,
                    )
                    registry.mark_success(preferred.name)
                    return response
                except asyncio.TimeoutError:
                    logger.warning("preferred_provider_timeout", extra={"provider": preferred.name})
                    registry.mark_failure(preferred.name)
                    last_error = asyncio.TimeoutError(f"{preferred.name} timed out")
                except Exception as exc:
                    logger.warning(
                        "preferred_provider_error",
                        extra={"provider": preferred.name, "error": str(exc)},
                    )
                    registry.mark_failure(preferred.name)
                    last_error = exc
            else:
                logger.warning(
                    "preferred_provider_not_found",
                    extra={"model_preference": request.model_preference},
                )

        # ── Tier-based fallback chain ─────────────────────────────────────────
        tier_order = self._build_fallback_chain(target_tier)
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
                        timeout=timeout,
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

    async def route_stream(
        self,
        request: InferenceRequest,
        on_provider_selected: Callable[[str, str], None] | None = None,
    ) -> AsyncIterator[str]:
        """
        Streaming variant of route().
        Selects the best available provider and delegates to its stream() method.
        Falls back to non-streaming complete() for providers that don't support it.
        """
        complexity = estimate_complexity(request)
        target_tier = select_tier(complexity, request.metadata.budget)
        messages = [m.model_dump() for m in request.messages]
        registry = get_health_registry()
        timeout = float(request.metadata.latency_sla_ms / 1000)

        # Preferred provider pin
        if request.model_preference:
            preferred = self._find_preferred_provider(request.model_preference)
            if preferred:
                if on_provider_selected:
                    on_provider_selected(preferred.name, preferred.tier)
                try:
                    async for chunk in preferred.stream(
                        messages=messages,
                        max_tokens=request.max_tokens,
                        temperature=request.temperature,
                    ):
                        yield chunk
                    registry.mark_success(preferred.name)
                    return
                except Exception as exc:
                    logger.warning(
                        "preferred_provider_stream_error",
                        extra={"provider": preferred.name, "error": str(exc)},
                    )
                    registry.mark_failure(preferred.name)

        # Tier-based selection
        tier_order = self._build_fallback_chain(target_tier)
        for tier in tier_order:
            candidates = self._get_candidates(tier)
            for provider in candidates:
                if on_provider_selected:
                    on_provider_selected(provider.name, tier)
                try:
                    async for chunk in provider.stream(
                        messages=messages,
                        max_tokens=request.max_tokens,
                        temperature=request.temperature,
                    ):
                        yield chunk
                    registry.mark_success(provider.name)
                    return
                except Exception as exc:
                    logger.warning(
                        "provider_stream_error",
                        extra={"provider": provider.name, "error": str(exc)},
                    )
                    registry.mark_failure(provider.name)

        raise RuntimeError("All providers exhausted for streaming request.")

    def _build_fallback_chain(self, target_tier: str) -> list[str]:
        """
        Start at target tier. If it fails, try tiers in this order:
          low → low, mid, high
          mid → mid, low, high
          high → high, mid, low
        """
        others = [t for t in self._fallback_order if t != target_tier]
        return [target_tier] + others
