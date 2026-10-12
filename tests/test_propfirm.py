"""Tests for prop-firm / funded-account rules.

Breach detection is safety-critical: a false negative means a terminated
account, so the monitor's limits must be strictly inside the firm's.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ts.propfirm import (  # noqa: E402
    PropFirmMonitor,
    PropFirmRules,
    apply_to_risk_settings,
    registry,
    rules_for,
)


class TestRegistry(unittest.TestCase):
    def test_registry_is_non_empty_and_well_formed(self):
        firms = registry()
        self.assertGreater(len(firms), 4)
        for firm in firms:
            self.assertTrue(firm["firm"], "every firm needs an id")
            self.assertGreater(firm["max_drawdown_pct"], 0)
            self.assertGreater(firm["max_daily_loss_pct"], 0)
            self.assertIn(firm["dd_reference"], ("initial", "high_water"))

    def test_rules_for_returns_a_copy(self):
        rules = rules_for("ftmo")
        self.assertIsNotNone(rules)
        self.assertEqual(rules.firm, "ftmo")
        self.assertEqual(rules.dd_reference, "initial")

    def test_rules_for_unknown_returns_none(self):
        self.assertIsNone(rules_for("not-a-real-firm"))

    def test_generic_conservative_is_preloaded(self):
        rules = rules_for("generic_conservative")
        self.assertEqual(rules.max_drawdown_pct, 0.06)
        self.assertEqual(rules.max_daily_loss_pct, 0.03)


class TestSafetyBuffer(unittest.TestCase):
    """The system must always stop BEFORE the firm terminates the account."""

    def test_daily_stop_is_inside_firm_limit(self):
        monitor = PropFirmMonitor(rules_for("ftmo"))
        self.assertLess(monitor.daily_stop_pct, monitor.rules.max_daily_loss_pct)
        self.assertAlmostEqual(monitor.daily_stop_pct,
                               monitor.rules.max_daily_loss_pct * 0.75, places=6)

    def test_drawdown_stop_is_inside_firm_limit(self):
        monitor = PropFirmMonitor(rules_for("ftmo"))
        self.assertLess(monitor.drawdown_stop_pct, monitor.rules.max_drawdown_pct)

    def test_buffer_is_applicable_and_bounded(self):
        tight = PropFirmMonitor(rules_for("ftmo"), safety_buffer=0.0)
        loose = PropFirmMonitor(rules_for("ftmo"), safety_buffer=0.4)
        self.assertAlmostEqual(tight.daily_stop_pct, 0.05, places=6)
        self.assertLess(loose.daily_stop_pct, tight.daily_stop_pct)
        with self.assertRaises(ValueError):
            PropFirmMonitor(rules_for("ftmo"), safety_buffer=1.5)

    def test_higher_buffer_always_tighter(self):
        stops = []
        for buffer in (0.0, 0.1, 0.25, 0.4):
            monitor = PropFirmMonitor(rules_for("the5ers"), safety_buffer=buffer)
            stops.append(monitor.drawdown_stop_pct)
        self.assertEqual(stops, sorted(stops, reverse=True))


class TestBreachDetection(unittest.TestCase):
    def test_daily_loss_breach_is_detected(self):
        monitor = PropFirmMonitor(rules_for("ftmo"), starting_balance=100_000.0)
        monitor.open_day()
        monitor.update(100_000.0 * (1 - 0.04))          # -4% on the day
        breaches = monitor.check()
        # firm allows 5%, system stops at 3.75% -> caught at 4%
        self.assertEqual(len(breaches), 1)
        self.assertIn("DAILY LOSS", breaches[0])

    def test_small_loss_is_not_a_breach(self):
        monitor = PropFirmMonitor(rules_for("ftmo"), starting_balance=100_000.0)
        monitor.open_day()
        monitor.update(99_000.0)                         # -1%: normal
        self.assertEqual(monitor.check(), [])

    def test_drawdown_breach_from_initial_balance(self):
        """Accumulate slowly across days so the daily limit never trips.

        A single -7% day would hit the daily stop first (correctly), so a real
        drawdown breach is reached by grinding out smaller losses.
        """
        monitor = PropFirmMonitor(rules_for("ftmo"), starting_balance=100_000.0)
        monitor.open_day()
        monitor.update(98_000.0)                          # -2% today: inside the daily stop
        self.assertEqual(monitor.check(), [])
        for _ in range(3):
            monitor.open_day(monitor.state.equity)
            monitor.update(monitor.state.equity * 0.98)   # another -2% each day
        breaches = monitor.check()
        self.assertEqual(len(breaches), 1)
        self.assertIn("DRAWDOWN", breaches[0])
        # -7.6% cumulative against a 7.5% drawdown stop
        self.assertGreater(monitor.state.drawdown_pct, 0.075)

    def test_high_water_drawdown_worsens_as_profit_grows(self):
        rules = rules_for("the5ers")
        self.assertEqual(rules.dd_reference, "high_water")
        monitor = PropFirmMonitor(rules, starting_balance=100_000.0)
        monitor.open_day()
        # +6% stays under The5ers' 8% evaluation target
        monitor.update(106_000.0)
        self.assertEqual(monitor.check(), [])
        # now fall from 106k, not from 100k: -3.8% off the high crosses the
        # 4.5% drawdown stop (6% firm limit less the 25% buffer)
        monitor.update(101_000.0)                    # -4.72% off the high
        breaches = monitor.check()
        self.assertTrue(any("DRAWDOWN" in b for b in breaches))

    def test_profit_target_is_reported_as_a_breach(self):
        rules = rules_for("ftmo")
        self.assertEqual(rules.profit_target_pct, 0.10)
        monitor = PropFirmMonitor(rules, starting_balance=100_000.0)
        monitor.open_day()
        monitor.update(112_000.0)                          # +12%: past target
        breaches = monitor.check()
        self.assertTrue(any("PROFIT TARGET" in b for b in breaches))

    def test_breach_halts_the_monitor(self):
        monitor = PropFirmMonitor(rules_for("ftmo"), starting_balance=100_000.0)
        monitor.open_day()
        monitor.update(94_000.0)
        ok, reason = monitor.approve_trade("EURUSD", risk_pct=0.005, side="long")
        self.assertFalse(ok, "a halted account must refuse every trade")
        self.assertTrue(monitor.state.halted)
        self.assertTrue(reason)

    def test_new_day_resets_the_daily_breach(self):
        monitor = PropFirmMonitor(rules_for("ftmo"), starting_balance=100_000.0)
        monitor.open_day()
        monitor.update(96_000.0)                           # -4%: breach
        self.assertTrue(monitor.check())
        monitor.open_day(96_000.0)
        self.assertEqual(monitor.check(), [])
        self.assertEqual(monitor.state.trading_days, 2)


class TestTradeApproval(unittest.TestCase):
    def monitor(self, firm="ftmo", **kwargs):
        return PropFirmMonitor(rules_for(firm), starting_balance=100_000.0, **kwargs)

    def test_healthy_account_approves_a_normal_trade(self):
        ok, reason = self.monitor().approve_trade("EURUSD", 0.005, "long")
        self.assertTrue(ok)
        self.assertEqual(reason, "approved")

    def test_per_trade_risk_cap_is_enforced(self):
        ok, reason = self.monitor().approve_trade("EURUSD", risk_pct=0.05, side="long")
        self.assertFalse(ok)
        self.assertIn("per-trade risk", reason)

    def test_prohibited_instrument_is_blocked(self):
        rules = rules_for("ftmo")
        rules.prohibited_instruments = ["XAUUSD"]
        monitor = PropFirmMonitor(rules, starting_balance=100_000.0)
        ok, reason = monitor.approve_trade("XAUUSD", 0.005, "long")
        self.assertFalse(ok)
        self.assertIn("prohibited", reason)
        # an allowed symbol still works
        ok2, _ = monitor.approve_trade("EURUSD", 0.005, "long")
        self.assertTrue(ok2)

    def test_allowed_instrument_list_is_whitelist(self):
        rules = rules_for("ftmo")
        rules.allowed_instruments = ["EURUSD", "GBPUSD"]
        monitor = PropFirmMonitor(rules, starting_balance=100_000.0)
        ok, reason = monitor.approve_trade("BTCUSD", 0.005, "long")
        self.assertFalse(ok)
        self.assertIn("permitted", reason)

    def test_ea_not_allowed_blocks_everything(self):
        rules = rules_for("ftmo")
        rules.ea_allowed = False
        monitor = PropFirmMonitor(rules, starting_balance=100_000.0)
        ok, reason = monitor.approve_trade("EURUSD", 0.005, "long")
        self.assertFalse(ok)
        self.assertIn("expert advisors", reason)


class TestRiskSettingsIntegration(unittest.TestCase):
    def test_apply_to_risk_settings_is_stricter_than_the_firm(self):
        rules = rules_for("ftmo")                     # 5% daily, 10% dd
        settings = apply_to_risk_settings(rules)
        self.assertLess(settings["daily_loss_limit"], rules.max_daily_loss_pct)
        self.assertLess(settings["max_drawdown_halt"], rules.max_drawdown_pct)
        self.assertLessEqual(settings["risk_per_trade"], rules.max_risk_per_trade)

    def test_total_risk_cannot_stack_into_a_daily_breach(self):
        for firm in ("ftmo", "the5ers", "myforexfunds"):
            settings = apply_to_risk_settings(rules_for(firm))
            self.assertLessEqual(settings["max_total_risk"],
                                 settings["daily_loss_limit"],
                                 f"{firm}: stacked risk could breach the daily stop")

    def test_total_risk_never_below_one_trade(self):
        settings = apply_to_risk_settings(rules_for("generic_conservative"))
        self.assertGreaterEqual(settings["max_total_risk"], settings["risk_per_trade"])


class TestProgress(unittest.TestCase):
    def test_progress_tracks_toward_target(self):
        monitor = PropFirmMonitor(rules_for("ftmo"), starting_balance=100_000.0)
        monitor.open_day()
        monitor.update(105_000.0)                     # +5% of a 10% target
        progress = monitor.progress()
        self.assertEqual(progress["status"], "evaluation")
        self.assertAlmostEqual(progress["target_progress_pct"], 50.0, places=1)

    def test_target_reached_reports_funded(self):
        monitor = PropFirmMonitor(rules_for("ftmo"), starting_balance=100_000.0)
        monitor.open_day()
        monitor.update(111_000.0)
        self.assertEqual(monitor.progress()["status"], "funded")

    def test_minimum_trading_days_are_counted(self):
        monitor = PropFirmMonitor(rules_for("ftmo"), starting_balance=100_000.0)
        for _ in range(5):
            monitor.open_day()
        progress = monitor.progress()
        self.assertEqual(progress["trading_days"], 5)
        self.assertEqual(progress["days_needed_for_minimum"], 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
