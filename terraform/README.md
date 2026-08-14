# Terraform Infrastructure

This directory defines the AWS infrastructure for the Multi-LLM Platform. The root module composes networking, data stores, authentication, compute, API Gateway, health checks, monitoring, and CI/CD into one deployment.

Use the [implementation guide](../IMPLEMENTATION_GUIDE.md) for the complete first-deployment and verification workflow. This document explains the Terraform layout and day-to-day infrastructure workflow.

## What Terraform manages

| Module | Responsibility |
|---|---|
| `networking` | VPC, private subnets, security groups, and AWS service endpoints |
| `auth` | DynamoDB auth, rate-limit, health, and usage tables; provider and bootstrap secrets |
| `caching` | ElastiCache Serverless and Aurora Serverless PostgreSQL with pgvector connectivity |
| `lambda_router` | Gateway Lambda, IAM permissions, alias, and provisioned concurrency |
| `api_gateway` | HTTP API routes and Lambda integration |
| `health_checker` | Scheduled provider-health Lambda and EventBridge trigger |
| `monitoring` | CloudWatch dashboard, alarms, and SNS notifications |
| `ci_cd` | GitHub OIDC provider and scoped Lambda deployment role |

The root `main.tf` wires these modules together. `terraform apply` also runs `scripts/migrate_pgvector.sh` through the RDS Data API after Aurora is ready.

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
| `anthropic_api_key` | Yes | Stored in Secrets Manager; also present in protected state |
| `openai_api_key` | No | Optional OpenAI credential stored in Secrets Manager |
| `alert_email` | Yes | SNS destination for CloudWatch alarms |

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

The `terraform_data.pgvector_migration` resource runs the migration after the Aurora cluster is ready. It re-runs when the cluster changes or `scripts/migrate_pgvector.sh` changes.

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
