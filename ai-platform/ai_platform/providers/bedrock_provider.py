"""
AWS Bedrock provider — used for low-cost tier and as the always-on fallback.
Uses boto3 async via run_in_executor since aioboto3 adds complexity.
"""
from __future__ import annotations

import asyncio
import json
from typing import AsyncIterator

import boto3

from .base import BaseProvider, ProviderConfig, ProviderResponse
from ..config.settings import get_settings


def nova_micro_config() -> ProviderConfig:
    """Amazon Nova Micro — cheapest Bedrock model, always-on fallback."""
    return ProviderConfig(
        name="bedrock-nova-micro",
        model_id="amazon.nova-micro-v1:0",
        tier="low",
        cost_per_input_token=0.035 / 1_000_000,
        cost_per_output_token=0.14 / 1_000_000,
        max_tokens_limit=5000,
        supports_streaming=False,
        priority=0,
    )


def bedrock_haiku_config() -> ProviderConfig:
    """Claude Haiku via Bedrock — same model, different billing."""
    return ProviderConfig(
        name="bedrock-claude-haiku",
        model_id="anthropic.claude-haiku-4-5-20251001-v1:0",
        tier="low",
        cost_per_input_token=0.80 / 1_000_000,
        cost_per_output_token=4.00 / 1_000_000,
        max_tokens_limit=8192,
        priority=1,
    )


class BedrockProvider(BaseProvider):
    def __init__(self, config: ProviderConfig) -> None:
        super().__init__(config)
        settings = get_settings()
        self._client = boto3.client("bedrock-runtime", region_name=settings.bedrock_region)

    def _invoke_sync(self, body: dict) -> dict:
        response = self._client.invoke_model(
            modelId=self.config.model_id,
            body=json.dumps(body),
            contentType="application/json",
            accept="application/json",
        )
        return json.loads(response["body"].read())

    async def complete(
        self,
        messages: list[dict],
        max_tokens: int,
        temperature: float,
    ) -> ProviderResponse:
        if "nova" in self.config.model_id:
            # Amazon Nova Messages API format
            nova_messages = []
            system_prompt = ""
            for msg in messages:
                if msg["role"] == "system":
                    system_prompt = msg["content"]
                else:
                    nova_messages.append({
                        "role": msg["role"],
                        "content": [{"text": msg["content"]}],
                    })
            body: dict = {
                "messages": nova_messages,
                "inferenceConfig": {
                    "maxTokens": min(max_tokens, self.config.max_tokens_limit),
                    "temperature": temperature,
                },
            }
            if system_prompt:
                body["system"] = [{"text": system_prompt}]

            result = await asyncio.get_running_loop().run_in_executor(None, self._invoke_sync, body)
            content = result["output"]["message"]["content"][0]["text"]
            input_tokens = result["usage"]["inputTokens"]
            output_tokens = result["usage"]["outputTokens"]

        else:
            # Anthropic models via Bedrock — Messages API format
            system_prompt = ""
            chat_messages = []
            for msg in messages:
                if msg["role"] == "system":
                    system_prompt = msg["content"]
                else:
                    chat_messages.append({"role": msg["role"], "content": msg["content"]})

            body = {
                "anthropic_version": "bedrock-2023-05-31",
                "max_tokens": min(max_tokens, self.config.max_tokens_limit),
                "temperature": temperature,
                "messages": chat_messages,
            }
            if system_prompt:
                body["system"] = system_prompt

            result = await asyncio.get_running_loop().run_in_executor(None, self._invoke_sync, body)
            content = result["content"][0]["text"]
            input_tokens = result["usage"]["input_tokens"]
            output_tokens = result["usage"]["output_tokens"]

        return ProviderResponse(
            content=content,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            model_id=self.config.model_id,
            provider_name=self.config.name,
        )

    async def stream(
        self,
        messages: list[dict],
        max_tokens: int,
        temperature: float,
    ) -> AsyncIterator[str]:
        response = await self.complete(messages, max_tokens, temperature)
        yield response.content

    async def health_check(self) -> bool:
        try:
            result = await self.complete(
                [{"role": "user", "content": "hi"}],
                max_tokens=5,
                temperature=0.0,
            )
            return bool(result.content)
        except Exception:
            return False
