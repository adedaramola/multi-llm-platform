variable "environment" { type = string }
variable "aws_region" { type = string }
variable "health_table_name" { type = string }
variable "anthropic_secret_arn" { type = string }
variable "openai_secret_arn" { type = string }
variable "bedrock_enabled" { type = bool }
variable "anthropic_enabled" { type = bool }
variable "openai_enabled" { type = bool }
variable "lambda_package_path" {
  type    = string
  default = "../ai-platform/dist/ai-platform.zip"
}
