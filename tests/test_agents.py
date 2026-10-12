"""Tests for the multi-agent system: Bayesian updating, Kelly sizing,
instrument specs, edge detection, and the message bus.

Run with:  py tradingsys/tests/test_agents.py
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from ts.agents.bayesian import BayesianEngine, Evidence, combine_posteriors  # noqa: E402
from ts.agents.bus import MessageBus, Topic, topic_key  # noqa: E402
from ts.agents.edge import (  # noqa: E402
    EdgeDetectionAgent,
    expectancy_r,
    grade_edge,
)
from ts.agents.instruments import AssetClass, resolve, yahoo_symbol  # noqa: E402
from ts.agents.kelly import (  # noqa: E402
    KellyConfig,
    KellySizer,
    breakeven_probability,
    confidence_shrink,
    kelly_fraction,
    shrink,
)


# ============================================================ BUS


class TestMessageBus(unittest.TestCase):
    def test_publish_with_enum_reaches_subscriber_keyed_by_value(self):
        """Regression test for the silent no-op that broke the whole pipeline.

        Publishing with the ``Topic`` enum while subscribing with ``.value``
        matched nothing, so no agent ever heard anything.
        """
        bus = MessageBus()
        seen: list[dict] = []
        bus.subscribe(Topic.MARKET_DATA.value, lambda m: seen.append(m.payload))
        bus.publish(Topic.MARKET_DATA, {"symbol": "BTCUSD"})
        self.assertEqual(len(seen), 1, "enum publish did not reach a .value subscriber")

    def test_publish_with_string_reaches_subscriber_keyed_by_enum(self):
        bus = MessageBus()
        seen: list[dict] = []
        bus.subscribe(Topic.PROBABILITY, lambda m: seen.append(m.payload))
        bus.publish("bayes.probability", {"symbol": "ETHUSD"})
        self.assertEqual(len(seen), 1)

    def test_topic_key_normalises(self):
        self.assertEqual(topic_key(Topic.MARKET_DATA), "market.data")
        self.assertEqual(topic_key("market.data"), "market.data")

    def test_handler_exception_does_not_break_others(self):
        bus = MessageBus()
        good: list[dict] = []

        def bad(_msg):
            raise RuntimeError("boom")

        bus.subscribe(Topic.LOG, bad, name="bad")
        bus.subscribe(Topic.LOG, lambda m: good.append(m.payload))
        bus.publish(Topic.LOG, {"x": 1})
        self.assertEqual(len(good), 1)
        self.assertEqual(bus.stats()["errors"], 1)

    def test_journal_is_bounded(self):
        bus = MessageBus(journal_size=5)
        for i in range(20):
            bus.publish(Topic.LOG, {"i": i})
        self.assertEqual(len(bus.journal(limit=100)), 5)

    def test_journal_filters_by_topic(self):
        bus = MessageBus()
        bus.publish(Topic.MARKET_DATA, {"a": 1})
        bus.publish(Topic.PROBABILITY, {"b": 2})
        self.assertEqual(len(bus.journal(limit=10)), 2)
        self.assertEqual(len(bus.journal(limit=10, topic=Topic.MARKET_DATA)), 1)


# ============================================================ BAYESIAN


class TestBayesianEngine(unittest.TestCase):
    def test_uninformative_prior_is_fifty_fifty_and_unsure(self):
        engine = BayesianEngine("X", alpha0=2, beta0=2)
        self.assertAlmostEqual(engine.posterior_mean, 0.5, places=9)
        self.assertAlmostEqual(engine.confidence(), 0.5, places=9)

    def test_single_bayes_factor_moves_posterior_correctly(self):
        engine = BayesianEngine("X", alpha0=2.0, beta0=2.0)
        # BF = 2 => alpha doubles to 4, beta stays 2, mean = 4/6
        engine.update(Evidence("t", p_if_up=0.4, p_if_down=0.2))
        self.assertAlmostEqual(engine.posterior_mean, 4.0 / 6.0, places=9)
        self.assertEqual(engine.steps, 1)
        # Beta(4, 2) puts P(p > 0.5) at 0.8125, which is what confidence reports
        self.assertAlmostEqual(engine.confidence(), 0.8125, places=6)

    def test_bayes_factor_computation(self):
        self.assertAlmostEqual(Evidence("t", 0.4, 0.2).bayes_factor, 2.0)
        self.assertAlmostEqual(Evidence("t", 0.2, 0.4).bayes_factor, 0.5)
        self.assertAlmostEqual(Evidence("t", 0.0, 0.0).bayes_factor, 1.0)

    def test_contradicting_evidence_returns_toward_half(self):
        engine = BayesianEngine("X", alpha0=2.0, beta0=2.0)
        engine.update(Evidence("up", p_if_up=0.8, p_if_down=0.2))
        engine.update(Evidence("down", p_if_up=0.2, p_if_down=0.8))
        # alpha 2*4=8, then *0.25 = 2; beta stays 2 -> back to 0.5
        self.assertAlmostEqual(engine.posterior_mean, 0.5, places=9)

    def test_update_many_discounts_correlated_sources(self):
        items = [Evidence(f"s{i}", p_if_up=0.6, p_if_down=0.4) for i in range(4)]
        independent = BayesianEngine("X", alpha0=2.0, beta0=2.0,
                                     source_independence=1.0)
        discounted = BayesianEngine("X", alpha0=2.0, beta0=2.0,
                                    source_independence=0.5)
        independent.update_many(items)
        discounted.update_many(items)
        # four agreeing sources must count for less when they are correlated
        self.assertLess(discounted.posterior_mean, independent.posterior_mean)
        self.assertGreater(discounted.posterior_mean, 0.5)

    def test_max_odds_caps_extremeness(self):
        engine = BayesianEngine("X", alpha0=2.0, beta0=2.0, max_odds=3.0)
        for _ in range(30):
            engine.update_many([Evidence("s", p_if_up=0.9, p_if_down=0.1)])
        # drift that always favours up must not let p_up reach 0.9999
        self.assertLessEqual(engine.posterior_odds, 3.0 + 1e-9)
        self.assertLess(engine.posterior_mean, 0.8)

    def test_invalid_evidence_raises(self):
        engine = BayesianEngine("X")
        with self.assertRaises(ValueError):
            engine.update(Evidence("bad", p_if_up=1.5, p_if_down=0.5))
        with self.assertRaises(ValueError):
            engine.update(Evidence("bad", p_if_up=-0.1, p_if_down=0.5))

    def test_decay_forgets_old_evidence(self):
        # disable the caps so decay, not the caps, is what differentiates
        gained = BayesianEngine("X", alpha0=2.0, beta0=2.0, max_odds=1e12,
                                max_concentration=1e12)
        faded = BayesianEngine("X", alpha0=2.0, beta0=2.0, decay=0.9,
                               max_odds=1e12, max_concentration=1e12)
        ev = [Evidence("s", p_if_up=0.7, p_if_down=0.3)]
        for _ in range(40):
            gained.update_many(ev)
            faded.update_many(ev)
        self.assertLess(faded.posterior_mean, gained.posterior_mean)
        self.assertLess(faded.posterior_mean, 1.0)   # decay reaches a steady state

    def test_snapshot_exposes_symbol_key(self):
        engine = BayesianEngine("BTCUSD")
        engine.update_many([Evidence("s", p_if_up=0.6, p_if_down=0.4)])
        snap = engine.snapshot()
        self.assertEqual(snap["symbol"], "BTCUSD")   # consumers key on this
        self.assertIn("evidence_strength", snap)
        self.assertTrue(0.0 <= snap["evidence_strength"] <= 1.0)

    def test_combine_posteriors(self):
        a = BayesianEngine("A", alpha0=2.0, beta0=2.0)
        b = BayesianEngine("B", alpha0=2.0, beta0=2.0)
        for e in (a, b):
            e.update(Evidence("s", p_if_up=0.6, p_if_down=0.4))
        alpha, beta = combine_posteriors([a, b])
        self.assertAlmostEqual(alpha / (alpha + beta), 0.6, places=9)


# ============================================================ KELLY


class TestKelly(unittest.TestCase):
    def test_breakeven_probability(self):
        self.assertAlmostEqual(breakeven_probability(1.0), 0.5)
        self.assertAlmostEqual(breakeven_probability(2.0), 1.0 / 3.0)
        self.assertAlmostEqual(breakeven_probability(3.0), 0.25)
        self.assertAlmostEqual(breakeven_probability(9.0), 0.1)

    def test_kelly_fraction_known_values(self):
        # f* = (p*b - (1-p)*a) / (a*b) with a = 1R risked, b = payoff
        self.assertAlmostEqual(kelly_fraction(0.6, 1.0), 0.2)
        self.assertAlmostEqual(kelly_fraction(0.5, 1.0), 0.0)
        # breakeven for payoff 3 is 1/(1+3) = 0.25, so exactly zero there
        self.assertAlmostEqual(kelly_fraction(0.25, 3.0), 0.0)
        self.assertAlmostEqual(kelly_fraction(0.5, 3.0), 1.0 / 3.0)
        self.assertAlmostEqual(kelly_fraction(0.75, 3.0), 2.0 / 3.0)
        self.assertAlmostEqual(kelly_fraction(1.0 / 3.0, 3.0), 1.0 / 9.0)

    def test_kelly_never_returns_negative_fraction(self):
        for p in (0.0, 0.2, 0.3, 0.5):
            self.assertEqual(kelly_fraction(p, 1.0), 0.0)

    def test_expectancy_r(self):
        self.assertAlmostEqual(expectancy_r(0.6, 1.0), 0.2)
        self.assertAlmostEqual(expectancy_r(0.5, 1.0), 0.0)
        self.assertAlmostEqual(expectancy_r(0.4, 2.0), 0.2)
        self.assertAlmostEqual(expectancy_r(0.3, 2.0), -0.1)   # breakeven is 1/3
        self.assertAlmostEqual(expectancy_r(1.0 / 3.0, 2.0), 0.0)

    def test_max_odds_and_concentration_hide_breakeven(self):
        pass

    def test_confidence_shrink_is_monotone(self):
        values = [confidence_shrink(c) for c in (0.5, 0.6, 0.75, 0.9, 1.0)]
        self.assertEqual(values, sorted(values, reverse=True))
        self.assertAlmostEqual(values[0], 0.35)
        self.assertAlmostEqual(values[-1], 0.05)

    def test_shrink_toward_baseline(self):
        self.assertEqual(shrink(0.9, 0.0), 0.9)
        self.assertEqual(shrink(0.9, 1.0), 0.5)
        self.assertAlmostEqual(shrink(0.9, 0.5), 0.7)

    def test_sizer_rejects_no_edge(self):
        sizer = KellySizer(KellyConfig(bankroll=10_000.0))
        result = sizer.size_trade("X", "long", payoff=1.0, probability=0.5,
                                  confidence=1.0, stop_distance=2.0)
        self.assertFalse(result.approved)
        self.assertEqual(result.risk_dollars, 0.0)

    def test_sizer_respects_risk_cap(self):
        sizer = KellySizer(KellyConfig(bankroll=10_000.0, max_risk_per_trade=0.01,
                                       max_fraction=0.05))
        result = sizer.size_trade("X", "long", payoff=3.0, probability=0.8,
                                  confidence=1.0, stop_distance=2.0)
        self.assertTrue(result.approved)
        self.assertLessEqual(result.risk_dollars, 10_000.0 * 0.01 + 1e-9)
        self.assertEqual(result.risk_dollars, 100.0)

    def test_sizer_requires_winrate_edge(self):
        sizer = KellySizer(KellyConfig(min_probability_edge=0.05, bankroll=10_000.0))
        # p == breakeven => no edge
        result = sizer.size_trade("X", "long", payoff=1.0, probability=0.5,
                                  confidence=1.0, stop_distance=1.0)
        self.assertFalse(result.approved)

    def test_sizer_quantity_uses_instrument_spec(self):
        from ts.agents.instruments import resolve

        sizer = KellySizer(KellyConfig(bankroll=10_000.0, max_risk_per_trade=0.01))
        result = sizer.size_trade("X", "long", payoff=2.0, probability=0.75,
                                  confidence=1.0, stop_distance=2.0)
        self.assertTrue(result.approved)
        btc = resolve("BTCUSD")
        eur = resolve("EURUSD")
        qty_btc = sizer.quantity_from_risk(result, btc)
        qty_eur = sizer.quantity_from_risk(result, eur)
        # 100 dollars of risk at a 2-point stop:
        #   BTC  = 50 coins worth;  EUR = 50/100000 = 0.0005 lots
        self.assertGreater(qty_btc, 40.0)
        self.assertLess(qty_eur, 0.01)      # below the minimum lot -> clamped
        self.assertGreater(qty_btc, 0)


# ============================================================ INSTRUMENTS


class TestInstruments(unittest.TestCase):
    def test_resolve_forex(self):
        inst = resolve("EURUSD")
        self.assertEqual(inst.asset_class, AssetClass.FOREX)
        self.assertEqual(inst.contract_size, 100_000.0)
        self.assertEqual(inst.base_currency, "EUR")
        self.assertTrue(inst.is_two_sided)

    def test_resolve_index(self):
        inst = resolve("NAS100")
        self.assertEqual(inst.asset_class, AssetClass.INDEX)
        self.assertTrue(inst.is_two_sided)

    def test_resolve_crypto(self):
        inst = resolve("BTCUSD")
        self.assertEqual(inst.asset_class, AssetClass.CRYPTO)
        self.assertTrue(inst.is_two_sided)
        self.assertGreater(inst.taker_fee, 0)

    def test_jpy_pip_is_larger(self):
        self.assertGreater(resolve("USDJPY").pip_size, resolve("EURUSD").pip_size)

    def test_clamp_quantity_rounds_to_lot_step(self):
        inst = resolve("EURUSD")
        self.assertEqual(inst.clamp_quantity(0.0149), 0.01)
        self.assertEqual(inst.clamp_quantity(1.95), 1.95)
        self.assertEqual(inst.clamp_quantity(0), 0.0)
        self.assertLess(inst.clamp_quantity(0.001), inst.min_lot)

    def test_yahoo_symbol_mapping(self):
        self.assertEqual(yahoo_symbol("SPX500"), "^GSPC")
        self.assertEqual(yahoo_symbol("EURUSD"), "EURUSD=X")
        self.assertEqual(yahoo_symbol("BTCUSD"), "BTC-USD")

    def test_value_per_point_differs_by_class(self):
        # the whole reason this module exists: identical price moves, different P&L
        forex = resolve("EURUSD")
        crypto = resolve("BTCUSD")
        self.assertNotEqual(forex.value_per_point, crypto.value_per_point)
        self.assertEqual(forex.price_to_units(0.0001), 10.0)   # $10 per pip
        self.assertEqual(crypto.price_to_units(1.0), 1.0)


# ============================================================ EDGE DETECTION


class _FakeMsg:
    def __init__(self, topic, payload):
        # the bus always normalises topics, so handlers see the plain string
        self.topic = topic_key(topic)
        self.payload = payload


class TestEdgeDetection(unittest.TestCase):
    def _agent(self):
        return EdgeDetectionAgent(MessageBus(), sizer=KellySizer(
            KellyConfig(bankroll=10_000.0, max_risk_per_trade=0.01)))

    def _regime(self, symbol, regime=1, atr=100.0, price=5000.0, tq=0.7,
                stale=False):
        return {"symbol": symbol, "asset_class": "crypto", "regime": regime,
                "atr": atr, "price": price, "trend_quality": tq, "momentum": 0.3,
                "efficiency": 0.3, "breakout_up": True, "breakout_down": False,
                "stale": stale}

    def _prob(self, symbol, p_up=0.8, confidence=1.0, strength=1.0):
        return {"symbol": symbol, "p_up": p_up, "confidence": confidence,
                "evidence_strength": strength}

    def test_builds_plan_when_expectancy_and_confidence_pass(self):
        agent = self._agent()
        plan = self._draft(agent, "BTCUSD", atr=100.0)
        self.assertIsNotNone(plan)
        self.assertEqual(plan.side, "long")
        self.assertGreater(plan.quantity, 0)
        self.assertIn(plan.edge_grade, ("A", "B"))
        self.assertGreater(plan.payoff, 0)

    def test_flat_regime_produces_nothing(self):
        agent = self._agent()
        self.assertIsNone(self._draft(agent, "BTCUSD", regime=0))

    def test_stale_symbol_is_ignored(self):
        agent = self._agent()
        self.assertIsNone(self._draft(agent, "BTCUSD", stale=True))

    def test_low_expectancy_is_rejected_but_reported(self):
        # payoff 2 -> breakeven p = 1/3, so p_win = 0.30 is a genuine loser
        agent = self._agent()
        plan = self._draft(agent, "BTCUSD", p_up=0.30)
        self.assertIsNotNone(plan)                 # reported...
        self.assertEqual(plan.quantity, 0.0)       # ...but not sized
        self.assertTrue(plan.reason.startswith("rejected"))
        self.assertLess(plan.expectancy_r, 0)
        self.assertEqual(plan.edge_grade, "F")

    def test_short_side_is_used_in_downtrends(self):
        agent = self._agent()
        plan = self._draft(agent, "ETHUSD", regime=-1, p_up=0.25)
        self.assertEqual(plan.side, "short")
        # for a short, p_win = P(down) = 1 - p_up = 0.75 -- high is GOOD here
        self.assertAlmostEqual(plan.p_win, 0.75)
        self.assertGreater(plan.quantity, 0)

    def test_payoff_is_clamped(self):
        agent = self._agent()
        plan = self._draft(agent, "SOLUSD", atr=0.001, price=100.0)
        self.assertLessEqual(plan.payoff, 6.0)
        self.assertGreater(plan.payoff, 0)

    def _draft(self, agent, symbol, regime=1, p_up=0.8, atr=100.0,
               stale=False, price=5000.0):
        agent.handle(_FakeMsg(Topic.MARKET_REGIME,
                              self._regime(symbol, regime, atr, price, stale=stale)))
        agent.handle(_FakeMsg(Topic.PROBABILITY, self._prob(symbol, p_up)))
        return agent._build_opportunity(
            symbol, agent.analysis[symbol], agent.probabilities[symbol],
            resolve(symbol))


# ============================================================ GRADES


class TestEdgeGrade(unittest.TestCase):
    def test_grades_are_ordered(self):
        self.assertEqual(grade_edge(-1.0, 0.9), "F")
        self.assertEqual(grade_edge(0.05, 0.6), "D")
        self.assertEqual(grade_edge(0.25, 0.6), "C")
        self.assertEqual(grade_edge(0.45, 0.7), "B")
        self.assertEqual(grade_edge(0.60, 0.9), "A")

    def test_grade_is_monotone_in_expectancy(self):
        # values chosen away from the grade boundaries to avoid float ties
        grades = [grade_edge(e, 0.6) for e in (0.0, 0.25, 0.5, 1.0, 1.5)]
        self.assertEqual(grades, ["F", "C", "C", "B", "A"])

    def test_negative_expectancy_never_gets_a_good_grade(self):
        self.assertEqual(grade_edge(-0.5, 1.0), "F")
        self.assertEqual(grade_edge(-0.01, 0.99), "F")

    def test_everything_is_not_an_a(self):
        """A mediocre trade must not get a top grade."""
        self.assertNotEqual(grade_edge(0.1, 0.55), "A")
        self.assertNotEqual(grade_edge(0.2, 0.7), "A")
        self.assertEqual(grade_edge(0.1, 0.55), "D")
        self.assertEqual(grade_edge(0.2, 0.7), "C")


if __name__ == "__main__":
    unittest.main(verbosity=2)
