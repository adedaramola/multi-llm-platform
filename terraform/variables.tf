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
  description = "Anthropic API key — copied into Secrets Manager; protect the encrypted Terraform state"
  type        = string
  sensitive   = true
  default     = ""
}

variable "enable_bedrock_provider" {
  description = "Enable AWS Bedrock models in the gateway."
  type        = bool
  default     = true

  validation {
    condition = (
      var.enable_bedrock_provider ||
      var.enable_anthropic_provider ||
      var.enable_openai_provider
    )
    error_message = "At least one LLM provider must be enabled."
  }
}

variable "enable_anthropic_provider" {
  description = "Enable direct Anthropic models and create the Anthropic secret."
  type        = bool
  default     = true

  validation {
    condition     = !var.enable_anthropic_provider || length(trimspace(var.anthropic_api_key)) > 0
    error_message = "anthropic_api_key must be set when the Anthropic provider is enabled."
  }
}

variable "enable_openai_provider" {
  description = "Enable OpenAI models and create the OpenAI secret."
  type        = bool
  default     = false

  validation {
    condition     = !var.enable_openai_provider || length(trimspace(var.openai_api_key)) > 0
    error_message = "openai_api_key must be set when the OpenAI provider is enabled."
  }
}

variable "openai_api_key" {
  description = "OpenAI API key — copied into Secrets Manager; protect the encrypted Terraform state"
  type        = string
  sensitive   = true
  default     = ""
}

variable "alert_email" {
  description = "Email address for CloudWatch alarm notifications"
  type        = string
}

variable "cache_enabled" {
  description = "Provision the private VPC, NAT, Valkey/Redis, and Aurora pgvector cache stack."
  type        = bool
  default     = false
}

variable "enable_provisioned_concurrency" {
  description = "Keep two gateway Lambda environments warm. Disable for portfolio/dev environments."
  type        = bool
  default     = false
}

variable "enable_scheduled_health_checks" {
  description = "Probe LLM providers every five minutes. Disable to avoid recurring provider calls in portfolio/dev environments."
  type        = bool
  default     = false
}

variable "aurora_min_capacity" {
  description = "Minimum Aurora serverless ACUs when the semantic cache stack is enabled."
  type        = number
  default     = 0

  validation {
    condition     = var.aurora_min_capacity >= 0 && var.aurora_min_capacity <= var.aurora_max_capacity
    error_message = "aurora_min_capacity must be between 0 and aurora_max_capacity."
  }
}

variable "aurora_max_capacity" {
  description = "Maximum Aurora serverless ACUs when the semantic cache stack is enabled."
  type        = number
  default     = 2

  validation {
    condition     = var.aurora_max_capacity >= 0.5 && var.aurora_max_capacity <= 256
    error_message = "aurora_max_capacity must be between 0.5 and 256."
  }
}

variable "aurora_seconds_until_auto_pause" {
  description = "Idle seconds before Aurora pauses when aurora_min_capacity is zero."
  type        = number
  default     = 600

  validation {
    condition = (
      var.aurora_seconds_until_auto_pause >= 300 &&
      var.aurora_seconds_until_auto_pause <= 86400
    )
    error_message = "Aurora auto-pause must be between 300 and 86400 seconds."
  }
}
