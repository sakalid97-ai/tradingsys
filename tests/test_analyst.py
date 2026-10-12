"""Tests for the LLM analyst adapter.

The safety properties matter more than the parsing: an unproven analyst must
have *zero* influence, a bad analyst must invert, and no single call may move
the posterior far.
"""

from __future__ import annotations

import json
import math
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ts.agents.analyst import (  # noqa: E402
    AnalystAdapter,
    AnalystAdapterConfig,
    AnalystSignal,
    StubAnalyst,
    build_prompt,
    parse_response,
)
from ts.agents.bayesian import BayesianEngine, Evidence  # noqa: E402


def _signal(direction="bullish", conviction=0.8, source="llm_analyst",
            symbol="AAPL", horizon=5):
    return AnalystSignal(symbol=symbol, direction=direction,
                         conviction=conviction, horizon_days=horizon,
                         reasoning="test", source=source)


def _trained(adapter: AnalystAdapter, source: str, correct: int, total: int) -> None:
    for i in range(total):
        adapter.record_outcome(source, was_correct=(i < correct))


class TestParseResponse(unittest.TestCase):
    def test_parses_clean_json(self):
        result = parse_response(json.dumps({
            "symbol": "AAPL", "direction": "bullish",
            "conviction": 0.75, "horizon_days": 10,
            "reasoning": "guidance raised"}), "AAPL")
        self.assertTrue(result)
        self.assertEqual(result.signal.direction, "bullish")
        self.assertEqual(result.signal.conviction, 0.75)
        self.assertEqual(result.signal.horizon_days, 10)

    def test_extracts_json_from_prose(self):
        payload = 'Here is my analysis:\n{"symbol":"MSFT","direction":"bearish","conviction":0.6,"horizon_days":5,"reasoning":"margin pressure"}\nHope that helps!'
        result = parse_response(payload, "MSFT")
        self.assertTrue(result)
        self.assertEqual(result.signal.direction, "bearish")

    def test_extracts_json_from_markdown_fence(self):
        text = 'Sure!\n```json\n{"symbol":"TSLA","direction":"bullish","conviction":0.9,"horizon_days":3,"reasoning":"deliveries"}\n```'
        result = parse_response(text, "TSLA")
        self.assertTrue(result)
        self.assertEqual(result.signal.symbol, "TSLA")

    def test_normalises_direction_aliases(self):
        for alias in ("up", "long", "positive", "buy", "Bullish"):
            result = parse_response(json.dumps(
                {"symbol": "X", "direction": alias, "conviction": 0.5}), "X")
            self.assertTrue(result, alias)
            self.assertEqual(result.signal.direction, "bullish", alias)
        for alias in ("down", "short", "negative", "sell"):
            result = parse_response(json.dumps(
                {"symbol": "X", "direction": alias, "conviction": 0.5}), "X")
            self.assertTrue(result, alias)
            self.assertEqual(result.signal.direction, "bearish", alias)

    # ---- rejections: a partial answer must be treated as NO answer
    def test_rejects_empty(self):
        self.assertFalse(parse_response("", "AAPL"))

    def test_rejects_no_json(self):
        self.assertFalse(parse_response("I think Apple looks good.", "AAPL"))

    def test_rejects_wrong_symbol(self):
        """The single most dangerous failure: an opinion about a different stock."""
        text = json.dumps({"symbol": "NVDA", "direction": "bullish",
                           "conviction": 0.9, "horizon_days": 5})
        result = parse_response(text, "AAPL")
        self.assertFalse(result)
        self.assertIn("NVDA", result.error)

    def test_rejects_bad_direction(self):
        result = parse_response(json.dumps(
            {"symbol": "X", "direction": "maybe", "conviction": 0.5}), "X")
        self.assertFalse(result)

    def test_rejects_out_of_range_conviction(self):
        for bad in (1.5, -0.2, "high"):
            result = parse_response(json.dumps(
                {"symbol": "X", "direction": "bullish", "conviction": bad}), "X")
            self.assertFalse(result, bad)

    def test_rejects_non_number_conviction(self):
        result = parse_response(json.dumps(
            {"symbol": "X", "direction": "bullish", "conviction": "very"}), "X")
        self.assertFalse(result)

    def test_clamps_horizon(self):
        result = parse_response(json.dumps(
            {"symbol": "X", "direction": "bullish", "conviction": 0.5,
             "horizon_days": 9999}), "X")
        self.assertTrue(result)
        self.assertLessEqual(result.signal.horizon_days, 90)

    def test_handles_nested_braces_in_reasoning(self):
        text = json.dumps({"symbol": "X", "direction": "bullish",
                           "conviction": 0.5,
                           "reasoning": "P/E {ratio} looks fine"})
        result = parse_response(text, "X")
        self.assertTrue(result)
        self.assertIn("ratio", result.signal.reasoning)


class TestSafetyProperties(unittest.TestCase):
    """The properties that keep a language model out of the risk path."""

    def test_unproven_analyst_has_zero_influence(self):
        adapter = AnalystAdapter(AnalystAdapterConfig(min_observations=10))
        evidence = adapter.to_evidence(_signal(direction="bullish", conviction=1.0))
        # exactly 1.0 -- the posterior does not move at all
        self.assertAlmostEqual(evidence.bayes_factor, 1.0, places=12)

    def test_unproven_even_with_extreme_conviction(self):
        adapter = AnalystAdapter()
        for conviction in (0.0, 0.5, 0.99, 1.0):
            for direction in ("bullish", "bearish"):
                ev = adapter.to_evidence(_signal(direction, conviction))
                self.assertAlmostEqual(ev.bayes_factor, 1.0, places=12)

    def test_good_analyst_amplifies_in_its_direction(self):
        adapter = AnalystAdapter()
        _trained(adapter, "llm_analyst", 8, 10)      # 80% accurate
        bullish = adapter.to_evidence(_signal("bullish", 0.8))
        bearish = adapter.to_evidence(_signal("bearish", 0.8))
        self.assertGreater(bullish.bayes_factor, 1.0)
        self.assertLess(bearish.bayes_factor, 1.0)
        # symmetric: a bullish call and a bearish call at equal conviction are
        # mirror images around 1.0
        self.assertAlmostEqual(bullish.bayes_factor, 1.0 / bearish.bayes_factor,
                               places=12)

    def test_bad_analyst_inverts(self):
        """A source that has been wrong more often than right becomes
        counter-evidence -- its bullish call argues for a fall."""
        adapter = AnalystAdapter()
        _trained(adapter, "llm_analyst", 2, 10)      # 20% accurate
        bullish = adapter.to_evidence(_signal("bullish", 0.8))
        bearish = adapter.to_evidence(_signal("bearish", 0.8))
        self.assertLess(bullish.bayes_factor, 1.0, "a bad analyst's bullish call "
                         "must argue for the downside")
        self.assertGreater(bearish.bayes_factor, 1.0)

    def test_coin_flip_analyst_has_zero_influence(self):
        adapter = AnalystAdapter()
        _trained(adapter, "llm_analyst", 5, 10)      # exactly 50%
        signal = _signal("bullish", 1.0)
        self.assertAlmostEqual(adapter.to_evidence(signal).bayes_factor, 1.0,
                               places=12)

    def test_influence_is_bounded(self):
        adapter = AnalystAdapter(AnalystAdapterConfig(max_log_odds=0.6))
        _trained(adapter, "llm_analyst", 10, 10)     # perfect so far
        ev = adapter.to_evidence(_signal("bullish", 1.0))
        log_bf = math.log(ev.bayes_factor)
        # 2 * delta where delta <= max_log_odds * skill_scale
        self.assertLessEqual(log_bf, 2 * 0.6 * 1.0 + 1e-9)
        self.assertGreater(log_bf, 0.0)
        self.assertLess(ev.bayes_factor, 4.0, "a single LLM call must never be "
                        "worth more than a few price features")

    def test_neutral_signal_is_inert_when_skilled(self):
        adapter = AnalystAdapter()
        _trained(adapter, "llm_analyst", 9, 10)
        ev = adapter.to_evidence(_signal("neutral", 0.5))
        self.assertAlmostEqual(ev.bayes_factor, 1.0, places=12)

    def test_evidence_carries_an_audit_trail(self):
        adapter = AnalystAdapter()
        _trained(adapter, "llm_analyst", 7, 10)
        ev = adapter.to_evidence(_signal("bullish", 0.6))
        self.assertIn("conviction", ev.detail)
        self.assertIn("skill", ev.detail)
        self.assertIn("reasoning", ev.detail)
        self.assertIn("calibration", ev.detail)
        self.assertTrue(ev.is_valid())

    def test_sources_are_calibrated_independently(self):
        adapter = AnalystAdapter()
        _trained(adapter, "good", 9, 10)
        _trained(adapter, "bad", 1, 10)
        good = adapter.to_evidence(_signal("bullish", 0.9, source="good"))
        bad = adapter.to_evidence(_signal("bullish", 0.9, source="bad"))
        self.assertGreater(good.bayes_factor, 1.0)
        self.assertLess(bad.bayes_factor, 1.0)


class TestAdapterCalling(unittest.TestCase):
    def test_analyse_returns_evidence_when_skilled(self):
        adapter = AnalystAdapter()
        _trained(adapter, "llm_analyst", 8, 10)
        stub = StubAnalyst(responses={
            "AAPL": json.dumps({"symbol": "AAPL", "direction": "bullish",
                                "conviction": 0.8, "horizon_days": 5,
                                "reasoning": "guidance up"})})
        evidence = adapter.analyse("AAPL", "context", stub)
        self.assertIsNotNone(evidence)
        self.assertGreater(evidence.bayes_factor, 1.0)
        self.assertEqual(len(adapter.history), 1)

    def test_analyse_returns_none_on_garbage(self):
        adapter = AnalystAdapter()
        _trained(adapter, "llm_analyst", 8, 10)
        stub = StubAnalyst(default="I'm not sure, maybe it goes up?")
        self.assertIsNone(adapter.analyse("AAPL", "ctx", stub))
        self.assertTrue(adapter.snapshot()["rejected"][0]["error"].startswith(
            "no JSON object found"))

    def test_analyse_survives_a_dead_api(self):
        def boom(_prompt):
            raise RuntimeError("connection refused")

        adapter = AnalystAdapter()
        _trained(adapter, "llm_analyst", 8, 10)
        self.assertIsNone(adapter.analyse("AAPL", "ctx", boom))
        self.assertEqual(adapter.calibration_for("llm_analyst").errors, 1)

    def test_errors_count_toward_calibration(self):
        adapter = AnalystAdapter(AnalystAdapterConfig(min_observations=3))
        _trained(adapter, "llm_analyst", 2, 3)      # 2/3 = 66% -> skilled
        for _ in range(5):
            adapter.record_error("llm_analyst")
        self.assertEqual(adapter.calibration_for("llm_analyst").errors, 5)
        # skill is unchanged by transport errors; only outcomes move it
        self.assertGreater(adapter.calibration_for("llm_analyst").skill(), 0.0)


class TestPrompt(unittest.TestCase):
    def test_prompt_contains_symbol_and_contract(self):
        prompt = build_prompt("AAPL", "revenue up 20%")
        self.assertIn("AAPL", prompt)
        self.assertIn("direction", prompt)
        self.assertIn("conviction", prompt)
        self.assertIn("revenue up 20%", prompt)

    def test_prompt_asks_for_narrow_output(self):
        prompt = build_prompt("X", "ctx")
        self.assertIn("EXACTLY one JSON object", prompt)
        self.assertIn("bullish", prompt)
        self.assertIn("neutral", prompt)


class TestEvidenceWithEngine(unittest.TestCase):
    def test_analyst_evidence_flows_into_bayesian_engine(self):
        adapter = AnalystAdapter()
        _trained(adapter, "llm_analyst", 8, 10)
        engine = BayesianEngine("AAPL", alpha0=2.0, beta0=2.0)
        before = engine.posterior_mean
        evidence = adapter.to_evidence(_signal("bullish", 0.8))
        engine.update(evidence)
        self.assertGreater(engine.posterior_mean, before)

    def test_unproven_analyst_does_not_move_engine(self):
        adapter = AnalystAdapter()
        engine = BayesianEngine("AAPL", alpha0=2.0, beta0=2.0)
        before = engine.posterior_mean
        evidence = adapter.to_evidence(_signal("bullish", 1.0))
        engine.update(evidence)
        self.assertEqual(engine.posterior_mean, before)

    def test_analyst_cannot_overrule_strong_price_evidence(self):
        """Even a perfect analyst must not dominate the price-based evidence."""
        adapter = AnalystAdapter(AnalystAdapterConfig(max_log_odds=0.6))
        _trained(adapter, "llm_analyst", 20, 20)
        engine = BayesianEngine("AAPL", alpha0=2.0, beta0=2.0)
        for _ in range(5):
            engine.update(Evidence("trend_quality", p_if_up=0.9, p_if_down=0.1))
        strong_price_view = engine.posterior_mean
        engine.update(adapter.to_evidence(_signal("bearish", 1.0)))
        # the analyst may lean against it, but only slightly
        self.assertLess(engine.posterior_mean, strong_price_view)
        self.assertGreater(engine.posterior_mean, strong_price_view - 0.2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
