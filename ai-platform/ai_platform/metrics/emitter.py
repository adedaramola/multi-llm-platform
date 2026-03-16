"""
CloudWatch EMF (Embedded Metric Format) publisher.
Emitting via stdout in Lambda is free and zero-dependency.
CloudWatch parses the JSON automatically and creates metrics.
"""
from __future__ import annotations

import json
import logging
import time

logger = logging.getLogger(__name__)


def emit_request_metric(
    *,
    request_id: str,
    caller_id: str,
    provider: str,
    model: str,
    tier: str,
    input_tokens: int,
    output_tokens: int,
    latency_ms: int,
    cache_hit: bool,
    cache_source: str,
    status_code: int,
    estimated_cost_usd: float,
) -> None:
    """
    Emit a structured CloudWatch EMF log line.
    Lambda stdout → CloudWatch Logs → metric extraction (no agent needed).
    """
    emf = {
        "_aws": {
            "Timestamp": int(time.time() * 1000),
            "CloudWatchMetrics": [
                {
                    "Namespace": "ai-platform/inference",
                    "Dimensions": [["provider", "model", "tier"]],
                    "Metrics": [
                        {"Name": "RequestCount", "Unit": "Count"},
                        {"Name": "InputTokens", "Unit": "Count"},
                        {"Name": "OutputTokens", "Unit": "Count"},
                        {"Name": "TotalTokens", "Unit": "Count"},
                        {"Name": "LatencyMs", "Unit": "Milliseconds"},
                        {"Name": "CacheHit", "Unit": "Count"},
                        {"Name": "EstimatedCostUSD", "Unit": "None"},
                        {"Name": "ErrorCount", "Unit": "Count"},
                    ],
                }
            ],
        },
        # Dimensions
        "provider": provider,
        "model": model,
        "tier": tier,
        # Metrics
        "RequestCount": 1,
        "InputTokens": input_tokens,
        "OutputTokens": output_tokens,
        "TotalTokens": input_tokens + output_tokens,
        "LatencyMs": latency_ms,
        "CacheHit": 1 if cache_hit else 0,
        "EstimatedCostUSD": round(estimated_cost_usd, 8),
        "ErrorCount": 1 if status_code >= 500 else 0,
        # Non-metric context (searchable in CloudWatch Logs Insights)
        "request_id": request_id,
        "caller_id": caller_id,
        "cache_source": cache_source,
        "status_code": status_code,
    }
    # Print to stdout — Lambda captures this as a structured CloudWatch log event
    print(json.dumps(emf))


def emit_error_metric(
    *,
    request_id: str,
    caller_id: str,
    error_type: str,
    status_code: int,
) -> None:
    emf = {
        "_aws": {
            "Timestamp": int(time.time() * 1000),
            "CloudWatchMetrics": [
                {
                    "Namespace": "ai-platform/errors",
                    "Dimensions": [["error_type"]],
                    "Metrics": [{"Name": "ErrorCount", "Unit": "Count"}],
                }
            ],
        },
        "error_type": error_type,
        "ErrorCount": 1,
        "request_id": request_id,
        "caller_id": caller_id,
        "status_code": status_code,
    }
    print(json.dumps(emf))
