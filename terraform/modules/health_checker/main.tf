locals {
  function_name = "ai-platform-health-checker-${var.environment}"
}

# ── IAM Role ──────────────────────────────────────────────────────────────────
resource "aws_iam_role" "health_checker" {
  name = "ai-platform-health-checker-${var.environment}"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "lambda.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy" "health_checker" {
  name = "ai-platform-health-checker-policy-${var.environment}"
  role = aws_iam_role.health_checker.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"]
        Resource = "arn:aws:logs:${var.aws_region}:*:log-group:/aws/lambda/${local.function_name}:*"
      },
      {
        Effect   = "Allow"
        Action   = ["dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:UpdateItem", "dynamodb:Scan"]
        Resource = "arn:aws:dynamodb:${var.aws_region}:*:table/${var.health_table_name}"
      },
      {
        Effect   = "Allow"
        Action   = ["secretsmanager:GetSecretValue"]
        Resource = [var.anthropic_secret_arn, var.openai_secret_arn]
      },
      {
        Effect   = "Allow"
        Action   = ["bedrock:InvokeModel"]
        Resource = "*"
      },
    ]
  })
}

# ── Lambda Function ───────────────────────────────────────────────────────────
# Runs outside VPC — calls external APIs directly; no private resource access needed.
resource "aws_lambda_function" "health_checker" {
  function_name    = local.function_name
  role             = aws_iam_role.health_checker.arn
  filename         = var.lambda_package_path
  source_code_hash = filebase64sha256(var.lambda_package_path)
  handler          = "ai_platform.health_checker.handler"
  runtime          = "python3.12"
  architectures    = ["arm64"]
  timeout          = 60   # 3 providers × 20s timeout + overhead
  memory_size      = 256  # lighter than gateway — no web framework

  environment {
    variables = {
      ENVIRONMENT          = var.environment
      AWS_REGION_NAME      = var.aws_region
      HEALTH_TABLE         = var.health_table_name
      ANTHROPIC_SECRET_ARN = var.anthropic_secret_arn
      OPENAI_SECRET_ARN    = var.openai_secret_arn
      CACHE_ENABLED        = "false"  # not needed for health checks
    }
  }

  lifecycle {
    ignore_changes = [filename, source_code_hash]
  }
}

resource "aws_cloudwatch_log_group" "health_checker" {
  name              = "/aws/lambda/${local.function_name}"
  retention_in_days = 14
}

# ── EventBridge Schedule ──────────────────────────────────────────────────────
resource "aws_cloudwatch_event_rule" "health_check_schedule" {
  name                = "ai-platform-health-check-${var.environment}"
  description         = "Trigger provider health checks every 5 minutes"
  schedule_expression = "rate(5 minutes)"
}

resource "aws_cloudwatch_event_target" "health_checker" {
  rule      = aws_cloudwatch_event_rule.health_check_schedule.name
  target_id = "HealthCheckerLambda"
  arn       = aws_lambda_function.health_checker.arn
}

resource "aws_lambda_permission" "eventbridge" {
  statement_id  = "AllowEventBridgeInvoke"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.health_checker.function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.health_check_schedule.arn
}
