"""Dashboard HTTP server and JSON API.

Standard library only -- ``http.server`` with a threading server, so the system
runs anywhere Python runs with no install step.  The server is deliberately
*read-mostly*: it renders from :class:`~ts.state.StateStore` and never holds
trading state itself, so the UI cannot crash the trading loop.

Endpoints
=========

=========================  =====  ==========================================
Path                       Verb   Meaning
=========================  =====  ==========================================
``/``                      GET    dashboard
``/static/*``              GET    assets
``/api/status``            GET    system health, mode, uptime
``/api/snapshot``          GET    live equity / positions / P&L
``/api/positions``         GET    open positions with live R
``/api/trades``            GET    recent closed trades
``/api/logs``              GET    event log
``/api/metrics``           GET    live + last-backtest metrics
``/api/equity``            GET    equity series for charting
``/api/backtest``          GET    latest backtest
``/api/backtest/run``      POST   run a backtest now
``/api/control/kill``      POST   emergency stop
``/api/control/resume``    POST   clear the kill switch
``/api/control/scanner``   GET    latest signals per symbol
=========================  =====  ==========================================
"""

from __future__ import annotations

import json
import logging
import threading
import time
import webbrowser
from dataclasses import dataclass, field
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from ..broker.base import BrokerError
import pandas as pd
from ..settings import Settings
from ..settings import SettingsStore
from ..state import StateStore, dumps

log = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).resolve().parent / "static"


def _trade_from_payload(payload: dict):
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


def _catalogue():
    from ..broker import registry

    return registry.catalogue()


def _data_sources():
    """The available data sources and what each one covers."""
    from ..feedrouter import SOURCE_NOTES

    return [
        {"id": key, "label": label, **note} for key, label in [
            ("router", "Auto (recommended)"),
            ("binance", "Binance public"),
            ("alpaca", "Alpaca market data"),
            ("yahoo", "Yahoo Finance"),
        ] for note in [dict(zip(("note",), (SOURCE_NOTES.get(key, ""),)))]
    ]


def _propfirm_registry():
    from ..propfirm import registry

    return registry()


def _propfirm_limits(rules, safety_buffer):
    from ..propfirm import PropFirmMonitor

    return PropFirmMonitor(rules, safety_buffer=safety_buffer).limits()

# A browser closing a tab or navigating away mid-request is routine for a
# long-polling dashboard; catching it keeps the server quiet.
_CLIENT_GONE = (ConnectionResetError, BrokenPipeError, ConnectionAbortedError)


@dataclass
class ServerConfig:
    host: str = "127.0.0.1"
    port: int = 8642
    open_browser: bool = True
    title: str = "Autonomous Trading System"

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}/"


@dataclass
class AppContext:
    """Everything the API needs, injected so the server stays decoupled."""

    state: StateStore
    engine: object | None = None           # TradingEngine, optional
    agents: object | None = None           # MultiAgentSupervisor, optional
    backtester_factory: object | None = None   # callable(params) -> Backtester
    data_provider: object | None = None    # callable() -> dict[str, DataFrame]
    engine_starter: object | None = None   # callable(config) -> dict; starts live trading
    engine_stopper: object | None = None   # callable() -> dict
    kill_file: Path = field(default=Path("tradingsys/state/KILL"))
    settings: Settings = field(default_factory=Settings)
    started_at: float = field(default_factory=time.time)
    version: str = "1.0.0"

    @property
    def killed(self) -> bool:
        return self.kill_file.exists()

    def set_killed(self, value: bool, reason: str = "") -> bool:
        self.kill_file.parent.mkdir(parents=True, exist_ok=True)
        if value:
            self.kill_file.write_text(
                dumps({"killed_at": datetime.now(UTC).isoformat(), "reason": reason}),
                encoding="utf-8",
            )
            self.state.log("kill_switch", level="WARN", reason=reason)
        else:
            try:
                self.kill_file.unlink()
            except OSError:
                pass
            self.state.log("kill_switch_cleared", level="INFO")
        return value


class DashboardServer:
    """Serves the dashboard and the JSON API."""

    def __init__(self, context: AppContext, config: ServerConfig | None = None) -> None:
        self.ctx = context
        self.cfg = config or ServerConfig()
        self.httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._backtest_lock = threading.Lock()
        self._engine_lock = threading.Lock()
        self._live_engine = None
        self._last_backtest_run: dict = {"status": "never", "at": None}

    # ------------------------------------------------------------------ routes

    def route(self, method: str, path: str, query: dict, body: dict) -> tuple[int, dict]:
        """Dispatch a request. Returns (status_code, response_body)."""
        if path == "/api/status":
            return 200, self._status()
        if path == "/api/snapshot":
            return 200, self._snapshot()
        if path == "/api/positions":
            return 200, {"positions": self._positions()}
        if path == "/api/trades":
            return 200, {"trades": self.ctx.state.read_trades(int(query.get("limit", ["300"])[0]))}
        if path == "/api/logs":
            return 200, {"logs": self.ctx.state.read_logs(int(query.get("limit", ["200"])[0]))}
        if path == "/api/metrics":
            return 200, self._metrics()
        if path == "/api/equity":
            return 200, {"equity": self.ctx.state.read_equity(int(query.get("limit", ["5000"])[0]))}
        if path == "/api/backtest":
            return 200, self.ctx.state.read_backtest()
        if path == "/api/backtest/run" and method == "POST":
            return self._run_backtest(body)
        if path == "/api/control/kill" and method == "POST":
            reason = str(body.get("reason", "manual kill switch"))
            return 200, {"killed": self.ctx.set_killed(True, reason)}
        if path == "/api/control/resume" and method == "POST":
            return 200, {"killed": self.ctx.set_killed(False)}
        if path == "/api/control/scanner":
            return 200, self._scanner()
        if path == "/api/agents":
            return 200, self._agents()
        if path == "/api/settings" and method == "GET":
            return 200, self.ctx.settings.redacted()
        if path == "/api/settings" and method == "POST":
            return self._save_settings(body)
        if path == "/api/brokers":
            return 200, {"brokers": _catalogue()}
        if path == "/api/broker/connect" and method == "POST":
            return self._broker_connect(body)
        if path == "/api/broker/disconnect" and method == "POST":
            return self._broker_disconnect()
        if path == "/api/broker/test" and method == "POST":
            return self._broker_test(body)
        if path == "/api/instruments":
            return 200, self._instruments()
        if path == "/api/data-sources":
            return 200, {"sources": _data_sources()}
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
            return 200, self._propfirm_status()
        if path == "/api/forward" and method == "GET":
            return 200, self._forward_summary()
        if path == "/api/forward/baseline" and method == "POST":
            return self._forward_baseline(body)
        if path == "/api/forward/reset" and method == "POST":
            return self._forward_reset()
        if path == "/api/telegram/config" and method == "GET":
            return 200, self._telegram_status()
        if path == "/api/telegram/test" and method == "POST":
            return self._telegram_test()
        if path == "/api/telegram/send" and method == "POST":
            return self._telegram_send(body)
        if path == "/api/telegram/command" and method == "POST":
            return self._telegram_command(body)
        if path == "/api/forward/import" and method == "POST":
            return self._forward_import()
        if path == "/api/engine/start" and method == "POST":
            return self._engine_start(body)
        if path == "/api/engine/stop" and method == "POST":
            return self._engine_stop()
        return 404, {"error": f"no route for {method} {path}"}

    # ---------------------------------------------------------------- handlers

    def _status(self) -> dict:
        engine = self.ctx.engine
        if engine is None:
            engine = self.ctx.agents or self._live_engine
        # "Running" means the worker thread is alive, NOT that it has finished a
        # cycle. `cycles` is only incremented at the END of a cycle, and the
        # first one spends 5-20s fetching the universe -- so keying the status
        # on it reported STOPPED while the engine was mid-flight.
        agents = self.ctx.agents
        thread = getattr(agents, "_thread", None)
        supervisor_alive = bool(thread is not None and thread.is_alive())
        running = bool(getattr(engine, "running", False)) or supervisor_alive

        mode = str(getattr(engine, "mode", "IDLE")) if engine else "IDLE"
        if agents is not None:
            mode = ("AGENTS-DRYRUN" if agents.cfg.dry_run else "AGENTS-LIVE") \
                if supervisor_alive else "IDLE"
            if supervisor_alive and not getattr(agents, "cycles", 0):
                # alive but mid-first-cycle: say so rather than implying IDLE
                mode = mode + " (starting)"
        return {
            "ok": True,
            "version": self.ctx.version,
            "mode": mode,
            "engine_running": running,
            "killed": self.ctx.killed,
            "uptime_seconds": round(time.time() - self.ctx.started_at, 1),
            "server_time": datetime.now(UTC).isoformat(),
            "last_backtest": self._last_backtest_run,
            "broker": str(getattr(engine, "broker_name", "none")) if engine else "none",
        }

    def _snapshot(self) -> dict:
        snap = self.ctx.state.read_snapshot()
        snap["killed"] = self.ctx.killed
        series = [
            {"t": r["t"], "equity": r.get("equity")}
            for r in self.ctx.state.read_equity(3000)
            if "t" in r
        ]
        snap["equity_series"] = series

        # A dry-run agent cycle records no broker equity, which would leave the
        # header reading $0 while the chart shows a real curve. Fall back to the
        # most recent recorded equity, then to the last backtest's final equity.
        if not snap.get("equity"):
            if series and series[-1].get("equity"):
                snap["equity"] = series[-1]["equity"]
            else:
                metrics = (self.ctx.state.read_backtest() or {}).get("metrics") or {}
                snap["equity"] = metrics.get("final_equity")
        return snap

    def _positions(self) -> list[dict]:
        engine = self.ctx.engine
        if engine is not None:
            try:
                return list(getattr(engine, "open_positions", []) or [])
            except (AttributeError, TypeError):  # pragma: no cover
                pass
        return self.ctx.state.read_snapshot().get("positions", [])

    def _metrics(self) -> dict:
        live = self.ctx.state.live_metrics()
        backtest = self.ctx.state.read_backtest()
        return {
            "live": live,
            "backtest": {
                "metrics": backtest.get("metrics", {}),
                "params": backtest.get("params", {}),
                "config": backtest.get("config", {}),
                "saved_at": backtest.get("saved_at"),
            },
        }

    def _scanner(self) -> dict:
        """Latest bar + per-symbol regime/breakout state, for the scanner table."""
        provider = self.ctx.data_provider
        if provider is None:
            return {"rows": [], "note": "no data provider configured"}
        try:
            frames = provider()
        except Exception as exc:  # noqa: BLE001 - dashboard must never 500
            log.warning("scanner failed: %s", exc)
            return {"rows": [], "error": str(exc)}

        from ..strategy import Strategy, StrategyParams

        rows: list[dict] = []
        strategy = Strategy(StrategyParams())
        for symbol, frame in frames.items():
            if frame is None or len(frame) < 30:
                continue
            try:
                sig = strategy.compute(frame)
                last = sig.iloc[-1]
                rows.append({
                    "symbol": symbol,
                    "close": round(float(sig["close"].iloc[-1]), 2),
                    "regime": int(last["regime"]),
                    "breakout_long": bool(last["breakout_long"]),
                    "breakout_short": bool(last["breakout_short"]),
                    "trend_strength": bool(last["trend_strength"]),
                    "volume_ok": bool(last["volume_ok"]),
                    "entry_long": bool(last["entry_long"]),
                    "atr": round(float(last["atr"]), 3) if last["atr"] == last["atr"] else None,
                })
            except (ValueError, KeyError, IndexError) as exc:
                rows.append({"symbol": symbol, "error": str(exc)})
        return {"rows": rows, "as_of": datetime.now(UTC).isoformat()}

    def _agents(self) -> dict:
        """Multi-agent system state, for the Agents dashboard view."""
        agents = self.ctx.agents
        if agents is not None:
            try:
                return agents.snapshot()
            except Exception as exc:  # noqa: BLE001
                log.warning("live agents snapshot failed: %s", exc)
        stored = self.ctx.state.read_agents()
        if stored:
            stored["source"] = "file"
            return stored
        return {"agents": {}, "note": "no agent system has run yet"}

    def _engine_start(self, body: dict) -> tuple[int, dict]:
        """Start the trading engine or the agent system in-process."""
        mode = str(body.get("mode", "agents")).lower()
        if mode not in ("paper", "agents"):
            return 400, {"error": f"unknown mode {mode!r}; use 'paper' or 'agents'"}
        if self.ctx.engine_starter is None:
            return 501, {"error": "this server was started without engine control"}
        try:
            result = self.ctx.engine_starter({"mode": mode, **(body or {})})
        except Exception as exc:  # noqa: BLE001
            log.exception("engine start failed")
            return 500, {"error": str(exc)}
        if self.ctx.state:
            self.ctx.state.log("engine_start_requested", level="INFO", mode=mode,
                           result=str(result)[:200])
        status = 202 if result.get("started") else 409
        return status, result

    def _engine_stop(self) -> tuple[int, dict]:
        if self.ctx.engine_stopper is None:
            return 501, {"error": "this server was started without engine control"}
        try:
            result = self.ctx.engine_stopper()
        except Exception as exc:  # noqa: BLE001
            return 500, {"error": str(exc)}
        if self.ctx.state:
            self.ctx.state.log("engine_stop_requested", level="INFO")
        return 200, result

    # -------------------------------------------------------------- telegram

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

    # ---------------------------------------------------------------- forward

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

    # ---------------------------------------------------------------- console

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
    # -------------------------------------------------------------- settings

    def _save_settings(self, body: dict) -> tuple[int, dict]:
        """Persist a settings update from the dashboard.

        A section name limits what can change, so a bad payload from one panel
        cannot corrupt another panel's values.
        """
        from ..settings import SettingsStore

        section = str(body.get("section", "")).strip()
        values = body.get("values") or {}
        if section not in ("broker", "strategy", "instruments", "risk"):
            return 400, {"error": "section must be broker, strategy, instruments or risk"}
        if not isinstance(values, dict):
            return 400, {"error": "values must be an object"}
        try:
            store = SettingsStore(self.ctx.state.root)
            updated = store.update(section, values)
        except KeyError as exc:
            return 400, {"error": str(exc)}
        except (OSError, ValueError) as exc:
            return 500, {"error": str(exc)}
        self.ctx.settings = updated
        if self.state:
            self.state.log("settings_updated", section=section, keys=list(values))
        return 200, {"ok": True, "settings": updated.redacted()}

    def _instruments(self) -> dict:
        """The tradable universe with per-class defaults."""
        from ..agents.instruments import DEFAULT_UNIVERSE

        return {
            "defaults": {c.value: list(s) for c, s in DEFAULT_UNIVERSE.items()},
            "selected": self.ctx.settings.instruments.symbols,
            "timeframe": self.ctx.settings.instruments.timeframe,
            "range": self.ctx.settings.instruments.range_,
            "asset_classes": self.ctx.settings.instruments.asset_classes,
        }

    # --------------------------------------------------------------- brokers

    def _broker_connect(self, body: dict) -> tuple[int, dict]:
        """Save credentials; does not open a connection by itself."""
        from ..settings import SettingsStore

        allowed = ("broker", "key", "secret", "passphrase", "testnet",
                   "store_credentials", "account_label")
        values = {k: v for k, v in (body or {}).items() if k in allowed}
        if not values.get("broker"):
            return 400, {"error": "broker id is required"}
        try:
            store = SettingsStore(self.ctx.state.root)
            updated = store.update("broker", values)
        except (KeyError, OSError, ValueError) as exc:
            return 400, {"error": str(exc)}
        self.ctx.settings = updated
        redacted = updated.broker.redacted()
        return 200, {
            "ok": True,
            "broker": redacted,
            "note": ("credentials stored on disk under state/" if values.get(
                "store_credentials") else "credentials held for this session only; "
                                          "environment variables are preferred"),
        }

    def _broker_disconnect(self) -> tuple[int, dict]:
        from ..settings import SettingsStore

        store = SettingsStore(self.ctx.state.root)
        updated = store.update("broker", {"key": "", "secret": "", "passphrase": "",
                                          "account_label": ""})
        self.ctx.settings = updated
        return 200, {"ok": True, "broker": updated.broker.redacted()}

    def _broker_test(self, body: dict) -> tuple[int, dict]:
        """Attempt a real connection and read the account back.

        Returns a structured result rather than a bare error so the UI can show
        *why* a connection failed -- which for Exness is nearly always a missing
        MT5 terminal.
        """
        from ..broker import registry
        from ..settings import SettingsStore

        store = SettingsStore(self.ctx.state.root)
        settings = store.load()
        broker_id = str((body or {}).get("broker") or settings.broker.broker).lower()

        descriptor = registry.describe(broker_id)
        if descriptor is None:
            return 404, {"ok": False, "error": f"unknown broker {broker_id!r}",
                         "supported": registry.catalogue()}
        if broker_id == "paper":
            return 200, {"ok": True, "broker": "paper",
                         "account": {"equity": settings.risk.capital, "cash": settings.risk.capital,
                                     "currency": "USD", "note": "simulated"}}

        credential = settings.broker.resolve()
        credential.broker = broker_id
        try:
            broker = registry.build(credential,
                                    price_fn=lambda s: self._test_price(s),
                                    starting_cash=settings.risk.capital)
            account = broker.account()
        except Exception as exc:  # noqa: BLE001 - surface every failure mode
            problem = str(exc)
            hint = ""
            if broker_id == "exness":
                hint = ("Exness has no REST API. This needs Windows, the MetaTrader 5 "
                        "terminal installed and running, an Exness MT5 account logged "
                        "in, algorithmic trading enabled, and "
                        "'py -m pip install MetaTrader5'.")
            elif broker_id == "binance":
                hint = ("Use testnet keys from testnet.binance.vision, or set "
                        "BINANCE_LIVE_CONFIRMED=yes to allow the live endpoint.")
            elif broker_id == "alpaca":
                hint = ("Use paper keys from alpaca.markets, or set "
                        "ALPACA_LIVE_CONFIRMED=yes for the live endpoint.")
            return 200, {"ok": False, "broker": broker_id, "error": problem,
                         "hint": hint, "requires": descriptor.requires}

        return 200, {"ok": True, "broker": broker.name, "mode": broker.mode,
                     "account": account, "redacted": credential.redacted()}

    def _test_price(self, symbol: str) -> float:
        """A price for connection tests, so the paper path has one."""
        try:
            if self.ctx.data_provider is None:
                raise BrokerError("no price source configured")
            frames = self.ctx.data_provider()
            if symbol in frames:
                return float(frames[symbol]["close"].iloc[-1])
            first = next(iter(frames.values()), None)
            if first is not None:
                return float(first["close"].iloc[-1])
        except Exception:  # noqa: BLE001
            pass
        return 100.0

    def _run_backtest(self, body: dict) -> tuple[int, dict]:
        """Start a backtest on a worker thread so the UI never blocks."""
        if self.ctx.backtester_factory is None or self.ctx.data_provider is None:
            return 400, {"error": "backtesting is not configured for this server"}
        if not self._backtest_lock.acquire(blocking=False):
            return 409, {"error": "a backtest is already running"}

        try:
            from ..backtest import Backtester

            def worker() -> None:
                started = time.time()
                try:
                    self._last_backtest_run = {"status": "running", "at": datetime.now(UTC).isoformat()}
                    frames = self.ctx.data_provider()
                    backtester = self.ctx.backtester_factory(body or {})
                    result = backtester.run(frames)
                    self.ctx.state.save_backtest(result)
                    self.ctx.state.log("backtest_complete", level="INFO",
                                      trades=result.metrics.get("n_trades"),
                                      expectancy_R=result.metrics.get("expectancy_R"),
                                      seconds=round(time.time() - started, 1))
                    self._last_backtest_run = {
                        "status": "done",
                        "at": datetime.now(UTC).isoformat(),
                        "seconds": round(time.time() - started, 1),
                        "trades": result.metrics.get("n_trades"),
                        "expectancy_R": result.metrics.get("expectancy_R"),
                    }
                except Exception as exc:  # noqa: BLE001
                    log.exception("background backtest failed")
                    self._last_backtest_run = {
                        "status": "error",
                        "at": datetime.now(UTC).isoformat(),
                        "error": str(exc),
                    }
                    self.ctx.state.log("backtest_failed", level="ERROR", error=str(exc))
                finally:
                    self._backtest_lock.release()

            threading.Thread(target=worker, name="backtest", daemon=True).start()
            return 202, {"started": True, "status": self._last_backtest_run}
        except Exception as exc:  # noqa: BLE001
            self._backtest_lock.release()
            return 500, {"error": str(exc)}

    # ------------------------------------------------------------------- server

    def start(self, blocking: bool = False, open_browser: bool | None = None) -> None:
        handler = self._make_handler()
        self.httpd = ThreadingHTTPServer((self.cfg.host, self.cfg.port), handler)
        self.httpd.daemon_threads = True
        log.info("dashboard listening on %s", self.cfg.url)

        if open_browser is None:
            open_browser = self.cfg.open_browser
        if open_browser:
            threading.Thread(target=self._open_browser, daemon=True).start()

        if blocking:
            self.httpd.serve_forever()
        else:
            self._thread = threading.Thread(
                target=self.httpd.serve_forever, name="dashboard", daemon=True
            )
            self._thread.start()

    def _open_browser(self) -> None:
        time.sleep(0.7)
        try:
            webbrowser.open(self.cfg.url)
        except Exception:  # noqa: BLE001 - headless environments
            pass

    def stop(self) -> None:
        if self.httpd:
            self.httpd.shutdown()
            self.httpd.server_close()

    # ------------------------------------------------------------------ handler

    def _make_handler(self):
        server = self

        class Handler(BaseHTTPRequestHandler):
            server_version = "TradingDashboard/1.0"
            protocol_version = "HTTP/1.1"

            def log_message(self, fmt, *args):  # pragma: no cover - quiet by default
                log.debug("%s - %s", self.address_string(), fmt % args)

            # ---- helpers
            def _send(self, status: int, body: dict | bytes, content_type: str) -> None:
                if isinstance(body, dict):
                    payload = json.dumps(body, default=str).encode("utf-8")
                else:
                    payload = body
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(payload)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(payload)

            def _send_json(self, status: int, body: dict) -> None:
                self._send(status, body, "application/json; charset=utf-8")

            def _send_file(self, path: Path) -> None:
                if not path.is_file():
                    self._send_json(404, {"error": "not found"})
                    return
                types = {".html": "text/html", ".css": "text/css", ".js": "text/javascript",
                         ".json": "application/json", ".svg": "image/svg+xml"}
                self._send(200, path.read_bytes(),
                           types.get(path.suffix.lower(), "application/octet-stream"))

            def _read_body(self) -> dict:
                length = int(self.headers.get("Content-Length", 0) or 0)
                if length <= 0:
                    return {}
                raw = self.rfile.read(length)
                try:
                    return json.loads(raw.decode("utf-8"))
                except (json.JSONDecodeError, UnicodeDecodeError):
                    return {}

            # ---- verbs
            def handle_one_request(self) -> None:
                # Swallow a client that vanished mid-request.
                try:
                    super().handle_one_request()
                except _CLIENT_GONE:
                    self.close_connection = True

            def stream(self) -> None:
                """Server-Sent Events: push the snapshot whenever it changes.

                Lives on the request handler because that is where send_response
                and wfile exist -- putting it on DashboardServer raises
                AttributeError. Tailing the state file means the UI updates on
                the engine's cadence, not the browser's poll interval.
                """
                try:
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.send_header("Cache-Control", "no-cache")
                    self.send_header("Connection", "keep-alive")
                    self.end_headers()

                    state_root = Path(server.ctx.state.root)
                    snapshot = state_root / "snapshot.json"
                    last_mtime = None
                    last_payload = ""
                    while True:
                        try:
                            mtime = snapshot.stat().st_mtime if snapshot.exists() else None
                            if mtime and mtime != last_mtime:
                                last_mtime = mtime
                                payload = dumps({"type": "snapshot",
                                                "data": server.ctx.state.read_snapshot()})
                                if payload != last_payload:
                                    last_payload = payload
                                    self.wfile.write(("data: " + payload + "\n\n").encode())
                                    self.wfile.flush()
                            time.sleep(1.0)
                        except Exception:  # noqa: BLE001 - client disconnected
                            break
                except Exception:  # noqa: BLE001 - client disconnected
                    pass

            def do_GET(self) -> None:  # noqa: N802
                parsed = urlparse(self.path)
                path = parsed.path
                query = parse_qs(parsed.query)
                try:
                    if path in ("/", "/index.html"):
                        self._send_file(STATIC_DIR / "index.html")
                        return
                    if path == "/api/stream":
                        self.stream()
                        return
                    if path.startswith("/static/"):
                        target = (STATIC_DIR / path[len("/static/"):]).resolve()
                        if not str(target).startswith(str(STATIC_DIR.resolve())):
                            self._send_json(403, {"error": "forbidden"})
                            return
                        self._send_file(target)
                        return
                    status, body = server.route("GET", path, query, {})
                    self._send_json(status, body)
                except _CLIENT_GONE:
                    self.close_connection = True
                except Exception as exc:  # noqa: BLE001 - never let the UI down
                    log.debug("GET %s failed: %s", self.path, exc)
                    try:
                        self._send_json(500, {"error": str(exc)})
                    except _CLIENT_GONE:
                        self.close_connection = True

            def do_POST(self) -> None:  # noqa: N802
                parsed = urlparse(self.path)
                body = self._read_body()
                try:
                    status, payload = server.route("POST", parsed.path, {}, body)
                    self._send_json(status, payload)
                except _CLIENT_GONE:
                    self.close_connection = True
                except Exception as exc:  # noqa: BLE001
                    log.debug("POST %s failed: %s", self.path, exc)
                    try:
                        self._send_json(500, {"error": str(exc)})
                    except _CLIENT_GONE:
                        self.close_connection = True

        return Handler


__all__ = ["DashboardServer", "ServerConfig", "AppContext"]
