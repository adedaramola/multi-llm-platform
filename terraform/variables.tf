variable "aws_region" {
  description = "AWS region for all resources"
  type        = string
  default     = "us-east-1"
}

variable "environment" {
  description = "Deployment environment: production, staging, dev"
  type        = string
  default     = "production"
  validation {
    condition     = contains(["production", "staging", "dev"], var.environment)
    error_message = "environment must be production, staging, or dev."
  }
}

variable "anthropic_api_key" {
  description = "Anthropic API key — stored in Secrets Manager, not state"
  type        = string
  sensitive   = true
}

variable "openai_api_key" {
  description = "OpenAI API key — stored in Secrets Manager, not state"
  type        = string
  sensitive   = true
  default     = ""
}

variable "alert_email" {
  description = "Email address for CloudWatch alarm notifications"
  type        = string
}
