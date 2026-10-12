"""Add the console API (run any command) and prop-firm endpoints."""

from __future__ import annotations

from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
server = ROOT / "ts" / "web" / "server.py"
text = server.read_text(encoding="utf-8")

if "/api/command/run" in text:
    print("[skip] server.py: console API already present")
    sys.exit(0)

# ---- routes
old = '        if path == "/api/instruments":\n            return 200, self._instruments()'
new = '''        if path == "/api/instruments":
            return 200, self._instruments()
        if path == "/api/command/list":
            return 200, {"commands": self._console().catalogue()}
        if path == "/api/command/run" and method == "POST":
            return self._command_run(body)
        if path == "/api/command/status":
            return 200, self._console().status()
        if path == "/api/command/output":
            return 200, {"lines": self._console().tail_log(
                int(query.get("lines", ["250"])[0]))}
        if path == "/api/command/stop" and method == "POST":
            return 200, self._console().stop_current()
        if path == "/api/propfirms":
            return 200, {"firms": _propfirm_registry(),
                         "active": self.ctx.settings.__dict__.get("propfirm", {})}
        if path == "/api/propfirm/select" and method == "POST":
            return self._propfirm_select(body)
        if path == "/api/propfirm/status":
            return 200, self._propfirm_status()'''
if old not in text:
    print("[FAIL] /api/instruments route anchor not found")
    sys.exit(1)
text = text.replace(old, new, 1)

# ---- handlers, inserted before the settings handlers
anchor = "    # -------------------------------------------------------------- settings"
handlers = '''    # ---------------------------------------------------------------- console

    def _console(self):
        if getattr(self, "_console_runner", None) is None:
            from .console import ConsoleRunner

            self._console_runner = ConsoleRunner(log_path=self.ctx.state.root / "console.log")
        return self._console_runner

    def _command_run(self, body: dict) -> tuple[int, dict]:
        """Run any CLI command from the dashboard."""
        command = str((body or {}).get("command", "")).strip()
        args = body.get("args") or {}
        if not command:
            return 400, {"error": "command is required"}
        from .console import COMMANDS

        if command not in COMMANDS:
            return 404, {"error": f"unknown command {command!r}",
                         "available": list(COMMANDS)}
        if command == "live":
            return 403, {
                "error": "live trading is never startable from the dashboard. It needs "
                         "explicit broker keys, ALPACA_LIVE_CONFIRMED=yes, and a "
                         "terminal -- deliberately outside the UI.",
            }
        try:
            code, run = self._console().start(command, args)
        except Exception as exc:  # noqa: BLE001
            return 500, {"error": str(exc)}
        if code == 409:
            return 409, {"error": "another command is already running",
                         "running": run.to_dict()}
        if self.ctx.state:
            self.ctx.state.log("command_started", command=command,
                              args={k: str(v) for k, v in args.items()})
        return 202, {"ok": True, "run": run.to_dict()}

    # -------------------------------------------------------------- prop firm

    def _propfirm_select(self, body: dict) -> tuple[int, dict]:
        """Enable a prop-firm profile and tighten the risk settings to match."""
        from ..propfirm import apply_to_risk_settings, rules_for
        from ..settings import SettingsStore

        firm_id = str((body or {}).get("firm", "")).strip()
        rules = rules_for(firm_id)
        if rules is None:
            return 404, {"error": f"unknown prop firm {firm_id!r}",
                         "available": [r["firm"] for r in _propfirm_registry()]}

        starting_balance = float((body or {}).get("starting_balance", 100_000.0) or 100_000.0)
        safety_buffer = float((body or {}).get("safety_buffer", 0.25) or 0.25)
        safety_buffer = min(max(safety_buffer, 0.0), 0.45)

        risk_values = apply_to_risk_settings(rules, safety_buffer)
        risk_values["bankroll"] = starting_balance
        risk_values["capital"] = starting_balance
        risk_values["kill_on_risk"] = True
        risk_values["dry_run"] = True

        store = SettingsStore(self.ctx.state.root)
        updated = store.update("risk", risk_values)
        payload = {
            "firm": rules.firm,
            "starting_balance": starting_balance,
            "safety_buffer": safety_buffer,
            "phase": rules.phase,
            "applied_at": datetime.now(UTC).isoformat(),
        }
        try:
            store.update("broker", {})
        except KeyError:
            pass
        # persist the profile itself
        path = self.ctx.state.root / "propfirm.json"
        path.write_text(str(payload).replace("'", '"'), encoding="utf-8")
        self.ctx.settings = updated
        self.ctx.settings.__dict__["propfirm"] = payload

        if self.ctx.state:
            self.ctx.state.log("propfirm_selected", firm=rules.firm,
                              safety_buffer=safety_buffer,
                              daily_stop=risk_values["daily_loss_limit"],
                              drawdown_stop=risk_values["max_drawdown_halt"])
        return 200, {
            "ok": True,
            "firm": rules.to_dict(),
            "risk_applied": risk_values,
            "limits": _propfirm_limits(rules, safety_buffer),
            "note": ("Risk limits were tightened to sit inside the firm's own rules, "
                     f"leaving a {(1 - safety_buffer) * 100:.0f}% safety buffer for gaps "
                     "and slippage."),
        }

    def _propfirm_status(self) -> dict:
        import json

        path = self.ctx.state.root / "propfirm.json"
        if not path.exists():
            return {"active": None, "firms": _propfirm_registry()}
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {"active": None}
        from ..propfirm import rules_for

        rules = rules_for(payload.get("firm", ""))
        return {
            "active": payload,
            "rules": rules.to_dict() if rules else None,
            "risk": self.ctx.settings.risk.__dict__,
        }

    # -------------------------------------------------------------- settings
'''
if anchor not in text:
    print("[FAIL] settings handlers anchor not found")
    sys.exit(1)
text = text.replace(anchor, handlers + anchor, 1)

# ---- module helpers
text = text.replace(
    "def _catalogue():\n    from ..broker import registry\n\n    return registry.catalogue()",
    "def _catalogue():\n"
    "    from ..broker import registry\n\n"
    "    return registry.catalogue()\n\n\n"
    "def _propfirm_registry():\n"
    "    from ..propfirm import registry\n\n"
    "    return registry()\n\n\n"
    "def _propfirm_limits(rules, safety_buffer):\n"
    "    from ..propfirm import PropFirmMonitor\n\n"
    "    return PropFirmMonitor(rules, safety_buffer=safety_buffer).limits()",
    1,
)
server.write_text(text, encoding="utf-8")
print("[ok]   server.py: console + prop-firm API added")

sys.path.insert(0, str(ROOT))
try:
    from ts.web.server import DashboardServer  # noqa: F401
    print("[ok]   server.py imports cleanly")
except Exception as exc:  # noqa: BLE001
    print(f"[FAIL] import error: {exc}")
sys.exit(0)
