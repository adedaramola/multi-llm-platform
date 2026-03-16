variable "environment" { type = string }
variable "aws_region" { type = string }

variable "anthropic_key" {
  type      = string
  sensitive = true
}

variable "openai_key" {
  type      = string
  sensitive = true
  default   = ""
}
