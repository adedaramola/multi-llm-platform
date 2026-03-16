"""
Anthropic Claude provider.
Supports Claude Haiku (low), Sonnet (mid), and Opus (high) tiers.
"""
from __future__ import annotations

from typing import AsyncIterator

import anthropic

from .base import BaseProvider, ProviderConfig, ProviderResponse


def haiku_config() -> ProviderConfig:
    return ProviderConfig(
        name="anthropic-haiku",
        model_id="claude-haiku-4-5-20251001",
        tier="low",
        cost_per_input_token=0.80 / 1_000_000,
        cost_per_output_token=4.00 / 1_000_000,
        max_tokens_limit=8192,
        priority=0,
    )


def sonnet_config() -> ProviderConfig:
    return ProviderConfig(
        name="anthropic-sonnet",
        model_id="claude-sonnet-4-6",
        tier="mid",
        cost_per_input_token=3.00 / 1_000_000,
        cost_per_output_token=15.00 / 1_000_000,
        max_tokens_limit=16384,
        priority=0,
    )


def opus_config() -> ProviderConfig:
    return ProviderConfig(
        name="anthropic-opus",
        model_id="claude-opus-4-6",
        tier="high",
        cost_per_input_token=15.00 / 1_000_000,
        cost_per_output_token=75.00 / 1_000_000,
        max_tokens_limit=32768,
        priority=0,
    )


class AnthropicProvider(BaseProvider):
    def __init__(self, config: ProviderConfig, api_key: str) -> None:
        super().__init__(config)
        self._client = anthropic.AsyncAnthropic(api_key=api_key)

    async def complete(
        self,
        messages: list[dict],
        max_tokens: int,
        temperature: float,
    ) -> ProviderResponse:
        # Separate system message if present
        system_prompt = ""
        chat_messages = []
        for msg in messages:
            if msg["role"] == "system":
                system_prompt = msg["content"]
            else:
                chat_messages.append({"role": msg["role"], "content": msg["content"]})

        kwargs: dict = dict(
            model=self.config.model_id,
            max_tokens=min(max_tokens, self.config.max_tokens_limit),
            temperature=temperature,
            messages=chat_messages,
        )
        if system_prompt:
            kwargs["system"] = system_prompt

        response = await self._client.messages.create(**kwargs)

        return ProviderResponse(
            content=response.content[0].text,
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
            model_id=self.config.model_id,
            provider_name=self.config.name,
            raw_metadata={"stop_reason": response.stop_reason},
        )

    async def stream(
        self,
        messages: list[dict],
        max_tokens: int,
        temperature: float,
    ) -> AsyncIterator[str]:
        system_prompt = ""
        chat_messages = []
        for msg in messages:
            if msg["role"] == "system":
                system_prompt = msg["content"]
            else:
                chat_messages.append({"role": msg["role"], "content": msg["content"]})

        kwargs: dict = dict(
            model=self.config.model_id,
            max_tokens=min(max_tokens, self.config.max_tokens_limit),
            temperature=temperature,
            messages=chat_messages,
        )
        if system_prompt:
            kwargs["system"] = system_prompt

        async with self._client.messages.stream(**kwargs) as stream:
            async for text in stream.text_stream:
                yield text

    async def health_check(self) -> bool:
        try:
            resp = await self._client.messages.create(
                model=self.config.model_id,
                max_tokens=5,
                messages=[{"role": "user", "content": "hi"}],
            )
            return bool(resp.content)
        except Exception:
            return False
