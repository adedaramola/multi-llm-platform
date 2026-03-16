"""
Pydantic schemas for request/response validation.
All external input enters through these models — no raw dicts passed internally.
"""
from __future__ import annotations

from enum import Enum
from typing import Any, Literal
from pydantic import BaseModel, Field, field_validator
import time


class BudgetHint(str, Enum):
    LOW = "low"
    STANDARD = "standard"
    HIGH = "high"


class Role(str, Enum):
    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"


class Message(BaseModel):
    role: Role
    content: str = Field(..., min_length=1, max_length=200_000)


class RequestMetadata(BaseModel):
    budget: BudgetHint = BudgetHint.STANDARD
    latency_sla_ms: int = Field(default=5000, ge=500, le=60_000)
    reasoning_required: bool = False
    stream: bool = False
    caller_app: str = Field(default="unknown", max_length=64)


class InferenceRequest(BaseModel):
    messages: list[Message] = Field(..., min_length=1, max_length=50)
    model_preference: str | None = Field(default=None, max_length=64)
    max_tokens: int = Field(default=1024, ge=1, le=32_768)
    temperature: float = Field(default=0.7, ge=0.0, le=2.0)
    metadata: RequestMetadata = Field(default_factory=RequestMetadata)

    @field_validator("messages")
    @classmethod
    def messages_not_empty_content(cls, msgs: list[Message]) -> list[Message]:
        for m in msgs:
            if not m.content.strip():
                raise ValueError("Message content cannot be blank.")
        return msgs

    @property
    def prompt_text(self) -> str:
        """Flat string representation for caching/embedding."""
        return "\n".join(f"{m.role.value}: {m.content}" for m in self.messages)


class UsageStats(BaseModel):
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    estimated_cost_usd: float = 0.0


class InferenceResponse(BaseModel):
    request_id: str
    model_used: str
    provider: str
    content: str
    usage: UsageStats
    cache_hit: bool = False
    cache_source: Literal["none", "exact", "semantic"] = "none"
    latency_ms: int
    timestamp: float = Field(default_factory=time.time)


class ErrorResponse(BaseModel):
    request_id: str
    error: str
    code: str
    timestamp: float = Field(default_factory=time.time)


class HealthResponse(BaseModel):
    status: Literal["ok", "degraded", "unhealthy"]
    providers: dict[str, bool]
    cache_available: bool
    timestamp: float = Field(default_factory=time.time)
