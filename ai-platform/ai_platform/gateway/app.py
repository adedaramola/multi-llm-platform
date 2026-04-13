"""
FastAPI application — Lambda entry point via Mangum.
All platform middleware and routing is wired here.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
from contextlib import asynccontextmanager
from typing import Annotated

import boto3

from fastapi import Depends, FastAPI, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse
from mangum import Mangum

from ..auth.authenticator import Authenticator, CallerIdentity, get_caller_identity
from ..auth.rate_limiter import RateLimiter
from ..cache.semantic_cache import SemanticCache
from ..config.settings import get_settings
from ..metrics.emitter import emit_error_metric, emit_request_metric
from ..models.schemas import (
    ErrorResponse,
    HealthResponse,
    InferenceRequest,
    InferenceResponse,
    UsageStats,
)
from ..providers.anthropic_provider import AnthropicProvider, haiku_config, opus_config, sonnet_config
from ..providers.bedrock_provider import BedrockProvider, bedrock_haiku_config, nova_micro_config
from ..providers.openai_provider import OpenAIProvider, gpt4o_config, gpt4o_mini_config
from ..router.health import get_health_registry
from ..router.router import LLMRouter
from ..utils import fetch_secret

logging.basicConfig(
    level=logging.INFO,
    format='{"time":"%(asctime)s","level":"%(levelname)s","name":"%(name)s","message":%(message)s}',
)
logger = logging.getLogger(__name__)


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
        raw = fetch_secret(settings.pg_secret_arn)
        creds = json.loads(raw)
        host = creds["host"]
        port = creds.get("port", 5432)
        user = creds["username"]
        password = creds["password"]
        dbname = creds.get("dbname", "ai_platform")
        dsn = f"postgresql://{user}:{password}@{host}:{port}/{dbname}"
        logger.info("pg_dsn_resolved_from_secret")
        return dsn
    except Exception as exc:
        logger.error("pg_secret_fetch_failed", extra={"error": str(exc)})
        return ""


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Initialize shared resources at cold start."""
    settings = get_settings()

    # Resolve Aurora DSN — inject into environment before SemanticCache reads settings
    pg_dsn = _resolve_pg_dsn(settings)
    if pg_dsn and not settings.pg_dsn:
        os.environ["PG_DSN"] = pg_dsn

    # Resolve API keys — prefer direct env var, fall back to Secrets Manager ARN
    anthropic_key = settings.anthropic_api_key
    if not anthropic_key and settings.anthropic_secret_arn:
        try:
            anthropic_key = fetch_secret(settings.anthropic_secret_arn)
            logger.info("anthropic_key_loaded_from_secrets_manager")
        except Exception as exc:
            logger.error("anthropic_secret_fetch_failed", extra={"error": str(exc)})

    openai_key = settings.openai_api_key
    if not openai_key and settings.openai_secret_arn:
        try:
            openai_key = fetch_secret(settings.openai_secret_arn)
            logger.info("openai_key_loaded_from_secrets_manager")
        except Exception as exc:
            logger.error("openai_secret_fetch_failed", extra={"error": str(exc)})

    # Build providers
    anthropic_providers = []
    if anthropic_key:
        anthropic_providers = [
            AnthropicProvider(haiku_config(), anthropic_key),
            AnthropicProvider(sonnet_config(), anthropic_key),
            AnthropicProvider(opus_config(), anthropic_key),
        ]

    openai_providers = []
    if openai_key:
        openai_providers = [
            OpenAIProvider(gpt4o_mini_config(), openai_key),
            OpenAIProvider(gpt4o_config(), openai_key),
        ]

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

    app.state.router = LLMRouter(providers_by_tier)
    app.state.cache = SemanticCache()
    app.state.authenticator = Authenticator()
    app.state.rate_limiter = RateLimiter()

    # Warm provider health registry
    get_health_registry().refresh()

    yield


app = FastAPI(
    title="AI Platform Gateway",
    version="1.0.0",
    docs_url=None,    # Disable Swagger UI in production
    redoc_url=None,
    lifespan=lifespan,
)


@app.middleware("http")
async def request_id_middleware(request: Request, call_next):
    request_id = request.headers.get("X-Request-ID") or str(uuid.uuid4())
    request.state.request_id = request_id
    response = await call_next(request)
    response.headers["X-Request-ID"] = request_id
    return response


@app.get("/health", response_model=HealthResponse)
async def health_check(request: Request) -> HealthResponse:
    registry = get_health_registry()
    registry.refresh()

    provider_statuses = {}
    all_providers = []
    for tier_providers in request.app.state.router._tiers.values():
        all_providers.extend(tier_providers)
    for provider in all_providers:
        provider_statuses[provider.name] = registry.is_healthy(provider.name)

    healthy_count = sum(provider_statuses.values())
    total_count = len(provider_statuses)
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
    start_time = time.perf_counter()

    # Rate limit check
    await request.app.state.rate_limiter.check_and_increment(caller)

    cache: SemanticCache = request.app.state.cache
    router: LLMRouter = request.app.state.router

    # ── Cache lookup ──────────────────────────────────────────────────────────
    cached = await cache.lookup(body.prompt_text)
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
        )
        return InferenceResponse(
            request_id=request_id,
            model_used=cached.model_used or "cached",
            provider="cache",
            content=cached.response,
            usage=UsageStats(),
            cache_hit=True,
            cache_source=cached.source,
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
        )
        logger.error("all_providers_failed", extra={"error": str(exc), "request_id": request_id})
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
    all_providers = []
    for tp in router._tiers.values():
        all_providers.extend(tp)
    provider_obj = next((p for p in all_providers if p.name == selected_provider_name[0]), None)
    cost = 0.0
    if provider_obj:
        cost = provider_response.estimated_cost(provider_obj.config)

    # ── Async cache write (fire and forget) ───────────────────────────────────
    asyncio.create_task(
        cache.write(
            prompt=body.prompt_text,
            response=provider_response.content,
            model_used=provider_response.model_id,
            input_tokens=provider_response.input_tokens,
            output_tokens=provider_response.output_tokens,
        )
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
    start_time = time.perf_counter()

    await request.app.state.rate_limiter.check_and_increment(caller)

    cache: SemanticCache = request.app.state.cache
    router: LLMRouter = request.app.state.router

    # Serve exact/semantic cache hits as a single synthetic SSE event
    cached = await cache.lookup(body.prompt_text)
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
        )

        async def _cached_sse():
            yield f"data: {cached.response}\n\n"
            yield "data: [DONE]\n\n"

        return StreamingResponse(_cached_sse(), media_type="text/event-stream")

    selected_provider_name = ["unknown"]
    selected_tier = ["unknown"]

    def on_provider_selected(name: str, tier: str) -> None:
        selected_provider_name[0] = name
        selected_tier[0] = tier

    async def _sse_generator():
        try:
            async for chunk in router.route_stream(body, on_provider_selected=on_provider_selected):
                # Escape newlines inside the chunk so SSE framing is not broken
                escaped = chunk.replace("\n", "\\n")
                yield f"data: {escaped}\n\n"
        except RuntimeError as exc:
            emit_error_metric(
                request_id=request_id,
                caller_id=caller.caller_id,
                error_type="all_providers_failed",
                status_code=503,
            )
            logger.error("stream_all_providers_failed", extra={"error": str(exc), "request_id": request_id})
            yield "data: [ERROR] All providers failed\n\n"
            return

        latency_ms = int((time.perf_counter() - start_time) * 1000)
        emit_request_metric(
            request_id=request_id,
            caller_id=caller.caller_id,
            provider=selected_provider_name[0],
            model=selected_provider_name[0],
            tier=selected_tier[0],
            input_tokens=0,  # streaming — token counts not available mid-stream
            output_tokens=0,
            latency_ms=latency_ms,
            cache_hit=False,
            cache_source="none",
            status_code=200,
            estimated_cost_usd=0.0,
        )
        yield "data: [DONE]\n\n"

    return StreamingResponse(
        _sse_generator(),
        media_type="text/event-stream",
        headers={"X-Request-ID": request_id, "Cache-Control": "no-cache"},
    )


# Lambda handler
handler = Mangum(app, lifespan="on")
