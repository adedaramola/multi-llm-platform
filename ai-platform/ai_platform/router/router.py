"""
Cost-aware LLM router.
Selects the cheapest healthy provider in the appropriate tier,
with automatic fallback to the next tier on failure.
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Callable

from ..models.schemas import InferenceRequest
from ..providers.base import BaseProvider, ProviderResponse
from .health import get_health_registry
from .policies import estimate_complexity, select_tier

logger = logging.getLogger(__name__)


class StreamInterruptedError(RuntimeError):
    """
    A provider stream failed after chunks were already sent to the client.
    Failover is no longer possible — restarting on another provider would
    duplicate the partial output the client has already received.
    """


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
        Return the healthy provider matching model_preference.
        Exact case-insensitive name/model_id matches take priority over
        substring matches, so "openai-gpt4o" cannot resolve to
        "openai-gpt4o-mini" just because it's a prefix of that name.
        """
        registry = get_health_registry()
        pref = model_preference.lower()
        substring_match: BaseProvider | None = None
        for providers in self._tiers.values():
            for p in providers:
                if not registry.is_healthy(p.name):
                    continue
                name, model_id = p.name.lower(), p.config.model_id.lower()
                if pref == name or pref == model_id:
                    return p
                if substring_match is None and (pref in name or pref in model_id):
                    substring_match = p
        return substring_match

    async def _stream_with_timeout(
        self,
        stream: AsyncIterator[str],
        timeout: float,
    ) -> AsyncIterator[str]:
        """
        Enforce a total timeout across the full provider stream so a hung stream
        doesn't hold the request open forever.
        """
        iterator = stream.__aiter__()
        deadline = asyncio.get_running_loop().time() + timeout

        while True:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                raise TimeoutError("provider stream timed out")

            try:
                chunk = await asyncio.wait_for(iterator.__anext__(), timeout=remaining)
            except StopAsyncIteration:
                return

            yield chunk

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
                except TimeoutError:
                    logger.warning("preferred_provider_timeout", extra={"provider": preferred.name})
                    registry.mark_failure(preferred.name)
                    last_error = TimeoutError(f"{preferred.name} timed out")
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
                except TimeoutError:
                    logger.warning("provider_timeout", extra={"provider": provider.name})
                    registry.mark_failure(provider.name)
                    last_error = TimeoutError(f"{provider.name} timed out")
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
        on_usage: Callable[[int, int], None] | None = None,
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
                yielded_any = False
                try:
                    async for chunk in self._stream_with_timeout(
                        preferred.stream(
                            messages=messages,
                            max_tokens=request.max_tokens,
                            temperature=request.temperature,
                            on_usage=on_usage,
                        ),
                        timeout,
                    ):
                        yielded_any = True
                        yield chunk
                    registry.mark_success(preferred.name)
                    return
                except TimeoutError as exc:
                    logger.warning(
                        "preferred_provider_stream_timeout",
                        extra={"provider": preferred.name, "mid_stream": yielded_any},
                    )
                    registry.mark_failure(preferred.name)
                    if yielded_any:
                        raise StreamInterruptedError(
                            f"{preferred.name} timed out mid-stream"
                        ) from exc
                except Exception as exc:
                    logger.warning(
                        "preferred_provider_stream_error",
                        extra={"provider": preferred.name, "error": str(exc), "mid_stream": yielded_any},
                    )
                    registry.mark_failure(preferred.name)
                    if yielded_any:
                        raise StreamInterruptedError(
                            f"{preferred.name} failed mid-stream: {exc}"
                        ) from exc

        # Tier-based selection
        tier_order = self._build_fallback_chain(target_tier)
        for tier in tier_order:
            candidates = self._get_candidates(tier)
            for provider in candidates:
                if on_provider_selected:
                    on_provider_selected(provider.name, tier)
                yielded_any = False
                try:
                    async for chunk in self._stream_with_timeout(
                        provider.stream(
                            messages=messages,
                            max_tokens=request.max_tokens,
                            temperature=request.temperature,
                            on_usage=on_usage,
                        ),
                        timeout,
                    ):
                        yielded_any = True
                        yield chunk
                    registry.mark_success(provider.name)
                    return
                except TimeoutError as exc:
                    logger.warning(
                        "provider_stream_timeout",
                        extra={"provider": provider.name, "mid_stream": yielded_any},
                    )
                    registry.mark_failure(provider.name)
                    if yielded_any:
                        raise StreamInterruptedError(
                            f"{provider.name} timed out mid-stream"
                        ) from exc
                except Exception as exc:
                    logger.warning(
                        "provider_stream_error",
                        extra={"provider": provider.name, "error": str(exc), "mid_stream": yielded_any},
                    )
                    registry.mark_failure(provider.name)
                    if yielded_any:
                        raise StreamInterruptedError(
                            f"{provider.name} failed mid-stream: {exc}"
                        ) from exc

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
