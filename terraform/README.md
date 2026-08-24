# Terraform Infrastructure

This directory defines the AWS infrastructure for the Multi-LLM Platform. The root module supports a lean portfolio deployment and an optional full semantic-cache deployment from the same configuration.

Use the [implementation guide](../IMPLEMENTATION_GUIDE.md) for the complete first-deployment and verification workflow. This document explains the Terraform layout and day-to-day infrastructure workflow.

## What Terraform manages

| Module | Responsibility |
|---|---|
| `networking` | Optional VPC, private subnets, security groups, and AWS service endpoints |
| `auth` | DynamoDB auth, rate-limit, health, and usage tables; provider and bootstrap secrets |
| `caching` | Optional ElastiCache Serverless and Aurora Serverless PostgreSQL with pgvector connectivity |
| `lambda_router` | Gateway Lambda, IAM permissions, alias, and optional provisioned concurrency |
| `api_gateway` | HTTP API routes and Lambda integration |
| `health_checker` | Provider-health Lambda and optional EventBridge schedule |
| `monitoring` | CloudWatch dashboard, alarms, and SNS notifications |
| `ci_cd` | GitHub OIDC provider and scoped Lambda deployment role |

The root `main.tf` wires these modules together. When caching is enabled, `terraform apply` also runs `scripts/migrate_pgvector.sh` through the RDS Data API after Aurora is ready.

## Deployment profiles

The checked-in example uses the lean portfolio profile:

```hcl
environment                    = "dev"
enable_bedrock_provider        = true
enable_anthropic_provider      = true
enable_openai_provider         = false
cache_enabled                  = false
enable_provisioned_concurrency = false
enable_scheduled_health_checks = false
```

This profile deploys one gateway Lambda, API Gateway, DynamoDB tables, only the enabled provider secrets, monitoring, and a scoped CI/CD role. It does not create the health-checker Lambda, platform VPC/NAT, ElastiCache, Aurora, pgvector migration, or recurring provider probes. The gateway Lambda runs outside a VPC and does not receive EC2 network-interface permissions.

Set `cache_enabled = true` to add the exact and semantic cache stack. Scheduled health checks and provisioned concurrency are separate opt-ins so they can be enabled only for sustained environments. When caching is disabled, configure consumers such as OpsDesk with `OPS_AGENT_LLM_GATEWAY_CACHE_POLICY=off`.

## Files

```text
terraform/
├── main.tf                       Root module and dependencies
├── variables.tf                  Deployment inputs
├── outputs.tf                    Runtime URLs, names, and bootstrap secret ARN
├── terraform.tfvars.example      Example deployment values
├── backend.hcl.example           Example private S3 backend configuration
├── modules/                      Reusable platform modules
└── scripts/
    ├── bootstrap_backend.sh      Creates or hardens the state bucket
    └── migrate_pgvector.sh       Idempotent database schema migration
```

`backend.hcl`, `terraform.tfvars`, state files, saved plans, and provider credentials must never be committed.

## Prerequisites

- Terraform 1.7 or newer
- AWS CLI credentials with permission to manage the platform resources
- A Lambda package at `../ai-platform/dist/ai-platform.zip`
- Anthropic API key
- Optional OpenAI API key
- Alert notification email

The Lambda package must target Python 3.12 on `linux/arm64`. Build it using the [documented container workflow](../IMPLEMENTATION_GUIDE.md#build-the-lambda-package) before the first apply.

## First initialization

Create a private, encrypted, versioned state bucket:

```bash
STATE_BUCKET="ai-platform-tfstate-<your-aws-account-id>" \
  AWS_REGION_NAME="us-east-1" \
  ./scripts/bootstrap_backend.sh
```

Create the ignored local configuration files:

```bash
cp backend.hcl.example backend.hcl
cp terraform.tfvars.example terraform.tfvars
```

Replace the placeholder bucket in `backend.hcl`, then set the region, environment, provider keys, and alert email in `terraform.tfvars`.

Initialize Terraform:

```bash
terraform init -backend-config=backend.hcl
```

Terraform state contains sensitive values even when CLI output is marked sensitive. Restrict access to the backend bucket and retain encryption, public-access blocking, and versioning.

## Routine workflow

Use a full root-module plan unless you are deliberately recovering a specific resource:

```bash
terraform fmt -check -recursive
terraform validate
terraform plan
terraform apply
```

Review the plan before approving it. In particular, inspect replacements or deletions involving Aurora, DynamoDB, Secrets Manager, networking, Lambda aliases, or the state backend.

Avoid routine use of `-target`. Targeted applies can bypass dependency changes and leave the platform only partially reconciled.

After applying, retrieve operational values with:

```bash
terraform output -raw api_gateway_url
terraform output -raw lambda_function_name
terraform output -raw cloudwatch_dashboard_url
terraform output -raw bootstrap_api_key_secret_arn
```

Then run the [post-deployment verification suite](../IMPLEMENTATION_GUIDE.md#3-verify-the-deployment).

## Inputs

The root module accepts:

| Variable | Required | Purpose |
|---|---|---|
| `aws_region` | No | AWS region; defaults to `us-east-1` |
| `environment` | No | `production`, `staging`, or `dev`; defaults to `production` |
| `anthropic_api_key` | Conditional | Required and stored in Secrets Manager only when Anthropic is enabled |
| `openai_api_key` | Conditional | Required and stored in Secrets Manager only when OpenAI is enabled |
| `alert_email` | Yes | SNS destination for CloudWatch alarms |
| `enable_bedrock_provider` | No | Enable Bedrock models; defaults to `true` |
| `enable_anthropic_provider` | No | Enable Anthropic models and its secret; defaults to `true` |
| `enable_openai_provider` | No | Enable OpenAI models and its secret; defaults to `false` |
| `cache_enabled` | No | Create the VPC, ElastiCache, Aurora, and pgvector cache stack; defaults to `false` |
| `enable_provisioned_concurrency` | No | Keep two gateway environments warm; defaults to `false` |
| `enable_scheduled_health_checks` | No | Probe providers every five minutes; defaults to `false` |
| `aurora_min_capacity` | No | Minimum ACUs when caching is enabled; defaults to `0` |
| `aurora_max_capacity` | No | Maximum ACUs when caching is enabled; defaults to `2` |
| `aurora_seconds_until_auto_pause` | No | Idle time before a zero-minimum Aurora cluster pauses; defaults to `600` |

Prefer protected CI variables such as `TF_VAR_anthropic_api_key` for automation. Regardless of input method, Terraform state will contain sensitive provider values and must be protected.

## Outputs and bootstrap access

The auth module generates an initial client API key. Its hash is stored in DynamoDB and the raw value is stored in Secrets Manager.

```bash
SECRET_ARN=$(terraform output -raw bootstrap_api_key_secret_arn)
aws secretsmanager get-secret-value \
  --secret-id "$SECRET_ARN" \
  --query SecretString \
  --output text
```

Treat the returned value as a production credential. Create separate API keys for individual applications rather than sharing the bootstrap key broadly.

## Database migration

When `cache_enabled = true`, the `terraform_data.pgvector_migration` resource runs the migration after the Aurora cluster is ready. It re-runs when the cluster changes or `scripts/migrate_pgvector.sh` changes.

The migration is idempotent and creates the pgvector extension, semantic-cache table, and indexes. Normal deployments do not require manual SQL execution.

If migration fails, verify:

- The Aurora cluster and RDS Data API are available.
- AWS credentials can call `rds-data:ExecuteStatement`.
- The RDS-managed secret and database name are correct.
- The failure is not an eventual-consistency or cluster-readiness delay.

## CI/CD boundary

Terraform creates the GitHub OIDC provider and its least-privilege deployment role. GitHub Actions validates Terraform but deploys Lambda code with AWS CLI rather than running `terraform apply` on every merge.

Terraform owns Lambda configuration and infrastructure. The workflow owns routine function-code updates and moves the gateway's `live` alias to newly published versions. Lifecycle rules prevent Terraform from reverting CI-managed code and alias changes.

Infrastructure changes still require a reviewed Terraform plan and apply.

## Safety

- Do not commit state, saved plans, `.terraform/`, `backend.hcl`, or `terraform.tfvars`.
- Do not change provider secrets without considering state exposure and rotation order.
- Do not disable state-bucket encryption, public-access blocking, or versioning.
- Do not manually edit Terraform-managed AWS resources except during controlled recovery.
- Do not destroy an environment without reviewing data retention, deletion protection, Secrets Manager recovery behavior, and the selected backend/workspace.
- Confirm SNS email subscriptions after creating or changing alert destinations.

For VPC endpoint, cache connectivity, Lambda packaging, and WAF troubleshooting, see [Operations and troubleshooting](../IMPLEMENTATION_GUIDE.md#5-operations-and-troubleshooting).
