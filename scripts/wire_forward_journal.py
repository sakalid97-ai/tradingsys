"""Auto-record engine trades into the forward journal.

Every closed trade is journalled automatically, so the Forward tab populates
without pressing Import. Backtest trades are deliberately NOT forwarded -- a
backtest is a prediction, not evidence.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def patch(path: Path, subs, label: str) -> bool:
    text = path.read_text(encoding="utf-8")
    applied = skipped = failed = 0
    for old, new in subs:
        if new and new in text:
            skipped += 1
            continue
        if old not in text:
            print(f"    [FAIL] {path.name}: anchor not found for {old[:52]!r}")
            failed += 1
            continue
        text = text.replace(old, new, 1)
        applied += 1
    if not failed:
        path.write_text(text, encoding="utf-8")
    print(f"[{'ok' if not failed else 'FAIL'}] {label}: "
          f"{applied} applied, {skipped} already present, {failed} failed")
    return not failed


ok = True

# ------------------------------------------------------------- 1. the engine
ok &= patch(ROOT / "ts" / "engine.py", [
    # the engine accepts an optional journal
    (
        """        kill_switch=None,
    ) -> None:""",
        """        kill_switch=None,
        forward_journal=None,
    ) -> None:""",
    ),
    (
        """        self.kill_switch = kill_switch        # callable() -> bool""",
        """        self.kill_switch = kill_switch        # callable() -> bool
        # When supplied, every closed trade is journalled here automatically.
        # Backtests never reach it -- a backtest is a prediction, not evidence.
        self.forward_journal = forward_journal""",
    ),
    # record on close
    (
        """        self.trades.append(trade)
        if self.state:
            payload = trade.to_dict()
            payload["exit_time"] = str(payload["exit_time"])
            payload["entry_time"] = str(payload["entry_time"])
            self.state.append_trade(payload)""",
        """        self.trades.append(trade)
        payload = trade.to_dict()
        payload["exit_time"] = str(payload["exit_time"])
        payload["entry_time"] = str(payload["entry_time"])
        if self.state:
            self.state.append_trade(payload)
        if self.forward_journal is not None:
            try:
                self.forward_journal.record(
                    ForwardTrade.from_engine_trade(
                        trade, "trend_breakout", getattr(self.broker, "mode", "paper")))
            except (TypeError, ValueError, AttributeError) as exc:
                log.warning("could not journal trade %s: %s", trade.symbol, exc)""",
    ),
    # import
    (
        "from .trades import Trade",
        "from .forward import ForwardTrade\nfrom .trades import Trade",
    ),
], "engine.py forwards closed trades")

# ------------------------------------------------------- 2. the agent supervisor
ok &= patch(ROOT / "ts" / "agents" / "supervisor.py", [
    (
        """                kill_switch=(lambda reason: state.set_killed(True, reason))
                if config.get("kill_on_risk") else None,
                state_store=state,
            )""",
        """                kill_switch=(lambda reason: state.set_killed(True, reason))
                if config.get("kill_on_risk") else None,
                state_store=state,
            )
            # every agent trade is journalled as forward evidence
            from ..forward import ForwardJournal as _FJ
            self._forward_journal = _FJ(state.root)
            self.execution.forward_journal = self._forward_journal""",
    ),
], "agents supervisor forwards trades")

# ------------------------------------------------------- 3. the execution agent
ok &= patch(ROOT / "ts" / "agents" / "execution.py", [
    (
        """        self.closed: list[dict] = []
        self.subscribe_all()""",
        """        self.closed: list[dict] = []
        # optional: auto-journal every closed trade for the Forward tab
        self.forward_journal = None
        self.subscribe_all()""",
    ),
    (
        """        self.closed.append(record)
        self.managed.pop(trade.symbol, None)""",
        """        self.closed.append(record)
        self._journal(record)
        self.managed.pop(trade.symbol, None)""",
    ),
    (
        """    # ---------------------------------------------------------------- reporting""",
        """    def _journal(self, record: dict) -> None:
        \"\"\"Write one closed trade to the forward journal, if one is attached.\"\"\"
        if self.forward_journal is None:
            return
        try:
            from ..forward import ForwardTrade

            self.forward_journal.record(ForwardTrade(
                symbol=record["symbol"],
                side=record["side"],
                quantity=float(record["quantity"]),
                entry_price=float(record["entry_price"]),
                exit_price=float(record["exit_price"]),
                r_multiple=float(record["r_multiple"]),
                net_pnl=float(record["pnl"]),
                entry_time=str(record.get("opened_at", "")),
                exit_time=str(record.get("closed_at", "")),
                exit_reason=record.get("exit_reason", ""),
                strategy="multi_agent",
                broker="paper" if self.dry_run else "live",
            ))
        except (KeyError, TypeError, ValueError) as exc:
            log.debug("could not journal agent trade: %s", exc)

    # ---------------------------------------------------------------- reporting""",
    ),
], "execution agent forwards trades")

# ------------------------------------------------- 4. the dashboard passes it in
ok &= patch(ROOT / "ts" / "web" / "context.py", [
    (
        """                engine = TradingEngine(
                    broker=broker,
                    data_fetcher=fetcher,""",
        """                from ts.forward import ForwardJournal

                engine = TradingEngine(
                    broker=broker,
                    data_fetcher=fetcher,""",
    ),
    (
        """                    kill_switch=lambda: (state.root / "KILL").exists(),
                )""",
        """                    kill_switch=lambda: (state.root / "KILL").exists(),
                    forward_journal=ForwardJournal(state.root),
                )""",
    ),
], "context passes a journal to the engine")

sys.path.insert(0, str(ROOT))
try:
    from ts.engine import TradingEngine
    from ts.agents.supervisor import MultiAgentSupervisor
    from ts.web.context import build_context
    print("[ok] all modules import cleanly")
except Exception as exc:  # noqa: BLE001
    print(f"[FAIL] import error: {exc}")
sys.exit(0 if ok else 1)
