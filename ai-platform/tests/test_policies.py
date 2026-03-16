"""
Unit tests for router/policies.py — pure functions, no AWS required.
"""
import pytest
from ai_platform.models.schemas import BudgetHint, InferenceRequest, RequestMetadata
from ai_platform.router.policies import estimate_complexity, select_tier


def _req(content: str, budget: BudgetHint = BudgetHint.STANDARD, turns: int = 1) -> InferenceRequest:
    messages = [{"role": "user", "content": content}] * turns
    return InferenceRequest(
        messages=messages,
        metadata=RequestMetadata(budget=budget),
    )


# ── estimate_complexity ────────────────────────────────────────────────────────

class TestEstimateComplexity:
    def test_short_simple_prompt_is_low(self):
        score = estimate_complexity(_req("hi"))
        assert score < 0.30

    def test_long_prompt_increases_score(self):
        long_text = "word " * 800          # ~800 tokens → token_score = 0.16
        score = estimate_complexity(_req(long_text))
        assert score >= 0.15

    def test_code_block_adds_score(self):
        score_plain = estimate_complexity(_req("Summarize this for me"))
        score_code  = estimate_complexity(_req("```python\nfor i in range(10): pass\n```"))
        assert score_code > score_plain

    def test_reasoning_keywords_add_score(self):
        score = estimate_complexity(_req("Please analyze and compare these two approaches"))
        assert score >= 0.15

    def test_multi_turn_adds_score(self):
        score_single = estimate_complexity(_req("hi", turns=1))
        score_multi  = estimate_complexity(_req("hi", turns=8))
        assert score_multi > score_single

    def test_high_budget_adds_bump(self):
        score_std  = estimate_complexity(_req("hi", budget=BudgetHint.STANDARD))
        score_high = estimate_complexity(_req("hi", budget=BudgetHint.HIGH))
        assert score_high == pytest.approx(score_std + 0.10, abs=0.001)

    def test_score_bounded_between_0_and_1(self):
        # Maximally complex request
        score = estimate_complexity(_req(
            "explain and analyze ```SELECT * FROM table``` " + "word " * 2000,
            budget=BudgetHint.HIGH,
            turns=10,
        ))
        assert 0.0 <= score <= 1.0


# ── select_tier ────────────────────────────────────────────────────────────────

class TestSelectTier:
    def test_low_budget_always_low(self):
        for complexity in [0.0, 0.5, 1.0]:
            assert select_tier(complexity, BudgetHint.LOW) == "low"

    def test_high_budget_low_complexity_is_mid(self):
        assert select_tier(0.2, BudgetHint.HIGH) == "mid"

    def test_high_budget_high_complexity_is_high(self):
        assert select_tier(0.8, BudgetHint.HIGH) == "high"

    def test_standard_below_threshold_is_low(self):
        assert select_tier(0.10, BudgetHint.STANDARD) == "low"
        assert select_tier(0.29, BudgetHint.STANDARD) == "low"

    def test_standard_mid_range_is_mid(self):
        assert select_tier(0.30, BudgetHint.STANDARD) == "mid"
        assert select_tier(0.55, BudgetHint.STANDARD) == "mid"
        assert select_tier(0.69, BudgetHint.STANDARD) == "mid"

    def test_standard_above_threshold_is_high(self):
        assert select_tier(0.70, BudgetHint.STANDARD) == "high"
        assert select_tier(1.00, BudgetHint.STANDARD) == "high"
