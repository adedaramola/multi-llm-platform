"""
Unit tests for cache/semantic_cache.py using in-memory fakes.
No Redis, Postgres, or AWS access — fakes are injected onto the instance.
"""

from __future__ import annotations

import asyncio

from ai_platform.cache.semantic_cache import SemanticCache, _vector_literal
from ai_platform.config.settings import Settings, get_settings


class FakeRedis:
    def __init__(self) -> None:
        self.store: dict[str, str] = {}

    async def get(self, key: str) -> str | None:
        return self.store.get(key)

    async def setex(self, key: str, ttl: int, value: str) -> None:
        self.store[key] = value


def _make_cache(pg_dsn: str = "", with_redis: bool = True) -> SemanticCache:
    cache = SemanticCache(pg_dsn=pg_dsn)
    cache._settings = Settings(
        redis_url="redis://fake" if with_redis else "",
        cache_enabled=True,
        pg_dsn="",
    )
    if with_redis:
        cache._redis = FakeRedis()
    return cache


def _track_embed_calls(cache: SemanticCache) -> list[str]:
    calls: list[str] = []

    async def fake_embed(text: str) -> list[float]:
        calls.append(text)
        return [0.0] * 4

    cache._embed = fake_embed
    return calls


def test_vector_literal_is_accepted_pgvector_text_form():
    assert _vector_literal([0.25, -1.0, 0.0]) == "[0.25,-1.0,0.0]"


# ── DSN wiring ─────────────────────────────────────────────────────────────────


def test_explicit_pg_dsn_overrides_settings():
    dsn = "postgresql://user:pw@aurora-host:5432/ai_platform"
    assert SemanticCache(pg_dsn=dsn)._pg_dsn == dsn


def test_default_pg_dsn_falls_back_to_settings():
    assert SemanticCache()._pg_dsn == get_settings().pg_dsn


def test_lookup_skips_semantic_layer_without_dsn():
    cache = _make_cache(pg_dsn="")
    embed_calls = _track_embed_calls(cache)

    result = asyncio.run(cache.lookup("some uncached prompt"))

    assert result is None
    assert embed_calls == []  # pgvector layer never attempted


def test_write_skips_semantic_layer_without_dsn():
    cache = _make_cache(pg_dsn="")
    embed_calls = _track_embed_calls(cache)

    asyncio.run(
        cache.write(
            prompt="a prompt",
            response="a response",
            model_used="test/model",
            input_tokens=3,
            output_tokens=5,
        )
    )

    assert embed_calls == []  # no embedding call without a pg backend
    assert len(cache._redis.store) == 1  # Redis layer still written


# ── Model-constraint keying ────────────────────────────────────────────────────


def test_pinned_lookup_never_hits_other_models_entry():
    cache = _make_cache(pg_dsn="")
    _track_embed_calls(cache)

    # Response produced under an "opus" pin
    asyncio.run(
        cache.write(
            prompt="Summarize this document",
            response="opus answer",
            model_used="claude-opus",
            input_tokens=5,
            output_tokens=5,
            model_constraint="opus",
        )
    )

    same_pin = asyncio.run(cache.lookup("Summarize this document", model_constraint="opus"))
    other_pin = asyncio.run(cache.lookup("Summarize this document", model_constraint="haiku"))
    unpinned = asyncio.run(cache.lookup("Summarize this document"))

    assert same_pin is not None and same_pin.response == "opus answer"
    assert other_pin is None
    assert unpinned is None


def test_constraint_is_normalized_for_keying():
    cache = _make_cache(pg_dsn="")
    _track_embed_calls(cache)

    asyncio.run(
        cache.write(
            prompt="hello",
            response="hi",
            model_used="claude-opus",
            input_tokens=1,
            output_tokens=1,
            model_constraint="Opus",
        )
    )
    result = asyncio.run(cache.lookup("hello", model_constraint="  opus "))

    assert result is not None
    assert result.response == "hi"


def test_write_then_exact_lookup_round_trips_via_redis():
    cache = _make_cache(pg_dsn="")
    _track_embed_calls(cache)

    asyncio.run(
        cache.write(
            prompt="What is the capital of France?",
            response="Paris.",
            model_used="test/model",
            input_tokens=8,
            output_tokens=2,
        )
    )
    result = asyncio.run(cache.lookup("what is  the capital of FRANCE?"))

    assert result is not None
    assert result.source == "exact"
    assert result.response == "Paris."
    assert result.model_used == "test/model"


def test_private_cache_entries_are_isolated_by_namespace():
    cache = _make_cache(pg_dsn="")
    _track_embed_calls(cache)

    asyncio.run(
        cache.write(
            prompt="Summarize ticket",
            response="caller-a response",
            model_used="test/model",
            input_tokens=3,
            output_tokens=2,
            namespace="caller:a",
        )
    )

    same_caller = asyncio.run(cache.lookup("Summarize ticket", namespace="caller:a"))
    other_caller = asyncio.run(cache.lookup("Summarize ticket", namespace="caller:b"))
    shared = asyncio.run(cache.lookup("Summarize ticket", namespace="shared"))

    assert same_caller is not None and same_caller.response == "caller-a response"
    assert other_caller is None
    assert shared is None
