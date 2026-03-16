"""
Provider health checker — invoked by EventBridge every 5 minutes.
Runs health_check() on one representative provider per family,
then writes results to DynamoDB so the gateway can route around failures.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time

import boto3

from .config.settings import get_settings
from .providers.anthropic_provider import AnthropicProvider, haiku_config
from .providers.bedrock_provider import BedrockProvider, nova_micro_config
from .providers.openai_provider import OpenAIProvider, gpt4o_mini_config
from .router.health import ProviderHealthRegistry

logging.basicConfig(
    level=logging.INFO,
    format='{"time":"%(asctime)s","level":"%(levelname)s","message":%(message)s}',
)
logger = logging.getLogger(__name__)

# Consecutive failures before marking a provider unhealthy
UNHEALTHY_THRESHOLD = 3


def _fetch_secret(arn: str) -> str:
    client = boto3.client("secretsmanager")
    resp = client.get_secret_value(SecretId=arn)
    value = resp.get("SecretString", "")
    try:
        parsed = json.loads(value)
        if isinstance(parsed, dict):
            return next(iter(parsed.values()))
    except (json.JSONDecodeError, StopIteration):
        pass
    return value


async def _check_and_record(
    provider,
    registry: ProviderHealthRegistry,
) -> dict:
    start = time.perf_counter()
    try:
        healthy = await asyncio.wait_for(provider.health_check(), timeout=20.0)
    except asyncio.TimeoutError:
        healthy = False
    latency_ms = int((time.perf_counter() - start) * 1000)

    if healthy:
        # mark_success writes status=healthy and resets counter
        registry.mark_success(provider.name)
        logger.info(f'"provider_healthy: {provider.name} latency={latency_ms}ms"', extra={})
    else:
        # Directly write unhealthy — mark_failure only increments a counter and
        # uses if_not_exists on status, so it never flips an existing "healthy" record.
        try:
            registry._table.put_item(Item={
                "provider_name": provider.name,
                "status": "unhealthy",
                "consecutive_failures": UNHEALTHY_THRESHOLD,
                "updated_at": int(time.time()),
            })
        except Exception as exc:
            logger.error(f'"health_write_failed: {exc}"', extra={})
        logger.warning(f'"provider_unhealthy: {provider.name} latency={latency_ms}ms"', extra={})

    return {"provider": provider.name, "healthy": healthy, "latency_ms": latency_ms}


async def _run_checks() -> list[dict]:
    settings = get_settings()

    # Resolve API keys
    anthropic_key = settings.anthropic_api_key
    if not anthropic_key and settings.anthropic_secret_arn:
        try:
            anthropic_key = _fetch_secret(settings.anthropic_secret_arn)
        except Exception as exc:
            logger.error(f'"anthropic_secret_fetch_failed: {exc}"', extra={})

    openai_key = settings.openai_api_key
    if not openai_key and settings.openai_secret_arn:
        try:
            openai_key = _fetch_secret(settings.openai_secret_arn)
        except Exception as exc:
            logger.error(f'"openai_secret_fetch_failed: {exc}"', extra={})

    # One representative per provider family — cheapest model sufficient for liveness check
    providers = [BedrockProvider(nova_micro_config())]
    if anthropic_key:
        providers.append(AnthropicProvider(haiku_config(), anthropic_key))
    if openai_key:
        providers.append(OpenAIProvider(gpt4o_mini_config(), openai_key))

    registry = ProviderHealthRegistry()

    tasks = [_check_and_record(p, registry) for p in providers]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    # Flatten any unexpected exceptions into failed results
    clean = []
    for r in results:
        if isinstance(r, Exception):
            logger.error(f'"check_exception: {r}"', extra={})
        else:
            clean.append(r)
    return clean


def handler(event, context):
    """EventBridge Lambda handler — synchronous entry point."""
    logger.info('"health_check_start"', extra={})
    results = asyncio.run(_run_checks())
    healthy_count = sum(1 for r in results if r.get("healthy"))
    logger.info(
        f'"health_check_complete: {healthy_count}/{len(results)} healthy"',
        extra={},
    )
    return {"statusCode": 200, "results": results}
