output "api_keys_table_name" {
  value = aws_dynamodb_table.api_keys.name
}

output "rate_limit_table_name" {
  value = aws_dynamodb_table.rate_limits.name
}

output "health_table_name" {
  value = aws_dynamodb_table.health.name
}

output "usage_table_name" {
  value = aws_dynamodb_table.usage.name
}

output "anthropic_secret_arn" {
  value = aws_secretsmanager_secret.anthropic.arn
}

output "openai_secret_arn" {
  value = aws_secretsmanager_secret.openai.arn
}

output "bootstrap_api_key_secret_arn" {
  description = "Secrets Manager ARN containing the generated bootstrap client API key"
  value       = aws_secretsmanager_secret.bootstrap_api_key.arn
}
