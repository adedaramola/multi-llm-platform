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

import pytest
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
from ai_platform.router.router import StreamInterruptedError


def test_resolve_pg_dsn_uses_complete_rds_secret():
    settings = SimpleNamespace(
        pg_dsn="",
        pg_secret_arn="rds-secret-arn",
        pg_host="aurora.internal",
        pg_port=5432,
        pg_database="ai_platform",
    )
    secret = '{"username":"platform_admin","password":"p@ss"}'

    with patch.object(gateway, "fetch_secret_value", return_value=secret):
        dsn = gateway._resolve_pg_dsn(settings)

    assert dsn == "postgresql://platform_admin:p%40ss@aurora.internal:5432/ai_platform"


@dataclass
class FakeCacheResult:
    response: str
    source: str
    model_used: str = "cached-model"


class FakeUsageRecorder:
    def __init__(self, summary: dict | None = None, error: Exception | None = None) -> None:
        self.records: list[dict] = []
        self._summary = summary
        self._error = error

    async def record(
        self,
        caller_id: str,
        *,
        input_tokens: int = 0,
        output_tokens: int = 0,
        estimated_cost_usd: float = 0.0,
        cache_hit: bool = False,
    ) -> None:
        self.records.append(
            {
                "caller_id": caller_id,
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "estimated_cost_usd": estimated_cost_usd,
                "cache_hit": cache_hit,
            }
        )

    async def month_summary(self, caller_id: str) -> dict:
        if self._error:
            raise self._error
        return self._summary or {
            "month": "2026-07",
            "request_count": 0,
            "cache_hits": 0,
            "input_tokens": 0,
            "output_tokens": 0,
            "estimated_cost_usd": 0.0,
        }


class FakeRateLimiter:
    def __init__(self) -> None:
        self.calls = 0

    async def check_and_increment(self, caller: CallerIdentity) -> None:
        self.calls += 1


class FakeCache:
    def __init__(self, lookup_result=None) -> None:
        self.lookup_result = lookup_result
        self.writes: list[dict] = []
        self.lookups: list[tuple[str, str, str]] = []

    async def lookup(self, prompt: str, model_constraint: str = "", namespace: str = "shared"):
        self.lookups.append((prompt, model_constraint, namespace))
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
        namespace: str = "shared",
    ) -> None:
        self.writes.append(
            {
                "prompt": prompt,
                "response": response,
                "model_used": model_used,
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "model_constraint": model_constraint,
                "namespace": namespace,
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
        stream_error_after_chunks: Exception | None = None,
        stream_usage: tuple[int, int] | None = None,
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
        self._stream_error_after_chunks = stream_error_after_chunks
        self._stream_usage = stream_usage
        self.route_calls = 0
        self.stream_calls = 0

    async def route(self, request: InferenceRequest, on_provider_selected=None) -> ProviderResponse:
        self.route_calls += 1
        if on_provider_selected:
            on_provider_selected(self.provider.name, self.provider.config.tier)
        if self._route_error:
            raise self._route_error
        return self._route_response

    async def route_stream(self, request: InferenceRequest, on_provider_selected=None, on_usage=None):
        self.stream_calls += 1
        if on_provider_selected:
            on_provider_selected(self.provider.name, self.provider.config.tier)
        if self._stream_error:
            raise self._stream_error
        for chunk in self._stream_chunks:
            yield chunk
        if self._stream_error_after_chunks:
            raise self._stream_error_after_chunks
        if self._stream_usage and on_usage:
            on_usage(*self._stream_usage)


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
        "metadata": {
            "budget": "standard",
            "latency_sla_ms": 1000,
            "caller_app": "test-app",
            "cache_policy": "shared",
            "data_classification": "public",
        },
    }


def _setup_app_state(router: FakeRouter, cache: FakeCache, limiter: FakeRateLimiter) -> None:
    gateway.app.state.router = router
    gateway.app.state.cache = cache
    gateway.app.state.rate_limiter = limiter
    gateway.app.state.usage_recorder = FakeUsageRecorder()
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
        gateway.app.state.usage_recorder = FakeUsageRecorder()
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
        gateway.app.state.usage_recorder = FakeUsageRecorder()
        resp = client.post("/v1/chat", json=_request_body())

    _teardown_app_state()
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["cache_hit"] is True
    assert payload["provider"] == "cache"
    assert payload["content"] == "from-cache"
    assert router.route_calls == 0
    assert limiter.calls == 1


def test_chat_preserves_opsdesk_trace_and_workflow_correlation():
    router = FakeRouter()
    cache = FakeCache(lookup_result=FakeCacheResult(response="from-cache", source="exact"))
    limiter = FakeRateLimiter()
    registry = FakeRegistry({"cheap-model": True})
    body = _request_body("correlated ticket")
    workflow_id = "12345678-1234-1234-1234-1234567890ab"
    traceparent = "00-1234567890abcdef1234567890abcdef-1234567890abcdef-01"
    body["metadata"]["workflow_id"] = workflow_id
    gateway.app.dependency_overrides[gateway.get_caller_identity] = _auth_override

    with (
        patch.object(gateway, "get_health_registry", return_value=registry),
        patch.object(gateway, "emit_request_metric", return_value=None) as metric,
        patch.object(gateway, "emit_error_metric", return_value=None),
        TestClient(gateway.app) as client,
    ):
        _setup_app_state(router, cache, limiter)
        response = client.post(
            "/v1/chat",
            headers={"traceparent": traceparent, "X-Workflow-ID": workflow_id},
            json=body,
        )

    _teardown_app_state()
    assert response.status_code == 200
    assert response.headers["traceparent"] == traceparent
    assert response.headers["X-Workflow-ID"] == workflow_id
    assert metric.call_args.kwargs["trace_id"] == "1234567890abcdef1234567890abcdef"
    assert metric.call_args.kwargs["workflow_id"] == workflow_id


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
        gateway.app.state.usage_recorder = FakeUsageRecorder()
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
        gateway.app.state.usage_recorder = FakeUsageRecorder()
        resp = client.post("/v1/chat", json=body)

    _teardown_app_state()
    assert resp.status_code == 200
    assert cache.lookups == [("user: pin me", "opus", "shared")]
    assert cache.writes[0]["model_constraint"] == "opus"


def test_chat_cache_off_skips_all_cache_operations():
    router = FakeRouter()
    cache = FakeCache(lookup_result=FakeCacheResult(response="must-not-be-used", source="exact"))
    limiter = FakeRateLimiter()
    registry = FakeRegistry({"cheap-model": True})
    body = _request_body("sensitive ticket")
    body["metadata"]["cache_policy"] = "off"
    body["metadata"]["data_classification"] = "restricted"

    gateway.app.dependency_overrides[gateway.get_caller_identity] = _auth_override
    with (
        patch.object(gateway, "get_health_registry", return_value=registry),
        patch.object(gateway, "emit_request_metric", return_value=None),
        patch.object(gateway, "emit_error_metric", return_value=None),
        TestClient(gateway.app) as client,
    ):
        _setup_app_state(router, cache, limiter)
        resp = client.post("/v1/chat", json=body)

    _teardown_app_state()
    assert resp.status_code == 200
    assert resp.json()["cache_policy"] == "off"
    assert cache.lookups == []
    assert cache.writes == []
    assert router.route_calls == 1


def test_chat_private_cache_is_scoped_to_authenticated_caller():
    router = FakeRouter()
    cache = FakeCache(lookup_result=None)
    limiter = FakeRateLimiter()
    registry = FakeRegistry({"cheap-model": True})
    body = _request_body("private ticket")
    body["metadata"]["cache_policy"] = "private"
    body["metadata"]["data_classification"] = "restricted"

    gateway.app.dependency_overrides[gateway.get_caller_identity] = _auth_override
    with (
        patch.object(gateway, "get_health_registry", return_value=registry),
        patch.object(gateway, "emit_request_metric", return_value=None),
        patch.object(gateway, "emit_error_metric", return_value=None),
        TestClient(gateway.app) as client,
    ):
        _setup_app_state(router, cache, limiter)
        resp = client.post("/v1/chat", json=body)

    _teardown_app_state()
    assert resp.status_code == 200
    assert cache.lookups == [("user: private ticket", "", "caller:test-caller")]
    assert cache.writes[0]["namespace"] == "caller:test-caller"
    assert resp.json()["cache_policy"] == "private"


def test_shared_cache_rejects_non_public_data():
    body = _request_body("not public")
    body["metadata"]["data_classification"] = "restricted"

    gateway.app.dependency_overrides[gateway.get_caller_identity] = _auth_override
    with TestClient(gateway.app) as client:
        resp = client.post("/v1/chat", json=body)

    _teardown_app_state()
    assert resp.status_code == 422


def test_caller_app_must_match_authenticated_identity():
    router = FakeRouter()
    cache = FakeCache()
    limiter = FakeRateLimiter()
    registry = FakeRegistry({"cheap-model": True})
    body = _request_body("spoofed caller")
    body["metadata"]["caller_app"] = "another-app"

    gateway.app.dependency_overrides[gateway.get_caller_identity] = _auth_override
    with (
        patch.object(gateway, "get_health_registry", return_value=registry),
        TestClient(gateway.app) as client,
    ):
        _setup_app_state(router, cache, limiter)
        resp = client.post("/v1/chat", json=body)

    _teardown_app_state()
    assert resp.status_code == 403
    assert resp.json()["detail"] == "caller_app does not match authenticated caller"


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
        gateway.app.state.usage_recorder = FakeUsageRecorder()
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
        gateway.app.state.usage_recorder = FakeUsageRecorder()
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
        gateway.app.state.usage_recorder = FakeUsageRecorder()
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


def test_stream_records_real_tokens_and_cost():
    router = FakeRouter(
        stream_chunks=["hello", "world"],
        model_id="test/cheap-model",
        stream_usage=(1000, 500),  # provider reports usage at stream end
    )
    cache = FakeCache(lookup_result=None)
    limiter = FakeRateLimiter()
    registry = FakeRegistry({"cheap-model": True})
    gateway.app.dependency_overrides[gateway.get_caller_identity] = _auth_override

    with (
        patch.object(gateway, "get_health_registry", return_value=registry),
        patch.object(gateway, "emit_request_metric", return_value=None) as metric,
        patch.object(gateway, "emit_error_metric", return_value=None),
        TestClient(gateway.app) as client,
    ):
        gateway.app.state.router = router
        gateway.app.state.cache = cache
        gateway.app.state.rate_limiter = limiter
        gateway.app.state.usage_recorder = FakeUsageRecorder()
        resp = client.post("/v1/chat/stream", json=_request_body("count my tokens"))

    _teardown_app_state()
    assert resp.status_code == 200
    kwargs = metric.call_args.kwargs
    assert kwargs["input_tokens"] == 1000
    assert kwargs["output_tokens"] == 500
    # cost = 1000 * 0.1/1M + 500 * 0.2/1M (FakeRouter provider config)
    assert kwargs["estimated_cost_usd"] == pytest.approx(0.0002)
    assert cache.writes[0]["input_tokens"] == 1000
    assert cache.writes[0]["output_tokens"] == 500


def test_chat_records_usage_per_caller():
    router = FakeRouter(
        route_response=ProviderResponse(
            content="from-provider",
            input_tokens=800,
            output_tokens=400,
            model_id="test/cheap-model",
            provider_name="cheap-model",
        )
    )
    cache = FakeCache(lookup_result=None)
    limiter = FakeRateLimiter()
    registry = FakeRegistry({"cheap-model": True})
    recorder = FakeUsageRecorder()
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
        gateway.app.state.usage_recorder = recorder
        resp = client.post("/v1/chat", json=_request_body("bill me"))

    _teardown_app_state()
    assert resp.status_code == 200
    assert len(recorder.records) == 1
    record = recorder.records[0]
    assert record["caller_id"] == "test-caller"
    assert record["input_tokens"] == 800
    assert record["output_tokens"] == 400
    assert record["estimated_cost_usd"] == pytest.approx(800 * 0.1e-6 + 400 * 0.2e-6)
    assert record["cache_hit"] is False


def test_chat_cache_hit_records_usage_as_cache_hit():
    router = FakeRouter()
    cache = FakeCache(lookup_result=FakeCacheResult(response="from-cache", source="exact"))
    limiter = FakeRateLimiter()
    registry = FakeRegistry({"cheap-model": True})
    recorder = FakeUsageRecorder()
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
        gateway.app.state.usage_recorder = recorder
        resp = client.post("/v1/chat", json=_request_body())

    _teardown_app_state()
    assert resp.status_code == 200
    assert len(recorder.records) == 1
    assert recorder.records[0]["cache_hit"] is True
    assert recorder.records[0]["estimated_cost_usd"] == 0.0


def test_usage_endpoint_returns_month_summary():
    router = FakeRouter()
    cache = FakeCache()
    limiter = FakeRateLimiter()
    registry = FakeRegistry({"cheap-model": True})
    recorder = FakeUsageRecorder(
        summary={
            "month": "2026-07",
            "request_count": 42,
            "cache_hits": 10,
            "input_tokens": 5000,
            "output_tokens": 2500,
            "estimated_cost_usd": 0.0375,
        }
    )
    gateway.app.dependency_overrides[gateway.get_caller_identity] = _auth_override

    with (
        patch.object(gateway, "get_health_registry", return_value=registry),
        TestClient(gateway.app) as client,
    ):
        gateway.app.state.router = router
        gateway.app.state.cache = cache
        gateway.app.state.rate_limiter = limiter
        gateway.app.state.usage_recorder = recorder
        resp = client.get("/v1/usage")

    _teardown_app_state()
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["caller_id"] == "test-caller"
    assert payload["month"] == "2026-07"
    assert payload["request_count"] == 42
    assert payload["cache_hits"] == 10
    assert payload["estimated_cost_usd"] == pytest.approx(0.0375)


def test_usage_endpoint_returns_503_when_store_unavailable():
    router = FakeRouter()
    cache = FakeCache()
    limiter = FakeRateLimiter()
    registry = FakeRegistry({"cheap-model": True})
    recorder = FakeUsageRecorder(error=RuntimeError("dynamo down"))
    gateway.app.dependency_overrides[gateway.get_caller_identity] = _auth_override

    with (
        patch.object(gateway, "get_health_registry", return_value=registry),
        TestClient(gateway.app) as client,
    ):
        gateway.app.state.router = router
        gateway.app.state.cache = cache
        gateway.app.state.rate_limiter = limiter
        gateway.app.state.usage_recorder = recorder
        resp = client.get("/v1/usage")

    _teardown_app_state()
    assert resp.status_code == 503
    assert resp.json()["code"] == "usage_unavailable"


def test_stream_interrupted_mid_stream_emits_error_and_skips_cache():
    router = FakeRouter(
        stream_chunks=["partial output"],
        stream_error_after_chunks=StreamInterruptedError("provider died mid-stream"),
    )
    cache = FakeCache(lookup_result=None)
    limiter = FakeRateLimiter()
    registry = FakeRegistry({"cheap-model": True})
    gateway.app.dependency_overrides[gateway.get_caller_identity] = _auth_override

    with (
        patch.object(gateway, "get_health_registry", return_value=registry),
        patch.object(gateway, "emit_request_metric", return_value=None),
        patch.object(gateway, "emit_error_metric", return_value=None) as error_metric,
        TestClient(gateway.app) as client,
    ):
        gateway.app.state.router = router
        gateway.app.state.cache = cache
        gateway.app.state.rate_limiter = limiter
        gateway.app.state.usage_recorder = FakeUsageRecorder()
        resp = client.post("/v1/chat/stream", json=_request_body("interrupt me"))

    _teardown_app_state()
    assert resp.status_code == 200  # headers already sent when the stream died
    assert "data: partial output" in resp.text
    assert "data: [ERROR] Stream interrupted" in resp.text
    assert "data: [DONE]" not in resp.text
    assert cache.writes == []  # partial output must never be cached
    assert error_metric.call_args.kwargs["error_type"] == "stream_interrupted"
