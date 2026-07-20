output "api_gateway_url" {
  description = "Base URL for the AI Platform API"
  value       = module.api_gateway.api_url
}

output "lambda_function_name" {
  description = "Lambda function name for direct invocation / monitoring"
  value       = module.lambda_router.lambda_function_name
}

output "cloudwatch_dashboard_url" {
  description = "CloudWatch operational dashboard URL"
  value       = module.monitoring.dashboard_url
}

output "bootstrap_api_key_secret_arn" {
  description = "Retrieve this secret once to call the deployed API"
  value       = module.auth.bootstrap_api_key_secret_arn
}
