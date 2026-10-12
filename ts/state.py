"""Persistent system state.

Everything the dashboard renders is read from here rather than queried from a
live object, so the web server never touches strategy internals and the UI keeps
working (showing last-known state) even if the trading engine is stopped.

Layout under ``state/``::

    snapshot.json     one dict, overwritten each tick
    trades.jsonl      one closed trade per line, append-only
    equity.jsonl      one equity sample per line, append-only
    logs.jsonl        one log event per line, append-only
    backtest.json     the most recent completed backtest
"""

from __future__ import annotations

import json
import threading
from dataclasses import asdict, is_dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pandas as pd


def _json_default(obj: Any):
    if isinstance(obj, (pd.Timestamp, datetime)):
        return obj.isoformat()
    if isinstance(obj, pd.Series):
        return obj.tolist()
    if isinstance(obj, pd.DataFrame):
        return obj.to_dict(orient="records")
    if is_dataclass(obj) and not isinstance(obj, type):
        return asdict(obj)
    if hasattr(obj, "item"):  # numpy scalar
        return obj.item()
    raise TypeError(f"not JSON serialisable: {type(obj).__name__}")


def dumps(obj: Any) -> str:
    return json.dumps(obj, default=_json_default, ensure_ascii=False)


class StateStore:
    """Thread-safe JSON/JSONL store for dashboard state."""

    def __init__(self, root: str | Path = "tradingsys/state") -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()

    # ------------------------------------------------------------------ paths
    @property
    def snapshot_path(self) -> Path:
        return self.root / "snapshot.json"

    @property
    def trades_path(self) -> Path:
        return self.root / "trades.jsonl"

    @property
    def equity_path(self) -> Path:
        return self.root / "equity.jsonl"

    @property
    def logs_path(self) -> Path:
        return self.root / "logs.jsonl"

    @property
    def backtest_path(self) -> Path:
        return self.root / "backtest.json"

    # ----------------------------------------------------------------- writes

    def save_snapshot(self, snapshot: dict) -> None:
        payload = dict(snapshot)
        payload["updated_at"] = datetime.now(UTC).isoformat()
        self._atomic_write(self.snapshot_path, payload)

    def save_backtest(self, result) -> None:
        """Persist a BacktestResult (full detail) for the dashboard."""
        eq = result.equity_curve
        equity_series = [
            {"t": str(idx), "equity": round(float(r["equity"]), 2)}
            for idx, r in eq.iterrows()
        ]
        drawdown_series = [
            {"t": str(idx), "dd": round(float(r["drawdown"]), 5)}
            for idx, r in eq.iterrows()
            if "drawdown" in eq.columns
        ]
        trades = result.trades_frame().to_dict(orient="records") if not eq.empty else []
        payload = {
            "saved_at": datetime.now(UTC).isoformat(),
            "metrics": result.metrics,
            "params": result.params,
            "config": result.config,
            "notes": result.notes,
            "equity": equity_series,
            "drawdown": drawdown_series,
            "trades": trades,
            "per_symbol": result.metrics.get("per_symbol", {}),
            "exit_reasons": result.metrics.get("exit_reasons", {}),
            "monte_carlo": result.metrics.get("monte_carlo", {}),
        }
        self._atomic_write(self.backtest_path, payload)

    def append(self, path: Path, payload: dict) -> None:
        line = dumps(payload) + "\n"
        with self._lock:
            with path.open("a", encoding="utf-8") as fh:
                fh.write(line)

    def log(self, event: str, level: str = "INFO", **fields) -> None:
        self.append(
            self.logs_path,
            {"ts": datetime.now(UTC).isoformat(), "level": level, "event": event, **fields},
        )

    def append_trade(self, trade: dict) -> None:
        self.append(self.trades_path, trade)

    def append_equity(self, row: dict) -> None:
        self.append(self.equity_path, row)

    def _atomic_write(self, path: Path, payload: dict) -> None:
        tmp = path.with_suffix(".tmp")
        with self._lock:
            with tmp.open("w", encoding="utf-8") as fh:
                fh.write(dumps(payload))
            tmp.replace(path)

    # ------------------------------------------------------------------ reads

    def read_snapshot(self) -> dict:
        return self._read_json(self.snapshot_path)

    def read_backtest(self) -> dict:
        return self._read_json(self.backtest_path)

    def read_trades(self, limit: int = 500) -> list[dict]:
        return self._read_jsonl(self.trades_path, limit)

    def read_logs(self, limit: int = 200) -> list[dict]:
        return self._read_jsonl(self.logs_path, limit)

    def read_equity(self, limit: int = 2000) -> list[dict]:
        return self._read_jsonl(self.equity_path, limit)

    @staticmethod
    def _read_json(path: Path) -> dict:
        if not path.exists():
            return {}
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}

    @staticmethod
    def _read_jsonl(path: Path, limit: int) -> list[dict]:
        if not path.exists():
            return []
        try:
            with path.open("r", encoding="utf-8") as fh:
                lines = fh.readlines()[-limit:]
        except OSError:
            return []
        out: list[dict] = []
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return out

    # ------------------------------------------------------------------ stats

    def live_metrics(self) -> dict:
        """Aggregate statistics over everything the engine has recorded."""
        trades = self.read_trades(limit=100_000)
        if not trades:
            return {"n_trades": 0}
        r = [float(t.get("r_multiple", 0.0)) for t in trades]
        pnl = [float(t.get("net_pnl", 0.0)) for t in trades]
        wins = [x for x in pnl if x > 0]
        losses = [-x for x in pnl if x < 0]
        return {
            "n_trades": len(trades),
            "win_rate": round(len(wins) / len(trades), 4),
            "expectancy_R": round(sum(r) / len(r), 4) if r else 0.0,
            "total_pnl": round(sum(pnl), 2),
            "profit_factor": round(sum(wins) / sum(losses), 3) if sum(losses) > 0 else None,
        }

    def save_agents(self, state: dict) -> None:
        """Persist the multi-agent system's live state for the dashboard."""
        payload = dict(state)
        payload["saved_at"] = datetime.now(UTC).isoformat()
        self._atomic_write(self.root / "agents.json", payload)

    def read_agents(self) -> dict:
        return self._read_json(self.root / "agents.json")

    def reset(self) -> None:
        for path in (self.trades_path, self.equity_path, self.logs_path,
                     self.backtest_path, self.snapshot_path,
                     self.root / "agents.json"):
            try:
                path.unlink()
            except OSError:
                pass


__all__ = ["StateStore", "dumps"]
