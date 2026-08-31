"""
Pydantic schemas for request/response validation.
All external input enters through these models — no raw dicts passed internally.
"""

from __future__ import annotations

import time
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator


class BudgetHint(StrEnum):
    LOW = "low"
    STANDARD = "standard"
    HIGH = "high"


class Role(StrEnum):
    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"


class CachePolicy(StrEnum):
    OFF = "off"
    PRIVATE = "private"
    SHARED = "shared"


class DataClassification(StrEnum):
    PUBLIC = "public"
    INTERNAL = "internal"
    RESTRICTED = "restricted"


class Message(BaseModel):
    role: Role
    content: str = Field(..., min_length=1, max_length=200_000)


class RequestMetadata(BaseModel):
    budget: BudgetHint = BudgetHint.STANDARD
    latency_sla_ms: int = Field(default=5000, ge=500, le=60_000)
    reasoning_required: bool = False
    stream: bool = False
    caller_app: str = Field(default="unknown", max_length=64)
    workflow_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=64,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$",
    )
    cache_policy: CachePolicy = CachePolicy.OFF
    data_classification: DataClassification = DataClassification.INTERNAL

    @model_validator(mode="after")
    def shared_cache_requires_public_data(self) -> RequestMetadata:
        if self.cache_policy == CachePolicy.SHARED and self.data_classification != DataClassification.PUBLIC:
            raise ValueError("Shared caching is allowed only for public data")
        return self


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
    cache_policy: CachePolicy = CachePolicy.OFF
    latency_ms: int
    timestamp: float = Field(default_factory=time.time)


class UsageSummary(BaseModel):
    """Per-caller aggregate for the current UTC month."""

    caller_id: str
    month: str  # "YYYY-MM"
    request_count: int = 0
    cache_hits: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    estimated_cost_usd: float = 0.0


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
