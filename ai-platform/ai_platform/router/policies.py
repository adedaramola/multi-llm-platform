"""
Routing policies: complexity estimation and tier selection.
These are pure functions — no I/O, fully testable.
"""
from __future__ import annotations

import re

from ..config.settings import get_settings
from ..models.schemas import BudgetHint, InferenceRequest


CODE_PATTERN = re.compile(
    r"```|def |class |import |function |SELECT |INSERT |UPDATE |FROM |WHERE |{.*}",
    re.IGNORECASE,
)
REASONING_KEYWORDS = re.compile(
    r"\b(explain|analyze|compare|evaluate|design|architect|reason|think|step.by.step|pros and cons)\b",
    re.IGNORECASE,
)


def estimate_complexity(request: InferenceRequest) -> float:
    """
    Returns a complexity score between 0.0 and 1.0.

    Factors:
      - Token count of combined messages
      - Presence of code blocks or SQL
      - Explicit reasoning signals in the prompt
      - Number of turns in the conversation
      - Caller metadata hint
    """
    prompt = request.prompt_text
    token_estimate = len(prompt.split())

    # Token volume score (0–0.4)
    token_score = min(token_estimate / 2000, 1.0) * 0.4

    # Code detection score (0 or 0.2)
    code_score = 0.20 if CODE_PATTERN.search(prompt) else 0.0

    # Reasoning signal score (0 or 0.15)
    reasoning_score = 0.15 if (
        REASONING_KEYWORDS.search(prompt) or request.metadata.reasoning_required
    ) else 0.0

    # Multi-turn conversation score (0–0.15)
    turn_score = min(len(request.messages) / 10, 1.0) * 0.15

    # Explicit high-tier request bump (0 or 0.1)
    budget_score = 0.10 if request.metadata.budget == BudgetHint.HIGH else 0.0

    return round(token_score + code_score + reasoning_score + turn_score + budget_score, 3)


def select_tier(complexity: float, budget: BudgetHint) -> str:
    """
    Map complexity score + budget hint → routing tier.

    Budget LOW always forces low tier regardless of complexity.
    Budget HIGH forces at least mid tier.
    Thresholds are driven by settings so they can be tuned without code changes.
    """
    if budget == BudgetHint.LOW:
        return "low"

    if budget == BudgetHint.HIGH:
        return "high" if complexity >= 0.5 else "mid"

    # STANDARD budget — complexity-based using configurable thresholds
    settings = get_settings()
    if complexity < settings.complexity_low_threshold:
        return "low"
    if complexity < settings.complexity_mid_threshold:
        return "mid"
    return "high"
