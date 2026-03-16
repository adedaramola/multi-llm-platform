"""
Semantic + exact-match response cache.

Two layers:
  1. Redis — exact prompt hash match (sub-millisecond)
  2. PostgreSQL pgvector — semantic similarity match (cosine distance)

On cache miss the caller is responsible for writing the response back.
"""
from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from typing import Literal

import asyncpg
import boto3
import redis.asyncio as aioredis

from ..config.settings import get_settings

logger = logging.getLogger(__name__)


@dataclass
class CacheResult:
    response: str
    source: Literal["exact", "semantic"]
    similarity: float = 1.0
    model_used: str = ""


def _normalize_prompt(prompt: str) -> str:
    return " ".join(prompt.lower().split())


def _hash_prompt(prompt: str) -> str:
    return hashlib.sha256(_normalize_prompt(prompt).encode()).hexdigest()


class SemanticCache:
    def __init__(self) -> None:
        settings = get_settings()
        self._settings = settings
        self._redis: aioredis.Redis | None = None
        self._pg: asyncpg.Connection | None = None
        self._bedrock = boto3.client("bedrock-runtime", region_name=settings.bedrock_region)

    async def _get_redis(self) -> aioredis.Redis | None:
        if not self._settings.redis_url:
            return None
        if self._redis is None:
            self._redis = await aioredis.from_url(
                self._settings.redis_url,
                encoding="utf-8",
                decode_responses=True,
            )
        return self._redis

    async def _get_pg(self) -> asyncpg.Connection:
        if self._pg is None or self._pg.is_closed():
            self._pg = await asyncpg.connect(self._settings.pg_dsn)
        return self._pg

    def _embed(self, text: str) -> list[float]:
        """Call Bedrock Titan Embeddings synchronously (fast enough in Lambda)."""
        response = self._bedrock.invoke_model(
            modelId=self._settings.embedding_model,
            body=json.dumps({"inputText": text[:8000]}),
            contentType="application/json",
            accept="application/json",
        )
        body = json.loads(response["body"].read())
        return body["embedding"]

    async def lookup(self, prompt: str) -> CacheResult | None:
        if not self._settings.cache_enabled:
            return None

        normalized = _normalize_prompt(prompt)
        prompt_hash = _hash_prompt(normalized)

        # ── Layer 1: Redis exact match ────────────────────────────────────────
        try:
            redis = await self._get_redis()
            raw = await redis.get(f"cache:{prompt_hash}") if redis else None
            if raw:
                data = json.loads(raw)
                logger.info("cache_hit", extra={"source": "exact"})
                return CacheResult(
                    response=data["response"],
                    source="exact",
                    model_used=data.get("model_used", ""),
                )
        except Exception as exc:
            logger.warning("redis_lookup_failed", extra={"error": str(exc)})

        # ── Layer 2: pgvector semantic search ─────────────────────────────────
        try:
            embedding = self._embed(normalized)
            pg = await self._get_pg()
            row = await pg.fetchrow(
                """
                SELECT response, model_used,
                       1 - (embedding <=> $1::vector) AS similarity
                FROM semantic_cache
                WHERE (expires_at IS NULL OR expires_at > NOW())
                ORDER BY embedding <=> $1::vector
                LIMIT 1
                """,
                embedding,
            )
            if row and row["similarity"] >= self._settings.semantic_cache_threshold:
                logger.info(
                    "cache_hit",
                    extra={"source": "semantic", "similarity": row["similarity"]},
                )
                # Promote to Redis so next exact hit is instant
                await self._promote_to_redis(prompt_hash, row["response"], row["model_used"])
                return CacheResult(
                    response=row["response"],
                    source="semantic",
                    similarity=row["similarity"],
                    model_used=row["model_used"],
                )
        except Exception as exc:
            logger.warning("pgvector_lookup_failed", extra={"error": str(exc)})

        return None

    async def write(
        self,
        prompt: str,
        response: str,
        model_used: str,
        input_tokens: int,
        output_tokens: int,
        ttl_seconds: int | None = None,
    ) -> None:
        """Store response in both Redis and pgvector."""
        if not self._settings.cache_enabled:
            return

        normalized = _normalize_prompt(prompt)
        prompt_hash = _hash_prompt(normalized)

        # Write to Redis first (fast path for future exact hits)
        await self._promote_to_redis(
            prompt_hash, response, model_used,
            ttl=ttl_seconds or self._settings.redis_ttl_seconds,
        )

        # Write to pgvector for semantic recall
        try:
            embedding = self._embed(normalized)
            pg = await self._get_pg()
            expires_sql = (
                f"NOW() + INTERVAL '{ttl_seconds} seconds'" if ttl_seconds else "NULL"
            )
            await pg.execute(
                f"""
                INSERT INTO semantic_cache
                    (prompt_hash, embedding, response, model_used, input_tokens, output_tokens, expires_at)
                VALUES ($1, $2::vector, $3, $4, $5, $6, {expires_sql})
                ON CONFLICT (prompt_hash) DO UPDATE
                    SET response = EXCLUDED.response,
                        model_used = EXCLUDED.model_used,
                        input_tokens = EXCLUDED.input_tokens,
                        output_tokens = EXCLUDED.output_tokens,
                        created_at = NOW()
                """,
                prompt_hash,
                embedding,
                response,
                model_used,
                input_tokens,
                output_tokens,
            )
        except Exception as exc:
            logger.warning("pgvector_write_failed", extra={"error": str(exc)})

    async def _promote_to_redis(
        self,
        prompt_hash: str,
        response: str,
        model_used: str,
        ttl: int | None = None,
    ) -> None:
        try:
            redis = await self._get_redis()
            if redis is None:
                return
            payload = json.dumps({"response": response, "model_used": model_used})
            ttl = ttl or self._settings.redis_ttl_seconds
            await redis.setex(f"cache:{prompt_hash}", ttl, payload)
        except Exception as exc:
            logger.warning("redis_write_failed", extra={"error": str(exc)})
