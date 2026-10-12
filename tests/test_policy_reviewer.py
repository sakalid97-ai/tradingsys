"""Tests for the approval policy, the LLM reviewer, and the inline buttons."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ts.agents.policy import AutoApprovePolicy  # noqa: E402
from ts.agents.reviewer import Review, TradeReviewer  # noqa: E402
from ts.llm import LLMClient, LLMConfig, LLMError  # noqa: E402
from ts.telegram_buttons import parse_callback  # noqa: E402


class TestConditionalApproval(unittest.TestCase):
    """You should only be interrupted when the edge is unconvincing."""

    def test_strong_edge_does_not_ask(self):
        policy = AutoApprovePolicy(min_expectancy=0.35, min_confidence=0.75)
        # 0.40R clears the 0.35R bar but not 1.8x of it, so it runs quietly
        needs, tier, reason = policy.evaluate(0.40, 0.90, risk_pct=0.01)
        self.assertFalse(needs)
        self.assertEqual(tier, "auto")
        self.assertIn("clear the", reason)

    def test_marginal_edge_asks(self):
        policy = AutoApprovePolicy(min_expectancy=0.35, min_confidence=0.75)
        needs, tier, _ = policy.evaluate(0.1, 0.55, risk_pct=0.01)
        self.assertTrue(needs)
        self.assertEqual(tier, "ask")

    def test_high_expectancy_low_confidence_asks(self):
        """A big edge it is unsure about still deserves a human."""
        policy = AutoApprovePolicy()
        needs, _, reason = policy.evaluate(0.9, 0.60, risk_pct=0.01)
        self.assertTrue(needs)
        self.assertIn("confidence", reason)

    def test_low_expectancy_high_confidence_asks(self):
        policy = AutoApprovePolicy()
        needs, _, reason = policy.evaluate(0.05, 0.95, risk_pct=0.01)
        self.assertTrue(needs)
        self.assertIn("expectancy", reason)

    def test_very_strong_edge_is_notified(self):
        policy = AutoApprovePolicy(min_expectancy=0.35)
        needs, tier, _ = policy.evaluate(0.9, 0.9, risk_pct=0.01)
        self.assertFalse(needs)
        self.assertEqual(tier, "auto_notify")

    def test_oversized_risk_always_asks(self):
        policy = AutoApprovePolicy(max_auto_risk_pct=0.02)
        needs, _, reason = policy.evaluate(0.9, 0.95, risk_pct=0.05)
        self.assertTrue(needs)
        self.assertIn("auto-approval ceiling", reason)

    def test_shorts_can_always_require_approval(self):
        policy = AutoApprovePolicy(always_ask_short=True)
        needs, _, reason = policy.evaluate(0.9, 0.95, side="short", risk_pct=0.01)
        self.assertTrue(needs)
        self.assertIn("shorts", reason)

    def test_llm_concern_escalates(self):
        policy = AutoApprovePolicy()
        needs, _, reason = policy.evaluate(0.9, 0.95, risk_pct=0.01, llm_objects=True)
        self.assertTrue(needs)
        self.assertIn("analyst", reason)

    def test_llm_concern_escalation_is_configurable(self):
        policy = AutoApprovePolicy(ask_on_llm_disagreement=False)
        needs, _, _ = policy.evaluate(0.9, 0.95, risk_pct=0.01, llm_objects=True)
        self.assertFalse(needs)

    def test_per_symbol_override(self):
        policy = AutoApprovePolicy(min_expectancy=0.35,
                                   expectancy_overrides={"BTCUSD": 0.8})
        needs, _, _ = policy.evaluate(0.5, 0.9, symbol="BTCUSD", risk_pct=0.01)
        self.assertTrue(needs, "a stricter per-symbol bar must apply")
        needs, _, _ = policy.evaluate(0.5, 0.9, symbol="ETHUSD", risk_pct=0.01)
        self.assertFalse(needs)

    def test_policy_clamps_bad_values(self):
        policy = AutoApprovePolicy(min_confidence=1.5, min_expectancy=-1,
                                   max_auto_risk_pct=0).clamp()
        self.assertLessEqual(policy.min_confidence, 0.99)
        self.assertGreaterEqual(policy.min_expectancy, 0.0)
        self.assertGreater(policy.max_auto_risk_pct, 0)


class FakeClient(LLMClient):
    def __init__(self, completion=None, raises=None):
        super().__init__(LLMConfig(base_url="http://local", model="hermes-test"))
        self.completion = completion
        self.raises = raises

    def complete(self, system, user, model=None, temperature=None):
        if self.raises:
            raise self.raises
        return self.completion or ""


class TestTradeReviewer(unittest.TestCase):
    def test_disabled_without_a_model(self):
        reviewer = TradeReviewer(LLMClient(LLMConfig(base_url="", model="")))
        self.assertFalse(reviewer.enabled)
        review = reviewer.review("BTCUSD", "long", 0.8, 0.9, "ctx")
        self.assertEqual(review.verdict, "unavailable")
        self.assertFalse(review.objects)

    def test_clear_review_does_not_object(self):
        reviewer = TradeReviewer(FakeClient(
            '{"verdict": "clear", "reason": ""}'))
        review = reviewer.review("BTCUSD", "long", 0.8, 0.9, "no events")
        self.assertEqual(review.verdict, "clear")
        self.assertFalse(review.objects)
        self.assertTrue(review.available)

    def test_concern_objects(self):
        reviewer = TradeReviewer(FakeClient(
            '{"verdict": "concern", "reason": "FOMC in 40 minutes"}'))
        review = reviewer.review("EURUSD", "long", 0.8, 0.9, "quiet session")
        self.assertEqual(review.verdict, "concern")
        self.assertTrue(review.objects)
        self.assertIn("FOMC", review.reason)
        self.assertEqual(reviewer.concerns, 1)

    def test_model_unavailable_does_not_object(self):
        reviewer = TradeReviewer(FakeClient(raises=LLMError("offline")))
        review = reviewer.review("BTCUSD", "long", 0.8, 0.9, "ctx")
        self.assertEqual(review.verdict, "unavailable")
        self.assertFalse(review.objects,
                         "an unreachable model must not block trading")

    def test_malformed_json_fails_open(self):
        reviewer = TradeReviewer(FakeClient("I think it looks fine"))
        review = reviewer.review("BTCUSD", "long", 0.8, 0.9, "ctx")
        self.assertEqual(review.verdict, "clear",
                         "an unparseable answer must fail open, not block")

    def test_unknown_verdict_fails_open(self):
        reviewer = TradeReviewer(FakeClient('{"verdict": "maybe"}'))
        review = reviewer.review("BTCUSD", "long", 0.8, 0.9, "ctx")
        self.assertEqual(review.verdict, "clear")

    def test_reviewer_cannot_name_parameters(self):
        from ts.agents.reviewer import SYSTEM_PROMPT

        self.assertIn('"verdict"', SYSTEM_PROMPT)
        self.assertNotIn("price", SYSTEM_PROMPT.replace("high-impact", ""))

    def test_stats(self):
        reviewer = TradeReviewer(FakeClient('{"verdict":"clear"}'))
        reviewer.review("A", "long", 0.5, 0.8, "")
        stats = reviewer.stats()
        self.assertTrue(stats["enabled"])
        self.assertEqual(stats["reviews"], 1)
        self.assertEqual(stats["concerns"], 0)


class TestButtonCallbacks(unittest.TestCase):
    def test_parse_approve(self):
        self.assertEqual(parse_callback("approve:ENT-0001"), ("approve", "ENT-0001"))

    def test_parse_reject(self):
        self.assertEqual(parse_callback("reject:ENT-0001"), ("reject", "ENT-0001"))

    def test_parse_rejects_other_callbacks(self):
        for bad in ("nav:overview", "approve:", ":ENT-0001", "", "approve",
                    "delete:ENT-0001", "approve:ENT-0001:extra"):
            self.assertIsNone(parse_callback(bad), bad)

    def test_request_id_round_trips(self):
        action, request_id = parse_callback("approve:ENT-0042")
        self.assertEqual(request_id, "ENT-0042")


if __name__ == "__main__":
    unittest.main(verbosity=2)
