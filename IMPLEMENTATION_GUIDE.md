# Multi-LLM Platform — Step-by-Step Implementation Guide

Each phase builds on the previous. Do not skip ahead — later steps assume earlier ones are done.

## Progress Tracker

| Phase | Status | Notes |
|-------|--------|-------|
| Prerequisites | ✅ Done | AWS CLI, Terraform, Python 3.12, Docker |
| Phase 1 — Local Service | ✅ Done | Gateway runs, routes to Haiku, real responses confirmed |
| Phase 2 — AWS Foundation | ✅ Done | VPC, DynamoDB, Secrets Manager, Aurora+pgvector, ElastiCache all live |
| Phase 3 — Lambda Deploy | ⬅ Next | Package zip, terraform apply lambda+apigw, smoke test |
| Phase 4 — Auth | 🔲 | Seed real API key, remove dev bypass |
| Phase 5 — Monitoring | 🔲 | CloudWatch dashboard + SNS alerts |
| Phase 6 — Semantic Cache | 🔲 | Wire Redis + Aurora endpoints into Lambda |
| Phase 7 — Health Checker | 🔲 | Scheduled Lambda for circuit breaking |
| Phase 8 — CI/CD | 🔲 | GitHub Actions + OIDC |
| Phase 9 — Tests | 🔲 | pytest for router logic |
| Phase 10 — Hardening | 🔲 | WAF, provisioned concurrency |

---

---

## Prerequisites (Do this before anything else)

- [ ] AWS account with admin access
- [ ] AWS CLI installed and configured (`aws configure`)
- [ ] Terraform >= 1.7 installed (`terraform -version`)
- [ ] Python 3.12 installed (`python3 --version`)
- [ ] Docker Desktop installed (for local testing)
- [ ] API keys ready: Anthropic (`sk-ant-...`) and optionally OpenAI (`sk-...`)

---

## Phase 1 — Local Service (Run it on your machine first)

**Goal:** Get the gateway running locally before touching AWS.

### Step 1 — Set up the Python project

```bash
# From the repo root
cd ai-platform

python3 -m venv .venv
source .venv/bin/activate

pip install -r requirements.txt
```

### Step 2 — Create a local `.env` file

Make sure `ai-platform/.env` is in your `.gitignore` before creating it:

```bash
echo ".env" >> .gitignore
echo "dist/" >> .gitignore
echo "__pycache__/" >> .gitignore
echo ".venv/" >> .gitignore
```

```bash
# ai-platform/.env  — NEVER commit this file
ENVIRONMENT=dev
ANTHROPIC_API_KEY=sk-ant-your-key-here
OPENAI_API_KEY=sk-your-key-here
CACHE_ENABLED=false          # disable cache for now — no Redis/Postgres yet
AWS_REGION=us-east-1
API_KEYS_TABLE=ai-platform-api-keys
RATE_LIMIT_TABLE=ai-platform-rate-limits
HEALTH_TABLE=ai-platform-provider-health
REDIS_URL=redis://localhost:6379
PG_DSN=postgresql://localhost/ai_platform
```

### Step 3 — Run the gateway locally

```bash
# From ai-platform/
uvicorn ai_platform.gateway.app:app --host 0.0.0.0 --port 8080 --reload
```

### Step 4 — Test with curl (no auth yet — we'll add that in Step 5)

```bash
# Health check
curl http://localhost:8080/health

# Chat request
curl -X POST http://localhost:8080/v1/chat \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer test-key" \
  -d '{
    "messages": [{"role": "user", "content": "What is 2+2?"}],
    "metadata": {"budget": "low"}
  }'
```

**Expected:** Response routes to Claude Haiku (low budget, simple question).

### Step 5 — Write a mock auth for local dev

Until DynamoDB is set up, patch the authenticator to accept any key in dev mode.
Add to `auth/authenticator.py`:

```python
# At the top of get_caller_identity()
if get_settings().environment == "dev":
    return CallerIdentity(
        caller_id="dev-user",
        app_name="local",
        rpm_limit=1000,
        rpd_limit=100_000,
        active=True,
    )
```

> **Package structure note:** All Python subpackages (`gateway/`, `router/`, `providers/`, etc.)
> must live inside an `ai_platform/` wrapper directory at `ai-platform/ai_platform/`.
> The relative imports (`from ..auth.authenticator import ...`) require this parent package.
> The uvicorn command `ai_platform.gateway.app:app` depends on it.
> This is already set up correctly — do not move files out of `ai_platform/`.

**Checkpoint:** Gateway starts, routes to Claude Haiku on `budget=low`, returns real response with token tracking. ✓

---

## Phase 2 — AWS Foundation (Core infrastructure, no app yet)

**Goal:** Get the AWS building blocks running with Terraform.

### Step 6 — Create an S3 bucket for Terraform state

> Include your AWS account ID in the bucket name — S3 names are globally unique and this avoids collisions.

```bash
# Get your account ID
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)

aws s3api create-bucket \
  --bucket ai-platform-tfstate-${ACCOUNT_ID} \
  --region us-east-1

aws s3api put-bucket-versioning \
  --bucket ai-platform-tfstate-${ACCOUNT_ID} \
  --versioning-configuration Status=Enabled

aws s3api put-bucket-encryption \
  --bucket ai-platform-tfstate-${ACCOUNT_ID} \
  --server-side-encryption-configuration \
  '{"Rules":[{"ApplyServerSideEncryptionByDefault":{"SSEAlgorithm":"AES256"}}]}'

aws s3api put-public-access-block \
  --bucket ai-platform-tfstate-${ACCOUNT_ID} \
  --public-access-block-configuration "BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true"
```

### Step 7 — Update the Terraform backend

Edit `terraform/main.tf`, fill in your bucket:

```hcl
backend "s3" {
  bucket  = "ai-platform-tfstate-<YOUR_ACCOUNT_ID>"
  key     = "ai-platform/terraform.tfstate"
  region  = "us-east-1"
  encrypt = true
}
```

### Step 8 — Create your tfvars file

```bash
# From the repo root
cd terraform

cp terraform.tfvars.example terraform.tfvars
# Edit terraform.tfvars — fill in your keys and alert email
```

> **Important:** Add `terraform.tfvars` to `.gitignore` — it contains your API keys.
> ```bash
> echo "terraform.tfvars" >> ../.gitignore
> echo "*.tfstate" >> ../.gitignore
> echo "*.tfstate.backup" >> ../.gitignore
> echo ".terraform/" >> ../.gitignore
> ```

### Step 9 — Deploy only the networking and auth modules first

```bash
terraform init

# Deploy networking first — everything else depends on it
terraform apply -target=module.networking -target=module.auth
```

**What gets created:**
- VPC with 2 private + 2 public subnets across 2 AZs
- NAT Gateway (Lambda needs this to reach external provider APIs)
- Security groups (Lambda SG, Cache SG)
- VPC endpoints for DynamoDB and Secrets Manager (avoids NAT cost for AWS-internal calls)
- DynamoDB tables: `ai-platform-api-keys-production`, `ai-platform-rate-limits-production`, `ai-platform-provider-health-production`
- Secrets Manager secrets with your Anthropic + OpenAI keys

**Expected time:** ~3 minutes (NAT Gateway is the slow part).

### Step 10 — Deploy the caching layer

```bash
terraform apply -target=module.caching
```

**Expected time:** ElastiCache ~2 min, Aurora cluster ~1 min, Aurora instance ~6 min.

> **Known issue fixed in code:** The original Aurora engine version `15.4` does not exist in AWS.
> The correct version is `16.9`. This is already fixed in `modules/caching/main.tf`.

### Step 11 — Run the pgvector database migration

> **Important:** Aurora is deployed in a **private VPC subnet** — you cannot reach it with `psql`
> directly from your local machine. Use the **RDS Data API** via AWS CLI instead.
> The `enable_http_endpoint = true` setting on the cluster enables this (already set in Terraform).

```bash
# Get the cluster ARN and its auto-managed secret ARN
CLUSTER_ARN=$(aws rds describe-db-clusters \
  --db-cluster-identifier ai-platform-pgvector-production \
  --query "DBClusters[0].DBClusterArn" --output text)

# List secrets to find the auto-generated RDS secret (starts with "rds!")
SECRET_ARN=$(aws secretsmanager list-secrets \
  --query "SecretList[?starts_with(Name, 'rds!')].ARN" \
  --output text)

echo "Cluster: $CLUSTER_ARN"
echo "Secret:  $SECRET_ARN"
```

Run each SQL statement individually — the Data API does **not** support multi-statement calls:

```bash
# Helper function to run SQL via Data API
run_sql() {
  aws rds-data execute-statement \
    --resource-arn "$CLUSTER_ARN" \
    --secret-arn "$SECRET_ARN" \
    --database "ai_platform" \
    --sql "$1"
}

run_sql "CREATE EXTENSION IF NOT EXISTS vector"

run_sql "CREATE TABLE IF NOT EXISTS semantic_cache (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    prompt_hash   TEXT NOT NULL UNIQUE,
    embedding     VECTOR(1536) NOT NULL,
    response      TEXT NOT NULL,
    model_used    TEXT NOT NULL,
    input_tokens  INT DEFAULT 0,
    output_tokens INT DEFAULT 0,
    created_at    TIMESTAMPTZ DEFAULT NOW(),
    expires_at    TIMESTAMPTZ
)"

run_sql "CREATE INDEX IF NOT EXISTS semantic_cache_embedding_idx
    ON semantic_cache USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100)"

run_sql "CREATE INDEX IF NOT EXISTS semantic_cache_hash_idx ON semantic_cache (prompt_hash)"

run_sql "CREATE INDEX IF NOT EXISTS semantic_cache_expires_idx
    ON semantic_cache (expires_at) WHERE expires_at IS NOT NULL"
```

Verify the table exists:
```bash
run_sql "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'"
```

**Checkpoint:** AWS networking, auth tables, and cache stores exist. ✓

---

## Phase 3 — Deploy the Lambda Gateway

**Goal:** Package and deploy the Python service to Lambda.

### Step 12 — Package the Lambda zip

```bash
# From the repo root
cd ai-platform

# Install dependencies into a local folder
pip install -r requirements.txt --target ./package

# Zip everything together
cd package && zip -r ../dist/ai-platform.zip . && cd ..
zip -r dist/ai-platform.zip ai_platform/
```

### Step 13 — Deploy Lambda + API Gateway

```bash
cd ../terraform
terraform apply -target=module.lambda_router -target=module.api_gateway
```

### Step 14 — Smoke test the live endpoint

```bash
# Get your API URL
API_URL=$(terraform output -raw api_gateway_url)
echo $API_URL

# Health check
curl $API_URL/health

# Chat (still using mock auth in dev — replace with real key after Step 15)
curl -X POST $API_URL/v1/chat \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer test-key" \
  -d '{"messages": [{"role": "user", "content": "Hello"}]}'
```

**Checkpoint:** Gateway is live on AWS, routing real requests. ✓

---

## Phase 4 — Auth and Rate Limiting (Make it real)

**Goal:** Issue real API keys, remove the dev bypass.

### Step 15 — Seed the first API key

```bash
# Generate a key
API_KEY=$(openssl rand -hex 32)

# Hash it — use the correct command for your OS:
# macOS:
KEY_HASH=$(echo -n "$API_KEY" | shasum -a 256 | awk '{print $1}')
# Linux:
# KEY_HASH=$(echo -n "$API_KEY" | sha256sum | awk '{print $1}')

# Write to DynamoDB
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
# Save this — it won't be shown again
```

### Step 16 — Remove the dev auth bypass

Remove the dev shortcut added in Step 5 from `authenticator.py`. Redeploy:

```bash
# From ai-platform/
zip -r dist/ai-platform.zip package/ ai_platform/

FUNCTION_NAME=$(cd ../terraform && terraform output -raw lambda_function_name)
aws lambda update-function-code \
  --function-name "$FUNCTION_NAME" \
  --zip-file fileb://dist/ai-platform.zip
```

### Step 17 — Test with the real API key

```bash
curl -X POST $API_URL/v1/chat \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $API_KEY" \
  -d '{"messages": [{"role": "user", "content": "Summarize what a vector database is"}]}'

# Test rate limiting — hammer it past the rpm limit
for i in {1..65}; do
  curl -s -o /dev/null -w "%{http_code}\n" \
    -X POST $API_URL/v1/chat \
    -H "Authorization: Bearer $API_KEY" \
    -H "Content-Type: application/json" \
    -d '{"messages": [{"role": "user", "content": "hi"}]}'
done
# Should see 429 after request 60
```

**Checkpoint:** Auth works, rate limiting works, gateway is secure. ✓

---

## Phase 5 — Monitoring and Observability

**Goal:** See what the platform is doing in production.

### Step 18 — Deploy monitoring

```bash
cd terraform
terraform apply -target=module.monitoring
```

Check your email — you'll get an SNS subscription confirmation. **Click the link to activate alerts.**

### Step 19 — Check your CloudWatch dashboard

```bash
terraform output cloudwatch_dashboard_url
# Open the URL in your browser
```

You should see:
- Request rate and error count
- p50 / p99 latency
- Token usage
- Cache hit rate
- Estimated cost

### Step 20 — Verify X-Ray traces

In AWS Console → X-Ray → Service Map.
Send a few requests, wait 30 seconds. You should see:
- API Gateway → Lambda segments
- Annotated subsegments: `auth_check`, `cache_lookup`, `routing_decision`, `provider_call`

### Step 21 — Confirm metrics are flowing

```bash
# Query CloudWatch for your custom metrics
aws cloudwatch list-metrics --namespace "ai-platform/inference"
```

You should see: `RequestCount`, `InputTokens`, `OutputTokens`, `LatencyMs`, `CacheHit`, `EstimatedCostUSD`

**Checkpoint:** Full observability — you can see every request, token, and dollar. ✓

---

## Phase 6 — Enable Semantic Cache

**Goal:** Stop paying for the same LLM calls twice.

### Step 22 — Update Lambda env vars with real cache endpoints

> **Security note:** Never put the database password directly in a Lambda environment variable —
> it is visible in the AWS console, CloudTrail logs, and Terraform state.
> The Aurora password lives in Secrets Manager (managed automatically by RDS).
> The Lambda reads it at runtime via the `PG_SECRET_ARN` env var already set by Terraform.

Only the non-secret endpoints need to be set:

```bash
# From terraform/
REDIS_ENDPOINT=$(terraform output -raw redis_endpoint)
FUNCTION_NAME=$(terraform output -raw lambda_function_name)

aws lambda update-function-configuration \
  --function-name "$FUNCTION_NAME" \
  --environment "Variables={CACHE_ENABLED=true,REDIS_URL=rediss://$REDIS_ENDPOINT:6379}"
```

The `semantic_cache.py` already retrieves the Aurora DSN from `PG_SECRET_ARN` at runtime —
no password is ever stored in an environment variable.

### Step 23 — Test the cache

```bash
# First request — cache miss (will call LLM)
time curl -X POST $API_URL/v1/chat \
  -H "Authorization: Bearer $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"messages": [{"role": "user", "content": "What is the capital of France?"}]}'

# Exact same request — should hit Redis (much faster, cache_hit: true)
time curl -X POST $API_URL/v1/chat \
  -H "Authorization: Bearer $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"messages": [{"role": "user", "content": "What is the capital of France?"}]}'

# Semantically similar — should hit pgvector
time curl -X POST $API_URL/v1/chat \
  -H "Authorization: Bearer $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"messages": [{"role": "user", "content": "Tell me the capital city of France"}]}'
```

Check response body for `"cache_hit": true` and `"cache_source": "exact"` or `"semantic"`.

**Checkpoint:** Cache is live and saving LLM calls. Watch costs drop in the dashboard. ✓

---

## Phase 7 — Provider Health Checker

**Goal:** Automated circuit breaking when a provider is down.

### Step 24 — Create the health checker Lambda

Create `ai-platform/health_checker/handler.py`:

```python
"""
Scheduled Lambda — runs every 2 minutes via EventBridge.
Pings each provider and updates the DynamoDB health table.
"""
import asyncio
import time
import boto3
from ai_platform.config.settings import get_settings
from ai_platform.providers.anthropic_provider import AnthropicProvider, haiku_config
from ai_platform.providers.bedrock_provider import BedrockProvider, titan_lite_config

settings = get_settings()
table = boto3.resource("dynamodb").Table(settings.health_table)

PROVIDERS = [
    AnthropicProvider(haiku_config(), settings.anthropic_api_key),
    BedrockProvider(titan_lite_config()),
]

async def check_all():
    for provider in PROVIDERS:
        healthy = await provider.health_check()
        table.put_item(Item={
            "provider_name": provider.name,
            "status": "healthy" if healthy else "unhealthy",
            "consecutive_failures": 0 if healthy else 1,
            "updated_at": int(time.time()),
        })

def handler(event, context):
    asyncio.run(check_all())
```

Add an EventBridge rule in Terraform (`terraform/modules/monitoring/main.tf`):

```hcl
resource "aws_cloudwatch_event_rule" "health_check" {
  name                = "ai-platform-health-check-${var.environment}"
  schedule_expression = "rate(2 minutes)"
}

resource "aws_cloudwatch_event_target" "health_check" {
  rule      = aws_cloudwatch_event_rule.health_check.name
  target_id = "HealthCheckLambda"
  arn       = var.health_lambda_arn
}
```

**Checkpoint:** Providers are automatically marked healthy/unhealthy every 2 minutes. ✓

---

## Phase 8 — CI/CD Pipeline

**Goal:** Never deploy manually again.

### Step 25 — Create GitHub Actions workflow

> **Security note:** Use GitHub OIDC to assume an IAM role instead of storing long-lived
> `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` in GitHub Secrets. Long-lived keys are a
> credential leak risk — if the secret is ever exposed, it stays valid until manually rotated.
> OIDC tokens are short-lived and scoped to a single workflow run.

**First, create the IAM OIDC trust in AWS (one-time setup):**

```bash
# Create the GitHub OIDC provider in your AWS account
aws iam create-open-id-connect-provider \
  --url https://token.actions.githubusercontent.com \
  --client-id-list sts.amazonaws.com \
  --thumbprint-list 6938fd4d98bab03faadb97b34396831e3780aea1

# Create an IAM role that GitHub Actions can assume
# Replace YOUR_GITHUB_ORG/YOUR_REPO with your actual values
aws iam create-role \
  --role-name ai-platform-github-deploy \
  --assume-role-policy-document '{
    "Version": "2012-10-17",
    "Statement": [{
      "Effect": "Allow",
      "Principal": {"Federated": "arn:aws:iam::<YOUR_ACCOUNT_ID>:oidc-provider/token.actions.githubusercontent.com"},
      "Action": "sts:AssumeRoleWithWebIdentity",
      "Condition": {
        "StringEquals": {"token.actions.githubusercontent.com:aud": "sts.amazonaws.com"},
        "StringLike":   {"token.actions.githubusercontent.com:sub": "repo:YOUR_GITHUB_ORG/YOUR_REPO:ref:refs/heads/main"}
      }
    }]
  }'
```

Create `.github/workflows/deploy.yml`:

```yaml
name: Deploy AI Platform

on:
  push:
    branches: [main]

permissions:
  id-token: write   # required for OIDC
  contents: read

jobs:
  deploy:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4

      - name: Configure AWS credentials (OIDC)
        uses: aws-actions/configure-aws-credentials@v4
        with:
          role-to-assume: arn:aws:iam::<YOUR_ACCOUNT_ID>:role/ai-platform-github-deploy
          aws-region: us-east-1

      - name: Set up Python
        uses: actions/setup-python@v5
        with: { python-version: "3.12" }

      - name: Run tests
        run: |
          cd ai-platform
          pip install -r requirements.txt
          pytest tests/ -v

      - name: Package Lambda
        run: |
          cd ai-platform
          pip install -r requirements.txt --target ./package
          cd package && zip -r ../dist/ai-platform.zip . && cd ..
          zip -r dist/ai-platform.zip ai_platform/

      - name: Terraform plan
        working-directory: terraform
        env:
          TF_VAR_anthropic_api_key: ${{ secrets.ANTHROPIC_API_KEY }}
          TF_VAR_openai_api_key: ${{ secrets.OPENAI_API_KEY }}
          TF_VAR_alert_email: ${{ secrets.ALERT_EMAIL }}
        run: |
          terraform init
          terraform plan -out=tfplan

      - name: Terraform apply
        working-directory: terraform
        env:
          TF_VAR_anthropic_api_key: ${{ secrets.ANTHROPIC_API_KEY }}
          TF_VAR_openai_api_key: ${{ secrets.OPENAI_API_KEY }}
          TF_VAR_alert_email: ${{ secrets.ALERT_EMAIL }}
        run: terraform apply tfplan
```

### Step 26 — Add GitHub Secrets

In your GitHub repo → Settings → Secrets → Actions:
- `ANTHROPIC_API_KEY` — your Anthropic API key
- `OPENAI_API_KEY` — your OpenAI API key (optional)
- `ALERT_EMAIL` — email for CloudWatch alarm notifications

> **Do NOT add** `AWS_ACCESS_KEY_ID` or `AWS_SECRET_ACCESS_KEY` — the OIDC role above replaces them.

**Checkpoint:** Push to main = automatic deploy. No manual steps. ✓

---

## Phase 9 — Write Tests

**Goal:** Catch routing bugs and cache regressions before they hit production.

### Step 27 — Unit test the router policies

Create `ai-platform/tests/test_policies.py`:

```python
from ai_platform.router.policies import estimate_complexity, select_tier
from ai_platform.models.schemas import InferenceRequest, BudgetHint

def make_request(content: str, budget: str = "standard", reasoning: bool = False):
    return InferenceRequest(
        messages=[{"role": "user", "content": content}],
        metadata={"budget": budget, "reasoning_required": reasoning}
    )

def test_simple_question_routes_low():
    req = make_request("What is 2+2?", budget="standard")
    assert select_tier(estimate_complexity(req), req.metadata.budget) == "low"

def test_code_question_routes_mid():
    req = make_request("Write a Python function to parse JSON ```code```")
    assert select_tier(estimate_complexity(req), req.metadata.budget) in ("mid", "high")

def test_budget_low_forces_low_tier():
    req = make_request("Design a distributed system architecture", budget="low")
    assert select_tier(estimate_complexity(req), req.metadata.budget) == "low"

def test_budget_high_forces_mid_or_high():
    req = make_request("hi", budget="high")
    assert select_tier(estimate_complexity(req), req.metadata.budget) in ("mid", "high")
```

```bash
cd ai-platform
pytest tests/ -v
```

**Checkpoint:** Tests pass locally and in CI. ✓

---

## Phase 10 — Production Hardening

**Goal:** Tighten security and reliability before opening to real traffic.

### Step 28 — Enable WAF on API Gateway

Add to `terraform/modules/api_gateway/main.tf`:

```hcl
resource "aws_wafv2_web_acl_association" "api_gw" {
  resource_arn = aws_apigatewayv2_stage.main.arn
  web_acl_arn  = aws_wafv2_web_acl.main.arn
}

resource "aws_wafv2_web_acl" "main" {
  name  = "ai-platform-${var.environment}"
  scope = "REGIONAL"

  default_action { allow {} }

  rule {
    name     = "AWSManagedRulesCommonRuleSet"
    priority = 1
    override_action { none {} }
    statement {
      managed_rule_group_statement {
        name        = "AWSManagedRulesCommonRuleSet"
        vendor_name = "AWS"
      }
    }
    visibility_config {
      cloudwatch_metrics_enabled = true
      metric_name                = "CommonRuleSetMetric"
      sampled_requests_enabled   = true
    }
  }

  visibility_config {
    cloudwatch_metrics_enabled = true
    metric_name                = "ai-platform-waf"
    sampled_requests_enabled   = true
  }
}
```

### Step 29 — Enable Lambda provisioned concurrency (eliminate cold starts)

Add to `terraform/modules/lambda_router/main.tf`:

```hcl
resource "aws_lambda_provisioned_concurrency_config" "gateway" {
  function_name                  = aws_lambda_function.gateway.function_name
  qualifier                      = aws_lambda_alias.live.name
  provisioned_concurrent_executions = 2  # keeps 2 warm instances at all times
}
```

### Step 30 — Final production checklist

- [ ] `deletion_protection = true` on Aurora cluster
- [ ] S3 Terraform state bucket has versioning enabled
- [ ] All secrets in Secrets Manager, not in env vars directly
- [ ] CloudWatch alarms are active and email confirmed
- [ ] WAF enabled
- [ ] At least 2 API keys exist for different apps
- [ ] Cache hit rate > 20% after 24 hours of traffic
- [ ] Run a load test: `ab -n 1000 -c 10 -H "Authorization: Bearer $API_KEY" $API_URL/health`

**Checkpoint:** Platform is hardened and production-ready. ✓

---

## Quick Reference

### Useful commands

```bash
# Resolve Lambda function name from Terraform (use this instead of hardcoding)
FUNCTION_NAME=$(cd terraform && terraform output -raw lambda_function_name)

# View Lambda logs live
aws logs tail /aws/lambda/$FUNCTION_NAME --follow

# Check cache hit rate (last hour)
# macOS:
START=$(date -u -v-1H +%Y-%m-%dT%H:%M:%SZ)
# Linux:
# START=$(date -u -d '1 hour ago' +%Y-%m-%dT%H:%M:%SZ)
END=$(date -u +%Y-%m-%dT%H:%M:%SZ)

aws cloudwatch get-metric-statistics \
  --namespace ai-platform/inference \
  --metric-name CacheHit \
  --start-time "$START" \
  --end-time "$END" \
  --period 3600 --statistics Sum

# Force Lambda redeploy without Terraform
aws lambda update-function-code \
  --function-name "$FUNCTION_NAME" \
  --zip-file fileb://ai-platform/dist/ai-platform.zip

# Destroy everything — requires explicit confirmation
cd terraform && terraform destroy
```

### Adding a new LLM provider

1. Create `ai-platform/providers/new_provider.py` implementing `BaseProvider`
2. Add config function returning `ProviderConfig` with correct tier + cost
3. Import and instantiate in `gateway/app.py` lifespan
4. Add to the appropriate tier list in `providers_by_tier`
5. No other files need to change

### Revoking an API key

```bash
aws dynamodb update-item \
  --table-name ai-platform-api-keys-production \
  --key '{"key_hash": {"S": "<hash>"}}' \
  --update-expression "SET active = :false" \
  --expression-attribute-values '{":false": {"BOOL": false}}'
```

---

*Last updated: 2026-03-15 — Platform at end of Phase 1 build.*
