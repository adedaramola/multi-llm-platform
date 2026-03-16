output "api_url" {
  value = aws_apigatewayv2_stage.main.invoke_url
}

output "api_id" {
  value = aws_apigatewayv2_api.main.id
}
