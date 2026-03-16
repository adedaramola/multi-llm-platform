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

  backend "s3" {
    bucket  = "ai-platform-tfstate-900009968072"
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
  source      = "./modules/networking"
  environment = var.environment
  aws_region  = var.aws_region
}

# ── Auth (DynamoDB tables + Secrets Manager) ──────────────────────────────────
module "auth" {
  source        = "./modules/auth"
  environment   = var.environment
  aws_region    = var.aws_region
  anthropic_key = var.anthropic_api_key
  openai_key    = var.openai_api_key
}

# ── Caching (ElastiCache Serverless + RDS Aurora Serverless pgvector) ─────────
module "caching" {
  source             = "./modules/caching"
  environment        = var.environment
  vpc_id             = module.networking.vpc_id
  private_subnet_ids = module.networking.private_subnet_ids
  lambda_sg_id       = module.networking.lambda_sg_id
  cache_sg_id        = module.networking.cache_sg_id
}

# ── Lambda Gateway ────────────────────────────────────────────────────────────
module "lambda_router" {
  source             = "./modules/lambda_router"
  environment        = var.environment
  aws_region         = var.aws_region
  vpc_id             = module.networking.vpc_id
  private_subnet_ids = module.networking.private_subnet_ids
  lambda_sg_id       = module.networking.lambda_sg_id

  # Auth
  api_keys_table_name   = module.auth.api_keys_table_name
  rate_limit_table_name = module.auth.rate_limit_table_name
  health_table_name     = module.auth.health_table_name
  anthropic_secret_arn  = module.auth.anthropic_secret_arn
  openai_secret_arn     = module.auth.openai_secret_arn

  # Cache
  redis_endpoint = module.caching.redis_endpoint
  pg_secret_arn  = module.caching.pg_secret_arn
}

# ── API Gateway ───────────────────────────────────────────────────────────────
module "api_gateway" {
  source            = "./modules/api_gateway"
  environment       = var.environment
  lambda_invoke_arn = module.lambda_router.lambda_invoke_arn
  lambda_arn        = module.lambda_router.lambda_arn
}

# ── Provider Health Checker (EventBridge scheduled) ──────────────────────────
module "health_checker" {
  source               = "./modules/health_checker"
  environment          = var.environment
  aws_region           = var.aws_region
  health_table_name    = module.auth.health_table_name
  anthropic_secret_arn = module.auth.anthropic_secret_arn
  openai_secret_arn    = module.auth.openai_secret_arn
}

# ── CI/CD (GitHub Actions OIDC) ───────────────────────────────────────────────
module "ci_cd" {
  source                      = "./modules/ci_cd"
  github_repo                 = "adedaramola/multi-llm-platform"
  gateway_function_arn        = module.lambda_router.lambda_arn
  health_checker_function_arn = module.health_checker.function_arn
}

# ── Monitoring ────────────────────────────────────────────────────────────────
module "monitoring" {
  source               = "./modules/monitoring"
  environment          = var.environment
  lambda_function_name = module.lambda_router.lambda_function_name
  alert_email          = var.alert_email
}
