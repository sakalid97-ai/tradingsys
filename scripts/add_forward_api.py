"""Expose the forward journal through the API and add its dashboard view."""

from __future__ import annotations

from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]

# ------------------------------------------------------------------ server API
server = ROOT / "ts" / "web" / "server.py"
text = server.read_text(encoding="utf-8")

if "/api/forward" in text:
    print("[skip] server.py: forward API already present")
    sys.exit(0)

old = '        if path == "/api/propfirm/status":\n            return 200, self._propfirm_status()'
new = '''        if path == "/api/propfirm/status":
            return 200, self._propfirm_status()
        if path == "/api/forward" and method == "GET":
            return 200, self._forward_summary()
        if path == "/api/forward/baseline" and method == "POST":
            return self._forward_baseline(body)
        if path == "/api/forward/reset" and method == "POST":
            return self._forward_reset()
        if path == "/api/forward/import" and method == "POST":
            return self._forward_import()'''
if old not in text:
    print("[FAIL] propfirm/status route anchor not found")
    sys.exit(1)
text = text.replace(old, new, 1)

anchor = "    # ---------------------------------------------------------------- console"
handlers = '''    # ---------------------------------------------------------------- forward

    def _forward(self):
        from ..forward import ForwardJournal

        if getattr(self, "_forward_journal", None) is None:
            self._forward_journal = ForwardJournal(self.ctx.state.root)
        return self._forward_journal

    def _forward_summary(self) -> dict:
        return self._forward().summary()

    def _forward_baseline(self, body: dict) -> tuple[int, dict]:
        """Set the prediction the realised results are measured against."""
        from ..forward import Baseline

        journal = self._forward()
        if (body or {}).get("from_backtest"):
            baseline = journal.capture_baseline_from_state(self.ctx.state)
            if baseline is None:
                return 404, {"error": "no backtest in state to use as a baseline; "
                                      "run a backtest first"}
        else:
            allowed = ("win_rate", "expectancy_r", "profit_factor", "avg_win_r",
                       "avg_loss_r", "n_trades", "source")
            values = {k: v for k, v in (body or {}).items() if k in allowed}
            if not values:
                return 400, {"error": "provide from_backtest=true or baseline values"}
            baseline = Baseline(**values, source=str(values.get("source", "manual")))
            journal.set_baseline(baseline)
        if self.ctx.state:
            self.ctx.state.log("forward_baseline_set", expectancy_r=baseline.expectancy_r,
                               win_rate=baseline.win_rate)
        return 200, {"ok": True, "baseline": baseline.to_dict()}

    def _forward_reset(self) -> tuple[int, dict]:
        self._forward().reset()
        if self.ctx.state:
            self.ctx.state.log("forward_journal_reset")
        return 200, {"ok": True, "note": "forward journal cleared"}

    def _forward_import(self) -> tuple[int, dict]:
        """Pull the trades already recorded by the engine into the journal."""
        from ..forward import ForwardTrade

        trades = self.ctx.state.read_trades(limit=100_000)
        journal = self._forward()
        imported = 0
        for payload in trades:
            try:
                engine_trade = _trade_from_payload(payload)
                journal.record(ForwardTrade.from_engine_trade(
                    engine_trade, "trend_breakout",
                    str((self.ctx.state.read_snapshot() or {}).get("broker", "paper"))))
                imported += 1
            except (TypeError, KeyError, ValueError, AttributeError):
                continue
        if imported and journal.load_baseline() is None:
            journal.capture_baseline_from_state(self.ctx.state)
        return 200, {"ok": True, "imported": imported, "total": len(journal.load())}

    # ---------------------------------------------------------------- console'''
if anchor not in text:
    print("[FAIL] console handlers anchor not found")
    sys.exit(1)
text = text.replace(anchor, handlers, 1)

# helper to rebuild an engine Trade from a stored dict
text = text.replace(
    "def _catalogue():",
    '''def _trade_from_payload(payload: dict):
    """Rebuild a ts.trades.Trade from the shape the state store records."""
    from ..trades import Trade

    return Trade(
        symbol=str(payload.get("symbol", "")),
        side=str(payload.get("side", "long")),
        quantity=float(payload.get("quantity", 0) or 0),
        entry_time=pd.Timestamp(str(payload.get("entry_time", "")) or pd.Timestamp.utcnow()),
        entry_price=float(payload.get("entry_price", 0) or 0),
        exit_time=pd.Timestamp(str(payload.get("exit_time", "")) or pd.Timestamp.utcnow()),
        exit_price=float(payload.get("exit_price", 0) or 0),
        stop_price=float(payload.get("stop_price", 0) or 0),
        initial_risk=float(payload.get("initial_risk", 0) or 0),
        fees=float(payload.get("fees", 0) or 0),
        exit_reason=str(payload.get("exit_reason", "")),
        bars_held=int(payload.get("bars_held", 0) or 0),
        mae=float(payload.get("mae", 0) or 0),
        mfe=float(payload.get("mfe", 0) or 0),
    )


def _catalogue():''',
    1,
)
text = text.replace(
    "from ..settings import Settings\nfrom ..settings import SettingsStore",
    "import pandas as pd\nfrom ..settings import Settings\nfrom ..settings import SettingsStore",
    1,
)
server.write_text(text, encoding="utf-8")
print("[ok] server.py: forward API added")

sys.path.insert(0, str(ROOT))
try:
    from ts.web.server import DashboardServer  # noqa: F401
    print("[ok] server.py imports cleanly")
except Exception as exc:  # noqa: BLE001
    print(f"[FAIL] import error: {exc}")
sys.exit(0)
