variable "github_repo" {
  type        = string
  description = "GitHub repo in org/name format (e.g. adedaramola/multi-llm-platform)"
}

variable "gateway_function_arn" {
  type        = string
  description = "Base ARN of the gateway Lambda function (no qualifier)"
}


variable "health_checker_function_arn" {
  type        = string
  description = "ARN of the health-checker Lambda function"
}
