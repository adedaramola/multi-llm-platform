# Multi-LLM Platform

A serverless AI gateway that routes requests across AWS Bedrock, Anthropic, and OpenAI. It combines cost-aware model selection, exact and semantic caching, API-key controls, usage accounting, and AWS observability behind one API.

The platform is designed for a small engineering team that needs production infrastructure without operating Kubernetes.

## What it provides

- Cost-aware routing based on prompt complexity, budget, and reasoning requirements
- Optional model or provider preference with automatic fallback
- AWS Bedrock, Anthropic, and OpenAI provider support
- Redis exact-match caching and Aurora pgvector semantic caching
- Synchronous and Server-Sent Events streaming responses
- DynamoDB API-key authentication and per-client RPM/RPD limits
- Per-client token, request, cache-hit, and estimated-cost accounting
- Optional scheduled provider health checks and circuit breaking
- CloudWatch metrics, alarms, dashboards, logs, and active X-Ray tracing
- Modular Terraform and GitHub Actions deployment through AWS OIDC

## How it works

```text
Client
  │
  ▼
API Gateway HTTP API
  │
  ▼
Lambda · FastAPI + Mangum
  ├── API-key authentication and rate limiting · DynamoDB
  ├── Exact cache · Redis
  ├── Semantic cache · Aurora PostgreSQL + pgvector
  ├── Cost-aware router
  │     ├── AWS Bedrock
  │     ├── Anthropic
  │     └── OpenAI
  └── Usage and operational metrics · DynamoDB + CloudWatch

EventBridge (optional) → Health-checker Lambda → Provider health table
```

Requests check the exact and semantic caches before reaching the router. On a cache miss, the router chooses an eligible model tier, skips unhealthy providers, and falls back when a provider fails. Low-budget requests are hard-capped to low-tier models. Successful responses update usage, metrics, and the cache.

See [ARCHITECTURE.md](ARCHITECTURE.md) for routing details, infrastructure design, the cache schema, cost assumptions, and operational risks.

## Quick start

### Requirements

- Python 3.12
- AWS credentials for Bedrock
- Anthropic and OpenAI API keys
- Docker, if you prefer the container workflow

### Run locally

```bash
cp ai-platform/.env.example ai-platform/.env
```

Set the provider keys in `ai-platform/.env` and keep local caching disabled unless Redis and PostgreSQL are available:

```dotenv
ENVIRONMENT=dev
CACHE_ENABLED=false
ANTHROPIC_API_KEY=sk-ant-...
OPENAI_API_KEY=sk-...
```

Then install and start the service:

```bash
cd ai-platform
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt
uvicorn ai_platform.gateway.app:app --reload --port 8080
```

In development, any non-empty bearer token is accepted:

```bash
curl -X POST http://localhost:8080/v1/chat \
  -H "Authorization: Bearer dev-key" \
  -H "Content-Type: application/json" \
  -d '{"messages":[{"role":"user","content":"Explain semantic caching briefly."}]}'
```

To run with Docker Compose instead (an `.env` file is optional for the public health endpoint):

```bash
docker compose up --build
curl http://localhost:8080/health
```

Compose reads provider credentials from `ai-platform/.env`, exposes the API on port 8080, and disables
caching by default so Redis and PostgreSQL are not required. Set `API_PORT` to change the host port, for
example `API_PORT=9000 docker compose up --build`. Stop the service with `docker compose down`.

To build and run the image directly:

```bash
docker build -t multi-llm-platform ./ai-platform
docker run --rm --env-file ai-platform/.env -e ENVIRONMENT=dev -e CACHE_ENABLED=false \
  -p 8080:8080 multi-llm-platform
```

## API

All `/v1` endpoints require `Authorization: Bearer <api-key>`. The `/health` endpoint is public.

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/v1/chat` | Return a complete chat response |
| `POST` | `/v1/chat/stream` | Stream a response as SSE events |
| `GET` | `/v1/usage` | Return the caller's current-month usage |
| `GET` | `/health` | Report platform and provider health |

### Chat request

```json
{
  "messages": [
    {"role": "system", "content": "You are a concise assistant."},
    {"role": "user", "content": "Explain async/await in Python."}
  ],
  "model_preference": "sonnet",
  "max_tokens": 1024,
  "temperature": 0.7,
  "metadata": {
    "budget": "standard",
    "latency_sla_ms": 5000,
    "reasoning_required": false,
    "caller_app": "example-service",
    "workflow_id": "workflow-123",
    "cache_policy": "private",
    "data_classification": "restricted"
  }
}
```

`messages` accepts 1–50 `system`, `user`, or `assistant` messages. `model_preference` is optional and matches an enabled provider name or model ID within the request's budget ceiling; normal tier routing is used if the preferred model is unavailable or exceeds that ceiling. Budget values are `low`, `standard`, and `high`.

Cache policy defaults to `off`. `private` isolates exact and semantic cache entries by the
authenticated caller. `shared` is accepted only when `data_classification` is `public`.
Production requests that set `caller_app` must match the application name bound to the API key.

### Chat response

```json
{
  "request_id": "a1b2c3d4-...",
  "model_used": "claude-sonnet-4-6-20251001",
  "provider": "anthropic",
  "content": "Async/await lets Python...",
  "usage": {
    "input_tokens": 42,
    "output_tokens": 120,
    "total_tokens": 162,
    "estimated_cost_usd": 0.0012
  },
  "cache_hit": false,
  "cache_source": "none",
  "cache_policy": "private",
  "latency_ms": 940,
  "timestamp": 1784600000.0
}
```

`cache_source` is `none`, `exact`, or `semantic`.

### Streaming

`POST /v1/chat/stream` accepts the same request body and returns `text/event-stream`:

```text
data: First response chunk

data: Next response chunk

data: [DONE]

```

Cache hits are returned as one synthetic data event followed by `[DONE]`. If streaming fails, the endpoint emits an `[ERROR]` event; partial output is not cached.

### Usage

`GET /v1/usage` returns the authenticated caller's current UTC month totals:

```json
{
  "caller_id": "app-001",
  "month": "2026-07",
  "request_count": 42,
  "cache_hits": 10,
  "input_tokens": 5000,
  "output_tokens": 2500,
  "estimated_cost_usd": 0.0375
}
```

### Errors

| Status | Meaning |
|---|---|
| `401` | Missing, invalid, or revoked API key |
| `422` | Invalid request body |
| `429` | Per-minute or per-day quota exceeded |
| `503` | Authentication dependency unavailable or all providers failed |

## Development checks

Run the same checks used by CI:

```bash
cd ai-platform
source .venv/bin/activate
ruff check ai_platform tests
mypy
python -m pytest tests/ -v --tb=short

cd ../terraform
terraform fmt -check -recursive
terraform init -backend=false -input=false
terraform validate
```

## Deployment

AWS infrastructure is managed entirely with Terraform. The default portfolio deployment creates:

- DynamoDB auth, rate-limit, provider-health, and usage tables
- Secrets Manager provider and bootstrap-client secrets
- One gateway Lambda; the health-checker Lambda is omitted by default
- API Gateway routes, CloudWatch monitoring, and SNS alerts
- GitHub Actions OIDC provider and least-privilege deployment role

The full opt-in profile also creates VPC networking, ElastiCache Serverless, Aurora Serverless with pgvector, scheduled health checks, and provisioned Lambda concurrency.

Follow [IMPLEMENTATION_GUIDE.md](IMPLEMENTATION_GUIDE.md) for the supported first deployment, verification suite, CI setup, routine updates, and troubleshooting. The first deployment requires a `linux/arm64` Lambda package before `terraform apply`.

## Configuration

Runtime settings are defined in [`ai-platform/ai_platform/config/settings.py`](ai-platform/ai_platform/config/settings.py) and can be supplied through environment variables. Start from [`ai-platform/.env.example`](ai-platform/.env.example) for local development and [`terraform/terraform.tfvars.example`](terraform/terraform.tfvars.example) for AWS.

Important groups include:

- Provider keys and Secrets Manager ARNs
- Redis and PostgreSQL connections
- Cache TTL and semantic-similarity threshold
- DynamoDB table names and rate-limit behavior
- Complexity thresholds, provider switches, budget ceilings, and timeouts
- Circuit-breaker thresholds and cooldowns

Settings are loaded once per Lambda cold start. Production provider credentials are resolved from Secrets Manager rather than stored as plaintext Lambda environment variables.

## Repository layout

```text
.
├── README.md                  Project overview and local quick start
├── IMPLEMENTATION_GUIDE.md   Deployment, verification, and operations
├── ARCHITECTURE.md            Detailed technical design
├── ai-platform/
│   ├── README.md               Python development and extension guide
│   ├── ai_platform/           Gateway, routing, providers, auth, cache, and metrics
│   └── tests/                 Unit and integration-style tests with mocked dependencies
└── terraform/
    ├── README.md               Infrastructure workflow and safety guide
    ├── modules/               AWS infrastructure modules
    └── scripts/               Backend bootstrap and pgvector migration
```

## Documentation

- [Implementation guide](IMPLEMENTATION_GUIDE.md) — deploy and operate the platform
- [Architecture](ARCHITECTURE.md) — understand design decisions and request flow
- [Python service guide](ai-platform/README.md) — develop, test, and extend the application
- [Terraform guide](terraform/README.md) — change and operate the AWS infrastructure

---

*Built for a small engineering team running a production AI platform on AWS without Kubernetes overhead.*
