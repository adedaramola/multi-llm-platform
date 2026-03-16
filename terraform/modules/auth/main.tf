# ── DynamoDB — API keys store ─────────────────────────────────────────────────
resource "aws_dynamodb_table" "api_keys" {
  name         = "ai-platform-api-keys-${var.environment}"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "key_hash"

  attribute {
    name = "key_hash"
    type = "S"
  }

  point_in_time_recovery { enabled = true }
  server_side_encryption { enabled = true }
}

# ── DynamoDB — Rate limit counters ───────────────────────────────────────────
resource "aws_dynamodb_table" "rate_limits" {
  name         = "ai-platform-rate-limits-${var.environment}"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "counter_key"

  attribute {
    name = "counter_key"
    type = "S"
  }

  ttl {
    attribute_name = "ttl"
    enabled        = true
  }
}

# ── DynamoDB — Provider health registry ──────────────────────────────────────
resource "aws_dynamodb_table" "health" {
  name         = "ai-platform-provider-health-${var.environment}"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "provider_name"

  attribute {
    name = "provider_name"
    type = "S"
  }
}

# ── Secrets Manager — LLM provider API keys ───────────────────────────────────
resource "aws_secretsmanager_secret" "anthropic" {
  name                    = "ai-platform/${var.environment}/anthropic-api-key"
  description             = "Anthropic API key for AI Platform"
  recovery_window_in_days = 7
}

resource "aws_secretsmanager_secret_version" "anthropic" {
  secret_id     = aws_secretsmanager_secret.anthropic.id
  secret_string = jsonencode({ api_key = var.anthropic_key })
}

resource "aws_secretsmanager_secret" "openai" {
  name                    = "ai-platform/${var.environment}/openai-api-key"
  description             = "OpenAI API key for AI Platform"
  recovery_window_in_days = 7
}

resource "aws_secretsmanager_secret_version" "openai" {
  secret_id     = aws_secretsmanager_secret.openai.id
  secret_string = jsonencode({ api_key = var.openai_key })
}
