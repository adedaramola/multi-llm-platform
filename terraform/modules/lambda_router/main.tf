locals {
  function_name = "ai-platform-gateway-${var.environment}"
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
    Statement = [
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
        # DynamoDB — auth tables + health registry
        Effect = "Allow"
        Action = [
          "dynamodb:GetItem", "dynamodb:PutItem",
          "dynamodb:UpdateItem", "dynamodb:Scan"
        ]
        Resource = [
          "arn:aws:dynamodb:${var.aws_region}:*:table/${var.api_keys_table_name}",
          "arn:aws:dynamodb:${var.aws_region}:*:table/${var.rate_limit_table_name}",
          "arn:aws:dynamodb:${var.aws_region}:*:table/${var.health_table_name}",
        ]
      },
      {
        # Secrets Manager — read API keys
        Effect   = "Allow"
        Action   = ["secretsmanager:GetSecretValue"]
        Resource = [var.anthropic_secret_arn, var.openai_secret_arn, var.pg_secret_arn]
      },
      {
        # Bedrock — inference + embeddings
        Effect   = "Allow"
        Action   = ["bedrock:InvokeModel"]
        Resource = "*"
      },
      {
        # VPC — required for Lambda in VPC
        Effect = "Allow"
        Action = [
          "ec2:CreateNetworkInterface",
          "ec2:DescribeNetworkInterfaces",
          "ec2:DeleteNetworkInterface"
        ]
        Resource = "*"
      }
    ]
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

  vpc_config {
    subnet_ids         = var.private_subnet_ids
    security_group_ids = [var.lambda_sg_id]
  }

  environment {
    variables = {
      ENVIRONMENT          = var.environment
      AWS_REGION_NAME      = var.aws_region
      REDIS_URL            = "rediss://${var.redis_endpoint}:6379"
      API_KEYS_TABLE       = var.api_keys_table_name
      RATE_LIMIT_TABLE     = var.rate_limit_table_name
      HEALTH_TABLE         = var.health_table_name
      ANTHROPIC_SECRET_ARN = var.anthropic_secret_arn
      OPENAI_SECRET_ARN    = var.openai_secret_arn
      PG_SECRET_ARN        = var.pg_secret_arn
      CACHE_ENABLED        = "true"
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

# ── Lambda Function URL (alternative to API GW for lower latency) ─────────────
# Kept disabled by default; API GW provides throttling + WAF
# resource "aws_lambda_function_url" "gateway" { ... }
