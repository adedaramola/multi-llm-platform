variable "environment" { type = string }
variable "aws_region" { type = string }

variable "anthropic_key" {
  type      = string
  sensitive = true
}

variable "anthropic_enabled" { type = bool }

variable "openai_key" {
  type      = string
  sensitive = true
  default   = ""
}


variable "openai_enabled" { type = bool }
