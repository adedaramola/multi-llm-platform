"""
Abstract provider interface.
All LLM providers implement this contract — the router never touches provider SDKs directly.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import AsyncIterator


@dataclass
class ProviderConfig:
    name: str
    model_id: str
    tier: str                        # "low" | "mid" | "high"
    cost_per_input_token: float      # USD per token
    cost_per_output_token: float     # USD per token
    max_tokens_limit: int = 8192
    supports_streaming: bool = True
    priority: int = 0                # lower = preferred within same tier


@dataclass
class ProviderResponse:
    content: str
    input_tokens: int
    output_tokens: int
    model_id: str
    provider_name: str
    raw_metadata: dict = field(default_factory=dict)

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    def estimated_cost(self, config: ProviderConfig) -> float:
        return (
            self.input_tokens * config.cost_per_input_token
            + self.output_tokens * config.cost_per_output_token
        )


class BaseProvider(ABC):
    def __init__(self, config: ProviderConfig) -> None:
        self.config = config

    @property
    def name(self) -> str:
        return self.config.name

    @property
    def tier(self) -> str:
        return self.config.tier

    @property
    def cost_per_token(self) -> float:
        """Blended cost estimate for routing comparison."""
        return (self.config.cost_per_input_token + self.config.cost_per_output_token) / 2

    @abstractmethod
    async def complete(
        self,
        messages: list[dict],
        max_tokens: int,
        temperature: float,
    ) -> ProviderResponse:
        """Send a chat completion request and return a structured response."""
        ...

    @abstractmethod
    async def stream(
        self,
        messages: list[dict],
        max_tokens: int,
        temperature: float,
    ) -> AsyncIterator[str]:
        """Stream tokens as they arrive."""
        ...

    @abstractmethod
    async def health_check(self) -> bool:
        """Lightweight liveness check — should complete in < 2 seconds."""
        ...
