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

# ── DynamoDB — Per-caller usage accounting ───────────────────────────────────
resource "aws_dynamodb_table" "usage" {
  name         = "ai-platform-usage-${var.environment}"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "caller_id"
  range_key    = "usage_date"

  attribute {
    name = "caller_id"
    type = "S"
  }

  attribute {
    name = "usage_date"
    type = "S"
  }

  point_in_time_recovery { enabled = true }
  server_side_encryption { enabled = true }
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
  count = var.anthropic_enabled ? 1 : 0

  name                    = "ai-platform/${var.environment}/anthropic-api-key"
  description             = "Anthropic API key for AI Platform"
  recovery_window_in_days = 0
}

resource "aws_secretsmanager_secret_version" "anthropic" {
  count = var.anthropic_enabled ? 1 : 0

  secret_id     = aws_secretsmanager_secret.anthropic[0].id
  secret_string = jsonencode({ api_key = var.anthropic_key })
}

resource "aws_secretsmanager_secret" "openai" {
  count = var.openai_enabled ? 1 : 0

  name                    = "ai-platform/${var.environment}/openai-api-key"
  description             = "OpenAI API key for AI Platform"
  recovery_window_in_days = 0
}

resource "aws_secretsmanager_secret_version" "openai" {
  count = var.openai_enabled ? 1 : 0

  secret_id     = aws_secretsmanager_secret.openai[0].id
  secret_string = jsonencode({ api_key = var.openai_key })
}

# ── Bootstrap client credential ───────────────────────────────────────────────
# A fresh deployment is immediately usable without a manual DynamoDB write.
# The raw key is held in Secrets Manager; DynamoDB stores only its SHA-256 hash.
resource "random_password" "bootstrap_api_key" {
  length  = 64
  special = false
}

locals {
  bootstrap_api_key = "mlp_${random_password.bootstrap_api_key.result}"
}

resource "aws_secretsmanager_secret" "bootstrap_api_key" {
  name_prefix             = "ai-platform/${var.environment}/bootstrap-client-api-key-"
  description             = "Bootstrap client API key for AI Platform"
  recovery_window_in_days = 7
}

resource "aws_secretsmanager_secret_version" "bootstrap_api_key" {
  secret_id     = aws_secretsmanager_secret.bootstrap_api_key.id
  secret_string = local.bootstrap_api_key
}

resource "aws_dynamodb_table_item" "bootstrap_api_key" {
  table_name = aws_dynamodb_table.api_keys.name
  hash_key   = aws_dynamodb_table.api_keys.hash_key

  item = jsonencode({
    key_hash   = { S = sha256(local.bootstrap_api_key) }
    caller_id  = { S = "bootstrap-client" }
    app_name   = { S = "bootstrap" }
    rpm_limit  = { N = "60" }
    rpd_limit  = { N = "5000" }
    active     = { BOOL = true }
    created_at = { S = timestamp() }
  })

  lifecycle {
    ignore_changes = [item]
  }
}

# ── OpsDesk Agent service credential ──────────────────────────────────────────
# Secrets Manager holds the raw credential. DynamoDB stores only its hash and
# binds it to the caller identity enforced by the gateway contract.
resource "random_password" "opsdesk_agent_api_key" {
  length  = 64
  special = false
}

locals {
  opsdesk_agent_api_key = "mlp_${random_password.opsdesk_agent_api_key.result}"
}

resource "aws_secretsmanager_secret" "opsdesk_agent_api_key" {
  name_prefix             = "ai-platform/${var.environment}/opsdesk-agent-api-key-"
  description             = "Scoped multi-LLM gateway credential for the OpsDesk Agent"
  recovery_window_in_days = 7
}

resource "aws_secretsmanager_secret_version" "opsdesk_agent_api_key" {
  secret_id     = aws_secretsmanager_secret.opsdesk_agent_api_key.id
  secret_string = local.opsdesk_agent_api_key
}

resource "aws_dynamodb_table_item" "opsdesk_agent_api_key" {
  table_name = aws_dynamodb_table.api_keys.name
  hash_key   = aws_dynamodb_table.api_keys.hash_key

  item = jsonencode({
    key_hash   = { S = sha256(local.opsdesk_agent_api_key) }
    caller_id  = { S = "opsdesk-agent" }
    app_name   = { S = "opsdesk-agent" }
    rpm_limit  = { N = "30" }
    rpd_limit  = { N = "1000" }
    active     = { BOOL = true }
    created_at = { S = timestamp() }
  })

  lifecycle {
    ignore_changes = [item]
  }
}
