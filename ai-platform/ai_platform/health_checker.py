"""
Provider health checker — invoked by EventBridge every 5 minutes.
Runs health_check() on one representative provider per family,
then writes results to DynamoDB so the gateway can route around failures.
"""
from __future__ import annotations

import asyncio
import logging
import time

from .config.settings import get_settings
from .providers.anthropic_provider import AnthropicProvider, haiku_config
from .providers.base import BaseProvider
from .providers.bedrock_provider import BedrockProvider, nova_micro_config
from .providers.openai_provider import OpenAIProvider, gpt4o_mini_config
from .router.health import ProviderHealthRegistry
from .utils import fetch_secret

logging.basicConfig(
    level=logging.INFO,
    format='{"time":"%(asctime)s","level":"%(levelname)s","message":%(message)s}',
)
logger = logging.getLogger(__name__)

# Consecutive failures before marking a provider unhealthy
UNHEALTHY_THRESHOLD = 3


async def _check_and_record(
    provider,
    registry: ProviderHealthRegistry,
) -> dict:
    start = time.perf_counter()
    try:
        healthy = await asyncio.wait_for(provider.health_check(), timeout=20.0)
    except TimeoutError:
        healthy = False
    latency_ms = int((time.perf_counter() - start) * 1000)
    status = "healthy"

    if healthy:
        # mark_success writes status=healthy and resets counter
        registry.mark_success(provider.name)
        logger.info("provider_healthy: provider=%s latency_ms=%d", provider.name, latency_ms)
    else:
        status = registry.record_probe_failure(provider.name, UNHEALTHY_THRESHOLD)
        logger.warning(
            "provider_%s: provider=%s latency_ms=%d",
            status,
            provider.name,
            latency_ms,
        )

    return {
        "provider": provider.name,
        "healthy": healthy,
        "latency_ms": latency_ms,
        "status": status,
    }


async def _run_checks() -> list[dict]:
    settings = get_settings()

    # Resolve API keys
    anthropic_key = settings.anthropic_api_key
    if not anthropic_key and settings.anthropic_secret_arn:
        try:
            anthropic_key = fetch_secret(settings.anthropic_secret_arn)
        except Exception as exc:
            logger.error("anthropic_secret_fetch_failed", extra={"error": str(exc)})

    openai_key = settings.openai_api_key
    if not openai_key and settings.openai_secret_arn:
        try:
            openai_key = fetch_secret(settings.openai_secret_arn)
        except Exception as exc:
            logger.error("openai_secret_fetch_failed", extra={"error": str(exc)})

    # One representative per provider family — cheapest model sufficient for liveness check
    providers: list[BaseProvider] = [BedrockProvider(nova_micro_config())]
    if anthropic_key:
        providers.append(AnthropicProvider(haiku_config(), anthropic_key))
    if openai_key:
        providers.append(OpenAIProvider(gpt4o_mini_config(), openai_key))

    registry = ProviderHealthRegistry()

    try:
        tasks = [_check_and_record(p, registry) for p in providers]
        results = await asyncio.gather(*tasks, return_exceptions=True)
    finally:
        close_results = await asyncio.gather(
            *(provider.close() for provider in providers),
            return_exceptions=True,
        )
        for provider, result in zip(providers, close_results, strict=True):
            if isinstance(result, BaseException):
                logger.warning(
                    "provider_close_failed",
                    extra={"provider": provider.name, "error": str(result)},
                )

    # Flatten any unexpected exceptions into failed results
    clean: list[dict] = []
    for r in results:
        if isinstance(r, BaseException):
            logger.error("check_exception", extra={"error": str(r)})
        else:
            clean.append(r)
    return clean


def handler(event, context):
    """EventBridge Lambda handler — synchronous entry point."""
    logger.info("health_check_start")
    results = asyncio.run(_run_checks())
    healthy_count = sum(1 for r in results if r.get("healthy"))
    logger.info(
        "health_check_complete",
        extra={"healthy": healthy_count, "total": len(results)},
    )
    return {"statusCode": 200, "results": results}
