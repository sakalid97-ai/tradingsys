"""Test suite for the trading system.

Run with:  py -m unittest discover -s tradingsys/tests -v
       or:  py tradingsys/tests/test_suite.py

These tests assert the properties that separate a real backtest from a
fantasy. The most important one is ``test_no_lookahead`` -- it proves signals
cannot see the future.
"""

from __future__ import annotations

import sys
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ts.backtest import BacktestConfig, Backtester, apply_slippage, commission
from ts.datafeed import YahooFeed  # noqa: F401  (kept for optional live tests)
from ts.indicators import atr, donchian_upper, sma
from ts.metrics import compute_metrics
from ts.quality import validate
from ts.risk import PortfolioGuard, PortfolioRiskState, RiskConfig
from ts.strategy import Strategy, StrategyParams, apply_market_gate
from ts.trades import Trade


# --------------------------------------------------------------------- helpers


def make_frame(n: int = 600, seed: int = 7, start: str = "2021-01-01") -> pd.DataFrame:
    """Reproducible synthetic OHLCV with a trend plus noise."""
    rng = np.random.default_rng(seed)
    tz = "UTC"
    index = pd.date_range(start, periods=n, tz=tz, freq="B")

    drift = 0.0004
    shock = rng.normal(0, 0.0009, n).tolist()
    # inject a couple of clean trends so breakouts have something to find
    for i in range(n // 4, n // 2):
        shock[i] += 0.0016
    for i in range(n - n // 5, n):
        shock[i] -= 0.0018

    close = [100.0]
    for s in shock:
        close.append(close[-1] * (1.0 + drift + s))
    close = close[1:]

    high, low, volume = [], [], []
    prev_close = close[0]
    for i, c in enumerate(close):
        rng_hi = abs(rng.normal(0, 0.006))
        rng_lo = abs(rng.normal(0, 0.006))
        h = max(c, prev_close) * (1.0 + rng_hi)
        low_ = min(c, prev_close) * (1.0 - rng_lo)
        high.append(h)
        low.append(low_)
        volume.append(float(rng.uniform(1e6, 5e6)))
        prev_close = c

    return pd.DataFrame(
        {"open": close, "high": high, "low": low, "close": close, "volume": volume},
        index=index,
    )


def make_universe(symbols=("AAA", "BBB", "CCC"), seed: int = 3):
    return {
        s: make_frame(seed=seed + i, start="2021-01-01")
        for i, s in enumerate(symbols)
    }


# ------------------------------------------------------------------------ tests


class TestIndicators(unittest.TestCase):
    def setUp(self):
        self.df = make_frame(400)

    def test_sma_matches_manual(self):
        s = sma(self.df["close"], 10)
        manual = self.df["close"].rolling(10).mean()
        pd.testing.assert_series_equal(s, manual)

    def test_atr_positive_and_wilder_smoothed(self):
        a = atr(self.df, 14)
        self.assertTrue((a.dropna() > 0).all())
        self.assertTrue(a.notna().sum() > 10)

    def test_donchian_excludes_current_bar(self):
        """Upper channel at bar t must be the max of highs strictly before t."""
        upper = donchian_upper(self.df, 20)
        t = 250
        expected = self.df["high"].iloc[t - 20:t].max()
        self.assertAlmostEqual(float(upper.iloc[t]), float(expected), places=6)

    def test_donchian_never_uses_future(self):
        """Upper channel at bar t must equal the max high over [t-20, t-1]."""
        upper = donchian_upper(self.df, 20)
        for t in (30, 100, 101, 250, 399):
            expected = float(self.df["high"].iloc[t - 20:t].max())
            self.assertAlmostEqual(float(upper.iloc[t]), expected, places=6)


class TestDataQuality(unittest.TestCase):
    def test_rejects_non_positive_prices(self):
        df = make_frame(300)
        df.iloc[50, df.columns.get_loc("close")] = -5.0
        out = validate(df, "TEST")
        self.assertNotIn(pd.Timestamp(df.index[50]), out.index)

    def test_repairs_inverted_high_low(self):
        df = make_frame(300)
        df.iloc[40, df.columns.get_loc("high")] = 1.0      # below low
        df.iloc[41, df.columns.get_loc("low")] = 10_000.0  # above high
        out = validate(df, "TEST")
        self.assertTrue((out["high"] >= out["low"]).all())

    def test_drops_duplicate_index_and_sorts(self):
        df = make_frame(200)
        dup = pd.concat([df, df.head(5)])
        shuffled = dup.sample(frac=1.0, random_state=1)
        out = validate(shuffled, "TEST")
        self.assertTrue(out.index.is_monotonic_increasing)
        self.assertEqual(len(out.index.unique()), len(out))
        self.assertEqual(len(out), 200)

    def test_missing_columns_raise(self):
        with self.assertRaises(ValueError):
            validate(pd.DataFrame({"close": [1, 2, 3]}), "TEST")


class TestStrategyCausality(unittest.TestCase):
    """The single most important test class in the suite."""

    def _signals(self, frame):
        return Strategy(StrategyParams(min_history=120)).compute(frame)

    def test_no_lookahead_appending_future_bars(self):
        """A signal must not change when extra future bars are appended."""
        longer = make_frame(800, seed=11)
        base = longer.iloc[:500].copy()      # a true prefix of `longer`

        sig_base = self._signals(base)
        sig_long = self._signals(longer)

        overlap = sig_base.index.intersection(sig_long.index)
        self.assertEqual(len(overlap), 500)

        for col in ("entry_long", "channel_exit"):
            self.assertTrue((sig_base.loc[overlap, col] == sig_long.loc[overlap, col]).all(),
                            f"column {col} changed when future bars were appended")
        # ATR is causal too, so it must match exactly over the overlap.
        a = sig_base.loc[overlap, "atr"].astype(float)
        b = sig_long.loc[overlap, "atr"].astype(float)
        self.assertTrue(np.allclose(a, b, equal_nan=True))

    def test_signal_depends_only_on_past_bars(self):
        """Perturb the last bar: earlier signals must not move."""
        base = make_frame(500, seed=5)
        perturbed = base.copy()
        perturbed.iloc[-1, perturbed.columns.get_loc("close")] *= 1.5
        perturbed.iloc[-1, perturbed.columns.get_loc("high")] *= 1.5

        a = self._signals(base)
        b = self._signals(perturbed)
        for col in ("entry_long", "channel_exit"):
            same = (a[col].iloc[:-2] == b[col].iloc[:-2]).all()
            self.assertTrue(same, f"{col} reacted to a future bar")

    def test_market_gate_never_affects_exits(self):
        frame = make_frame(400, seed=21)
        sig = self._signals(frame)
        market = make_frame(400, seed=99)

        gated = apply_market_gate({"X": sig}, market)["X"]
        self.assertTrue((gated["entry_long"] & ~sig["entry_long"]).sum() == 0,
                        "gating must only ever REMOVE entries")
        if "market_risk_on" in gated.columns:
            # channel exits are untouched by the gate
            self.assertTrue((gated["channel_exit"] == sig["channel_exit"]).all())

    def test_insufficient_history_raises(self):
        short = make_frame(50)
        with self.assertRaises(ValueError):
            Strategy(StrategyParams(min_history=220)).compute(short)

    def test_params_clamp_repairs_nonsense(self):
        p = StrategyParams(entry_lookback=-5, adx_min=999, atr_stop_mult=0.0)
        c = p.clamp()
        self.assertGreaterEqual(c.entry_lookback, 2)
        self.assertLessEqual(c.adx_min, 60)
        self.assertGreaterEqual(c.atr_stop_mult, 0.5)


class TestRiskSizing(unittest.TestCase):
    def test_risk_is_exactly_one_percent(self):
        cfg = RiskConfig(risk_per_trade=0.01, max_position_pct=1.0).clamp()
        from ts.risk import PositionSizer

        sizer = PositionSizer(cfg)
        equity = 250_000.0
        price = 50.0
        stop_distance = 2.0

        decision = sizer.quantity_for_risk(equity, price, stop_distance, "T", "buy")
        self.assertTrue(decision.approved)
        self.assertEqual(decision.quantity, int((equity * 0.01) / stop_distance))
        self.assertAlmostEqual(decision.risk_dollars, decision.quantity * stop_distance, places=6)
        self.assertAlmostEqual(decision.risk_dollars / equity, 0.01, places=6)

    def test_rejects_when_one_share_too_big(self):
        """1% of a small account must not be spent on a wide stop."""
        from ts.risk import PositionSizer

        cfg = RiskConfig(risk_per_trade=0.01, max_position_pct=1.0).clamp()
        # equity 1,000 -> risk budget 10 dollars, but the stop is 20 dollars away
        decision = PositionSizer(cfg).quantity_for_risk(1_000.0, 10.0, 20.0, "T", "buy")
        self.assertFalse(decision.approved)
        self.assertIn("one share", decision.rejection)

    def test_position_cap_binds(self):
        from ts.risk import PositionSizer

        cfg = RiskConfig(risk_per_trade=0.01, max_position_pct=0.05).clamp()
        decision = PositionSizer(cfg).quantity_for_risk(
            100_000.0, 500.0, 0.5, "T", "buy")
        self.assertTrue((decision.quantity * 500.0) <= 100_000.0 * 0.05 + 1e-6)
        self.assertTrue(decision.approved)

    def test_guard_blocks_on_position_limit(self):
        guard = PortfolioGuard(RiskConfig(max_positions=2))
        from ts.risk import OrderDecision

        order = OrderDecision("T", "buy", 10, 10.0, 9.0, 100.0, 100.0)
        state = PortfolioRiskState(equity=100_000, peak_equity=100_000, cash=100_000,
                                   open_positions=2)
        result = guard.approve(state, order)
        self.assertFalse(result.approved)

    def test_guard_blocks_on_total_risk(self):
        guard = PortfolioGuard(RiskConfig(max_total_risk=0.02, risk_per_trade=0.01))
        from ts.risk import OrderDecision

        order = OrderDecision("T", "buy", 10, 10.0, 9.0, 100.0, 100.0)
        # budget 2% of 100k = 2,000; 1,950 existing + 100 new = 2,050 -> blocked
        state = PortfolioRiskState(equity=100_000, peak_equity=100_000, cash=100_000,
                                   open_risk=1_950)
        result = guard.approve(state, order)
        self.assertFalse(result.approved)

    def test_guard_blocks_on_daily_loss(self):
        guard = PortfolioGuard(RiskConfig(daily_loss_limit=0.03))
        from ts.risk import OrderDecision

        order = OrderDecision("T", "buy", 10, 10.0, 9.0, 100.0, 100.0)
        state = PortfolioRiskState(equity=96_000, peak_equity=100_000, cash=96_000,
                                   day_start_equity=100_000)
        result = guard.approve(state, order)
        self.assertFalse(result.approved)


class TestBacktestInvariants(unittest.TestCase):
    def setUp(self):
        self.data = make_universe()
        self.params = StrategyParams(min_history=150)

    def _run(self, data=None, params=None):
        return Backtester(
            strategy_params=params or self.params,
            risk_config=RiskConfig(risk_per_trade=0.01, max_positions=6, max_total_risk=0.06),
            config=BacktestConfig(initial_capital=100_000.0, slippage_bps=5.0),
            market_gate=None,
        ).run(data or self.data)

    def test_produces_trades_and_equity(self):
        result = self._run()
        self.assertGreater(len(result.trades), 0, "synthetic data should generate trades")
        self.assertFalse(result.equity_curve.empty)
        self.assertIn("equity", result.equity_curve.columns)

    def test_cash_never_negative(self):
        result = self._run()
        self.assertTrue((result.equity_curve["cash"] >= -1e-6).all(),
                        "the backtester spent cash it did not have")

    def test_equity_equals_cash_plus_positions(self):
        result = self._run()
        diff = (result.equity_curve["equity"]
                - result.equity_curve["cash"]
                - result.equity_curve["positions_value"]).abs().max()
        self.assertLess(diff, 1e-6)

    def test_entries_fill_the_bar_after_the_signal(self):
        result = self._run()
        frame = self.data["AAA"]
        sig = self._signals_for(frame)
        filled = [t for t in result.trades if t.symbol == "AAA"]
        self.assertTrue(filled, "expected at least one AAA fill")
        for trade in filled:
            entry_day = trade.entry_time.normalize()
            prior = sig[sig.index < entry_day]
            self.assertGreater(len(prior), 0, "a fill happened with no bar before it")
            # the bar immediately preceding the fill must have been an entry signal
            self.assertTrue(bool(prior["entry_long"].iloc[-1]),
                            "the bar before the fill was not an entry signal")

    def _signals_for(self, frame):
        return Strategy(StrategyParams(min_history=150)).compute(frame)

    def test_stops_never_lose_more_than_plan(self):
        """No losing trade may lose materially more than the 1R it risked."""
        result = self._run()
        for trade in result.trades:
            if trade.net_pnl >= 0:
                continue
            self.assertGreater(trade.r_multiple, -1.6,
                               f"{trade.symbol} lost {trade.r_multiple:.2f}R, over 1.6R plan")

    def test_stop_out_is_bounded_to_one_percent(self):
        """A deterministic stop-out must lose about 1R, never several R."""
        frame = self._breakout_then_crash()
        result = Backtester(
            strategy_params=StrategyParams(min_history=150, require_regime_long=False,
                                           adx_min=0, volume_min_ratio=0),
            risk_config=RiskConfig(risk_per_trade=0.01, max_positions=6,
                                   max_position_pct=1.0),
            config=BacktestConfig(initial_capital=100_000.0),
        ).run({"X": frame})

        self.assertEqual(len(result.trades), 1,
                         "the spike should produce exactly one trade")
        trade = result.trades[0]
        self.assertEqual(trade.exit_reason, "stop")
        self.assertLess(trade.r_multiple, 0)
        # 1R was planned; allow room for commission and slippage only
        self.assertGreater(trade.r_multiple, -1.25)
        self.assertLess(trade.return_pct, 0)
        self.assertGreater(trade.return_pct, -0.015)

    def _breakout_then_crash(self, n_calm: int = 300, n_crash: int = 25) -> pd.DataFrame:
        """Decline (no breakouts) -> single spike (one breakout) -> crash.

        The calm leg is strictly monotone, so it can never make a new 20-day
        high; the spike is the only breakout in the whole series.  The first
        crash bar *opens at the spike* and then trades down through the stop, so
        the stop is filled at the stop price rather than through a gap -- this
        tests the stop mechanism, not gap risk.
        """
        index = pd.date_range("2021-01-01", periods=n_calm + n_crash + 3,
                              tz="UTC", freq="B")
        closes = [100.0 * (1.0 - 0.20 * i / n_calm) for i in range(n_calm)]
        spike = closes[-1] * 1.22                      # the one and only breakout

        # one deep bar that contains the stop inside its range
        crash_first = spike * 0.99
        tail = [crash_first * (1.0 - 0.005 * j) for j in range(n_crash)]
        series = closes + [spike] + [crash_first] + tail

        # first crash bar opens at the spike (no gap) then falls
        opens = closes + [spike] + [spike] + tail
        highs = [c * 1.001 for c in series]
        lows = [c * 0.999 for c in series]
        highs[n_calm] = spike * 1.001
        lows[n_calm] = spike * 0.999
        highs[n_calm + 1] = spike * 1.0005             # the stop-hunting bar
        lows[n_calm + 1] = spike * 0.985
        opens[n_calm + 1] = spike                      # opens above the stop

        return pd.DataFrame(
            {
                "open": opens,
                "high": highs,
                "low": lows,
                "close": series,
                "volume": 1_000_000.0,
            },
            index=index[: len(series)],
        )

    def test_slippage_and_commission_are_charged(self):
        cfg = BacktestConfig(initial_capital=100_000.0, slippage_bps=100.0,
                             commission_per_share=0.5, commission_min=5.0)
        result = Backtester(
            strategy_params=self.params,
            risk_config=RiskConfig(),
            config=cfg,
        ).run(self.data)
        self.assertGreater(result.metrics["total_fees"], 0)
        self.assertGreater(result.metrics["total_slippage"], 0)

    def test_fill_price_matches_next_open_with_slippage(self):
        """Reproduce one trade's fill price exactly from the source data."""
        result = self._run()
        frame = self.data[result.trades[0].symbol]
        trade = result.trades[0]
        bar = frame.index[frame.index >= trade.entry_time]
        self.assertTrue(len(bar) > 0)
        open_price = float(frame.loc[bar[0], "open"])
        expected = apply_slippage(open_price, "buy", 5.0)
        self.assertAlmostEqual(trade.entry_price, expected, places=6)

    def test_higher_risk_increases_position_size(self):
        """Doubling the risk budget must double the share count for the same stop."""
        from ts.risk import PositionSizer

        equity, price, stop_dist = 250_000.0, 100.0, 2.0
        cfg = RiskConfig(risk_per_trade=0.01, max_position_pct=1.0).clamp()
        small = PositionSizer(cfg).quantity_for_risk(equity, price, stop_dist, "T", "buy")

        cfg2 = RiskConfig(risk_per_trade=0.02, max_position_pct=1.0).clamp()
        big = PositionSizer(cfg2).quantity_for_risk(equity, price, stop_dist, "T", "buy")

        self.assertAlmostEqual(big.quantity, small.quantity * 2, delta=1)
        self.assertAlmostEqual(big.risk_dollars, small.risk_dollars * 2, delta=1.0)

    def test_skips_short_history(self):
        data = dict(self.data)
        data["TINY"] = make_frame(60)
        result = self._run(data=data)
        self.assertIn("TINY", result.metrics["skipped_symbols"])
        self.assertTrue(all(t.symbol != "TINY" for t in result.trades))

    def test_no_trades_when_gate_never_allows(self):
        """A permanently bearish market gate must produce zero entries."""
        baseline = self._run()
        bearish_raw = make_frame(600, seed=3)
        bearish = pd.DataFrame({
            "open": np.linspace(300, 20, len(bearish_raw)),
            "high": np.linspace(300, 20, len(bearish_raw)) * 1.01,
            "low": np.linspace(300, 20, len(bearish_raw)) * 0.99,
            "close": np.linspace(300, 20, len(bearish_raw)),
            "volume": 1_000_000.0,
        }, index=bearish_raw.index)

        gated = Backtester(
            strategy_params=self.params,
            risk_config=RiskConfig(max_positions=6),
            config=BacktestConfig(initial_capital=100_000.0),
            market_gate=bearish,
        ).run(self.data)

        self.assertEqual(len(gated.trades), 0,
                         "a permanently risk-off gate must never allow an entry")
        self.assertAlmostEqual(gated.metrics["final_equity"], 100_000.0, places=2)
        self.assertLess(len(gated.trades), len(baseline.trades))

    def test_metrics_computed(self):
        result = self._run()
        m = result.metrics
        for key in ("total_return", "cagr", "max_drawdown", "sharpe",
                    "expectancy_R", "win_rate", "n_trades", "profit_factor"):
            self.assertIn(key, m)
        self.assertGreaterEqual(m["max_drawdown"], 0.0)
        self.assertLessEqual(m["win_rate"], 1.0)


class TestTradeRecord(unittest.TestCase):
    def test_r_multiple_matches_hand_calc(self):
        t = Trade(
            symbol="T", side="long", quantity=100,
            entry_time=pd.Timestamp("2024-01-01"), entry_price=50.0,
            exit_time=pd.Timestamp("2024-02-01"), exit_price=55.0,
            stop_price=48.0, initial_risk=200.0, fees=10.0,
            equity_before=100_000.0,
        )
        self.assertEqual(t.initial_risk, 200.0)
        self.assertAlmostEqual(t.net_pnl, 490.0)
        self.assertAlmostEqual(t.r_multiple, 490.0 / 200.0)
        self.assertTrue(t.is_winner)

    def test_short_side_direction(self):
        t = Trade(
            symbol="T", side="short", quantity=10,
            entry_time=pd.Timestamp("2024-01-01"), entry_price=100.0,
            exit_time=pd.Timestamp("2024-01-05"), exit_price=90.0,
            stop_price=105.0, initial_risk=50.0,
        )
        self.assertAlmostEqual(t.gross_pnl, 100.0)
        self.assertTrue(t.is_winner)


class TestMetricsAndMonteCarlo(unittest.TestCase):
    def test_compute_metrics_on_cash_only(self):
        idx = pd.date_range("2024-01-01", periods=60, freq="B", tz="UTC")
        eq = pd.DataFrame({"equity": np.linspace(100_000, 110_000, 60)}, index=idx)
        # ``pnl`` is authoritative and net of fees, so expected outcomes are
        # stated directly rather than recomputed from prices.
        trades = [
            Trade("A", "long", 10, idx[0], 10.0, idx[5], 11.0, 9.9, 10.0,
                  equity_before=100_000.0),
            Trade("B", "long", 10, idx[10], 10.0, idx[15], 9.85, 9.9, 10.0,
                  equity_before=100_000.0),
        ]
        m = compute_metrics(eq, trades, risk_per_trade=0.01)
        self.assertEqual(m["n_trades"], 2)
        self.assertEqual(m["n_wins"], 1)
        self.assertEqual(m["n_losses"], 1)
        self.assertAlmostEqual(m["win_rate"], 0.5)
        self.assertGreater(m["expectancy_R"], 0)

    def test_max_drawdown_hand_calc(self):
        from ts.metrics import max_drawdown

        eq = pd.Series([100, 120, 60, 90, 100])
        self.assertAlmostEqual(max_drawdown(eq), 0.5)

    def test_monte_carlo_returns_distribution(self):
        from ts.metrics import monte_carlo_trades

        rng = np.random.default_rng(1)
        trades = [
            Trade("A", "long", 1, pd.Timestamp("2024-01-01"), 10.0,
                  pd.Timestamp("2024-01-02"), 10.0, 9.0, 1.0)
            for _ in range(120)
        ]
        mc = monte_carlo_trades(trades, n_runs=300, seed=2, risk_per_trade=0.01)
        self.assertEqual(mc["n_runs"], 300)
        self.assertIn("p50_ending", mc)
        self.assertIn("prob_finish_positive", mc)


class TestBrokerPaper(unittest.TestCase):
    def test_paper_fills_and_accounting(self):
        from ts.broker.base import Order
        from ts.broker.paper import PaperBroker, PaperBrokerConfig

        prices = {"XYZ": 100.0}
        broker = PaperBroker(lambda s: prices[s],
                             PaperBrokerConfig(starting_cash=50_000.0, slippage_bps=0.0,
                                               commission_min=1.0,
                                               commission_per_share=0.0))
        fill = broker.submit(Order("XYZ", 10, "buy", type="market"))
        self.assertEqual(fill.quantity, 10)
        self.assertEqual(fill.price, 100.0)
        self.assertEqual(broker.cash, 50_000.0 - 1000.0 - 1.0)
        self.assertEqual(len(broker.positions()), 1)

        # price moves up, sell it back
        prices["XYZ"] = 110.0
        pos = broker.positions()[0]
        broker.submit(Order("XYZ", pos.quantity, "sell", type="market"))
        self.assertEqual(len(broker.positions()), 0)
        # 10 x 10 gross profit, minus the entry fee and the exit fee
        self.assertAlmostEqual(broker.realised_pnl, 100.0 - 2.0, places=6)
        # and cash must agree with the summed P&L
        self.assertAlmostEqual(broker.cash, 50_000.0 - 1001.0 + (1100.0 - 1.0), places=6)
        self.assertAlmostEqual(broker.cash - 50_000.0, broker.realised_pnl, places=6)

    def test_paper_rejects_over_buying(self):
        from ts.broker.base import BrokerError, Order
        from ts.broker.paper import PaperBroker, PaperBrokerConfig

        broker = PaperBroker(lambda s: 100.0,
                             PaperBrokerConfig(starting_cash=1_000.0))
        with self.assertRaises(BrokerError):
            broker.submit(Order("XYZ", 100, "buy", type="market"))

    def test_paper_rejects_selling_nothing(self):
        from ts.broker.base import BrokerError, Order
        from ts.broker.paper import PaperBroker, PaperBrokerConfig

        broker = PaperBroker(lambda s: 100.0)
        with self.assertRaises(BrokerError):
            broker.submit(Order("XYZ", 10, "sell", type="market"))


class TestCommissions(unittest.TestCase):
    def test_commission_floor(self):
        self.assertEqual(commission(10, 50.0, 0.005, 1.0), 1.0)      # floor binds
        self.assertEqual(commission(1000, 50.0, 0.005, 1.0), 5.0)    # per-share binds
        self.assertEqual(commission(0, 50.0, 0.005, 1.0), 0.0)       # no fills, no fee

    def test_slippage_always_against_fill(self):
        self.assertGreater(apply_slippage(100.0, "buy", 10.0), 100.0)
        self.assertLess(apply_slippage(100.0, "sell", 10.0), 100.0)


class TestStateStore(unittest.TestCase):
    def test_roundtrip(self):
        import tempfile

        from ts.state import StateStore

        with tempfile.TemporaryDirectory() as tmp:
            store = StateStore(tmp)
            store.save_snapshot({"equity": 123_456.0, "positions": []})
            self.assertEqual(store.read_snapshot()["equity"], 123_456.0)

            store.append_trade({"symbol": "A", "r_multiple": 1.5, "net_pnl": 150.0})
            store.append_trade({"symbol": "B", "r_multiple": -1.0, "net_pnl": -100.0})
            self.assertEqual(len(store.read_trades()), 2)

            store.log("hello", level="INFO")
            logs = store.read_logs()
            self.assertEqual(logs[-1]["event"], "hello")

            live = store.live_metrics()
            self.assertEqual(live["n_trades"], 2)
            self.assertAlmostEqual(live["win_rate"], 0.5)


if __name__ == "__main__":
    unittest.main(verbosity=2)
