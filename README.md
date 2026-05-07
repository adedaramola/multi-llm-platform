# Multi-LLM Platform

A production-grade AI gateway that routes requests across multiple LLM providers (Anthropic Claude, OpenAI, AWS Bedrock) with cost-aware routing, two-layer semantic caching, DynamoDB-backed auth, and full observability — deployed on AWS Lambda via Terraform.

---

## Table of Contents

- [Architecture](#architecture)
- [Features](#features)
- [Project Structure](#project-structure)
- [API Reference](#api-reference)
- [Local Development](#local-development)
- [Configuration](#configuration)
- [Deployment](#deployment)
- [Observability](#observability)
- [Roadmap](#roadmap)

---

## Architecture

```
Client → API Gateway (HTTP v2) → Lambda (FastAPI/Mangum)
                                       │
                          ┌────────────┼────────────┐
                          ▼            ▼             ▼
                       Auth       Rate Limit     Validator
                     (DynamoDB)  (DynamoDB)    (Pydantic)
                          │
                    Cache Lookup
                   Redis (exact) → pgvector (semantic)
                          │ MISS
                    Cost-Aware Router
                    ┌─────┼─────────┐
                    ▼     ▼         ▼
                 Bedrock Anthropic  OpenAI
                    └─────┴─────────┘
                          │
                  Cache Write + Metrics
                  (CloudWatch EMF / X-Ray)
```

See [ARCHITECTURE.md](ARCHITECTURE.md) for the full design, routing pseudocode, cost model, and operational risk register.

---

## Features

- **Cost-aware routing** — scores request complexity (token count, code detection, reasoning keywords) and routes to the cheapest model tier that can handle it. Falls back through tiers when providers are unhealthy.
- **Model preference** — clients can pin a specific model; the router falls back to complexity-based routing only if that provider is unavailable.
- **Two-layer cache** — Redis exact-match (sub-millisecond) and pgvector semantic similarity (cosine threshold 0.92). Cache writes are fire-and-forget (non-blocking).
- **Streaming** — SSE streaming endpoint (`POST /v1/chat/stream`) with cache hits served as a single synthetic event.
- **DynamoDB auth** — API key validation with per-key rate limits (requests per minute + per day). Dev bypass available locally.
- **Provider health registry** — DynamoDB-backed health state, refreshed by an EventBridge Lambda every 5 minutes. Unhealthy providers are skipped during routing.
- **CloudWatch EMF metrics** — `RequestCount`, `InputTokens`, `OutputTokens`, `LatencyMs`, `CacheHit`, `EstimatedCostUSD`, `ErrorCount` emitted via stdout without a CloudWatch agent.
- **X-Ray tracing** — segments for auth, cache lookup, routing, provider call, and cache write.
- **Terraform IaC** — all infrastructure in modular Terraform; OIDC-based GitHub Actions deploy role (no long-lived credentials).

---

## Project Structure

```
.
├── ARCHITECTURE.md         # Full design: routing logic, cache schema, cost model, risk register
├── IMPLEMENTATION_GUIDE.md # Step-by-step build log, gotchas, coding conventions, runbooks
├── ai-platform/            # Python service (FastAPI + Mangum)
│   ├── ai_platform/        # gateway, router, providers, cache, auth, metrics, config
│   └── tests/
└── terraform/              # IaC — networking, auth, caching, lambda, API GW, monitoring, CI/CD
```

See [ARCHITECTURE.md §7–8](ARCHITECTURE.md#7-service-implementation-structure) for the full annotated file tree.

---

## API Reference

All endpoints require `Authorization: Bearer <api_key>`.

### `POST /v1/chat`

Synchronous chat completion.

**Request**

```json
{
  "messages": [
    { "role": "system", "content": "You are a helpful assistant." },
    { "role": "user", "content": "Explain async/await in Python." }
  ],
  "model_preference": "claude-sonnet-4-6",
  "max_tokens": 1024,
  "temperature": 0.7,
  "metadata": {
    "budget": "standard",
    "latency_sla_ms": 5000,
    "reasoning_required": false,
    "caller_app": "my-service"
  }
}
```

| Field | Type | Default | Notes |
|---|---|---|---|
| `messages` | `Message[]` | required | 1–50 messages; roles: `system`, `user`, `assistant` |
| `model_preference` | `string` | `null` | Case-insensitive model name/ID. Router falls back if unavailable. |
| `max_tokens` | `int` | `1024` | 1–32768 |
| `temperature` | `float` | `0.7` | 0.0–2.0 |
| `metadata.budget` | `"low"` \| `"standard"` \| `"high"` | `"standard"` | `low` forces low-tier; `high` allows mid/high-tier |
| `metadata.latency_sla_ms` | `int` | `5000` | Provider call timeout in ms (500–60000) |
| `metadata.reasoning_required` | `bool` | `false` | Nudges routing toward higher-capability models |

**Response**

```json
{
  "request_id": "a1b2c3d4-...",
  "model_used": "claude-sonnet-4-6-20251001",
  "provider": "anthropic",
  "content": "Async/await in Python...",
  "usage": {
    "input_tokens": 42,
    "output_tokens": 310,
    "total_tokens": 352,
    "estimated_cost_usd": 0.002112
  },
  "cache_hit": false,
  "cache_source": "none",
  "latency_ms": 1840,
  "timestamp": 1746662400.0
}
```

**Error responses**

| Status | Code | Meaning |
|---|---|---|
| `401` | `unauthorized` | Missing or invalid API key |
| `422` | — | Request body validation failed |
| `429` | `rate_limit_exceeded` | Per-minute or per-day quota hit |
| `503` | `provider_unavailable` | All providers failed after retries |

---

### `POST /v1/chat/stream`

Streaming chat completion via Server-Sent Events.

Same request body as `/v1/chat`. Response is a stream of:

```
data: <token>\n\n
data: <token>\n\n
...
data: [DONE]\n\n
```

Cache hits are served as a single synthetic SSE event followed by `[DONE]`. Errors emit `data: [ERROR] All providers failed\n\n`.

---

### `GET /health`

Returns platform health. No auth required.

```json
{
  "status": "ok",
  "providers": {
    "bedrock-nova-micro": true,
    "bedrock-claude-haiku": true,
    "anthropic-claude-haiku": true,
    "anthropic-claude-sonnet": true,
    "anthropic-claude-opus": true,
    "openai-gpt4o-mini": true,
    "openai-gpt4o": true
  },
  "cache_available": true,
  "timestamp": 1746662400.0
}
```

`status` is `"ok"` (all providers healthy), `"degraded"` (some unhealthy), or `"unhealthy"` (none healthy).

---

## Local Development

**Prerequisites:** Docker, Python 3.12+, AWS credentials (for Bedrock).

### 1. Copy and fill in environment variables

```bash
cp ai-platform/.env.example ai-platform/.env
```

Minimum required in `.env`:

```
ENVIRONMENT=dev
ANTHROPIC_API_KEY=sk-ant-...
OPENAI_API_KEY=sk-...
# Leave REDIS_URL and PG_DSN blank to disable caching locally
CACHE_ENABLED=false
```

### 2. Run with Docker

```bash
cd ai-platform
docker build -t ai-platform .
docker run --env-file .env -p 8080:8080 ai-platform
```

### 3. Or run directly

```bash
cd ai-platform
pip install -r requirements.txt
uvicorn ai_platform.gateway.app:app --reload --port 8080
```

### 4. Make a request

```bash
curl -X POST http://localhost:8080/v1/chat \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer dev-key" \
  -d '{
    "messages": [{"role": "user", "content": "Hello!"}]
  }'
```

> In `dev` environment the auth layer accepts any key prefixed with `dev-`.

### 5. Run tests

```bash
cd ai-platform
pytest tests/ -v
```

---

## Configuration

All configuration is via environment variables (or a `.env` file). Loaded once at Lambda cold start.

| Variable | Default | Description |
|---|---|---|
| `ENVIRONMENT` | `production` | `dev` enables auth bypass and debug logging |
| `LOG_LEVEL` | `INFO` | Python log level |
| `AWS_REGION` | `us-east-1` | AWS region |
| `ANTHROPIC_API_KEY` | `""` | Anthropic API key (or use `ANTHROPIC_SECRET_ARN`) |
| `ANTHROPIC_SECRET_ARN` | `""` | Secrets Manager ARN — takes precedence if key not set directly |
| `OPENAI_API_KEY` | `""` | OpenAI API key (or use `OPENAI_SECRET_ARN`) |
| `OPENAI_SECRET_ARN` | `""` | Secrets Manager ARN for OpenAI key |
| `BEDROCK_REGION` | `us-east-1` | AWS region for Bedrock calls |
| `REDIS_URL` | `""` | ElastiCache Serverless endpoint |
| `REDIS_TTL_SECONDS` | `3600` | Default Redis TTL |
| `PG_DSN` | `""` | Aurora pgvector DSN (or use `PG_SECRET_ARN`) |
| `PG_SECRET_ARN` | `""` | RDS-managed secret ARN — DSN resolved at cold start |
| `SEMANTIC_CACHE_THRESHOLD` | `0.92` | Cosine similarity threshold for cache hits |
| `CACHE_ENABLED` | `true` | Set to `false` to disable all caching |
| `API_KEYS_TABLE` | `ai-platform-api-keys` | DynamoDB table for API key validation |
| `RATE_LIMIT_TABLE` | `ai-platform-rate-limits` | DynamoDB table for rate limit counters |
| `HEALTH_TABLE` | `ai-platform-provider-health` | DynamoDB table for provider health state |
| `DEFAULT_RPM` | `60` | Default requests per minute (overridden per key) |
| `DEFAULT_RPD` | `5000` | Default requests per day (overridden per key) |
| `COMPLEXITY_LOW_THRESHOLD` | `0.3` | Complexity score below which low-tier is used |
| `COMPLEXITY_MID_THRESHOLD` | `0.7` | Complexity score below which mid-tier is used |
| `MAX_PROVIDER_RETRIES` | `2` | Retries per provider before marking unhealthy |
| `PROVIDER_TIMEOUT_SECONDS` | `30` | Hard timeout per provider call |
| `EMBEDDING_MODEL` | `amazon.titan-embed-text-v1` | Bedrock embedding model for semantic cache |

---

## Deployment

Infrastructure is managed with Terraform. A GitHub Actions CI/CD pipeline handles plan and deploy via an OIDC-federated IAM role (no long-lived credentials).

### First-time setup

```bash
cd terraform

# Copy and edit your tfvars
cp terraform.tfvars.example terraform.tfvars

# Initialise with S3 backend
terraform init -backend-config=backend.hcl

# Review and apply
terraform plan
terraform apply
```

`backend.hcl` is gitignored. See [backend.hcl.example](terraform/backend.hcl.example) for the required fields.

### Deploy a Lambda code update

CI handles this automatically on merge to `main`. For a manual push see the [Quick Reference](IMPLEMENTATION_GUIDE.md#quick-reference) in the Implementation Guide — it includes the arm64 cross-compile step required when building on macOS.

For a full module-by-module breakdown of what Terraform provisions, see [ARCHITECTURE.md §8](ARCHITECTURE.md#8-terraform-infrastructure-structure).

---

## Observability

Metrics are emitted via CloudWatch EMF through Lambda stdout (no agent needed) in the `ai-platform/inference` namespace, dimensioned by `provider`, `model`, and `tier`. Key metrics: `RequestCount`, `InputTokens`, `OutputTokens`, `LatencyMs`, `CacheHit`, `EstimatedCostUSD`, `ErrorCount`.

CloudWatch Alarms fire to SNS on error rate >5%, p99 latency >10s, all providers unhealthy, or projected cost overrun. X-Ray active tracing covers `auth_check`, `cache_lookup`, `routing_decision`, `provider_call`, and `cache_write` segments.

See [ARCHITECTURE.md §5](ARCHITECTURE.md#5-observability-and-monitoring) for the full metric spec, alarm thresholds, and dashboard layout.

---

## Roadmap

See [ARCHITECTURE.md §11](ARCHITECTURE.md#11-platform-evolution-roadmap) for the three-phase evolution plan (current state through scaled platform service) and [IMPLEMENTATION_GUIDE.md](IMPLEMENTATION_GUIDE.md#build-status) for the live build status of each phase.

---

*Built for a small engineering team running a production AI platform on AWS without Kubernetes overhead.*
