"""
OpenAI-compatible provider.
Works with OpenAI directly or any OpenAI-compatible endpoint (local Ollama, Azure OpenAI).
"""
from __future__ import annotations

from typing import AsyncIterator

from openai import AsyncOpenAI

from .base import BaseProvider, ProviderConfig, ProviderResponse


def gpt4o_mini_config() -> ProviderConfig:
    return ProviderConfig(
        name="openai-gpt4o-mini",
        model_id="gpt-4o-mini",
        tier="mid",
        cost_per_input_token=0.15 / 1_000_000,
        cost_per_output_token=0.60 / 1_000_000,
        max_tokens_limit=16384,
        priority=1,  # prefer Anthropic Sonnet at same tier
    )


def gpt4o_config() -> ProviderConfig:
    return ProviderConfig(
        name="openai-gpt4o",
        model_id="gpt-4o",
        tier="high",
        cost_per_input_token=2.50 / 1_000_000,
        cost_per_output_token=10.00 / 1_000_000,
        max_tokens_limit=32768,
        priority=1,
    )


class OpenAIProvider(BaseProvider):
    def __init__(
        self,
        config: ProviderConfig,
        api_key: str,
        base_url: str | None = None,
    ) -> None:
        super().__init__(config)
        self._client = AsyncOpenAI(api_key=api_key, base_url=base_url)

    async def complete(
        self,
        messages: list[dict],
        max_tokens: int,
        temperature: float,
    ) -> ProviderResponse:
        response = await self._client.chat.completions.create(
            model=self.config.model_id,
            messages=messages,
            max_tokens=min(max_tokens, self.config.max_tokens_limit),
            temperature=temperature,
        )
        choice = response.choices[0]
        usage = response.usage

        return ProviderResponse(
            content=choice.message.content or "",
            input_tokens=usage.prompt_tokens,
            output_tokens=usage.completion_tokens,
            model_id=self.config.model_id,
            provider_name=self.config.name,
            raw_metadata={"finish_reason": choice.finish_reason},
        )

    async def stream(
        self,
        messages: list[dict],
        max_tokens: int,
        temperature: float,
    ) -> AsyncIterator[str]:
        stream = await self._client.chat.completions.create(
            model=self.config.model_id,
            messages=messages,
            max_tokens=min(max_tokens, self.config.max_tokens_limit),
            temperature=temperature,
            stream=True,
        )
        async for chunk in stream:
            delta = chunk.choices[0].delta.content
            if delta:
                yield delta

    async def health_check(self) -> bool:
        try:
            resp = await self._client.chat.completions.create(
                model=self.config.model_id,
                messages=[{"role": "user", "content": "hi"}],
                max_tokens=5,
            )
            return bool(resp.choices)
        except Exception:
            return False
