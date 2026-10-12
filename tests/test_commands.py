"""Tests for the two-way Telegram command channel."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ts.commands import (  # noqa: E402
    CommandChannel,
    CommandContext,
    CommandError,
    TelegramPoller,
    parse,
)
from ts.notify import Notifier, NotifyConfig  # noqa: E402


class DummyContext(CommandContext):
    def __init__(self, **kwargs):
        self.calls: list = []
        super().__init__(**kwargs)

    def _wrap(self, name, value=None):
        def handler(*args, **kw):
            self.calls.append((name, args))
            return value() if callable(value) else value

        return handler

    def with_defaults(self) -> "DummyContext":
        ctx = DummyContext(
            get_status=self._wrap("get_status", lambda: {
                "mode": "AGENTS-DRYRUN", "engine_running": True, "equity": 100_000.0,
                "day_pnl": 250.0, "n_positions": 2, "killed": False,
                "uptime_seconds": 900, "drawdown": 0.031}),
            get_positions=self._wrap("get_positions", lambda: [
                {"symbol": "BTCUSD", "side": "long", "quantity": 4.0,
                 "entry_price": 83000.0, "r_open": 1.24, "unrealized_pnl": 248.0}]),
            get_risk=self._wrap("get_risk", lambda: {
                "risk": 0.01, "kelly": 0.25, "daily": 0.03, "dd": 0.15,
                "max_risk": 0.06}),
            get_forward=self._wrap("get_forward", lambda: {
                "n_trades": 60, "win_rate": 0.45, "expectancy_r": 0.54,
                "total_pnl": 5400.0, "max_drawdown": 900.0,
                "verdict": "TRACKING: within noise"}),
            set_risk=self._wrap("set_risk", lambda *a: True),
            kill=self._wrap("kill", lambda *a: True),
            resume=self._wrap("resume", lambda: True),
            pause=self._wrap("pause", lambda: True),
            start=self._wrap("start", lambda: True),
        )
        self.calls = ctx.calls
        return ctx


class TestParser(unittest.TestCase):
    def setUp(self):
        self.ctx = DummyContext().with_defaults()

    def parse(self, text):
        return parse(text, self.ctx)

    def test_status(self):
        self.assertEqual(self.parse("/status"), ("status", {}))

    def test_help(self):
        self.assertEqual(self.parse("help")[0], "help")
        self.assertEqual(self.parse("/?")[0], "help")

    def test_positions_alias(self):
        self.assertEqual(self.parse("/pos")[0], "positions")
        self.assertEqual(self.parse("/trades")[0], "positions")

    def test_kill_with_reason(self):
        command, args = self.parse("/kill drawdown breached")
        self.assertEqual(command, "kill")
        self.assertIn("drawdown breached", args["reason"])

    def test_set_parses_number(self):
        command, args = self.parse("/set risk 0.005")
        self.assertEqual(command, "set")
        self.assertEqual(args["key"], "risk")
        self.assertAlmostEqual(args["value"], 0.005)

    def test_set_rejects_non_number(self):
        with self.assertRaises(CommandError):
            self.parse("/set risk abc")

    def test_set_rejects_unknown_key(self):
        with self.assertRaises(CommandError):
            self.parse("/set leverage 5")

    def test_set_rejects_out_of_bounds(self):
        for bad in ("0.5", "0", "99"):
            with self.assertRaises(CommandError):
                self.parse(f"/set risk {bad}")

    def test_run_command(self):
        command, args = self.parse("/run backtest --range 2y")
        self.assertEqual(command, "run")
        self.assertEqual(args["command"], "backtest")
        self.assertEqual(args["args"]["range"], "2y")

    def test_botname_suffix_is_stripped(self):
        command, _ = self.parse("/status@TradingSystemBot")
        self.assertEqual(command, "status")

    def test_empty_message_raises(self):
        with self.assertRaises(CommandError):
            self.parse("   ")

    def test_unknown_command_raises(self):
        with self.assertRaises(CommandError):
            self.parse("/buy BTCUSD now")

    def test_cancel_reply_is_safe(self):
        self.assertEqual(self.parse("no")[0], "confirm")


class TestChannel(unittest.TestCase):
    def channel(self, chat_id="12345", allowed=None):
        self.ctx = DummyContext().with_defaults()
        return CommandChannel(Notifier(NotifyConfig()), self.ctx,
                              allowed_chat_id=allowed or chat_id)

    def test_status_returns_readable_text(self):
        reply = self.channel().handle("12345", "/status")
        self.assertIn("System status", reply)
        self.assertIn("100,000", reply)
        self.assertIn("Forward", reply)

    def test_positions_lists_them(self):
        reply = self.channel().handle("12345", "/positions")
        self.assertIn("BTCUSD", reply)
        self.assertIn("1.24", reply)

    def test_no_positions_message(self):
        ctx = DummyContext(get_positions=lambda: [])
        reply = CommandChannel(Notifier(NotifyConfig()), ctx).handle("1", "/positions")
        self.assertEqual(reply, "No open positions.")

    def test_kill_calls_the_handler(self):
        channel = self.channel()
        reply = channel.handle("12345", "/kill manual test")
        self.assertTrue(any(call[0] == "kill" for call in self.ctx.calls))
        self.assertIn("Kill switch engaged", reply)

    def test_resume(self):
        reply = self.channel().handle("12345", "/resume")
        self.assertTrue(any(call[0] == "resume" for call in self.ctx.calls))

    def test_pause_and_start(self):
        reply = self.channel().handle("12345", "/pause")
        self.assertIn("Paused", reply)
        reply = self.channel().handle("12345", "/start")
        self.assertIn("Resumed", reply)

    def test_forward_reports_the_verdict(self):
        reply = self.channel().handle("12345", "/forward")
        self.assertIn("TRACKING", reply)
        self.assertIn("trades: 60", reply)

    def test_forward_with_no_data(self):
        ctx = DummyContext(get_forward=lambda: {"n_trades": 0})
        reply = CommandChannel(Notifier(NotifyConfig()), ctx).handle("1", "/forward")
        self.assertIn("No forward trades", reply)

    def test_unknown_command_returns_error_not_exception(self):
        reply = self.channel().handle("12345", "/moon the account")
        self.assertIn("unknown command", reply)
        self.assertNotIn("Traceback", reply)

    def test_unauthorised_chat_is_ignored(self):
        channel = self.channel(allowed="99999")
        reply = channel.handle("12345", "/kill")
        self.assertIn("not authorised", reply)
        self.assertFalse(self.ctx.calls, "an unauthorised command must not run")

    def test_raising_a_limit_asks_for_confirmation(self):
        channel = self.channel()
        reply = channel.handle("12345", "/set risk 0.05")   # up from 0.01
        self.assertIn("confirm", reply.lower())
        # nothing changed yet
        self.assertFalse(any(c[0] == "set_risk" for c in self.ctx.calls))

    def test_confirm_applies_the_raise(self):
        channel = self.channel()
        channel.handle("12345", "/set risk 0.05")
        reply = channel.handle("12345", "yes")
        self.assertIn("raised", reply)
        self.assertTrue(any(c[0] == "set_risk" for c in self.ctx.calls))

    def test_lowering_a_limit_needs_no_confirmation(self):
        channel = self.channel()
        reply = channel.handle("12345", "/set risk 0.005")   # down from 0.01
        self.assertIn("set to", reply)
        self.assertTrue(any(c[0] == "set_risk" for c in self.ctx.calls))

    def test_cancel_leaves_the_limit_alone(self):
        channel = self.channel()
        channel.handle("12345", "/set risk 0.05")
        before = len(self.ctx.calls)
        channel.handle("12345", "no")
        self.assertEqual(len(self.ctx.calls), before)


if __name__ == "__main__":
    unittest.main(verbosity=2)
