"""Tests for the scheduled provider health-check Lambda."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

from ai_platform import health_checker


def test_run_checks_closes_all_provider_clients_inside_event_loop():
    bedrock = MagicMock(name="bedrock")
    bedrock.name = "bedrock-nova-micro"
    bedrock.health_check = AsyncMock(return_value=True)
    bedrock.close = AsyncMock()

    anthropic = MagicMock(name="anthropic")
    anthropic.name = "anthropic-haiku"
    anthropic.health_check = AsyncMock(return_value=True)
    anthropic.close = AsyncMock()

    openai = MagicMock(name="openai")
    openai.name = "openai-gpt4o-mini"
    openai.health_check = AsyncMock(return_value=True)
    openai.close = AsyncMock()

    settings = MagicMock(
        anthropic_api_key="anthropic-key",
        anthropic_secret_arn="",
        openai_api_key="openai-key",
        openai_secret_arn="",
    )
    registry = MagicMock()

    with (
        patch.object(health_checker, "get_settings", return_value=settings),
        patch.object(health_checker, "BedrockProvider", return_value=bedrock),
        patch.object(health_checker, "AnthropicProvider", return_value=anthropic),
        patch.object(health_checker, "OpenAIProvider", return_value=openai),
        patch.object(health_checker, "ProviderHealthRegistry", return_value=registry),
    ):
        results = asyncio.run(health_checker._run_checks())

    assert len(results) == 3
    bedrock.close.assert_awaited_once()
    anthropic.close.assert_awaited_once()
    openai.close.assert_awaited_once()


def test_run_checks_still_closes_clients_when_check_raises():
    provider = MagicMock()
    provider.name = "bedrock-nova-micro"
    provider.health_check = AsyncMock(side_effect=RuntimeError("failed"))
    provider.close = AsyncMock()
    settings = MagicMock(
        anthropic_api_key="",
        anthropic_secret_arn="",
        openai_api_key="",
        openai_secret_arn="",
    )

    with (
        patch.object(health_checker, "get_settings", return_value=settings),
        patch.object(health_checker, "BedrockProvider", return_value=provider),
        patch.object(health_checker, "ProviderHealthRegistry", return_value=MagicMock()),
    ):
        results = asyncio.run(health_checker._run_checks())

    assert results == []
    provider.close.assert_awaited_once()


def test_failed_probe_is_degraded_before_consecutive_failure_threshold():
    provider = MagicMock()
    provider.name = "anthropic-haiku"
    provider.health_check = AsyncMock(return_value=False)
    registry = MagicMock()
    registry.record_probe_failure.return_value = "degraded"

    result = asyncio.run(health_checker._check_and_record(provider, registry))

    registry.record_probe_failure.assert_called_once_with(
        "anthropic-haiku", health_checker.UNHEALTHY_THRESHOLD
    )
    assert result["healthy"] is False
    assert result["status"] == "degraded"
