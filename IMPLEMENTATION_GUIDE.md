# Multi-LLM Platform — Implementation Guide

This guide is the shortest supported path from a fresh checkout to a verified AWS deployment. For API details, see [README.md](README.md). For component design and Terraform module details, see [ARCHITECTURE.md](ARCHITECTURE.md).

## 1. Local setup

### Prerequisites

- Python 3.12
- Docker Desktop
- AWS CLI with credentials configured
- Terraform 1.7 or newer
- An AWS account with access to Bedrock and permission to create the resources in this project
- Anthropic and OpenAI API keys

Never commit `.env`, `terraform.tfvars`, `backend.hcl`, Terraform state, saved plan files, or `dist/` artifacts.

### Configure the application

```bash
cp ai-platform/.env.example ai-platform/.env
```

Edit `ai-platform/.env` and supply the provider keys. For local development, keep:

```dotenv
ENVIRONMENT=dev
CACHE_ENABLED=false
ANTHROPIC_API_KEY=sk-ant-...
OPENAI_API_KEY=sk-...
```

In the `dev` environment, any non-empty bearer token is accepted. No DynamoDB table or source-code auth bypass is needed.

### Install, check, and run

```bash
cd ai-platform
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt

ruff check ai_platform tests
mypy
python -m pytest tests/ -v --tb=short

uvicorn ai_platform.gateway.app:app --reload --port 8080
```

In another terminal:

```bash
curl http://localhost:8080/health

curl -X POST http://localhost:8080/v1/chat \
  -H "Authorization: Bearer dev-key" \
  -H "Content-Type: application/json" \
  -d '{"messages":[{"role":"user","content":"Say hello in one sentence."}]}'
```

You can also run the application with Docker:

```bash
cd ai-platform
docker build -t ai-platform .
docker run --env-file .env -p 8080:8080 ai-platform
```

## 2. First AWS deployment

Terraform supports two profiles. The default lean portfolio profile provisions DynamoDB tables, only the enabled Secrets Manager provider credentials, one gateway Lambda, API Gateway, monitoring, and the GitHub Actions OIDC role. It does not provision the health-checker Lambda, provider-health schedule, VPC/NAT, Redis-compatible cache, Aurora pgvector database, or provisioned concurrency.

The full profile adds those optional components without changing the application contract. Do not apply individual modules for a normal deployment.

### Build the Lambda package

Lambda uses `linux/arm64`. Build dependencies inside the Lambda image so native packages are compatible when building from macOS or an x86 host:

```bash
cd ai-platform
mkdir -p dist/package

docker run --rm --platform linux/arm64 \
  -v "$(pwd)":/src \
  --entrypoint /bin/bash \
  public.ecr.aws/lambda/python:3.12-arm64 \
  -c "pip install -r /src/requirements.txt -t /src/dist/package --quiet \
      && cp -r /src/ai_platform /src/dist/package/"

cd dist/package
zip -r ../ai-platform.zip . -q
cd ../../..
```

The resulting `ai-platform/dist/ai-platform.zip` is required by the first Terraform apply.

### Bootstrap remote state

Choose a globally unique S3 bucket name, normally including your AWS account ID:

```bash
cd terraform

STATE_BUCKET="ai-platform-tfstate-<your-aws-account-id>" \
  AWS_REGION_NAME="us-east-1" \
  ./scripts/bootstrap_backend.sh

cp backend.hcl.example backend.hcl
```

Edit `backend.hcl` and replace the placeholder bucket name.

### Configure and apply Terraform

```bash
cp terraform.tfvars.example terraform.tfvars
```

Edit `terraform.tfvars` with the target region, environment, provider API keys, and alert email. Then run:

```bash
terraform init -backend-config=backend.hcl
terraform fmt -check -recursive
terraform validate
terraform plan
terraform apply
```

Review the plan before approving it. A full-profile apply can take several minutes while Aurora, ElastiCache, VPC endpoints, and provisioned Lambda concurrency become ready; the lean profile omits them.

With the default lean values, those long-lived cache resources are omitted. Bedrock and Anthropic are enabled, while OpenAI is an explicit opt-in. Before connecting OpsDesk, set `OPS_AGENT_LLM_GATEWAY_CACHE_POLICY=off` in its AWS overlay. OpsDesk requests use `budget=low`, which is a hard ceiling that cannot fall through to mid- or high-tier models. To demonstrate the full cache profile later, set `cache_enabled = true`; the pgvector migration then runs automatically through the RDS Data API. Enabling scheduled health checks creates the health-checker Lambda and its EventBridge schedule as one opt-in unit.

### Retrieve deployment values

```bash
API_URL=$(terraform output -raw api_gateway_url)
FUNCTION_NAME=$(terraform output -raw lambda_function_name)
DASHBOARD_URL=$(terraform output -raw cloudwatch_dashboard_url)

SECRET_ARN=$(terraform output -raw bootstrap_api_key_secret_arn)
API_KEY=$(aws secretsmanager get-secret-value \
  --secret-id "$SECRET_ARN" \
  --query SecretString \
  --output text)
```

Terraform stores only a hash of the bootstrap API key in DynamoDB; the raw key is stored in Secrets Manager. Treat both Terraform state and the retrieved key as sensitive.

Confirm the SNS subscription sent to the configured alert email, or CloudWatch alarms cannot deliver notifications.

## 3. Verify the deployment

Run this suite after the first apply and after infrastructure changes:

```bash
# Health should return HTTP 200.
curl -sS "$API_URL/health"

# Missing authentication should return 401.
curl -sS -o /dev/null -w "%{http_code}\n" \
  -X POST "$API_URL/v1/chat" \
  -H "Content-Type: application/json" \
  -d '{"messages":[{"role":"user","content":"hello"}]}'

# A valid key should return 200.
curl -sS -o /dev/null -w "%{http_code}\n" \
  -X POST "$API_URL/v1/chat" \
  -H "Authorization: Bearer $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"messages":[{"role":"user","content":"Return one word: healthy"}]}'

# Streaming should emit SSE events and finish with data: [DONE].
curl -N -X POST "$API_URL/v1/chat/stream" \
  -H "Authorization: Bearer $API_KEY" \
  -H "Content-Type: application/json" \
  -H "Accept: text/event-stream" \
  -d '{"messages":[{"role":"user","content":"Stream a short greeting."}]}'

# Review the deployment window for unexpected errors.
aws logs tail "/aws/lambda/$FUNCTION_NAME" --since 10m
```

To verify exact caching, send the same authenticated `/v1/chat` request twice. The second response should report `"cache_hit": true` after the first response has been persisted.

Deployment is ready when:

- Tests, linting, type checks, and Terraform validation pass.
- `/health` returns `200` and providers are not all unhealthy.
- Authentication returns `401` without a key and `200` with the bootstrap key.
- Streaming terminates with `[DONE]`.
- The CloudWatch dashboard opens and recent Lambda logs show no unexpected errors.
- The SNS email subscription is confirmed.

## 4. Routine deployments and access

### CI/CD

Pushes and pull requests run tests and Terraform validation. A push to `main` builds the arm64 package and deploys the gateway through GitHub Actions. The workflow uses the `DEPLOY_ENVIRONMENT` repository variable, defaulting to `dev`, and deploys the health-checker only when `ENABLE_SCHEDULED_HEALTH_CHECKS=true`.

Before relying on CI, store the deployment role ARN as the repository secret `AWS_DEPLOY_ROLE_ARN`:

```bash
aws iam get-role \
  --role-name ai-platform-github-actions \
  --query Role.Arn \
  --output text | gh secret set AWS_DEPLOY_ROLE_ARN
```

The OIDC role is restricted to this repository's `main` branch and does not require long-lived AWS credentials.

### Manual Lambda code update

Prefer CI for routine code updates. If a manual lean-profile update is necessary, rebuild the arm64 zip using the command in section 2, then update the gateway:

```bash
aws lambda update-function-code \
  --function-name ai-platform-gateway-dev \
  --zip-file fileb://ai-platform/dist/ai-platform.zip \
  --architectures arm64

aws lambda wait function-updated \
  --function-name ai-platform-gateway-dev
```

The CI workflow publishes a gateway version and moves the `live` alias. It waits for provisioned concurrency only when `ENABLE_PROVISIONED_CONCURRENCY=true`.

### Additional API keys

Create separate API keys for separate applications. Store only a SHA-256 hash in the API-keys table and deliver the raw key through an approved secret-management channel. Each record needs:

- `key_hash`
- `caller_id`
- `app_name`
- `rpm_limit`
- `rpd_limit`
- `active`
- `created_at`

To revoke a key, set its `active` attribute to `false`; do not delete audit or usage records.

## 5. Operations and troubleshooting

### Useful commands

```bash
cd terraform
terraform output -raw api_gateway_url
terraform output -raw lambda_function_name
terraform output -raw cloudwatch_dashboard_url

aws logs tail "/aws/lambda/$(terraform output -raw lambda_function_name)" --follow
aws cloudwatch list-metrics --namespace ai-platform/inference
```

The primary custom metrics are `RequestCount`, `InputTokens`, `OutputTokens`, `LatencyMs`, `CacheHit`, `EstimatedCostUSD`, and `ErrorCount`.

### Common failures

**Lambda times out while loading secrets**

Secrets Manager uses an interface VPC endpoint. Confirm its security group permits inbound HTTPS from the VPC/Lambda security group. The expected rule is managed in `terraform/modules/networking/main.tf`.

**Redis or PostgreSQL connections hang**

Confirm ElastiCache and Aurora use the dedicated cache security group and permit Lambda traffic on ports `6379` and `5432`. The expected configuration is in `terraform/modules/caching/main.tf`.

**Lambda reports incompatible binaries or `exec format error`**

Rebuild the package inside the `python:3.12-arm64` Lambda image. Do not package native dependencies installed directly on macOS or x86 Linux.

**A provider becomes unavailable**

When `enable_scheduled_health_checks = true`, Terraform creates a health-checker Lambda that updates the provider-health table every five minutes. In the lean profile that Lambda and schedule are both absent; request-driven circuit breakers still persist provider state in DynamoDB. Check gateway logs, Bedrock model access, and enabled provider secrets before changing routing code.

**WAF cannot be associated with the API**

This project uses API Gateway v2 HTTP APIs, which cannot be directly associated with WAFv2. If WAF is required, place CloudFront in front of API Gateway and attach a CloudFront-scoped Web ACL.

### Production checklist

- [ ] Aurora deletion protection is enabled for long-lived production data.
- [ ] The Terraform state bucket is private, encrypted, and versioned.
- [ ] Runtime secrets are stored in Secrets Manager.
- [ ] SNS alarm subscriptions are confirmed.
- [ ] Each consuming application has its own API key and limits.
- [ ] Cache hit rate and cost metrics are reviewed after real traffic begins.
- [ ] Load and recovery testing has been completed for the expected traffic level.
- [ ] CloudFront and WAF are added if the external threat model requires them.

Infrastructure removal is intentionally not included as a quick-reference command. Before destroying an environment, review data retention, deletion protection, Secrets Manager behavior, and the exact Terraform workspace and plan.

---

*Last updated: 2026-07-21*
