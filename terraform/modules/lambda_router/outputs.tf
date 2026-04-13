output "lambda_invoke_arn" {
  value = aws_lambda_alias.live.invoke_arn
}

output "lambda_arn" {
  value = aws_lambda_alias.live.arn
}

# Base function ARN (no qualifier) — required for UpdateFunctionCode / PublishVersion
output "lambda_function_arn" {
  value = aws_lambda_function.gateway.arn
}

output "lambda_function_name" {
  value = aws_lambda_function.gateway.function_name
}
