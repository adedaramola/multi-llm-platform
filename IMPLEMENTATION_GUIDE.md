# Multi-LLM Platform — Implementation Guide

## Build Status

| Phase | Status | Notes |
|-------|--------|-------|
| Prerequisites | ✅ Done | AWS CLI, Terraform, Python 3.12, Docker |
| Phase 1 — Local Service | ✅ Done | Gateway runs, routes to Haiku, real responses confirmed |
| Phase 2 — AWS Foundation | ✅ Done | VPC, DynamoDB, Secrets Manager, Aurora+pgvector, ElastiCache all live |
| Phase 3 — Lambda Deploy | ✅ Done | Docker arm64 build, Secrets Manager HTTPS SG fix, cache SG fix |
| Phase 4 — Auth | ✅ Done | Real API key seeded, dev bypass removed |
| Phase 5 — Monitoring | ✅ Done | CloudWatch dashboard + SNS alerts live |
| Phase 6 — Semantic Cache | ✅ Done | Redis + Aurora cache live; cache hit confirmed (934ms → 143ms) |
| Phase 7 — Health Checker | ✅ Done | Scheduled Lambda, Nova Micro, all 3 providers healthy |
| Phase 8 — CI/CD | ✅ Done | GitHub Actions OIDC pipeline green; 18/18 tests, full deploy, smoke test |
| Phase 9 — Tests | ✅ Done | 18 pytest tests covering policies + router; passing in CI |
| Phase 10 — Hardening | ✅ Done | Provisioned concurrency (2 warm instances); WAF skipped — not supported on API GW v2 HTTP APIs |
| Phase 11 — Improvements | ✅ Done | Bug fixes, streaming endpoint, model_preference routing, settings-driven thresholds |

### Live environment

| Resource | Value |
|----------|-------|
| API Gateway URL | `https://ozj6y1pi1g.execute-api.us-east-1.amazonaws.com/` |
| Lambda function | `ai-platform-gateway-production` |
| CloudWatch dashboard | `https://us-east-1.console.aws.amazon.com/cloudwatch/home#dashboards:name=ai-platform-production` |
| AWS account | `900009968072` / `us-east-1` |
| Aurora cluster | `ai-platform-pgvector-production` |
| ElastiCache | `ai-platform-production` |

---

## Prerequisites

- AWS account with admin access
- AWS CLI configured (`aws configure`)
- Terraform >= 1.7
- Python 3.12
- Docker Desktop
- Anthropic API key (`sk-ant-...`) and optionally OpenAI (`sk-...`)

The `.env` file, `terraform.tfvars`, and `dist/` directory must never be committed. They are already in `.gitignore`.

---

## Phase 1 — Local Service

**Goal:** Validate the gateway locally before touching AWS.

Run the service from `ai-platform/`:

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
uvicorn ai_platform.gateway.app:app --host 0.0.0.0 --port 8080 --reload
```

Create `ai-platform/.env` with your keys and `CACHE_ENABLED=false`. Add a dev auth bypass in `authenticator.py` so local testing works without DynamoDB:

```python
if get_settings().environment == "dev":
    return CallerIdentity(caller_id="dev-user", app_name="local",
                          rpm_limit=1000, rpd_limit=100_000, active=True)
```

Test with `curl http://localhost:8080/health` and a POST to `/v1/chat` with `"budget": "low"`. Expected: response from Claude Haiku.

> All Python subpackages must live inside `ai_platform/` — relative imports and the Mangum handler depend on this layout.

---

## Phase 2 — AWS Foundation

**Goal:** Create the core AWS infrastructure with Terraform.

Create an S3 bucket for Terraform state (append your account ID to make it globally unique) and update the `backend "s3"` block in `terraform/main.tf`. Copy `terraform.tfvars.example` to `terraform.tfvars` and fill in your API keys.

Deploy everything at once — Terraform resolves the dependency order automatically:

```bash
cd terraform
terraform init
terraform apply
```

After the caching layer is up, run the pgvector migration via the **RDS Data API** (Aurora is in a private subnet — direct `psql` access from your laptop is not possible):

```bash
CLUSTER_ARN=$(aws rds describe-db-clusters \
  --db-cluster-identifier ai-platform-pgvector-production \
  --query "DBClusters[0].DBClusterArn" --output text)

SECRET_ARN=$(aws secretsmanager list-secrets \
  --query "SecretList[?starts_with(Name, 'rds!')].ARN" --output text)

run_sql() {
  aws rds-data execute-statement \
    --resource-arn "$CLUSTER_ARN" --secret-arn "$SECRET_ARN" \
    --database "ai_platform" --sql "$1"
}

run_sql "CREATE EXTENSION IF NOT EXISTS vector"
run_sql "CREATE TABLE IF NOT EXISTS semantic_cache (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    prompt_hash TEXT NOT NULL UNIQUE,
    embedding VECTOR(1536) NOT NULL,
    response TEXT NOT NULL,
    model_used TEXT NOT NULL,
    input_tokens INT DEFAULT 0,
    output_tokens INT DEFAULT 0,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    expires_at TIMESTAMPTZ)"
run_sql "CREATE INDEX IF NOT EXISTS semantic_cache_embedding_idx
    ON semantic_cache USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100)"
run_sql "CREATE INDEX IF NOT EXISTS semantic_cache_hash_idx ON semantic_cache (prompt_hash)"
```

> The Data API does not support multi-statement calls — run each SQL statement individually.

---

## Phase 3 — Lambda Deploy

**Goal:** Package and deploy the Python service to Lambda.

Lambda runs on `linux/arm64`. Always build the zip inside the Lambda base image — plain `pip install` on macOS produces incompatible binaries:

```bash
cd ai-platform && mkdir -p dist/package

docker run --rm --platform linux/arm64 \
  -v "$(pwd)":/src \
  --entrypoint /bin/bash \
  public.ecr.aws/lambda/python:3.12-arm64 \
  -c "pip install -r /src/requirements.txt -t /src/dist/package --quiet \
      && cp -r /src/ai_platform /src/dist/package/"

cd dist/package && zip -r ../ai-platform.zip . -q
```

Deploy Lambda and API Gateway:

```bash
cd terraform
terraform apply -target=module.lambda_router -target=module.api_gateway
```

**Known issues fixed in code:**

- **Secrets Manager timeout:** Interface VPC endpoints are ENIs — they require an inbound port 443 rule in their security group. Without it, the Lambda cold start hangs silently for 60 seconds. The fix is in `terraform/modules/networking/main.tf` (HTTPS ingress from `10.0.0.0/16` on the Lambda SG).

- **Redis connection hang:** The caching module originally used `var.lambda_sg_id` for ElastiCache and Aurora. That SG has no inbound rules on 6379/5432. The correct SG is `var.cache_sg_id`. Fixed in `terraform/modules/caching/main.tf`.

> Gateway endpoints (S3, DynamoDB) are route-table entries — they do not need SG rules. Interface endpoints (Secrets Manager, etc.) behave like private IPs and do.

---

## Phase 4 — Auth and Rate Limiting

**Goal:** Issue real API keys and remove the local dev bypass.

Generate and seed an API key into DynamoDB:

```bash
API_KEY=$(openssl rand -hex 32)
KEY_HASH=$(echo -n "$API_KEY" | shasum -a 256 | awk '{print $1}')  # macOS

aws dynamodb put-item \
  --table-name ai-platform-api-keys-production \
  --item "{
    \"key_hash\": {\"S\": \"$KEY_HASH\"},
    \"caller_id\": {\"S\": \"app-001\"},
    \"app_name\": {\"S\": \"my-first-app\"},
    \"rpm_limit\": {\"N\": \"60\"},
    \"rpd_limit\": {\"N\": \"5000\"},
    \"active\": {\"BOOL\": true},
    \"created_at\": {\"S\": \"$(date -u +%Y-%m-%dT%H:%M:%SZ)\"}
  }"

echo "Your API key: $API_KEY"
```

Remove the dev bypass from `authenticator.py`, rebuild the zip, and redeploy:

```bash
aws lambda update-function-code \
  --function-name ai-platform-gateway-production \
  --zip-file fileb://ai-platform/dist/ai-platform.zip \
  --architectures arm64
```

Verify rate limiting by sending 65+ requests in quick succession — you should see `429` after request 60.

---

## Phase 5 — Monitoring

**Goal:** Visibility into every request, token, and dollar.

```bash
cd terraform && terraform apply -target=module.monitoring
```

Confirm your SNS email subscription — click the confirmation link or alarms will not notify you.

View the dashboard:
```bash
terraform output cloudwatch_dashboard_url
```

Verify custom metrics are flowing:
```bash
aws cloudwatch list-metrics --namespace "ai-platform/inference"
# Expected: RequestCount, InputTokens, OutputTokens, LatencyMs, CacheHit, EstimatedCostUSD
```

> **Known issue fixed in code:** CloudWatch `PutDashboard` returns HTTP 400 if any widget is missing a `"region"` field. Each widget in `terraform/modules/monitoring/main.tf` includes `"region": "${data.aws_region.current.name}"`.

---

## Phase 6 — Semantic Cache

**Goal:** Stop paying for duplicate LLM calls.

Wire the Redis endpoint into the Lambda environment:

```bash
REDIS_ENDPOINT=$(cd terraform && terraform output -raw redis_endpoint)
FUNCTION_NAME=$(cd terraform && terraform output -raw lambda_function_name)

aws lambda update-function-configuration \
  --function-name "$FUNCTION_NAME" \
  --environment "Variables={CACHE_ENABLED=true,REDIS_URL=rediss://$REDIS_ENDPOINT:6379}"
```

The Aurora DSN is read from `PG_SECRET_ARN` at runtime — no password is ever stored in an environment variable.

Test the cache:
```bash
# First call — LLM is invoked (~900ms)
curl -X POST $API_URL/v1/chat -H "Authorization: Bearer $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"messages": [{"role": "user", "content": "What is the capital of France?"}]}'

# Second identical call — Redis exact hit (~140ms, "cache_hit": true)
curl -X POST $API_URL/v1/chat -H "Authorization: Bearer $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"messages": [{"role": "user", "content": "What is the capital of France?"}]}'
```

> **Known issue fixed in code:** The caching module was passing the Lambda SG instead of the dedicated cache SG to ElastiCache and Aurora. The Lambda SG has no inbound rules on 6379/5432, causing Redis to hang silently at TLS handshake. Fixed in `terraform/modules/caching/main.tf` using `var.cache_sg_id`.

---

## Phase 7 — Provider Health Checker

**Goal:** Automatic circuit breaking when a provider is down.

```bash
cd terraform && terraform apply -target=module.health_checker
```

This creates a separate Lambda (`ai-platform-health-checker-production`) triggered by EventBridge every 5 minutes. It checks Bedrock Nova Micro, Anthropic Haiku, and OpenAI GPT-4o-mini, then writes results directly to the DynamoDB health table.

**Key notes:**

- **Nova Micro replaces Titan Lite:** `amazon.titan-text-lite-v1` is not available in all accounts. The platform uses `amazon.nova-micro-v1:0` (8x cheaper, Messages API format with `content: [{text: "..."}]` arrays). Handled in `bedrock_provider.py`.

- **Health table writes:** `mark_failure()` uses DynamoDB `if_not_exists` on the `status` field — once set to "healthy" it never overwrites to "unhealthy". The health checker Lambda uses `put_item` directly to bypass this. `UNHEALTHY_THRESHOLD = 3` consecutive failures before a provider is marked down.

Check which models are active in your account:
```bash
aws bedrock list-foundation-models \
  --query "modelSummaries[?contains(modelId,'nova')].{id:modelId,status:modelLifecycle.status}"
```

---

## Phase 8 — CI/CD

**Goal:** Every push to `main` automatically tests and deploys.

The OIDC trust and IAM deploy role are managed by `terraform/modules/ci_cd/`:

```bash
cd terraform && terraform apply -target=module.ci_cd
terraform output github_actions_role_arn
```

Store the role ARN as a GitHub secret (use stdin to avoid escaping issues):

```bash
echo -n "arn:aws:iam::YOUR_ACCOUNT_ID:role/ai-platform-github-actions" \
  | gh secret set AWS_DEPLOY_ROLE_ARN
```

The workflow at `.github/workflows/deploy.yml` has two jobs:

- **`test`** — runs on every push and PR; installs dependencies, runs `pytest`
- **`deploy`** — runs only on pushes to `main` after `test` passes; builds the arm64 zip using Docker + QEMU, deploys both Lambdas, smoke-tests `/health`

> GitHub Actions runners are x86. QEMU (`docker/setup-qemu-action@v3`) is required to run `linux/arm64` containers on them. Without it, the Docker build silently produces an x86 binary that fails with `exec format error` at Lambda runtime.

> The IAM role is scoped to `repo:YOUR_ORG/YOUR_REPO:ref:refs/heads/main` — it cannot be assumed from any other branch or fork.

---

## Phase 11 — Improvements

**Goal:** Fix correctness bugs, eliminate code duplication, and add streaming + model pinning.

### What changed

**Bug fixes**

- **SQL injection** — `semantic_cache.py` `write()` was interpolating `ttl_seconds` directly into SQL (`INTERVAL '{ttl_seconds} seconds'`). Replaced with a fully parameterised `$7` binding using a `datetime | None` value.

- **Blocking event loop** — `SemanticCache._embed()` called the Bedrock SDK synchronously inside async methods, stalling the event loop on every cache lookup and write. Split into `_embed_sync()` + async `_embed()` that offloads via `run_in_executor`.

- **Deprecated `asyncio.get_event_loop()`** — replaced with `asyncio.get_running_loop()` in `bedrock_provider.py`, `rate_limiter.py`, and `authenticator.py`. Also removed the stale `self._loop` stored at `__init__` time in `BedrockProvider` (a different event loop may be running at call time in Lambda).

- **Health status `"degraded"` never returned** — `/health` only returned `"ok"` or `"unhealthy"`. Now returns `"degraded"` when some but not all providers are down, which is the common real-world failure mode.

**Code quality**

- **Duplicate `_fetch_secret`** — the function existed identically in both `gateway/app.py` and `health_checker.py`. Extracted to `ai_platform/utils.py` and both files import from there.

- **Health checker log format** — logger calls used malformed f-string patterns (`logger.info(f'"event: {val}"', extra={})`) that produced double-quoted strings in CloudWatch. Fixed to `logger.info("event", extra={"key": val})`.

**Config-driven behaviour**

- **Hardcoded complexity thresholds** — `policies.py` `select_tier()` hardcoded `0.30` and `0.70`. Now reads `settings.complexity_low_threshold` and `settings.complexity_mid_threshold` so thresholds can be tuned via environment variables without code changes.

**New features**

- **`model_preference` routing** — `InferenceRequest.model_preference` was accepted but silently ignored. The router now attempts the named provider first (case-insensitive substring match on provider name or model ID) before falling back to the normal tier chain.

- **Streaming SSE endpoint** — `POST /v1/chat/stream` returns `text/event-stream`. Each token is emitted as `data: <token>\n\n`. Cache hits are served as a single synthetic SSE event. Final event is always `data: [DONE]\n\n`.

**Infrastructure**

- Added `POST /v1/chat/stream` route to API Gateway (`terraform/modules/api_gateway/main.tf`).
- Added `pg_secret_arn` field to `Settings` so the Lambda can resolve the Aurora DSN from the RDS-managed secret at cold start — no plaintext password in environment variables.

### Rebuild and redeploy after changes

```bash
# 1. Rebuild the zip with updated source
cd ai-platform
cp -r ai_platform package/
cd package && zip -r ../dist/ai-platform.zip . -q

# 2. Push to Lambda
aws lambda update-function-code \
  --function-name ai-platform-gateway-production \
  --zip-file fileb://dist/ai-platform.zip \
  --architectures arm64

# 3. Apply any Terraform changes (e.g. new API GW routes)
cd ../terraform && terraform apply
```

---

## Phase 9 — Tests

**Goal:** Catch routing and policy bugs before they reach production.

```bash
cd ai-platform
python -m pytest tests/ -v --tb=short
```

| File | Tests | Covers |
|------|-------|--------|
| `tests/test_policies.py` | 13 | `estimate_complexity()` scoring, `select_tier()` tier selection |
| `tests/test_router.py` | 5 | End-to-end routing, provider fallback, exhausted-providers error, callback |

All 18 tests run in under 2 seconds with no network calls — providers are fully mocked via `AsyncMock`. Tests run automatically in CI on every push and PR.

> When using `pytest.raises(RuntimeError, match=...)`, match on `"All providers exhausted"` — that is the actual error message raised by the router.

---

## Phase 10 — Production Hardening

**Goal:** Tighten security and reliability for real traffic.

### WAF

> **Known limitation:** WAFv2 `AssociateWebACL` does **not** support API Gateway v2 HTTP APIs. Supported targets are REST API (v1), ALB, CloudFront, AppSync, and Cognito. This platform uses API Gateway v2 (70% cheaper) — WAF cannot be directly attached.

> **Current protection:** API Gateway throttling (200 rps sustained / 500 burst) + DynamoDB API key auth + sliding-window rate limiting covers the primary attack surface.

> **To add WAF later:** Place CloudFront in front of the API Gateway and attach a WAF Web ACL at `scope = "CLOUDFRONT"`. This also adds a global CDN layer.

> **Additional debugging note:** `aws_apigatewayv2_stage.arn` outputs an ARN with an empty account ID field (`arn:aws:apigateway:region::/apis/...`). If you ever need the stage ARN with account ID, construct it explicitly:
> ```
> arn:aws:apigateway:${region}:${account_id}:/apis/${api_id}/stages/${stage_name}
> ```

### Provisioned Concurrency

Eliminates cold starts for the gateway Lambda:

```hcl
resource "aws_lambda_provisioned_concurrency_config" "gateway" {
  function_name                      = aws_lambda_function.gateway.function_name
  qualifier                          = aws_lambda_alias.live.name
  provisioned_concurrent_executions  = 2
}
```

### Final Production Checklist

- [ ] WAF — add CloudFront layer if external-facing WAF is required (API GW v2 does not support WAFv2 directly)
- [ ] `deletion_protection = true` on Aurora cluster
- [ ] S3 Terraform state bucket has versioning enabled
- [ ] All secrets in Secrets Manager, not in env vars
- [ ] CloudWatch alarms active and SNS email confirmed
- [ ] At least 2 API keys exist for different applications
- [ ] Cache hit rate > 20% after 24 hours of traffic
- [ ] Load test: `ab -n 1000 -c 10 -H "Authorization: Bearer $API_KEY" $API_URL/health`

---

## Quick Reference

### Useful commands

```bash
# Get the live API URL
cd terraform && terraform output api_gateway_url

# View Lambda logs live
FUNCTION_NAME=$(cd terraform && terraform output -raw lambda_function_name)
aws logs tail /aws/lambda/$FUNCTION_NAME --follow

# Force redeploy without Terraform
aws lambda update-function-code \
  --function-name "$FUNCTION_NAME" \
  --zip-file fileb://ai-platform/dist/ai-platform.zip \
  --architectures arm64

# Check cache hit rate (last hour)
START=$(date -u -v-1H +%Y-%m-%dT%H:%M:%SZ)   # macOS
# START=$(date -u -d '1 hour ago' +%Y-%m-%dT%H:%M:%SZ)  # Linux
aws cloudwatch get-metric-statistics \
  --namespace ai-platform/inference --metric-name CacheHit \
  --start-time "$START" --end-time "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
  --period 3600 --statistics Sum

# Revoke an API key
aws dynamodb update-item \
  --table-name ai-platform-api-keys-production \
  --key '{"key_hash": {"S": "<hash>"}}' \
  --update-expression "SET active = :false" \
  --expression-attribute-values '{":false": {"BOOL": false}}'

# Destroy all infrastructure
cd terraform && terraform destroy
```

### Streaming endpoint

```bash
# Tokens arrive as SSE events; [DONE] signals completion
curl -N -X POST $API_URL/v1/chat/stream \
  -H "Authorization: Bearer $API_KEY" \
  -H "Content-Type: application/json" \
  -H "Accept: text/event-stream" \
  -d '{"messages": [{"role": "user", "content": "Explain recursion briefly."}], "metadata": {"budget": "low"}}'

# Expected output format:
# data: Recursion is...
# data:  a function...
# data: [DONE]
```

### Model preference pinning

Force a specific provider regardless of complexity score:

```bash
curl -X POST $API_URL/v1/chat \
  -H "Authorization: Bearer $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "messages": [{"role": "user", "content": "Hello"}],
    "model_preference": "sonnet"
  }'
# Routes to anthropic-sonnet. Falls back to normal tier routing if unavailable.
# Match is case-insensitive substring on provider name OR model ID.
```

### Tuning routing thresholds without code changes

```bash
# Override complexity thresholds via Lambda environment variables
aws lambda update-function-configuration \
  --function-name ai-platform-gateway-production \
  --environment "Variables={COMPLEXITY_LOW_THRESHOLD=0.25,COMPLEXITY_MID_THRESHOLD=0.65}"
```

### Adding a new LLM provider

1. Create `ai-platform/ai_platform/providers/new_provider.py` implementing `BaseProvider`
2. Add a config function returning `ProviderConfig` with the correct tier and cost
3. Import and instantiate in `gateway/app.py` lifespan
4. Add to the appropriate tier list in `providers_by_tier`
5. If the provider needs an API key, add a secret ARN field to `Settings` and resolve it in the lifespan the same way `anthropic_key` and `openai_key` are resolved

No other files need to change.

---

## Coding conventions

These conventions were established through fixes made to the codebase. Follow them to avoid reintroducing the same classes of bugs.

**Async I/O**

- Never call blocking SDK methods (boto3, asyncpg, Redis) directly inside `async def`. Always wrap in `run_in_executor`.
- Use `asyncio.get_running_loop()` inside async functions. Never use `asyncio.get_event_loop()` — it is deprecated in Python 3.10+ when called from a running loop.
- Never store `self._loop = asyncio.get_event_loop()` in `__init__`. The event loop at `__init__` time may differ from the loop at call time in Lambda.

```python
# Wrong
result = self._boto3_client.invoke_model(...)  # blocks event loop

# Right
loop = asyncio.get_running_loop()
result = await loop.run_in_executor(None, self._invoke_sync, args)
```

**Parameterised SQL**

- Never interpolate variables into SQL strings. Always use asyncpg positional parameters (`$1`, `$2`, ...).

```python
# Wrong — SQL injection risk
await pg.execute(f"... expires_at = NOW() + INTERVAL '{ttl} seconds'")

# Right
expires_at = datetime.utcnow() + timedelta(seconds=ttl) if ttl else None
await pg.execute("... expires_at = $7", ..., expires_at)
```

**Shared utilities**

- If a helper is used in more than one module, put it in `ai_platform/utils.py`. Do not duplicate it.

**Logging**

- Use structured logging: `logger.info("event_name", extra={"key": value})`.
- Never embed values directly in the message string with f-strings — it breaks CloudWatch Logs Insights queries.

```python
# Wrong
logger.info(f'"provider_healthy: {name} latency={ms}ms"')

# Right
logger.info("provider_healthy", extra={"provider": name, "latency_ms": ms})
```

**Settings and config**

- Every tunable value belongs in `Settings` (`config/settings.py`) as a typed field with a sensible default. Never hardcode thresholds, timeouts, or table names in business logic.
- `get_settings()` is `@lru_cache` — it is called once per cold start. To inject values at startup (e.g. a DSN resolved from a secret), set `os.environ["FIELD_NAME"]` before the first call, or pass the value explicitly to the consuming class.

---

*Last updated: 2026-04-13 — Phase 11 complete. Streaming endpoint live, model_preference routing added, bug fixes applied. Live URL: `https://ozj6y1pi1g.execute-api.us-east-1.amazonaws.com/`*
