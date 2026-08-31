"""
FastAPI application — Lambda entry point via Mangum.
All platform middleware and routing is wired here.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
import uuid
from contextlib import asynccontextmanager
from typing import Annotated, Literal
from urllib.parse import quote

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from mangum import Mangum

from ..accounting.usage_recorder import UsageRecorder
from ..auth.authenticator import Authenticator, CallerIdentity, get_caller_identity
from ..auth.rate_limiter import RateLimiter
from ..cache.semantic_cache import SemanticCache
from ..config.settings import get_settings
from ..metrics.emitter import emit_error_metric, emit_request_metric
from ..models.schemas import (
    CachePolicy,
    ErrorResponse,
    HealthResponse,
    InferenceRequest,
    InferenceResponse,
    UsageStats,
    UsageSummary,
)
from ..providers.anthropic_provider import AnthropicProvider, haiku_config, opus_config, sonnet_config
from ..providers.base import BaseProvider
from ..providers.bedrock_provider import BedrockProvider, bedrock_haiku_config, nova_micro_config
from ..providers.openai_provider import OpenAIProvider, gpt4o_config, gpt4o_mini_config
from ..router.health import get_health_registry
from ..router.router import LLMRouter, StreamInterruptedError
from ..utils import fetch_secret, fetch_secret_value

logging.basicConfig(
    level=logging.INFO,
    format='{"time":"%(asctime)s","level":"%(levelname)s","name":"%(name)s","message":%(message)s}',
)
logger = logging.getLogger(__name__)

TRACEPARENT_PATTERN = re.compile(r"^00-(?!0{32})[0-9a-f]{32}-(?!0{16})[0-9a-f]{16}-[0-9a-f]{2}$")
WORKFLOW_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$")


def _resolve_pg_dsn(settings) -> str:
    """
    Build the asyncpg DSN from the RDS-managed master user secret.
    The secret JSON produced by Aurora contains: username, password, host, port, dbname.
    Falls back to the direct pg_dsn env var if already set.
    """
    if settings.pg_dsn:
        return settings.pg_dsn
    if not settings.pg_secret_arn:
        return ""
    try:
        raw = fetch_secret_value(settings.pg_secret_arn)
        creds = json.loads(raw)
        host = creds.get("host") or settings.pg_host
        if not host:
            raise ValueError("PostgreSQL host is not configured")
        port = creds.get("port", settings.pg_port)
        user = creds["username"]
        password = creds["password"]
        dbname = creds.get("dbname", settings.pg_database)
        dsn = (
            f"postgresql://{quote(user, safe='')}:{quote(password, safe='')}"
            f"@{host}:{port}/{quote(dbname, safe='')}"
        )
        logger.info("pg_dsn_resolved_from_secret")
        return dsn
    except Exception as exc:
        logger.error("pg_secret_fetch_failed", extra={"error": str(exc)})
        return ""


def _all_providers(router: LLMRouter) -> list[BaseProvider]:
    providers: list[BaseProvider] = []
    for tier_providers in router._tiers.values():
        providers.extend(tier_providers)
    return providers


def _find_provider(router: LLMRouter, provider_name: str) -> BaseProvider | None:
    return next((provider for provider in _all_providers(router) if provider.name == provider_name), None)


def _validate_caller_metadata(body: InferenceRequest, caller: CallerIdentity) -> None:
    if caller.app_name == "local-dev":
        return
    caller_app = body.metadata.caller_app
    if caller_app != "unknown" and caller_app != caller.app_name:
        raise HTTPException(status_code=403, detail="caller_app does not match authenticated caller")


def _correlation_context(request: Request, body: InferenceRequest) -> tuple[str | None, str | None]:
    header_workflow_id = request.state.workflow_id
    body_workflow_id = body.metadata.workflow_id
    if header_workflow_id and body_workflow_id and header_workflow_id != body_workflow_id:
        raise HTTPException(status_code=400, detail="workflow correlation identifiers do not match")
    return request.state.trace_id, header_workflow_id or body_workflow_id


def _cache_namespace(body: InferenceRequest, caller: CallerIdentity) -> str | None:
    if body.metadata.cache_policy == CachePolicy.OFF:
        return None
    if body.metadata.cache_policy == CachePolicy.PRIVATE:
        return f"caller:{caller.caller_id}"
    return "shared"


async def _safe_cache_write(
    cache: SemanticCache,
    *,
    prompt: str,
    response: str,
    model_used: str,
    input_tokens: int,
    output_tokens: int,
    model_constraint: str = "",
    namespace: str,
) -> None:
    settings = get_settings()
    timeout_seconds = max(settings.cache_write_timeout_ms, 1) / 1000

    try:
        await asyncio.wait_for(
            cache.write(
                prompt=prompt,
                response=response,
                model_used=model_used,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                model_constraint=model_constraint,
                namespace=namespace,
            ),
            timeout=timeout_seconds,
        )
    except TimeoutError:
        logger.warning(
            "cache_write_timeout",
            extra={"model_used": model_used, "timeout_ms": settings.cache_write_timeout_ms},
        )
    except Exception as exc:
        logger.warning("cache_write_failed", extra={"error": str(exc), "model_used": model_used})


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Initialize shared resources at cold start."""
    settings = get_settings()

    # Resolve Aurora DSN — passed directly to SemanticCache below; the cached
    # Settings object cannot pick it up from the environment after construction.
    pg_dsn = _resolve_pg_dsn(settings)

    # Resolve API keys — prefer direct env var, fall back to Secrets Manager ARN
    anthropic_key = settings.anthropic_api_key if settings.anthropic_enabled else ""
    if settings.anthropic_enabled and not anthropic_key and settings.anthropic_secret_arn:
        try:
            anthropic_key = fetch_secret(settings.anthropic_secret_arn)
            logger.info("anthropic_key_loaded_from_secrets_manager")
        except Exception as exc:
            logger.error("anthropic_secret_fetch_failed", extra={"error": str(exc)})

    openai_key = settings.openai_api_key if settings.openai_enabled else ""
    if settings.openai_enabled and not openai_key and settings.openai_secret_arn:
        try:
            openai_key = fetch_secret(settings.openai_secret_arn)
            logger.info("openai_key_loaded_from_secrets_manager")
        except Exception as exc:
            logger.error("openai_secret_fetch_failed", extra={"error": str(exc)})

    # Build providers
    anthropic_providers = []
    if settings.anthropic_enabled and anthropic_key:
        anthropic_providers = [
            AnthropicProvider(haiku_config(), anthropic_key),
            AnthropicProvider(sonnet_config(), anthropic_key),
            AnthropicProvider(opus_config(), anthropic_key),
        ]

    openai_providers = []
    if settings.openai_enabled and openai_key:
        openai_providers = [
            OpenAIProvider(gpt4o_mini_config(), openai_key),
            OpenAIProvider(gpt4o_config(), openai_key),
        ]

    bedrock_providers = []
    if settings.bedrock_enabled:
        bedrock_providers = [
            BedrockProvider(nova_micro_config()),
            BedrockProvider(bedrock_haiku_config()),
        ]

    providers_by_tier = {
        "low": [*bedrock_providers, *(p for p in anthropic_providers if p.tier == "low")],
        "mid": [
            *(p for p in anthropic_providers if p.tier == "mid"),
            *(p for p in openai_providers if p.tier == "mid"),
        ],
        "high": [
            *(p for p in anthropic_providers if p.tier == "high"),
            *(p for p in openai_providers if p.tier == "high"),
        ],
    }

    if not any(providers_by_tier.values()):
        raise RuntimeError("At least one configured LLM provider must be enabled")

    app.state.router = LLMRouter(providers_by_tier)
    app.state.cache = SemanticCache(pg_dsn=pg_dsn)
    app.state.authenticator = Authenticator()
    app.state.rate_limiter = RateLimiter()
    app.state.usage_recorder = UsageRecorder()

    # Warm provider health registry
    get_health_registry().refresh()

    yield


app = FastAPI(
    title="AI Platform Gateway",
    version="1.0.0",
    docs_url=None,  # Disable Swagger UI in production
    redoc_url=None,
    lifespan=lifespan,
)


@app.middleware("http")
async def request_id_middleware(request: Request, call_next):
    request_id = request.headers.get("X-Request-ID") or str(uuid.uuid4())
    request.state.request_id = request_id
    traceparent = request.headers.get("traceparent", "")
    request.state.traceparent = traceparent if TRACEPARENT_PATTERN.fullmatch(traceparent) else None
    request.state.trace_id = traceparent[3:35] if request.state.traceparent else None
    workflow_id = request.headers.get("X-Workflow-ID", "")
    request.state.workflow_id = workflow_id if WORKFLOW_ID_PATTERN.fullmatch(workflow_id) else None
    response = await call_next(request)
    response.headers["X-Request-ID"] = request_id
    if request.state.traceparent:
        response.headers["traceparent"] = request.state.traceparent
    if request.state.workflow_id:
        response.headers["X-Workflow-ID"] = request.state.workflow_id
    return response


@app.get("/health", response_model=HealthResponse)
async def health_check(request: Request) -> HealthResponse:
    registry = get_health_registry()
    registry.refresh()

    provider_statuses = {}
    for provider in _all_providers(request.app.state.router):
        provider_statuses[provider.name] = registry.is_healthy(provider.name)

    healthy_count = sum(provider_statuses.values())
    total_count = len(provider_statuses)
    status: Literal["ok", "degraded", "unhealthy"]
    if healthy_count == 0:
        status = "unhealthy"
    elif healthy_count < total_count:
        status = "degraded"
    else:
        status = "ok"

    return HealthResponse(
        status=status,
        providers=provider_statuses,
        cache_available=get_settings().cache_enabled,
    )


@app.post("/v1/chat", response_model=InferenceResponse)
async def chat_completion(
    request: Request,
    body: InferenceRequest,
    caller: Annotated[CallerIdentity, Depends(get_caller_identity)],
) -> InferenceResponse | JSONResponse:
    request_id = request.state.request_id
    trace_id, workflow_id = _correlation_context(request, body)
    start_time = time.perf_counter()

    _validate_caller_metadata(body, caller)

    # Rate limit check
    await request.app.state.rate_limiter.check_and_increment(caller)

    cache: SemanticCache = request.app.state.cache
    router: LLMRouter = request.app.state.router
    model_constraint = body.model_preference or ""
    cache_namespace = _cache_namespace(body, caller)

    # ── Cache lookup ──────────────────────────────────────────────────────────
    cached = None
    if cache_namespace is not None:
        cached = await cache.lookup(
            body.prompt_text,
            model_constraint=model_constraint,
            namespace=cache_namespace,
        )
    if cached:
        latency_ms = int((time.perf_counter() - start_time) * 1000)
        emit_request_metric(
            request_id=request_id,
            caller_id=caller.caller_id,
            provider="cache",
            model=cached.model_used or "cached",
            tier="cache",
            input_tokens=0,
            output_tokens=0,
            latency_ms=latency_ms,
            cache_hit=True,
            cache_source=cached.source,
            status_code=200,
            estimated_cost_usd=0.0,
            trace_id=trace_id,
            workflow_id=workflow_id,
        )
        await request.app.state.usage_recorder.record(caller.caller_id, cache_hit=True)
        return InferenceResponse(
            request_id=request_id,
            model_used=cached.model_used or "cached",
            provider="cache",
            content=cached.response,
            usage=UsageStats(),
            cache_hit=True,
            cache_source=cached.source,
            cache_policy=body.metadata.cache_policy,
            latency_ms=latency_ms,
        )

    # ── Route to LLM ──────────────────────────────────────────────────────────
    selected_provider_name = ["unknown"]
    selected_tier = ["unknown"]

    def on_provider_selected(name: str, tier: str) -> None:
        selected_provider_name[0] = name
        selected_tier[0] = tier

    try:
        provider_response = await router.route(body, on_provider_selected=on_provider_selected)
    except RuntimeError as exc:
        latency_ms = int((time.perf_counter() - start_time) * 1000)
        emit_error_metric(
            request_id=request_id,
            caller_id=caller.caller_id,
            error_type="all_providers_failed",
            status_code=503,
            trace_id=trace_id,
            workflow_id=workflow_id,
        )
        logger.error(
            "all_providers_failed",
            extra={
                "error": str(exc),
                "request_id": request_id,
                "trace_id": trace_id,
                "workflow_id": workflow_id,
            },
        )
        return JSONResponse(
            status_code=503,
            content=ErrorResponse(
                request_id=request_id,
                error="All LLM providers failed. Please retry.",
                code="provider_unavailable",
            ).model_dump(),
        )

    latency_ms = int((time.perf_counter() - start_time) * 1000)

    # Find the provider config to compute cost
    provider_obj = _find_provider(router, selected_provider_name[0])
    cost = 0.0
    if provider_obj:
        cost = provider_response.estimated_cost(provider_obj.config)

    if cache_namespace is not None:
        await _safe_cache_write(
            cache,
            prompt=body.prompt_text,
            response=provider_response.content,
            model_used=provider_response.model_id,
            input_tokens=provider_response.input_tokens,
            output_tokens=provider_response.output_tokens,
            model_constraint=model_constraint,
            namespace=cache_namespace,
        )

    emit_request_metric(
        request_id=request_id,
        caller_id=caller.caller_id,
        provider=provider_response.provider_name,
        model=provider_response.model_id,
        tier=selected_tier[0],
        input_tokens=provider_response.input_tokens,
        output_tokens=provider_response.output_tokens,
        latency_ms=latency_ms,
        cache_hit=False,
        cache_source="none",
        status_code=200,
        estimated_cost_usd=cost,
        trace_id=trace_id,
        workflow_id=workflow_id,
    )

    await request.app.state.usage_recorder.record(
        caller.caller_id,
        input_tokens=provider_response.input_tokens,
        output_tokens=provider_response.output_tokens,
        estimated_cost_usd=cost,
    )

    return InferenceResponse(
        request_id=request_id,
        model_used=provider_response.model_id,
        provider=provider_response.provider_name,
        content=provider_response.content,
        usage=UsageStats(
            input_tokens=provider_response.input_tokens,
            output_tokens=provider_response.output_tokens,
            total_tokens=provider_response.total_tokens,
            estimated_cost_usd=round(cost, 6),
        ),
        cache_hit=False,
        cache_policy=body.metadata.cache_policy,
        latency_ms=latency_ms,
    )


@app.post("/v1/chat/stream")
async def chat_completion_stream(
    request: Request,
    body: InferenceRequest,
    caller: Annotated[CallerIdentity, Depends(get_caller_identity)],
) -> StreamingResponse:
    """
    Server-Sent Events streaming endpoint.
    Each token is emitted as: data: <token>\n\n
    The final event is: data: [DONE]\n\n
    """
    request_id = request.state.request_id
    trace_id, workflow_id = _correlation_context(request, body)
    start_time = time.perf_counter()

    _validate_caller_metadata(body, caller)

    await request.app.state.rate_limiter.check_and_increment(caller)

    cache: SemanticCache = request.app.state.cache
    router: LLMRouter = request.app.state.router
    model_constraint = body.model_preference or ""
    cache_namespace = _cache_namespace(body, caller)

    # Serve exact/semantic cache hits as a single synthetic SSE event
    cached = None
    if cache_namespace is not None:
        cached = await cache.lookup(
            body.prompt_text,
            model_constraint=model_constraint,
            namespace=cache_namespace,
        )
    if cached:
        latency_ms = int((time.perf_counter() - start_time) * 1000)
        emit_request_metric(
            request_id=request_id,
            caller_id=caller.caller_id,
            provider="cache",
            model=cached.model_used or "cached",
            tier="cache",
            input_tokens=0,
            output_tokens=0,
            latency_ms=latency_ms,
            cache_hit=True,
            cache_source=cached.source,
            status_code=200,
            estimated_cost_usd=0.0,
            trace_id=trace_id,
            workflow_id=workflow_id,
        )

        await request.app.state.usage_recorder.record(caller.caller_id, cache_hit=True)

        async def _cached_sse():
            yield f"data: {cached.response}\n\n"
            yield "data: [DONE]\n\n"

        return StreamingResponse(
            _cached_sse(),
            media_type="text/event-stream",
            headers={"X-Request-ID": request_id, "Cache-Control": "no-cache"},
        )

    selected_provider_name = ["unknown"]
    selected_tier = ["unknown"]
    stream_usage = {"input_tokens": 0, "output_tokens": 0}

    def on_provider_selected(name: str, tier: str) -> None:
        selected_provider_name[0] = name
        selected_tier[0] = tier

    def on_usage(input_tokens: int, output_tokens: int) -> None:
        stream_usage["input_tokens"] = input_tokens
        stream_usage["output_tokens"] = output_tokens

    async def _sse_generator():
        streamed_chunks: list[str] = []
        try:
            async for chunk in router.route_stream(
                body,
                on_provider_selected=on_provider_selected,
                on_usage=on_usage,
            ):
                streamed_chunks.append(chunk)
                # Escape newlines inside the chunk so SSE framing is not broken
                escaped = chunk.replace("\n", "\\n")
                yield f"data: {escaped}\n\n"
        except StreamInterruptedError as exc:
            # Provider died after chunks reached the client — the partial
            # output must not be cached and cannot be transparently retried.
            emit_error_metric(
                request_id=request_id,
                caller_id=caller.caller_id,
                error_type="stream_interrupted",
                status_code=502,
                trace_id=trace_id,
                workflow_id=workflow_id,
            )
            logger.error("stream_interrupted", extra={"error": str(exc), "request_id": request_id})
            yield "data: [ERROR] Stream interrupted\n\n"
            return
        except RuntimeError as exc:
            emit_error_metric(
                request_id=request_id,
                caller_id=caller.caller_id,
                error_type="all_providers_failed",
                status_code=503,
                trace_id=trace_id,
                workflow_id=workflow_id,
            )
            logger.error("stream_all_providers_failed", extra={"error": str(exc), "request_id": request_id})
            yield "data: [ERROR] All providers failed\n\n"
            return

        latency_ms = int((time.perf_counter() - start_time) * 1000)
        provider_obj = _find_provider(router, selected_provider_name[0])
        model_id = provider_obj.config.model_id if provider_obj else selected_provider_name[0]
        input_tokens = stream_usage["input_tokens"]
        output_tokens = stream_usage["output_tokens"]
        cost = 0.0
        if provider_obj:
            cost = (
                input_tokens * provider_obj.config.cost_per_input_token
                + output_tokens * provider_obj.config.cost_per_output_token
            )
        if cache_namespace is not None:
            await _safe_cache_write(
                cache,
                prompt=body.prompt_text,
                response="".join(streamed_chunks),
                model_used=model_id,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                model_constraint=model_constraint,
                namespace=cache_namespace,
            )
        emit_request_metric(
            request_id=request_id,
            caller_id=caller.caller_id,
            provider=selected_provider_name[0],
            model=model_id,
            tier=selected_tier[0],
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            latency_ms=latency_ms,
            cache_hit=False,
            cache_source="none",
            status_code=200,
            estimated_cost_usd=cost,
            trace_id=trace_id,
            workflow_id=workflow_id,
        )
        await request.app.state.usage_recorder.record(
            caller.caller_id,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            estimated_cost_usd=cost,
        )
        yield "data: [DONE]\n\n"

    return StreamingResponse(
        _sse_generator(),
        media_type="text/event-stream",
        headers={"X-Request-ID": request_id, "Cache-Control": "no-cache"},
    )


@app.get("/v1/usage", response_model=UsageSummary)
async def usage_summary(
    request: Request,
    caller: Annotated[CallerIdentity, Depends(get_caller_identity)],
) -> UsageSummary | JSONResponse:
    """Current-month usage and estimated spend for the calling API key."""
    recorder: UsageRecorder = request.app.state.usage_recorder
    try:
        summary = await recorder.month_summary(caller.caller_id)
    except Exception as exc:
        logger.error(
            "usage_query_failed",
            extra={"error": str(exc), "caller_id": caller.caller_id},
        )
        return JSONResponse(
            status_code=503,
            content=ErrorResponse(
                request_id=request.state.request_id,
                error="Usage data temporarily unavailable.",
                code="usage_unavailable",
            ).model_dump(),
        )
    return UsageSummary(caller_id=caller.caller_id, **summary)


# Lambda handler
handler = Mangum(app, lifespan="on")
