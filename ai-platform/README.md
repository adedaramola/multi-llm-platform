# AI Platform Service

This directory contains the Python application behind the Multi-LLM Platform. FastAPI handles the HTTP API, Mangum adapts it to AWS Lambda, and the router coordinates providers, caching, authentication, rate limits, health, usage, and metrics.

For API examples and a platform overview, see the [root README](../README.md). For AWS deployment instructions, see the [implementation guide](../IMPLEMENTATION_GUIDE.md).

## Directory map

```text
ai-platform/
├── ai_platform/
│   ├── accounting/    Per-caller usage aggregation
│   ├── auth/          API-key authentication and rate limiting
│   ├── cache/         Redis exact cache and pgvector semantic cache
│   ├── config/        Environment-driven application settings
│   ├── gateway/       FastAPI routes, middleware, and application lifecycle
│   ├── metrics/       CloudWatch Embedded Metric Format emission
│   ├── models/        Pydantic request and response schemas
│   ├── providers/     Bedrock, Anthropic, and OpenAI adapters
│   ├── router/        Complexity scoring, tier selection, and health state
│   ├── health_checker.py
│   └── utils.py
├── tests/
├── .env.example
├── Dockerfile
├── pyproject.toml
├── requirements.txt
└── requirements-dev.txt
```

## Local development

Python 3.12 and AWS credentials for Bedrock are required. Anthropic and OpenAI keys enable their respective providers.

```bash
cp .env.example .env
```

For a minimal local configuration:

```dotenv
ENVIRONMENT=dev
CACHE_ENABLED=false
ANTHROPIC_API_KEY=sk-ant-...
OPENAI_API_KEY=sk-...
```

Install the development dependencies and start the API:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt
uvicorn ai_platform.gateway.app:app --reload --port 8080
```

In `dev`, any non-empty bearer token is accepted so DynamoDB is not required for local authentication:

```bash
curl -X POST http://localhost:8080/v1/chat \
  -H "Authorization: Bearer dev-key" \
  -H "Content-Type: application/json" \
  -d '{"messages":[{"role":"user","content":"Hello"}]}'
```

Caching is optional locally. When `CACHE_ENABLED=false`, Redis and PostgreSQL connections are not required.

## Containers

From the repository root, start the production-style image with local development settings:

```bash
docker compose up --build
curl http://localhost:8080/health
```

The root `compose.yaml` loads `ai-platform/.env` when present and disables caching by default. The image
runs as an unprivileged user, includes an HTTP health check, and accepts a `PORT` environment variable.

## Quality checks

Run all checks before opening a pull request:

```bash
source .venv/bin/activate
ruff check ai_platform tests
mypy
python -m pytest tests/ -v --tb=short
```

Pytest configuration and the coverage gate are defined in `pyproject.toml`. Tests mock external providers and AWS dependencies; they should not make paid inference calls.

## Request lifecycle

For authenticated inference requests, the gateway:

1. Validates the bearer token and applies the caller's rate limits.
2. Checks Redis for an exact cache match.
3. Checks pgvector for a sufficiently similar prompt.
4. Scores request complexity and selects an eligible provider tier.
5. Calls providers with timeout, retry, health, and fallback handling.
6. Records usage and CloudWatch metrics.
7. Persists successful responses to the cache.

Streaming follows the same routing path. Provider chunks are emitted as SSE events, final usage is recorded, and partial failed streams are not cached.

## Configuration

`ai_platform/config/settings.py` is the source of truth for runtime settings. Pydantic Settings reads uppercase environment variables and `.env` values, such as:

- `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, and their Secrets Manager ARN alternatives
- `REDIS_URL`, `PG_DSN`, and `CACHE_ENABLED`
- DynamoDB table names and default rate limits
- Complexity thresholds, provider timeout, and retry count
- Circuit-breaker threshold and cooldown
- Semantic-cache threshold and embedding model

Settings are cached once per process. Tests that change environment variables must clear `get_settings()` between configurations.

Production credentials should be resolved through Secrets Manager. Direct API-key environment variables are intended primarily for local development.

## Adding a provider

1. Add an adapter under `ai_platform/providers/` that implements `BaseProvider`.
2. Define a `ProviderConfig` with its name, model ID, tier, token costs, limits, and priority.
3. Implement `complete()`, `stream()`, and `health_check()` without blocking the event loop.
4. Instantiate the provider during the lifespan in `ai_platform/gateway/app.py` and add it to the appropriate tier.
5. Add configuration or secret resolution if the provider requires credentials.
6. Add tests for completion, streaming usage, errors, health checks, timeout, and fallback behavior.

Provider SDKs that expose synchronous methods must be offloaded with `asyncio.get_running_loop().run_in_executor()` or an equivalent non-blocking mechanism.

## Application conventions

- Validate external input with the models in `ai_platform/models/`.
- Keep provider SDK details behind `BaseProvider`.
- Use parameterized SQL; never interpolate values into statements.
- Use structured logs such as `logger.info("event", extra={"key": value})`.
- Put tunable behavior in `Settings`, not business-logic constants.
- Put shared helpers in `ai_platform/utils.py` rather than duplicating them.
- Do not perform blocking network or SDK calls directly inside `async def`.

## Lambda packaging

The deployed functions use Python 3.12 on `linux/arm64`. Native dependencies must be installed inside the matching Lambda container image. Do not deploy packages built directly on macOS or x86 Linux.

The canonical build and deployment commands are maintained in the [implementation guide](../IMPLEMENTATION_GUIDE.md#build-the-lambda-package).
