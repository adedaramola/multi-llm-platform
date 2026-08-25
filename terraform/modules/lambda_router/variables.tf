variable "environment" { type = string }
variable "aws_region" { type = string }
variable "vpc_enabled" { type = bool }
variable "private_subnet_ids" { type = list(string) }
variable "lambda_sg_id" { type = string }
variable "api_keys_table_name" { type = string }
variable "rate_limit_table_name" { type = string }
variable "health_table_name" { type = string }
variable "usage_table_name" { type = string }
variable "anthropic_secret_arn" { type = string }
variable "openai_secret_arn" { type = string }
variable "pg_secret_arn" { type = string }
variable "pg_host" { type = string }
variable "redis_endpoint" { type = string }
variable "cache_enabled" { type = bool }
variable "enable_provisioned_concurrency" { type = bool }
variable "bedrock_enabled" { type = bool }
variable "anthropic_enabled" { type = bool }
variable "openai_enabled" { type = bool }
variable "lambda_package_path" {
  type    = string
  default = "../ai-platform/dist/ai-platform.zip"
}
