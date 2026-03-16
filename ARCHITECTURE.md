# Multi-LLM Platform — Architecture Design
**Version:** 1.0
**Date:** 2026-03-15
**Owner:** AI Platform Engineering

---

## 1. Platform Architecture

```
┌─────────────────────────────────────────────────────────────────────────┐
│                           CLIENT APPLICATIONS                           │
│           (Internal apps / SaaS features / Chatbots / RAG)             │
└────────────────────────────┬────────────────────────────────────────────┘
                             │  HTTPS  (API Key in header)
                             ▼
┌─────────────────────────────────────────────────────────────────────────┐
│                        AWS API GATEWAY (v2 HTTP)                        │
│         Rate limiting · Usage plans · Request validation · WAF          │
└────────────────────────────┬────────────────────────────────────────────┘
                             │  Invoke
                             ▼
┌─────────────────────────────────────────────────────────────────────────┐
│                     LAMBDA — AI Gateway (Python/FastAPI)                │
│                                                                         │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐  ┌────────────┐ │
│  │  Auth Layer  │  │ Rate Limiter │  │  Validator   │  │  Metrics   │ │
│  │ (API Key →   │  │  (DynamoDB   │  │  (Pydantic)  │  │  Emitter   │ │
│  │  Cognito/    │  │   counters)  │  │              │  │(CloudWatch)│ │
│  │  DynamoDB)   │  └──────────────┘  └──────────────┘  └────────────┘ │
│  └──────────────┘                                                       │
│                                                                         │
│  ┌───────────────────────────────────────────────────────────────────┐ │
│  │                    SEMANTIC CACHE CHECK                           │ │
│  │   Embed prompt → query pgvector (RDS Postgres) → hit/miss        │ │
│  └───────────────────────────┬───────────────────────────────────────┘ │
│                               │ MISS                                    │
│  ┌───────────────────────────▼───────────────────────────────────────┐ │
│  │                    COST-AWARE ROUTER                              │ │
│  │   Evaluate: complexity · budget · latency SLA · provider health  │ │
│  └───────┬──────────────┬──────────────┬───────────────┬────────────┘ │
└──────────┼──────────────┼──────────────┼───────────────┼──────────────┘
           │              │              │               │
           ▼              ▼              ▼               ▼
    ┌──────────┐   ┌──────────┐   ┌──────────┐   ┌──────────────┐
    │  AWS     │   │ Anthropic │   │  OpenAI  │   │  Open-source │
    │ Bedrock  │   │  Claude   │   │  GPT-4o  │   │  (Ollama /   │
    │(Titan/   │   │  API      │   │  API     │   │   vLLM on    │
    │ Haiku)   │   │           │   │          │   │  Fargate)    │
    └──────────┘   └──────────┘   └──────────┘   └──────────────┘
           │              │              │               │
           └──────────────┴──────────────┴───────────────┘
                                    │
                    ┌───────────────▼───────────────┐
                    │    RESPONSE PROCESSING         │
                    │  · Write to semantic cache     │
                    │  · Emit token/latency metrics  │
                    │  · Log to CloudWatch Logs      │
                    └───────────────┬───────────────┘
                                    │
                    ┌───────────────▼───────────────┐
                    │         OBSERVABILITY          │
                    │  CloudWatch · X-Ray · Grafana  │
                    │  (optional) · SNS alerts       │
                    └────────────────────────────────┘
```

---

## 2. Infrastructure Stack

| Component | AWS Service | Justification |
|-----------|------------|---------------|
| API entry point | API Gateway v2 (HTTP API) | 70% cheaper than REST API, built-in throttling, JWT auth support |
| Compute | Lambda (Python 3.12, arm64) | Zero idle cost, auto-scales to thousands of concurrent requests, arm64 is 20% cheaper |
| Semantic cache store | RDS PostgreSQL + pgvector | Native vector similarity search, no separate vector DB to operate, Aurora Serverless v2 for auto-scale |
| Response cache (exact) | ElastiCache Serverless (Redis) | Sub-millisecond exact match hits, serverless removes capacity planning |
| Rate limit counters | DynamoDB (on-demand) | Single-digit ms reads, TTL-based counter expiry, no ops overhead |
| API key store | DynamoDB | Key/secret lookup with low latency |
| Secrets (provider API keys) | AWS Secrets Manager | Automatic rotation, IAM-based access, audit trail |
| Logs | CloudWatch Logs | Native Lambda integration, structured JSON, cost-effective at moderate scale |
| Metrics | CloudWatch Metrics + EMF | Embedded Metric Format allows rich custom metrics without extra agents |
| Tracing | AWS X-Ray | Native Lambda instrumentation, service map for latency debugging |
| Alerting | CloudWatch Alarms + SNS | Operational alerts to PagerDuty/Slack via SNS HTTP endpoint |
| IaC | Terraform | State management, modules, cross-team standard |
| Container (optional) | ECS Fargate (open-source models) | Run Ollama/vLLM without managing EC2; pay per-task |

---

## 3. LLM Routing Strategy

### Model Tiers

| Tier | Models | Use Case | Cost (per 1M tokens) |
|------|--------|----------|---------------------|
| Low | Bedrock Titan Lite, Claude Haiku 4.5 | Simple Q&A, classification, summarization | ~$0.25–$1 |
| Mid | Claude Sonnet 4.6, GPT-4o mini | Multi-step reasoning, code gen, RAG answers | ~$3–$10 |
| High | Claude Opus 4.6, GPT-4o | Complex analysis, long-context, high-stakes | ~$15–$60 |
| Open | Llama 3 on Fargate | Batch, non-sensitive, cost-critical workloads | ~infra cost only |

### Routing Pseudocode

```python
def route_request(request: InferenceRequest) -> Provider:
    # 1. Check semantic cache first
    cached = cache.lookup(request.prompt)
    if cached:
        return CacheHit(cached)

    # 2. Determine complexity score (0.0 – 1.0)
    complexity = estimate_complexity(
        token_count=count_tokens(request.prompt),
        has_code=detect_code(request.prompt),
        requires_reasoning=request.metadata.get("reasoning_required", False),
        context_length=len(request.messages),
    )

    # 3. Check caller's budget hint
    budget = request.metadata.get("budget", "standard")  # "low" | "standard" | "high"

    # 4. Check latency SLA
    latency_sla_ms = request.metadata.get("latency_sla_ms", 5000)

    # 5. Route decision
    if complexity < 0.3 or budget == "low":
        providers = [BedrockTitanLite, ClaudeHaiku]
    elif complexity < 0.7 and latency_sla_ms > 3000:
        providers = [ClaudeSonnet, GPT4oMini]
    else:
        providers = [ClaudeOpus, GPT4o]

    # 6. Health check — skip unhealthy providers
    healthy = [p for p in providers if health_registry.is_healthy(p)]

    # 7. Fallback chain
    if not healthy:
        healthy = [BedrockTitanLite]  # last resort always-on

    # 8. Pick lowest cost among healthy providers
    return min(healthy, key=lambda p: p.cost_per_token)
```

---

## 4. Semantic Cache Design

### How It Works

1. Incoming prompt is embedded using a lightweight embedding model (Bedrock Titan Embeddings or `text-embedding-3-small` from OpenAI)
2. The embedding vector is queried against pgvector using cosine similarity
3. If similarity score ≥ 0.92, return the cached response — semantically equivalent request
4. On cache miss, after receiving the LLM response, store `(embedding, prompt_hash, response, model_used, token_cost, created_at)` in the vector table
5. Exact-match responses (same prompt hash) bypass the embedding step and hit Redis directly

### Schema

```sql
CREATE TABLE semantic_cache (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    prompt_hash  TEXT NOT NULL,           -- SHA256 of normalized prompt
    embedding    VECTOR(1536) NOT NULL,   -- text-embedding-3-small dimensions
    response     TEXT NOT NULL,
    model_used   TEXT NOT NULL,
    input_tokens  INT,
    output_tokens INT,
    created_at   TIMESTAMPTZ DEFAULT NOW(),
    expires_at   TIMESTAMPTZ              -- NULL = permanent
);

CREATE INDEX ON semantic_cache
    USING ivfflat (embedding vector_cosine_ops)
    WITH (lists = 100);

CREATE INDEX ON semantic_cache (prompt_hash);
```

### Retrieval Logic

```python
def lookup(prompt: str, threshold: float = 0.92) -> CacheResult | None:
    # Fast path: exact match via Redis
    key = f"cache:{sha256(normalize(prompt))}"
    if hit := redis.get(key):
        return CacheResult(response=hit, source="exact")

    # Semantic path: vector similarity
    embedding = embed(prompt)
    row = db.execute("""
        SELECT response, 1 - (embedding <=> %s) AS similarity
        FROM semantic_cache
        WHERE expires_at IS NULL OR expires_at > NOW()
        ORDER BY embedding <=> %s
        LIMIT 1
    """, [embedding, embedding]).fetchone()

    if row and row.similarity >= threshold:
        redis.setex(key, 3600, row.response)  # promote to Redis
        return CacheResult(response=row.response, source="semantic", similarity=row.similarity)

    return None
```

**Cache TTL policy:**
- Static/factual queries: 7 days
- Dynamic queries (current events, time-sensitive): 1 hour
- No TTL for idempotent document summarization

---

## 5. Observability and Monitoring

### Metrics (CloudWatch EMF)

```
Namespace: ai-platform/inference

Dimensions: provider, model, route_tier, status

Metrics:
  - request_count          (Count)
  - input_tokens           (Count)
  - output_tokens          (Count)
  - total_cost_usd         (None — dollar value)
  - latency_ms             (Milliseconds) — p50, p90, p99
  - cache_hit_rate         (Percent)
  - provider_error_rate    (Percent)
  - routing_tier           (Count by tier: low/mid/high)
```

### Dashboards

**Operational dashboard** (CloudWatch):
- Request rate, error rate, latency percentiles
- Cache hit rate (target: >40%)
- Provider health heatmap
- Cost per hour

**Cost dashboard**:
- Token spend by model over time
- Cost breakdown by caller application
- Cache savings (estimated tokens avoided)

### Alerts (CloudWatch Alarms → SNS → PagerDuty/Slack)

| Alert | Threshold | Severity |
|-------|-----------|----------|
| Error rate | >5% over 5 min | P2 |
| p99 latency | >10s over 5 min | P2 |
| All providers unhealthy | any | P1 |
| Monthly cost | >$500 projected overage | P3 |
| Lambda concurrency | >80% reserved | P3 |

### Tracing

X-Ray active tracing on all Lambda invocations. Segments:
- `auth_check`
- `cache_lookup`
- `routing_decision`
- `provider_call` (annotated with model, tokens)
- `cache_write`

---

## 6. Cost Model

### Assumptions
- Average request: 500 input tokens + 300 output tokens
- Cache hit rate: 35% (saving ~35% of LLM calls)
- Model mix: 60% low-tier, 30% mid-tier, 10% high-tier

### Low Traffic (~5K requests/day = 150K/month)

| Component | Monthly Cost |
|-----------|-------------|
| API Gateway (HTTP) | $0.15 |
| Lambda (128K invocations × ~800ms) | $2.50 |
| RDS Aurora Serverless v2 (pgvector) | $15–25 |
| ElastiCache Serverless | $5 |
| DynamoDB (on-demand) | $2 |
| CloudWatch Logs + Metrics | $5 |
| Secrets Manager | $1 |
| **LLM API costs** | ~$30–50 |
| **Total** | **~$60–90/month** |

### Moderate Traffic (~50K requests/day = 1.5M/month)

| Component | Monthly Cost |
|-----------|-------------|
| API Gateway | $1.50 |
| Lambda | $20 |
| RDS Aurora Serverless v2 | $40–60 |
| ElastiCache Serverless | $15 |
| DynamoDB | $10 |
| CloudWatch | $20 |
| X-Ray | $5 |
| **LLM API costs** | ~$300–500 |
| **Total** | **~$400–630/month** |

### Higher Usage (~300K requests/day = 9M/month)

| Component | Monthly Cost |
|-----------|-------------|
| API Gateway | $9 |
| Lambda (provisioned concurrency added) | $120 |
| RDS Aurora Serverless v2 | $150–200 |
| ElastiCache Serverless | $60 |
| DynamoDB | $50 |
| CloudWatch + X-Ray | $80 |
| **LLM API costs** | ~$1,800–3,000 |
| **Total** | **~$2,300–3,500/month** |

> LLM API costs dominate at all tiers. Semantic caching at 35%+ hit rate is the single highest-leverage cost control.

---

## 7. Service Implementation Structure

```
ai-platform/
├── gateway/
│   ├── __init__.py
│   ├── app.py              # FastAPI app, Lambda handler (Mangum)
│   └── middleware.py       # CORS, request ID injection
├── router/
│   ├── __init__.py
│   ├── router.py           # Core routing decision engine
│   ├── policies.py         # Complexity estimator, budget policies
│   └── health.py           # Provider health registry (DynamoDB-backed)
├── providers/
│   ├── __init__.py
│   ├── base.py             # Abstract BaseProvider interface
│   ├── openai_provider.py  # OpenAI & compatible APIs
│   ├── anthropic_provider.py
│   └── bedrock_provider.py
├── cache/
│   ├── __init__.py
│   ├── semantic_cache.py   # pgvector lookup + write
│   └── exact_cache.py      # Redis exact-match cache
├── auth/
│   ├── __init__.py
│   ├── authenticator.py    # API key validation
│   └── rate_limiter.py     # DynamoDB sliding window
├── metrics/
│   ├── __init__.py
│   ├── tracker.py          # Token + cost accumulator
│   └── emitter.py          # CloudWatch EMF publisher
├── models/
│   ├── __init__.py
│   └── schemas.py          # Pydantic request/response models
├── config/
│   ├── __init__.py
│   └── settings.py         # Pydantic settings (env vars)
├── requirements.txt
└── Dockerfile              # For local dev / Fargate open-source tier
```

---

## 8. Terraform Infrastructure Structure

```
terraform/
├── main.tf                 # Root module wiring
├── variables.tf            # Global input variables
├── outputs.tf              # Exported values (API URL, etc.)
├── terraform.tfvars.example
└── modules/
    ├── api_gateway/
    │   ├── main.tf         # HTTP API, routes, stages, WAF
    │   ├── variables.tf
    │   └── outputs.tf
    ├── lambda_router/
    │   ├── main.tf         # Lambda function, IAM role, layers
    │   ├── variables.tf
    │   └── outputs.tf
    ├── caching/
    │   ├── main.tf         # RDS Aurora Serverless (pgvector), ElastiCache Serverless
    │   ├── variables.tf
    │   └── outputs.tf
    ├── auth/
    │   ├── main.tf         # DynamoDB api_keys table, Secrets Manager refs
    │   ├── variables.tf
    │   └── outputs.tf
    ├── monitoring/
    │   ├── main.tf         # CloudWatch dashboards, alarms, SNS topics
    │   ├── variables.tf
    │   └── outputs.tf
    └── networking/
        ├── main.tf         # VPC, subnets, security groups, VPC endpoints
        ├── variables.tf
        └── outputs.tf
```

---

## 9. Request Lifecycle

```
1. CLIENT sends POST /v1/chat with:
   - Authorization: Bearer <api_key>
   - Body: { model_preference, messages[], metadata: { budget, latency_sla_ms } }

2. API GATEWAY
   - Validates JWT or passes API key header through
   - Applies usage plan throttle (global rate limit)
   - Routes to Lambda integration

3. LAMBDA — AUTH LAYER
   - Looks up API key in DynamoDB api_keys table
   - Returns 401 if invalid or revoked
   - Resolves caller_id and per-key rate limits

4. LAMBDA — RATE LIMITER
   - Increments sliding window counter in DynamoDB (TTL = 60s)
   - Returns 429 if caller exceeds per-minute or per-day quota

5. LAMBDA — VALIDATOR
   - Pydantic validation of request body
   - Rejects malformed or oversized prompts

6. LAMBDA — CACHE CHECK
   - Normalize and hash the prompt
   - Redis GET on hash key → fast exact match
   - On miss: embed prompt → pgvector cosine search
   - If similarity ≥ 0.92 → return cached response, skip steps 7–8

7. LAMBDA — ROUTER
   - Score complexity (token count, code detection, reasoning hints)
   - Select model tier based on complexity + budget hint
   - Filter by provider health (DynamoDB health flags)
   - Return ordered provider list

8. LAMBDA — PROVIDER CALL
   - Call selected provider with retry (exponential backoff, max 2 retries)
   - On provider failure → immediately try next provider in fallback chain
   - Record start/end time, token usage from response headers/body

9. LAMBDA — CACHE WRITE (async, non-blocking)
   - Embed response prompt
   - Write to pgvector semantic cache
   - Write exact hash to Redis with TTL

10. LAMBDA — METRICS EMIT
    - CloudWatch EMF: latency, tokens, cost, model, cache_hit, status
    - X-Ray segment closed

11. API GATEWAY
    - Returns response to client
    - Headers: X-Request-ID, X-Model-Used, X-Cache-Hit, X-Tokens-Used
```

---

## 10. MVP Deployment Plan

**Goal:** Deploy a working gateway within 1 week with real routing and caching.

### Week 1 — MVP Scope

- [x] API Gateway + Lambda with FastAPI/Mangum
- [x] DynamoDB-backed API key auth
- [x] Basic rate limiting (per-minute counter in DynamoDB)
- [x] Support for 2 providers: Anthropic Claude + AWS Bedrock
- [x] Exact-match Redis cache (semantic cache deferred to Phase 2)
- [x] CloudWatch structured logging + basic request metrics
- [x] Terraform modules: networking, lambda_router, api_gateway, auth

### What's Deferred

- Semantic/pgvector cache (replace with Redis exact-match)
- Open-source model tier on Fargate
- Full CloudWatch dashboards
- Per-caller cost tracking
- WAF integration

### MVP Test Criteria

- Gateway reachable at API Gateway URL
- Auth rejects invalid keys, accepts valid keys
- Rate limit returns 429 after quota exceeded
- Requests routed between providers based on complexity score
- Cache returns hit on identical second request
- CloudWatch logs show structured JSON per request

---

## 11. Platform Evolution Roadmap

### Phase 1 — Initial Platform Launch (Month 1–2)

**Stack:** API Gateway → Lambda → 2 providers (Anthropic, Bedrock) → Redis exact cache → DynamoDB auth
**Capacity:** Up to 10K requests/day
**Focus:** Working routing, auth, logging, basic caching

Deliverables:
- Single Lambda function with all middleware
- Terraform managing all infra
- Runbook for: adding API keys, provider outage response
- CloudWatch dashboard: request count, error rate, latency

---

### Phase 2 — Moderate Production Usage (Month 3–5)

**Additions:**
- Semantic cache (RDS Aurora + pgvector)
- OpenAI provider added
- Per-caller token and cost tracking in DynamoDB
- CloudWatch cost dashboard with SNS budget alerts
- X-Ray tracing enabled
- Provider health registry with automatic circuit breaking
- CI/CD pipeline (GitHub Actions → Terraform plan/apply + Lambda deploy)

**Capacity:** Up to 100K requests/day
**Focus:** Cost control, reliability, developer experience

---

### Phase 3 — Scaled Platform Service (Month 6+)

**Additions:**
- Lambda provisioned concurrency for latency-sensitive paths
- Open-source model tier (Llama 3 on ECS Fargate, invoked via same provider interface)
- Streaming response support (API Gateway WebSocket or response streaming)
- Prompt compression middleware (reduces input token cost 10–30%)
- Multi-region active-passive for DR
- Grafana Cloud connected to CloudWatch for richer dashboards
- Usage API for callers to query their own token spend
- SLA enforcement (per-caller latency budgets)

**Capacity:** 500K+ requests/day
**Focus:** Efficiency, multi-tenant operations, advanced routing

---

## 12. Cost Optimization Strategy

### 1. Semantic Caching (highest impact)
- Target: 35–50% cache hit rate after warmup
- At 50K requests/day with $0.008 average LLM cost: saves ~$40–60/day = $1,200–1,800/month
- Invest in embedding quality; a small embedding call is always cheaper than an LLM call

### 2. Model Tiering
- Enforce a "complexity gate" — simple requests must use low-tier models
- Route 60%+ of traffic to Haiku/Titan Lite
- Dashboard visibility into tier distribution; alert if high-tier usage exceeds 20%

### 3. Prompt Optimization
- Strip whitespace and redundant context from prompts before routing
- System prompt deduplication: hash common system prompts, store once
- Target: 10–20% reduction in input token counts

### 4. Batching (async workloads)
- Non-interactive workloads (document processing, batch embeddings) go through SQS → Lambda batch processor
- Bedrock Batch Inference API: up to 50% cost reduction for offline jobs

### 5. Autoscaling Infrastructure
- Lambda scales to zero — no idle compute cost
- Aurora Serverless v2 scales to 0 ACUs when idle (dev/staging environments)
- ElastiCache Serverless: pay per GB stored + per ECU used, no minimum

### 6. Reserved Capacity (Phase 3)
- Anthropic provisioned throughput for predictable high-volume traffic: up to 30% cheaper
- OpenAI Batch API for non-real-time requests: 50% cheaper

---

## 13. Operational Risks

| Risk | Impact | Mitigation |
|------|--------|------------|
| All external LLM providers down simultaneously | P1 — complete outage | Maintain open-source Llama tier on Fargate as last-resort fallback; circuit breaker per provider |
| Lambda cold starts causing high p99 latency | Degraded UX on traffic spikes | Provisioned concurrency for hot path; keep deployment package < 50MB using Lambda layers |
| pgvector query becomes slow at scale | Cache lookup adds more latency than it saves | IVFFlat index tuned to data size; monitor cache lookup p99; migrate to Aurora auto-scale read replicas |
| API key leakage | Unauthorized usage and cost overrun | Short-lived keys + rotation policy in Secrets Manager; per-key spend alerts; HMAC signature validation |
| Cost runaway from routing to high-tier models | Unexpected monthly bill spike | Hard cap on high-tier routing by caller; CloudWatch billing alert at 80% of budget |
| DynamoDB rate limiter under hot-key load | Rate limiter bypassed under burst traffic | Use DynamoDB DAX for high-frequency counter reads; consider token bucket in ElastiCache |
| Vendor price changes | Platform cost model breaks | Provider abstraction layer makes switching fast; maintain ≥2 providers per tier |
| Small team alert fatigue | Critical alerts ignored | Limit P1/P2 alerts to truly actionable; route P3 to weekly digest not pager |

---

*Architecture designed for a small engineering team operating a production AI platform on AWS without Kubernetes overhead.*
