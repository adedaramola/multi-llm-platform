# Multi-LLM Platform — Architecture Design
**Version:** 1.1
**Date:** 2026-04-13
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
│              Throttling (200 rps / 500 burst) · Request routing         │
└────────────────────────────┬────────────────────────────────────────────┘
                             │  Invoke  (POST /v1/chat  or  /v1/chat/stream)
                             ▼
┌─────────────────────────────────────────────────────────────────────────┐
│                     LAMBDA — AI Gateway (Python/FastAPI)                │
│                                                                         │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐  ┌────────────┐ │
│  │  Auth Layer  │  │ Rate Limiter │  │  Validator   │  │  Metrics   │ │
│  │ (API Key →   │  │  (DynamoDB   │  │  (Pydantic)  │  │  Emitter   │ │
│  │  DynamoDB)   │  │   counters)  │  │              │  │(CloudWatch)│ │
│  └──────────────┘  └──────────────┘  └──────────────┘  └────────────┘ │
│                                                                         │
│  ┌───────────────────────────────────────────────────────────────────┐ │
│  │                    SEMANTIC CACHE CHECK                           │ │
│  │  Redis exact match → pgvector cosine search → hit/miss           │ │
│  └───────────────────────────┬───────────────────────────────────────┘ │
│                               │ MISS                                    │
│  ┌───────────────────────────▼───────────────────────────────────────┐ │
│  │                    COST-AWARE ROUTER                              │ │
│  │  complexity · budget · model_preference · latency SLA · health   │ │
│  └───────┬──────────────┬──────────────┬───────────────┬────────────┘ │
└──────────┼──────────────┼──────────────┼───────────────┼──────────────┘
           │              │              │               │
           ▼              ▼              ▼               ▼
    ┌──────────┐   ┌──────────┐   ┌──────────┐   ┌──────────────┐
    │  AWS     │   │ Anthropic │   │  OpenAI  │   │  Open-source │
    │ Bedrock  │   │  Claude   │   │  GPT-4o  │   │  (Ollama /   │
    │(Nova     │   │  API      │   │  API     │   │   vLLM on    │
    │ Micro /  │   │           │   │          │   │  Fargate)    │
    │ Haiku)   │   │           │   │          │   │  [Phase 3]   │
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
| API entry point | API Gateway v2 (HTTP API) | 70% cheaper than REST API, built-in throttling and rate limiting |
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
| Low | Bedrock Nova Micro, Claude Haiku 4.5 | Simple Q&A, classification, summarization | ~$0.04–$1 |
| Mid | Claude Sonnet 4.6, GPT-4o mini | Multi-step reasoning, code gen, RAG answers | ~$3–$10 |
| High | Claude Opus 4.6, GPT-4o | Complex analysis, long-context, high-stakes | ~$15–$60 |
| Open | Llama 3 on Fargate | Batch, non-sensitive, cost-critical workloads | ~infra cost only [Phase 3] |

### Routing Pseudocode

```python
def route_request(request: InferenceRequest) -> Provider:
    # 1. Check semantic cache first
    cached = cache.lookup(request.prompt)
    if cached:
        return CacheHit(cached)

    # 2. Honour explicit model preference (bypasses complexity routing)
    if request.model_preference:
        preferred = find_provider(request.model_preference)  # case-insensitive name/model_id match
        if preferred and health_registry.is_healthy(preferred):
            return preferred
        # Falls through to complexity routing if preferred provider is unavailable

    # 3. Determine complexity score (0.0 – 1.0)
    #    Factors: token count, code detection, reasoning keywords, turn count, budget hint
    complexity = estimate_complexity(request)

    # 4. Select tier from complexity + budget hint
    #    Thresholds driven from settings (COMPLEXITY_LOW_THRESHOLD / COMPLEXITY_MID_THRESHOLD)
    tier = select_tier(complexity, request.metadata.budget)
    # budget=LOW → always "low"; budget=HIGH → "mid" or "high"; STANDARD → complexity-based

    # 5. Build fallback chain starting at target tier
    #    low → [low, mid, high]   mid → [mid, low, high]   high → [high, mid, low]
    tier_order = build_fallback_chain(tier)

    # 6. For each tier, filter to healthy providers sorted by cost
    for tier in tier_order:
        candidates = sorted(
            [p for p in providers[tier] if health_registry.is_healthy(p)],
            key=lambda p: p.cost_per_token,
        )
        for provider in candidates:
            try:
                return provider.complete(request, timeout=request.metadata.latency_sla_ms)
            except (ProviderError, TimeoutError):
                health_registry.mark_failure(provider)
                continue  # try next provider

    raise AllProvidersExhausted()
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
    id            BIGSERIAL PRIMARY KEY,
    prompt_hash   TEXT NOT NULL UNIQUE,   -- SHA256 of normalized prompt
    embedding     VECTOR(1536) NOT NULL,  -- Bedrock Titan Embeddings v1 dimensions
    response      TEXT NOT NULL,
    model_used    TEXT NOT NULL DEFAULT '',
    input_tokens  INT NOT NULL DEFAULT 0,
    output_tokens INT NOT NULL DEFAULT 0,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at    TIMESTAMPTZ             -- NULL = permanent
);

CREATE INDEX semantic_cache_embedding_idx
    ON semantic_cache USING ivfflat (embedding vector_cosine_ops)
    WITH (lists = 100);
```

> The `prompt_hash` column has a `UNIQUE` constraint, so no separate hash index is needed. Embedding model is always Bedrock Titan Embeddings v1 (`amazon.titan-embed-text-v1`) — no OpenAI embeddings dependency.

### Retrieval Logic

```python
async def lookup(prompt: str, threshold: float = 0.92) -> CacheResult | None:
    # Fast path: exact match via Redis (sub-millisecond)
    key = f"cache:{sha256(normalize(prompt))}"
    if hit := await redis.get(key):
        return CacheResult(response=hit, source="exact")

    # Semantic path: vector similarity via pgvector
    embedding = await embed(prompt)  # non-blocking — runs in executor
    row = await pg.fetchrow("""
        SELECT response, model_used,
               1 - (embedding <=> $1::vector) AS similarity
        FROM semantic_cache
        WHERE (expires_at IS NULL OR expires_at > NOW())
        ORDER BY embedding <=> $1::vector
        LIMIT 1
    """, embedding)

    if row and row["similarity"] >= threshold:
        await promote_to_redis(key, row["response"])  # warm exact cache
        return CacheResult(response=row["response"], source="semantic",
                           similarity=row["similarity"])

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

Dimensions: [provider, model, tier]

Metrics:
  - RequestCount        (Count)
  - InputTokens         (Count)
  - OutputTokens        (Count)
  - TotalTokens         (Count)
  - LatencyMs           (Milliseconds)
  - CacheHit            (Count)        -- 1 = hit, 0 = miss
  - EstimatedCostUSD    (None)
  - ErrorCount          (Count)        -- 1 when status >= 500

Namespace: ai-platform/errors

Dimensions: [error_type]

Metrics:
  - ErrorCount          (Count)
```

All metrics are emitted via stdout in Lambda (EMF JSON format) — no CloudWatch agent or PutMetricData API calls needed. Lambda captures stdout as structured CloudWatch log events and extracts the metrics automatically.

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
│   └── app.py              # FastAPI app, all middleware, Lambda handler (Mangum)
│                           # Endpoints: POST /v1/chat, POST /v1/chat/stream, GET /health
├── router/
│   ├── __init__.py
│   ├── router.py           # Cost-aware routing engine + streaming variant
│   ├── policies.py         # Complexity estimator, tier selection (settings-driven thresholds)
│   └── health.py           # Provider health registry (DynamoDB-backed)
├── providers/
│   ├── __init__.py
│   ├── base.py             # Abstract BaseProvider (complete + stream + health_check)
│   ├── anthropic_provider.py
│   ├── openai_provider.py  # OpenAI & any OpenAI-compatible endpoint
│   └── bedrock_provider.py # Nova Micro (low tier) + Claude Haiku via Bedrock
├── cache/
│   ├── __init__.py
│   └── semantic_cache.py   # Two-layer cache: Redis exact match + pgvector semantic
├── auth/
│   ├── __init__.py
│   ├── authenticator.py    # DynamoDB API key validation (dev bypass for local)
│   └── rate_limiter.py     # DynamoDB sliding window (per-minute + per-day)
├── metrics/
│   ├── __init__.py
│   └── emitter.py          # CloudWatch EMF publisher (stdout → Lambda → CW Logs)
├── models/
│   ├── __init__.py
│   └── schemas.py          # Pydantic request/response models
├── config/
│   ├── __init__.py
│   └── settings.py         # Pydantic settings from env vars (lru_cache singleton)
├── utils.py                # Shared helpers (fetch_secret)
├── health_checker.py       # Standalone EventBridge Lambda — checks all providers
├── requirements.txt
└── Dockerfile              # Local dev (uvicorn) — mirrors Lambda Python 3.12 arm64
```

---

## 8. Terraform Infrastructure Structure

```
terraform/
├── main.tf                 # Root module wiring
├── variables.tf            # Global input variables
├── outputs.tf              # Exported values (API URL, dashboard URL, function name)
├── terraform.tfvars.example
└── modules/
    ├── networking/
    │   └── main.tf         # VPC, public/private subnets, NAT GW, SGs, VPC endpoints
    │                       # (DynamoDB Gateway endpoint, Secrets Manager Interface endpoint)
    ├── auth/
    │   └── main.tf         # DynamoDB tables (api_keys, rate_limits, health), Secrets Manager
    ├── caching/
    │   └── main.tf         # Aurora Serverless v2 + pgvector, ElastiCache Serverless (Redis)
    ├── lambda_router/
    │   └── main.tf         # Gateway Lambda, IAM role, CW log group, alias, provisioned concurrency
    ├── api_gateway/
    │   └── main.tf         # HTTP API v2, routes (POST /v1/chat, POST /v1/chat/stream, GET /health)
    │                       # Note: WAF not supported on API GW v2; add CloudFront layer for WAF
    ├── health_checker/
    │   └── main.tf         # Health-checker Lambda, EventBridge rule (every 5 min)
    ├── monitoring/
    │   └── main.tf         # CloudWatch dashboard, alarms (error rate, p99, throttles), SNS alerts
    └── ci_cd/
        └── main.tf         # GitHub Actions OIDC provider + IAM deploy role
```

---

## 9. Request Lifecycle

```
1. CLIENT sends POST /v1/chat (or /v1/chat/stream) with:
   - Authorization: Bearer <api_key>
   - Body: { messages[], model_preference?, max_tokens, temperature,
             metadata: { budget, latency_sla_ms, reasoning_required, stream } }

2. API GATEWAY
   - Applies throttling (200 rps sustained / 500 burst)
   - Routes to Lambda integration via AWS_PROXY

3. LAMBDA — REQUEST ID MIDDLEWARE
   - Attaches X-Request-ID to request state and response headers

4. LAMBDA — AUTH LAYER
   - Extracts Bearer token from Authorization header
   - SHA256 hashes the key, looks up in DynamoDB api_keys table
   - Returns 401 if key missing, invalid, or revoked
   - Resolves caller_id and per-key rpm/rpd limits

5. LAMBDA — RATE LIMITER
   - Increments per-minute and per-day sliding window counters in DynamoDB (TTL-expired)
   - Returns 429 with Retry-After header if either limit exceeded

6. LAMBDA — VALIDATOR
   - Pydantic validates request body (message roles, content length, token limits)
   - Rejects malformed requests with 422

7. LAMBDA — CACHE CHECK
   - Normalize and SHA256 hash the prompt
   - Redis GET on prompt hash → exact match (sub-millisecond)
   - On miss: Bedrock Titan embed prompt (async, non-blocking) → pgvector cosine search
   - If similarity ≥ 0.92 → return cached response, promote to Redis, skip steps 8–9

8. LAMBDA — ROUTER
   - If model_preference set → attempt pinned provider first, fall back on failure
   - Score complexity (token volume, code detection, reasoning keywords, turn count)
   - Select tier from complexity + budget hint using settings-driven thresholds
   - Build fallback chain from target tier
   - For each tier: filter healthy providers (DynamoDB health registry), sort by cost
   - Call provider with latency_sla_ms timeout; on failure mark unhealthy, try next

9. LAMBDA — CACHE WRITE (asyncio.create_task — fire and forget)
   - Embed prompt, write (embedding, response, tokens) to pgvector
   - Write exact hash → Redis with TTL

10. LAMBDA — METRICS EMIT
    - CloudWatch EMF via stdout: RequestCount, InputTokens, OutputTokens, LatencyMs,
      CacheHit, EstimatedCostUSD, ErrorCount (dimensions: provider, model, tier)
    - X-Ray segment closed

11. API GATEWAY
    - Returns JSON response to client
    - Response headers: X-Request-ID
```

**Streaming path** (`POST /v1/chat/stream`): steps 1–7 are identical. Step 8 calls `provider.stream()` instead of `complete()` and yields tokens as SSE events (`data: <token>\n\n`). Cache hits are returned as a single synthetic SSE event. Final event is always `data: [DONE]\n\n`. Step 9 is skipped (token counts not available mid-stream).

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
