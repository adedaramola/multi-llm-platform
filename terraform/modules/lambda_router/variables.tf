variable "environment" { type = string }
variable "aws_region" { type = string }
variable "vpc_id" { type = string }
variable "private_subnet_ids" { type = list(string) }
variable "lambda_sg_id" { type = string }
variable "api_keys_table_name" { type = string }
variable "rate_limit_table_name" { type = string }
variable "health_table_name" { type = string }
variable "anthropic_secret_arn" { type = string }
variable "openai_secret_arn" { type = string }
variable "pg_secret_arn" { type = string }
variable "redis_endpoint" { type = string }
variable "lambda_package_path" {
  type    = string
  default = "../ai-platform/dist/ai-platform.zip"
}
