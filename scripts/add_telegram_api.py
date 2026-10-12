"""Expose the command channel through the API and add a `talk` CLI command."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# ------------------------------------------------------------ 1. server routes
server = ROOT / "ts" / "web" / "server.py"
text = server.read_text(encoding="utf-8")

if "/api/telegram" in text:
    print("[skip] server.py: telegram routes already present")
    sys.exit(0)

old = '''        if path == "/api/forward/reset" and method == "POST":
            return self._forward_reset()'''
new = '''        if path == "/api/forward/reset" and method == "POST":
            return self._forward_reset()
        if path == "/api/telegram/config" and method == "GET":
            return 200, self._telegram_status()
        if path == "/api/telegram/test" and method == "POST":
            return self._telegram_test()
        if path == "/api/telegram/send" and method == "POST":
            return self._telegram_send(body)
        if path == "/api/telegram/command" and method == "POST":
            return self._telegram_command(body)'''
if old not in text:
    print("[FAIL] forward/reset anchor not found")
    sys.exit(1)
text = text.replace(old, new, 1)

anchor = "    # ---------------------------------------------------------------- forward"
handlers = '''    # -------------------------------------------------------------- telegram

    def _telegram(self):
        """The notifier + command channel, built lazily from the settings."""
        if getattr(self, "_telegram_stack", None) is None:
            from ..commands import CommandChannel, CommandContext
            from ..notify import Notifier, NotifyConfig
            from ..settings import SettingsStore

            store = SettingsStore(self.ctx.state.root)
            settings = store.load()
            credential = settings.broker.resolve()
            notifier = Notifier(NotifyConfig(
                telegram_token=__import__("os").getenv("TELEGRAM_BOT_TOKEN", "")
                or getattr(credential, "telegram_token", ""),
                telegram_chat_id=__import__("os").getenv("TELEGRAM_CHAT_ID", ""),
            ))
            context = CommandContext(
                get_status=self._status,
                get_positions=self._telegram_positions,
                get_risk=self._telegram_risk,
                get_forward=self._forward,
                set_risk=self._telegram_set_risk,
                kill=self._telegram_kill,
                resume=self._telegram_resume,
                pause=self._telegram_pause,
                start=self._telegram_start,
                run_cli=self._telegram_run_cli,
            )
            self._telegram_stack = (notifier, CommandChannel(
                notifier, context, allowed_chat_id=__import__("os").getenv(
                    "TELEGRAM_CHAT_ID")))
        return self._telegram_stack

    def _telegram_notifier(self):
        return self._telegram()[0]

    def _telegram_channel(self):
        return self._telegram()[1]

    def _telegram_status(self) -> dict:
        notifier = self._telegram_notifier()
        return {"notifier": notifier.stats(),
                "commands_handled": self._telegram_channel().handled,
                "commands_rejected": self._telegram_channel().rejected,
                "history": list(reversed(self._telegram_channel().history[-15:]))}

    def _telegram_test(self) -> tuple[int, dict]:
        from ..notify import NotifyConfig

        notifier = self._telegram_notifier()
        if not notifier.cfg.any_enabled:
            return 400, {"ok": False, "error":
                         "set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID first"}
        ok, message = notifier.test()
        return (200 if ok else 502), {"ok": ok, "message": message}

    def _telegram_send(self, body: dict) -> tuple[int, dict]:
        title = str((body or {}).get("title", "")).strip()
        if not title:
            return 400, {"error": "title is required"}
        from ..notify import Severity

        try:
            severity = Severity(str((body or {}).get("severity", "info")).lower())
        except ValueError:
            severity = Severity.INFO
        delivered = self._telegram_notifier().send(
            title, str((body or {}).get("body", "")), severity,
            (body or {}).get("fields") or {}, kind="manual")
        return (200 if delivered else 502), {
            "ok": delivered,
            "note": "delivered" if delivered else
                    "not delivered - check the token and chat id, or the rate limit"}

    def _telegram_command(self, body: dict) -> tuple[int, dict]:
        """Handle a command exactly as if it arrived from Telegram."""
        text_ = str((body or {}).get("text", "")).strip()
        if not text_:
            return 400, {"error": "text is required"}
        import os

        channel = self._telegram_channel()
        reply = channel.handle(str((body or {}).get("chat_id",
                                                    os.getenv("TELEGRAM_CHAT_ID", ""))),
                               text_)
        return 200, {"ok": True, "reply": reply,
                     "handled": channel.handled, "rejected": channel.rejected}

    # --- command handlers the channel calls into
    def _telegram_positions(self):
        return self._positions()

    def _telegram_risk(self) -> dict:
        from ..settings import SettingsStore

        settings = SettingsStore(self.ctx.state.root).load()
        risk = settings.risk
        out = {k: getattr(risk, k) for k in
               ("risk_per_trade", "max_total_risk", "max_positions",
                "daily_loss_limit", "max_drawdown_halt", "max_fraction",
                "kelly_fraction", "slippage_bps", "bankroll", "capital")}
        prop_path = self.ctx.state.root / "propfirm.json"
        if prop_path.exists():
            import json

            try:
                out["propfirm"] = json.loads(prop_path.read_text(
                    encoding="utf-8")).get("firm")
            except (OSError, json.JSONDecodeError):
                pass
        out["kelly_fraction"] = settings.strategy.kelly_fraction
        out["daily"] = risk.daily_loss_limit
        out["dd"] = risk.max_drawdown_halt
        out["risk"] = risk.risk_per_trade
        out["max_risk"] = risk.max_total_risk
        out["expectancy"] = settings.strategy.min_expectancy
        out["confidence"] = settings.strategy.min_confidence
        return out

    def _telegram_set_risk(self, key: str, value: float) -> bool:
        from ..settings import SettingsStore

        store = SettingsStore(self.ctx.state.root)
        settings = store.load()
        strategy_keys = {"kelly": "kelly_fraction", "expectancy": "min_expectancy",
                         "confidence": "min_confidence"}
        if key in strategy_keys:
            return bool(store.update("strategy",
                                     {strategy_keys[key]: float(value)}))
        risk_field = {"risk": "risk_per_trade", "max_risk": "max_total_risk",
                      "daily": "daily_loss_limit",
                      "dd": "max_drawdown_halt"}.get(key)
        if risk_field is None:
            return False
        return bool(store.update("risk", {risk_field: float(value)}))

    def _telegram_kill(self, reason: str = "telegram command") -> bool:
        self.ctx.set_killed(True, reason)
        engine = self.ctx.engine
        if engine is not None:
            engine.stop()
        return True

    def _telegram_resume(self) -> bool:
        self.ctx.set_killed(False)
        return True

    def _telegram_pause(self) -> bool:
        engine = self.ctx.engine
        if engine is not None and hasattr(engine, "set_paused"):
            engine.set_paused(True)
        return True

    def _telegram_start(self) -> bool:
        engine = self.ctx.engine
        if engine is not None and hasattr(engine, "set_paused"):
            engine.set_paused(False)
        return True

    def _telegram_run_cli(self, command: str, args: dict | None) -> dict:
        if self.ctx.engine_starter is None:
            return {"error": "command execution is not configured on this server"}
        try:
            code, run = self._console().start(command, args or {})
        except Exception as exc:  # noqa: BLE001
            return {"error": str(exc)}
        return {"ok": code == 202, "started": code == 202, "run": run.to_dict()}

    # ---------------------------------------------------------------- forward'''
if anchor not in text:
    print("[FAIL] forward handlers anchor not found")
    sys.exit(1)
text = text.replace(anchor, handlers, 1)
server.write_text(text, encoding="utf-8")
print("[ok] server.py: telegram routes + handlers added")

sys.path.insert(0, str(ROOT))
try:
    from ts.web.server import DashboardServer  # noqa: F401
    print("[ok] server.py imports cleanly")
except Exception as exc:  # noqa: BLE001
    print(f"[FAIL] import error: {exc}")
sys.exit(0)
