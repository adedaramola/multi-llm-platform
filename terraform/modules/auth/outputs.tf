output "api_keys_table_name" {
  value = aws_dynamodb_table.api_keys.name
}

output "rate_limit_table_name" {
  value = aws_dynamodb_table.rate_limits.name
}

output "health_table_name" {
  value = aws_dynamodb_table.health.name
}

output "anthropic_secret_arn" {
  value = aws_secretsmanager_secret.anthropic.arn
}

output "openai_secret_arn" {
  value = aws_secretsmanager_secret.openai.arn
}
