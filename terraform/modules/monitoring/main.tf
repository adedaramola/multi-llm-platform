# ── SNS Topic for alerts ──────────────────────────────────────────────────────
resource "aws_sns_topic" "alerts" {
  name = "ai-platform-alerts-${var.environment}"
}

resource "aws_sns_topic_subscription" "email" {
  topic_arn = aws_sns_topic.alerts.arn
  protocol  = "email"
  endpoint  = var.alert_email
}

# ── CloudWatch Alarms ─────────────────────────────────────────────────────────
resource "aws_cloudwatch_metric_alarm" "error_rate" {
  alarm_name          = "ai-platform-error-rate-${var.environment}"
  alarm_description   = "Error rate exceeded 5% over 5 minutes"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 1
  threshold           = 5

  metric_query {
    id          = "error_rate"
    expression  = "errors / requests * 100"
    label       = "Error Rate %"
    return_data = true
  }
  metric_query {
    id = "errors"
    metric {
      namespace   = "AWS/Lambda"
      metric_name = "Errors"
      dimensions  = { FunctionName = var.lambda_function_name }
      period      = 300
      stat        = "Sum"
    }
  }
  metric_query {
    id = "requests"
    metric {
      namespace   = "AWS/Lambda"
      metric_name = "Invocations"
      dimensions  = { FunctionName = var.lambda_function_name }
      period      = 300
      stat        = "Sum"
    }
  }

  alarm_actions = [aws_sns_topic.alerts.arn]
  ok_actions    = [aws_sns_topic.alerts.arn]
}

resource "aws_cloudwatch_metric_alarm" "p99_latency" {
  alarm_name          = "ai-platform-p99-latency-${var.environment}"
  alarm_description   = "p99 latency exceeded 10 seconds"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 1
  threshold           = 10000
  namespace           = "AWS/Lambda"
  metric_name         = "Duration"
  dimensions          = { FunctionName = var.lambda_function_name }
  period              = 300
  extended_statistic  = "p99"
  alarm_actions       = [aws_sns_topic.alerts.arn]
}

resource "aws_cloudwatch_metric_alarm" "lambda_throttles" {
  alarm_name          = "ai-platform-throttles-${var.environment}"
  alarm_description   = "Lambda throttling detected"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 1
  threshold           = 10
  namespace           = "AWS/Lambda"
  metric_name         = "Throttles"
  dimensions          = { FunctionName = var.lambda_function_name }
  period              = 300
  statistic           = "Sum"
  alarm_actions       = [aws_sns_topic.alerts.arn]
}

# ── CloudWatch Dashboard ──────────────────────────────────────────────────────
resource "aws_cloudwatch_dashboard" "main" {
  dashboard_name = "ai-platform-${var.environment}"

  dashboard_body = jsonencode({
    widgets = [
      {
        type = "metric"
        properties = {
          title  = "Request Rate & Errors"
          region = data.aws_region.current.name
          period = 60
          metrics = [
            ["AWS/Lambda", "Invocations", "FunctionName", var.lambda_function_name, { stat = "Sum", label = "Requests" }],
            ["AWS/Lambda", "Errors", "FunctionName", var.lambda_function_name, { stat = "Sum", label = "Errors", color = "#d62728" }]
          ]
          view = "timeSeries"
        }
      },
      {
        type = "metric"
        properties = {
          title  = "Latency (p50 / p99)"
          region = data.aws_region.current.name
          period = 60
          metrics = [
            ["AWS/Lambda", "Duration", "FunctionName", var.lambda_function_name, { stat = "p50", label = "p50" }],
            ["AWS/Lambda", "Duration", "FunctionName", var.lambda_function_name, { stat = "p99", label = "p99", color = "#ff7f0e" }]
          ]
          view = "timeSeries"
        }
      },
      {
        type = "metric"
        properties = {
          title  = "Token Usage by Model"
          region = data.aws_region.current.name
          period = 300
          metrics = [
            ["ai-platform/inference", "InputTokens"],
            ["ai-platform/inference", "OutputTokens"],
          ]
          view = "timeSeries"
        }
      },
      {
        type = "metric"
        properties = {
          title  = "Cache Hit Rate"
          region = data.aws_region.current.name
          period = 300
          metrics = [
            ["ai-platform/inference", "CacheHit", { stat = "Sum" }]
          ]
          view = "timeSeries"
        }
      },
      {
        type = "metric"
        properties = {
          title  = "Estimated Cost (USD)"
          region = data.aws_region.current.name
          period = 3600
          metrics = [
            ["ai-platform/inference", "EstimatedCostUSD", { stat = "Sum" }]
          ]
          view = "timeSeries"
        }
      }
    ]
  })
}
