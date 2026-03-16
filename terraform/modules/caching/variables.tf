variable "environment" { type = string }
variable "vpc_id" { type = string }
variable "private_subnet_ids" {
  type = list(string)
}
variable "lambda_sg_id" { type = string }
variable "cache_sg_id" { type = string }
