locals {
  function_name       = "ai-platform-gateway-${var.environment}"
  runtime_secret_arns = compact([var.anthropic_secret_arn, var.openai_secret_arn, var.pg_secret_arn])
}

# ── IAM Role ──────────────────────────────────────────────────────────────────
resource "aws_iam_role" "lambda" {
  name = "ai-platform-lambda-${var.environment}"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "lambda.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy" "lambda" {
  name = "ai-platform-lambda-policy-${var.environment}"
  role = aws_iam_role.lambda.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = concat(
      [
        {
          # CloudWatch Logs
          Effect   = "Allow"
          Action   = ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"]
          Resource = "arn:aws:logs:${var.aws_region}:*:log-group:/aws/lambda/${local.function_name}:*"
        },
        {
          # X-Ray
          Effect   = "Allow"
          Action   = ["xray:PutTraceSegments", "xray:PutTelemetryRecords"]
          Resource = "*"
        },
        {
          # DynamoDB — auth tables + health registry + usage accounting
          # Query is needed for per-caller month aggregation on the usage table
          Effect = "Allow"
          Action = [
            "dynamodb:GetItem", "dynamodb:PutItem",
            "dynamodb:UpdateItem", "dynamodb:Scan",
            "dynamodb:Query"
          ]
          Resource = [
            "arn:aws:dynamodb:${var.aws_region}:*:table/${var.api_keys_table_name}",
            "arn:aws:dynamodb:${var.aws_region}:*:table/${var.rate_limit_table_name}",
            "arn:aws:dynamodb:${var.aws_region}:*:table/${var.health_table_name}",
            "arn:aws:dynamodb:${var.aws_region}:*:table/${var.usage_table_name}",
          ]
        },
      ],
      length(local.runtime_secret_arns) > 0 ? [
        {
          # Secrets Manager — enabled provider keys and optional pgvector credentials
          Effect   = "Allow"
          Action   = ["secretsmanager:GetSecretValue"]
          Resource = local.runtime_secret_arns
        }
      ] : [],
      var.bedrock_enabled || var.cache_enabled ? [
        {
          # Bedrock — inference and optional semantic-cache embeddings
          Effect   = "Allow"
          Action   = ["bedrock:InvokeModel"]
          Resource = "*"
        }
      ] : [],
      var.vpc_enabled ? [
        {
          # VPC — required only when the private cache stack is enabled
          Effect = "Allow"
          Action = [
            "ec2:CreateNetworkInterface",
            "ec2:DescribeNetworkInterfaces",
            "ec2:DeleteNetworkInterface"
          ]
          Resource = "*"
        }
      ] : []
    )
  })
}

# ── Lambda Function ───────────────────────────────────────────────────────────
resource "aws_lambda_function" "gateway" {
  function_name    = local.function_name
  role             = aws_iam_role.lambda.arn
  filename         = var.lambda_package_path # zip built by CI/CD
  source_code_hash = filebase64sha256(var.lambda_package_path)
  handler          = "ai_platform.gateway.app.handler"
  runtime          = "python3.12"
  architectures    = ["arm64"] # 20% cheaper, same performance
  timeout          = 60        # max provider call time
  memory_size      = 512       # sufficient for FastAPI + boto3 + asyncpg
  publish          = true      # required for provisioned concurrency

  dynamic "vpc_config" {
    for_each = var.vpc_enabled ? [1] : []
    content {
      subnet_ids         = var.private_subnet_ids
      security_group_ids = [var.lambda_sg_id]
    }
  }

  environment {
    variables = {
      ENVIRONMENT          = var.environment
      AWS_REGION_NAME      = var.aws_region
      REDIS_URL            = "rediss://${var.redis_endpoint}:6379"
      API_KEYS_TABLE       = var.api_keys_table_name
      RATE_LIMIT_TABLE     = var.rate_limit_table_name
      HEALTH_TABLE         = var.health_table_name
      USAGE_TABLE          = var.usage_table_name
      ANTHROPIC_SECRET_ARN = var.anthropic_secret_arn
      OPENAI_SECRET_ARN    = var.openai_secret_arn
      BEDROCK_ENABLED      = tostring(var.bedrock_enabled)
      ANTHROPIC_ENABLED    = tostring(var.anthropic_enabled)
      OPENAI_ENABLED       = tostring(var.openai_enabled)
      PG_SECRET_ARN        = var.pg_secret_arn
      PG_HOST              = var.pg_host
      PG_PORT              = "5432"
      PG_DATABASE          = "ai_platform"
      CACHE_ENABLED        = tostring(var.cache_enabled)
      RATE_LIMIT_FAIL_OPEN = "false"
      LOG_LEVEL            = var.environment == "production" ? "INFO" : "DEBUG"
    }
  }

  tracing_config {
    mode = "Active" # X-Ray tracing
  }

  lifecycle {
    ignore_changes = [filename, source_code_hash] # managed by CI/CD
  }
}

resource "aws_cloudwatch_log_group" "lambda" {
  name              = "/aws/lambda/${local.function_name}"
  retention_in_days = 30
}

# ── Alias + Provisioned Concurrency ───────────────────────────────────────────
# "live" alias always points to the latest published version.
# CI/CD publishes a new version after each code deploy and updates this alias.
# Provisioned concurrency is attached to the alias, keeping 2 warm instances
# at all times to eliminate cold starts on the hot path.
resource "aws_lambda_alias" "live" {
  name             = "live"
  function_name    = aws_lambda_function.gateway.function_name
  function_version = aws_lambda_function.gateway.version

  lifecycle {
    ignore_changes = [function_version] # CI/CD updates the alias after each deploy
  }
}

resource "aws_lambda_provisioned_concurrency_config" "gateway" {
  count = var.enable_provisioned_concurrency ? 1 : 0

  function_name                     = aws_lambda_function.gateway.function_name
  qualifier                         = aws_lambda_alias.live.name
  provisioned_concurrent_executions = 2
}

# ── Lambda Function URL (alternative to API GW for lower latency) ─────────────
# Kept disabled by default; API GW provides throttling + WAF
# resource "aws_lambda_function_url" "gateway" { ... }
