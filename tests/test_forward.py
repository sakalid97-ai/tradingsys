"""Tests for the forward-tracking journal and its statistics."""

from __future__ import annotations

import statistics
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ts.forward import (  # noqa: E402
    Baseline,
    ForwardJournal,
    ForwardTrade,
    mean_interval,
    wilson_interval,
)
from ts.trades import Trade  # noqa: E402


def trade(r: float, symbol="EURUSD", pnl=None, reason="stop") -> ForwardTrade:
    return ForwardTrade(
        symbol=symbol, side="long", quantity=1.0, entry_price=100.0,
        exit_price=100.0 + r, r_multiple=r,
        net_pnl=(pnl if pnl is not None else r * 100.0),
        exit_reason=reason)


class TestWilsonInterval(unittest.TestCase):
    def test_known_values(self):
        # 45 wins in 100 trials: classic worked example
        low, high = wilson_interval(45, 100)
        self.assertAlmostEqual(low, 0.356, places=2)
        self.assertAlmostEqual(high, 0.548, places=2)

    def test_zero_total_is_empty(self):
        self.assertEqual(wilson_interval(0, 0), (0.0, 0.0))

    def test_all_successes_does_not_reach_one(self):
        low, high = wilson_interval(10, 10)
        self.assertLessEqual(high, 1.0)
        self.assertLess(low, 1.0, "a perfect sample must not imply certainty")

    def test_no_successes_stays_above_zero(self):
        low, high = wilson_interval(0, 10)
        self.assertGreaterEqual(low, 0.0, "never a negative bound")
        self.assertLess(high, 0.3)

    def test_interval_narrows_with_more_data(self):
        narrow = wilson_interval(45, 1000)
        wide = wilson_interval(45, 100)
        self.assertLess(narrow[1] - narrow[0], wide[1] - wide[0])

    def test_invalid_counts_raise(self):
        with self.assertRaises(ValueError):
            wilson_interval(11, 10)
        with self.assertRaises(ValueError):
            wilson_interval(-1, 10)


class TestMeanInterval(unittest.TestCase):
    def test_single_value_is_exact(self):
        self.assertEqual(mean_interval([0.5]), (0.5, 0.5))

    def test_empty_is_empty(self):
        self.assertEqual(mean_interval([]), (0.0, 0.0))

    def test_interval_contains_the_mean(self):
        values = [0.5, -0.3, 1.2, -0.8, 0.1, 0.9]
        low, high = mean_interval(values)
        mean = statistics.fmean(values)
        self.assertLess(low, mean)
        self.assertGreater(high, mean)

    def test_narrows_with_more_data(self):
        few = mean_interval([1.0, -1.0, 0.5])
        many = mean_interval([1.0, -1.0, 0.5] * 20)
        self.assertLess(many[1] - many[0], few[1] - few[0])


class TestJournalRoundTrip(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_record_and_load(self):
        journal = ForwardJournal(self.root)
        journal.record(trade(1.5))
        journal.record(trade(-0.9))
        loaded = journal.load()
        self.assertEqual(len(loaded), 2)
        self.assertAlmostEqual(loaded[0].r_multiple, 1.5)

    def test_empty_journal_summary(self):
        summary = ForwardJournal(self.root).summary()
        self.assertEqual(summary["n_trades"], 0)
        self.assertEqual(summary["verdict"], "no data")

    def test_baseline_round_trip(self):
        journal = ForwardJournal(self.root)
        journal.set_baseline(Baseline(win_rate=0.45, expectancy_r=0.57))
        loaded = journal.load_baseline()
        self.assertAlmostEqual(loaded.win_rate, 0.45)
        self.assertAlmostEqual(loaded.expectancy_r, 0.57)

    def test_reset_clears_everything(self):
        journal = ForwardJournal(self.root)
        journal.record(trade(1.0))
        journal.set_baseline(Baseline(win_rate=0.5))
        journal.reset()
        self.assertEqual(journal.load(), [])
        self.assertIsNone(journal.load_baseline())

    def test_corrupt_lines_are_skipped_not_fatal(self):
        import json as _json

        good = ForwardTrade(symbol="X", side="long", quantity=1.0,
                           entry_price=100.0, exit_price=101.0,
                           r_multiple=1.0, net_pnl=10.0)
        (self.root / "forward_trades.jsonl").write_text(
            _json.dumps(good.to_dict()) + '\nnot json\n', encoding="utf-8")
        journal = ForwardJournal(self.root)
        self.assertEqual(len(journal.load()), 1)

    def test_engine_trade_adapts(self):
        ts = pd_index = None
        import pandas as pd

        engine_trade = Trade(
            symbol="BTCUSD", side="long", quantity=2,
            entry_time=pd.Timestamp("2026-01-01"), entry_price=100.0,
            exit_time=pd.Timestamp("2026-01-02"), exit_price=105.0,
            stop_price=95.0, initial_risk=10.0,
        )
        forward = ForwardTrade.from_engine_trade(engine_trade, "trend_breakout", "paper")
        self.assertEqual(forward.symbol, "BTCUSD")
        self.assertAlmostEqual(forward.r_multiple, 1.0)
        self.assertEqual(forward.strategy, "trend_breakout")


class TestDriftVerdict(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.journal = ForwardJournal(self.root)
        # the strategy's actual backtest numbers
        self.journal.set_baseline(Baseline(
            win_rate=0.4488, expectancy_r=0.497, profit_factor=2.52,
            avg_win_r=2.27, avg_loss_r=-0.94, n_trades=127,
            source="last backtest"))

    def tearDown(self):
        self.tmp.cleanup()

    def test_matching_performance_is_tracking(self):
        # a realistic stream: ~45% winners at +2.3R, ~55% losers at -0.9R,
        # which has expectancy ~0.6R -- close enough to the backtest's 0.497R
        pattern = [2.3, -0.9, -0.9, 2.3, 2.3, -0.9, -0.9, -0.9, 2.3, -0.9,
                   -0.9, 2.3, -0.9, -0.9, -0.9, 2.3, -0.9, 2.3, 2.3, 2.3]
        for _ in range(3):
            for r in pattern:
                self.journal.record(trade(r))
        summary = self.journal.summary()
        self.assertEqual(summary["verdict"].split(":")[0], "TRACKING")
        self.assertFalse(summary["drift"]["metrics"]["expectancy_r"]["outside_ci"])

    def test_too_few_trades_says_so(self):
        self.journal.record(trade(-1.0))
        summary = self.journal.summary()
        self.assertIn("too early", summary["verdict"])
        self.assertIn("do not adjust", summary["verdict"])

    def test_all_losses_is_drifting(self):
        for _ in range(40):
            self.journal.record(trade(-1.0))
        summary = self.journal.summary()
        self.assertIn("DRIFTING", summary["verdict"])
        self.assertLess(summary["drift"]["metrics"]["expectancy_r"]["gap"], -0.9)
        self.assertTrue(summary["drift"]["metrics"]["expectancy_r"]["outside_ci"])

    def test_short_losing_streak_is_not_a_verdict(self):
        """Six losses after a good backtest is noise, and the tool must say so."""
        self.journal.set_baseline(Baseline(win_rate=0.45, expectancy_r=0.57))
        for _ in range(6):
            self.journal.record(trade(-1.0))
        summary = self.journal.summary()
        self.assertIn("too early", summary["verdict"])

    def test_no_baseline_is_reported(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            journal = ForwardJournal(tmp)
            for _ in range(30):
                journal.record(trade(0.5))
            summary = journal.summary()
        self.assertIn("no baseline", summary["verdict"])
        self.assertEqual(summary["drift"]["status"], "no baseline")
    def test_expected_value_inside_ci_is_not_drifting(self):
        """The core claim: a prediction inside the CI is not drift."""
        for _ in range(50):
            # R values roughly consistent with 0.497R expectancy
            self.journal.record(trade(0.497))
        summary = self.journal.summary()
        low, high = summary["expectancy_r_ci"]
        self.assertLessEqual(low, 0.497)
        self.assertGreaterEqual(high, 0.497)
        self.assertEqual(summary["drift"]["metrics"]["expectancy_r"]["outside_ci"],
                         False)

    def test_equity_curve_and_drawdown(self):
        journal = ForwardJournal(self.root)
        for r in (1.0, 2.0, -1.0, -2.0, 1.0):
            journal.record(trade(r))
        summary = journal.summary()
        self.assertEqual(len(summary["equity_curve"]), 5)
        # equity +100,+300,+200,+0,+100 -> peak 300, final 100, max dd 300
        self.assertAlmostEqual(summary["max_drawdown"], 300.0, places=6)
        self.assertAlmostEqual(summary["total_pnl"], 100.0, places=6)
        self.assertEqual(summary["profit_factor"], round(400.0 / 300.0, 3))


class TestCaptureBaselineFromState(unittest.TestCase):
    def test_captures_from_backtest_json(self):
        import json
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "backtest.json").write_text(json.dumps({
                "metrics": {"win_rate": 0.4474, "expectancy_R": 0.5678,
                            "n_trades": 127}}), encoding="utf-8")
            journal = ForwardJournal(tmp)
            baseline = journal.capture_baseline_from_state()
            self.assertIsNotNone(baseline)
            self.assertAlmostEqual(baseline.expectancy_r, 0.5678)
            self.assertEqual(baseline.source, "last backtest")


if __name__ == "__main__":
    unittest.main(verbosity=2)
