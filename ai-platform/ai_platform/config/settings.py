"""
Centralized configuration via environment variables.
Loaded once at Lambda cold start; never queried at runtime per-request.
"""
from __future__ import annotations
from functools import lru_cache
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    # ── Platform ──────────────────────────────────────────────────────────────
    environment: str = "production"
    log_level: str = "INFO"
    aws_region: str = "us-east-1"

    # ── API Keys — either direct env var or fetched from Secrets Manager ARN
    openai_api_key: str = ""
    anthropic_api_key: str = ""
    anthropic_secret_arn: str = ""
    openai_secret_arn: str = ""

    # ── AWS Bedrock ───────────────────────────────────────────────────────────
    bedrock_region: str = "us-east-1"

    # ── Cache ──────────────────────────────────────────────────────────────────
    redis_url: str = ""                    # ElastiCache Serverless endpoint
    redis_ttl_seconds: int = 3600
    pg_dsn: str = ""                       # Aurora Serverless pgvector DSN
    semantic_cache_threshold: float = 0.92
    cache_enabled: bool = True

    # ── DynamoDB ──────────────────────────────────────────────────────────────
    api_keys_table: str = "ai-platform-api-keys"
    rate_limit_table: str = "ai-platform-rate-limits"
    health_table: str = "ai-platform-provider-health"

    # ── Rate Limits (defaults, overridden per API key) ────────────────────────
    default_rpm: int = 60       # requests per minute
    default_rpd: int = 5_000    # requests per day

    # ── Routing ───────────────────────────────────────────────────────────────
    complexity_low_threshold: float = 0.3
    complexity_mid_threshold: float = 0.7
    max_provider_retries: int = 2
    provider_timeout_seconds: int = 30

    # ── Embeddings ────────────────────────────────────────────────────────────
    embedding_model: str = "amazon.titan-embed-text-v1"  # Bedrock — no extra cost
    embedding_dimensions: int = 1536


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
