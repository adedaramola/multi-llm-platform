# HTTP API Gateway v2 — 70% cheaper than REST API, native Lambda proxy
resource "aws_apigatewayv2_api" "main" {
  name          = "ai-platform-${var.environment}"
  protocol_type = "HTTP"
  description   = "AI Platform inference gateway"

  cors_configuration {
    allow_methods = ["POST", "GET", "OPTIONS"]
    allow_headers = ["Content-Type", "Authorization", "X-Request-ID"]
    max_age       = 300
  }
}

resource "aws_apigatewayv2_stage" "main" {
  api_id      = aws_apigatewayv2_api.main.id
  name        = "$default"   # $default stage avoids prepending stage name to rawPath
  auto_deploy = true

  access_log_settings {
    destination_arn = aws_cloudwatch_log_group.api_gw.arn
    format = jsonencode({
      requestId      = "$context.requestId"
      ip             = "$context.identity.sourceIp"
      httpMethod     = "$context.httpMethod"
      routeKey       = "$context.routeKey"
      status         = "$context.status"
      latency        = "$context.responseLatency"
      integrationErr = "$context.integrationErrorMessage"
    })
  }

  default_route_settings {
    throttling_burst_limit = 500 # max concurrent in-flight requests
    throttling_rate_limit  = 200 # sustained req/s
  }
}

resource "aws_apigatewayv2_integration" "lambda" {
  api_id                 = aws_apigatewayv2_api.main.id
  integration_type       = "AWS_PROXY"
  integration_uri        = var.lambda_invoke_arn
  payload_format_version = "2.0"
  timeout_milliseconds   = 29000 # API Gateway v2 max is 30000ms
}

resource "aws_apigatewayv2_route" "chat" {
  api_id    = aws_apigatewayv2_api.main.id
  route_key = "POST /v1/chat"
  target    = "integrations/${aws_apigatewayv2_integration.lambda.id}"
}

resource "aws_apigatewayv2_route" "health" {
  api_id    = aws_apigatewayv2_api.main.id
  route_key = "GET /health"
  target    = "integrations/${aws_apigatewayv2_integration.lambda.id}"
}

resource "aws_lambda_permission" "api_gw" {
  statement_id  = "AllowAPIGWInvoke"
  action        = "lambda:InvokeFunction"
  function_name = var.lambda_arn
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${aws_apigatewayv2_api.main.execution_arn}/*/*"
}

resource "aws_cloudwatch_log_group" "api_gw" {
  name              = "/aws/apigw/ai-platform-${var.environment}"
  retention_in_days = 14
}

# ── WAF note ──────────────────────────────────────────────────────────────────
# WAFv2 AssociateWebACL does not support API Gateway v2 HTTP APIs.
# Supported targets: REST API (v1), ALB, CloudFront, AppSync, Cognito.
# This platform uses API Gateway v2 (70% cheaper). WAF can be added by placing
# CloudFront in front of the API and attaching WAF at scope=CLOUDFRONT.
# Current protection: API Gateway throttling (200 rps / 500 burst) + DynamoDB
# auth + rate limiting covers the primary attack surface.
