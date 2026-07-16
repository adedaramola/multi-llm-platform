"""
API-level tests for gateway/app.py using lightweight in-memory fakes.
These tests exercise endpoint behavior without AWS/network dependencies.
"""
from __future__ import annotations

import sys
import types
from dataclasses import dataclass
from types import SimpleNamespace
from unittest.mock import patch

from fastapi.testclient import TestClient

# Some local environments used for fast unit tests may not include all optional
# runtime dependencies. Provide tiny import-time shims where needed.
if "asyncpg" not in sys.modules:
    asyncpg_stub = types.ModuleType("asyncpg")

    class Connection:  # pragma: no cover - import shim
        def is_closed(self) -> bool:
            return True

    async def connect(*args, **kwargs):  # pragma: no cover - import shim
        raise RuntimeError("asyncpg stub should not be used in test runtime")

    asyncpg_stub.Connection = Connection
    asyncpg_stub.connect = connect
    sys.modules["asyncpg"] = asyncpg_stub

if "redis.asyncio" not in sys.modules:
    redis_stub = types.ModuleType("redis")
    redis_asyncio_stub = types.ModuleType("redis.asyncio")

    class Redis:  # pragma: no cover - import shim
        async def get(self, key: str):
            return None

    async def from_url(*args, **kwargs):  # pragma: no cover - import shim
        return Redis()

    redis_asyncio_stub.Redis = Redis
    redis_asyncio_stub.from_url = from_url
    redis_stub.asyncio = redis_asyncio_stub
    sys.modules["redis"] = redis_stub
    sys.modules["redis.asyncio"] = redis_asyncio_stub

# Some local environments do not have Mangum installed. The gateway only needs
# it for the exported Lambda handler, not for TestClient endpoint tests.
if "mangum" not in sys.modules:
    mangum_stub = types.ModuleType("mangum")

    class Mangum:  # pragma: no cover - trivial shim
        def __init__(self, *args, **kwargs):
            pass

    mangum_stub.Mangum = Mangum
    sys.modules["mangum"] = mangum_stub

from ai_platform.auth.authenticator import CallerIdentity
from ai_platform.gateway import app as gateway
from ai_platform.models.schemas import InferenceRequest
from ai_platform.providers.base import ProviderConfig, ProviderResponse


@dataclass
class FakeCacheResult:
    response: str
    source: str
    model_used: str = "cached-model"


class FakeRateLimiter:
    def __init__(self) -> None:
        self.calls = 0

    async def check_and_increment(self, caller: CallerIdentity) -> None:
        self.calls += 1


class FakeCache:
    def __init__(self, lookup_result=None) -> None:
        self.lookup_result = lookup_result
        self.writes: list[dict] = []
        self.lookups: list[tuple[str, str]] = []

    async def lookup(self, prompt: str, model_constraint: str = ""):
        self.lookups.append((prompt, model_constraint))
        return self.lookup_result

    async def write(
        self,
        prompt: str,
        response: str,
        model_used: str,
        input_tokens: int,
        output_tokens: int,
        ttl_seconds: int | None = None,
        model_constraint: str = "",
    ) -> None:
        self.writes.append(
            {
                "prompt": prompt,
                "response": response,
                "model_used": model_used,
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "model_constraint": model_constraint,
            }
        )


class FakeRouter:
    def __init__(
        self,
        *,
        provider_name: str = "cheap-model",
        model_id: str = "test/cheap-model",
        route_response: ProviderResponse | None = None,
        stream_chunks: list[str] | None = None,
        route_error: Exception | None = None,
        stream_error: Exception | None = None,
    ) -> None:
        self.provider = SimpleNamespace(
            name=provider_name,
            config=ProviderConfig(
                name=provider_name,
                model_id=model_id,
                tier="low",
                cost_per_input_token=0.1 / 1_000_000,
                cost_per_output_token=0.2 / 1_000_000,
            ),
        )
        self._tiers = {"low": [self.provider], "mid": [], "high": []}
        self._route_response = route_response or ProviderResponse(
            content="router response",
            input_tokens=12,
            output_tokens=7,
            model_id=model_id,
            provider_name=provider_name,
        )
        self._stream_chunks = stream_chunks or ["chunk-a", "chunk-b"]
        self._route_error = route_error
        self._stream_error = stream_error
        self.route_calls = 0
        self.stream_calls = 0

    async def route(self, request: InferenceRequest, on_provider_selected=None) -> ProviderResponse:
        self.route_calls += 1
        if on_provider_selected:
            on_provider_selected(self.provider.name, self.provider.config.tier)
        if self._route_error:
            raise self._route_error
        return self._route_response

    async def route_stream(self, request: InferenceRequest, on_provider_selected=None):
        self.stream_calls += 1
        if on_provider_selected:
            on_provider_selected(self.provider.name, self.provider.config.tier)
        if self._stream_error:
            raise self._stream_error
        for chunk in self._stream_chunks:
            yield chunk


class FakeRegistry:
    def __init__(self, statuses: dict[str, bool]) -> None:
        self.statuses = statuses
        self.refresh_calls = 0

    def refresh(self) -> None:
        self.refresh_calls += 1

    def is_healthy(self, provider_name: str) -> bool:
        return self.statuses.get(provider_name, True)


def _auth_override():
    return CallerIdentity(
        caller_id="test-caller",
        app_name="test-app",
        rpm_limit=1000,
        rpd_limit=100000,
        active=True,
    )


def _request_body(content: str = "hello") -> dict:
    return {
        "messages": [{"role": "user", "content": content}],
        "metadata": {"budget": "standard", "latency_sla_ms": 1000},
    }


def _setup_app_state(router: FakeRouter, cache: FakeCache, limiter: FakeRateLimiter) -> None:
    gateway.app.state.router = router
    gateway.app.state.cache = cache
    gateway.app.state.rate_limiter = limiter
    gateway.app.dependency_overrides[gateway.get_caller_identity] = _auth_override


def _teardown_app_state() -> None:
    gateway.app.dependency_overrides.clear()


def test_lifespan_passes_resolved_dsn_to_cache():
    dsn = "postgresql://user:pw@aurora-host:5432/ai_platform"
    with (
        patch.object(gateway, "_resolve_pg_dsn", return_value=dsn),
        TestClient(gateway.app),
    ):
        assert gateway.app.state.cache._pg_dsn == dsn


def test_health_endpoint_returns_provider_statuses():
    router = FakeRouter(provider_name="cheap-model")
    cache = FakeCache()
    limiter = FakeRateLimiter()
    registry = FakeRegistry({"cheap-model": True})
    gateway.app.dependency_overrides[gateway.get_caller_identity] = _auth_override

    with (
        patch.object(gateway, "get_health_registry", return_value=registry),
        patch.object(gateway, "emit_request_metric", return_value=None),
        patch.object(gateway, "emit_error_metric", return_value=None),
        TestClient(gateway.app) as client,
    ):
        gateway.app.state.router = router
        gateway.app.state.cache = cache
        gateway.app.state.rate_limiter = limiter
        resp = client.get("/health")

    _teardown_app_state()
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["status"] == "ok"
    assert payload["providers"]["cheap-model"] is True
    assert registry.refresh_calls >= 1


def test_chat_cache_hit_returns_cached_response():
    router = FakeRouter()
    cache = FakeCache(lookup_result=FakeCacheResult(response="from-cache", source="exact"))
    limiter = FakeRateLimiter()
    registry = FakeRegistry({"cheap-model": True})
    gateway.app.dependency_overrides[gateway.get_caller_identity] = _auth_override

    with (
        patch.object(gateway, "get_health_registry", return_value=registry),
        patch.object(gateway, "emit_request_metric", return_value=None),
        patch.object(gateway, "emit_error_metric", return_value=None),
        TestClient(gateway.app) as client,
    ):
        gateway.app.state.router = router
        gateway.app.state.cache = cache
        gateway.app.state.rate_limiter = limiter
        resp = client.post("/v1/chat", json=_request_body())

    _teardown_app_state()
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["cache_hit"] is True
    assert payload["provider"] == "cache"
    assert payload["content"] == "from-cache"
    assert router.route_calls == 0
    assert limiter.calls == 1


def test_chat_cache_miss_routes_and_writes_cache():
    router = FakeRouter(
        route_response=ProviderResponse(
            content="from-provider",
            input_tokens=8,
            output_tokens=3,
            model_id="test/cheap-model",
            provider_name="cheap-model",
        )
    )
    cache = FakeCache(lookup_result=None)
    limiter = FakeRateLimiter()
    registry = FakeRegistry({"cheap-model": True})
    gateway.app.dependency_overrides[gateway.get_caller_identity] = _auth_override

    with (
        patch.object(gateway, "get_health_registry", return_value=registry),
        patch.object(gateway, "emit_request_metric", return_value=None),
        patch.object(gateway, "emit_error_metric", return_value=None),
        TestClient(gateway.app) as client,
    ):
        gateway.app.state.router = router
        gateway.app.state.cache = cache
        gateway.app.state.rate_limiter = limiter
        resp = client.post("/v1/chat", json=_request_body("route this"))

    _teardown_app_state()
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["cache_hit"] is False
    assert payload["provider"] == "cheap-model"
    assert payload["model_used"] == "test/cheap-model"
    assert payload["content"] == "from-provider"
    assert router.route_calls == 1
    assert len(cache.writes) == 1
    assert cache.writes[0]["response"] == "from-provider"


def test_chat_model_preference_flows_into_cache_key():
    router = FakeRouter()
    cache = FakeCache(lookup_result=None)
    limiter = FakeRateLimiter()
    registry = FakeRegistry({"cheap-model": True})
    gateway.app.dependency_overrides[gateway.get_caller_identity] = _auth_override

    body = _request_body("pin me")
    body["model_preference"] = "opus"

    with (
        patch.object(gateway, "get_health_registry", return_value=registry),
        patch.object(gateway, "emit_request_metric", return_value=None),
        patch.object(gateway, "emit_error_metric", return_value=None),
        TestClient(gateway.app) as client,
    ):
        gateway.app.state.router = router
        gateway.app.state.cache = cache
        gateway.app.state.rate_limiter = limiter
        resp = client.post("/v1/chat", json=body)

    _teardown_app_state()
    assert resp.status_code == 200
    assert cache.lookups == [("user: pin me", "opus")]
    assert cache.writes[0]["model_constraint"] == "opus"


def test_chat_returns_503_when_all_providers_fail():
    router = FakeRouter(route_error=RuntimeError("all providers failed"))
    cache = FakeCache(lookup_result=None)
    limiter = FakeRateLimiter()
    registry = FakeRegistry({"cheap-model": True})
    gateway.app.dependency_overrides[gateway.get_caller_identity] = _auth_override

    with (
        patch.object(gateway, "get_health_registry", return_value=registry),
        patch.object(gateway, "emit_request_metric", return_value=None),
        patch.object(gateway, "emit_error_metric", return_value=None),
        TestClient(gateway.app) as client,
    ):
        gateway.app.state.router = router
        gateway.app.state.cache = cache
        gateway.app.state.rate_limiter = limiter
        resp = client.post("/v1/chat", json=_request_body("fail please"))

    _teardown_app_state()
    assert resp.status_code == 503
    payload = resp.json()
    assert payload["code"] == "provider_unavailable"
    assert "All LLM providers failed" in payload["error"]


def test_stream_cache_hit_returns_sse_done():
    router = FakeRouter()
    cache = FakeCache(lookup_result=FakeCacheResult(response="stream-cache", source="exact"))
    limiter = FakeRateLimiter()
    registry = FakeRegistry({"cheap-model": True})
    gateway.app.dependency_overrides[gateway.get_caller_identity] = _auth_override

    with (
        patch.object(gateway, "get_health_registry", return_value=registry),
        patch.object(gateway, "emit_request_metric", return_value=None),
        patch.object(gateway, "emit_error_metric", return_value=None),
        TestClient(gateway.app) as client,
    ):
        gateway.app.state.router = router
        gateway.app.state.cache = cache
        gateway.app.state.rate_limiter = limiter
        resp = client.post("/v1/chat/stream", json=_request_body("stream"))

    _teardown_app_state()
    assert resp.status_code == 200
    assert "data: stream-cache" in resp.text
    assert "data: [DONE]" in resp.text
    assert router.stream_calls == 0


def test_stream_cache_miss_streams_chunks_and_writes_cache():
    router = FakeRouter(stream_chunks=["hello", "world"], model_id="test/cheap-model")
    cache = FakeCache(lookup_result=None)
    limiter = FakeRateLimiter()
    registry = FakeRegistry({"cheap-model": True})
    gateway.app.dependency_overrides[gateway.get_caller_identity] = _auth_override

    with (
        patch.object(gateway, "get_health_registry", return_value=registry),
        patch.object(gateway, "emit_request_metric", return_value=None),
        patch.object(gateway, "emit_error_metric", return_value=None),
        TestClient(gateway.app) as client,
    ):
        gateway.app.state.router = router
        gateway.app.state.cache = cache
        gateway.app.state.rate_limiter = limiter
        resp = client.post("/v1/chat/stream", json=_request_body("stream miss"))

    _teardown_app_state()
    assert resp.status_code == 200
    assert "data: hello" in resp.text
    assert "data: world" in resp.text
    assert "data: [DONE]" in resp.text
    assert router.stream_calls == 1
    assert len(cache.writes) == 1
    assert cache.writes[0]["response"] == "helloworld"
    assert cache.writes[0]["model_used"] == "test/cheap-model"
