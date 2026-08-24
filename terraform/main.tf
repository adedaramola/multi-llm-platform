terraform {
  required_version = ">= 1.7"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.0"
    }
  }

  # Partial backend config — bucket is provided via backend.hcl (gitignored).
  # Run: terraform init -backend-config=backend.hcl
  backend "s3" {
    key     = "ai-platform/terraform.tfstate"
    region  = "us-east-1"
    encrypt = true
  }
}

provider "aws" {
  region = var.aws_region

  default_tags {
    tags = {
      Project     = "ai-platform"
      Environment = var.environment
      ManagedBy   = "terraform"
    }
  }
}

# ── Networking ────────────────────────────────────────────────────────────────
module "networking" {
  count       = var.cache_enabled ? 1 : 0
  source      = "./modules/networking"
  environment = var.environment
  aws_region  = var.aws_region
}

# ── Auth (DynamoDB tables + Secrets Manager) ──────────────────────────────────
module "auth" {
  source            = "./modules/auth"
  environment       = var.environment
  aws_region        = var.aws_region
  anthropic_enabled = var.enable_anthropic_provider
  openai_enabled    = var.enable_openai_provider
  anthropic_key     = var.anthropic_api_key
  openai_key        = var.openai_api_key
}

# ── Caching (ElastiCache Serverless + RDS Aurora Serverless pgvector) ─────────
module "caching" {
  count              = var.cache_enabled ? 1 : 0
  source             = "./modules/caching"
  environment        = var.environment
  vpc_id             = module.networking[0].vpc_id
  private_subnet_ids = module.networking[0].private_subnet_ids
  lambda_sg_id       = module.networking[0].lambda_sg_id
  cache_sg_id        = module.networking[0].cache_sg_id
  min_capacity       = var.aurora_min_capacity
  max_capacity       = var.aurora_max_capacity
  auto_pause_seconds = var.aurora_seconds_until_auto_pause
}

# Idempotent schema bootstrap. Re-runs only when the migration script changes
# or when Terraform creates a new Aurora cluster.
resource "terraform_data" "pgvector_migration" {
  count = var.cache_enabled ? 1 : 0

  triggers_replace = [
    module.caching[0].pg_cluster_arn,
    filesha256("${path.module}/scripts/migrate_pgvector.sh"),
  ]

  provisioner "local-exec" {
    command = "${path.module}/scripts/migrate_pgvector.sh"
    environment = {
      AWS_REGION_NAME = var.aws_region
      CLUSTER_ARN     = module.caching[0].pg_cluster_arn
      SECRET_ARN      = module.caching[0].pg_secret_arn
      DATABASE_NAME   = "ai_platform"
    }
  }

  depends_on = [module.caching]
}

# ── Lambda Gateway ────────────────────────────────────────────────────────────
module "lambda_router" {
  source             = "./modules/lambda_router"
  environment        = var.environment
  aws_region         = var.aws_region
  vpc_enabled        = var.cache_enabled
  private_subnet_ids = var.cache_enabled ? module.networking[0].private_subnet_ids : []
  lambda_sg_id       = var.cache_enabled ? module.networking[0].lambda_sg_id : ""

  # Auth
  api_keys_table_name   = module.auth.api_keys_table_name
  rate_limit_table_name = module.auth.rate_limit_table_name
  health_table_name     = module.auth.health_table_name
  usage_table_name      = module.auth.usage_table_name
  anthropic_secret_arn  = module.auth.anthropic_secret_arn
  openai_secret_arn     = module.auth.openai_secret_arn

  # Cache
  cache_enabled  = var.cache_enabled
  redis_endpoint = var.cache_enabled ? module.caching[0].redis_endpoint : ""
  pg_secret_arn  = var.cache_enabled ? module.caching[0].pg_secret_arn : ""
  pg_host        = var.cache_enabled ? module.caching[0].pg_cluster_endpoint : ""

  enable_provisioned_concurrency = var.enable_provisioned_concurrency
  bedrock_enabled                = var.enable_bedrock_provider
  anthropic_enabled              = var.enable_anthropic_provider
  openai_enabled                 = var.enable_openai_provider

  depends_on = [terraform_data.pgvector_migration]
}

# ── API Gateway ───────────────────────────────────────────────────────────────
module "api_gateway" {
  source              = "./modules/api_gateway"
  environment         = var.environment
  lambda_invoke_arn   = module.lambda_router.lambda_invoke_arn
  lambda_arn          = module.lambda_router.lambda_arn
  lambda_function_arn = module.lambda_router.lambda_function_arn
}

# ── Provider Health Checker (EventBridge scheduled) ──────────────────────────
module "health_checker" {
  count = var.enable_scheduled_health_checks ? 1 : 0

  source               = "./modules/health_checker"
  environment          = var.environment
  aws_region           = var.aws_region
  health_table_name    = module.auth.health_table_name
  anthropic_secret_arn = module.auth.anthropic_secret_arn
  openai_secret_arn    = module.auth.openai_secret_arn
  bedrock_enabled      = var.enable_bedrock_provider
  anthropic_enabled    = var.enable_anthropic_provider
  openai_enabled       = var.enable_openai_provider
}

# ── CI/CD (GitHub Actions OIDC) ───────────────────────────────────────────────
module "ci_cd" {
  source                      = "./modules/ci_cd"
  github_repo                 = "adedaramola/multi-llm-platform"
  gateway_function_arn        = module.lambda_router.lambda_function_arn
  health_checker_function_arn = var.enable_scheduled_health_checks ? module.health_checker[0].function_arn : ""
  provisioned_concurrency     = var.enable_provisioned_concurrency
}

# ── Monitoring ────────────────────────────────────────────────────────────────
module "monitoring" {
  source               = "./modules/monitoring"
  environment          = var.environment
  lambda_function_name = module.lambda_router.lambda_function_name
  alert_email          = var.alert_email
}
