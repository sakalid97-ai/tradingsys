"""Wire a real start/stop engine factory into the dashboard's AppContext.

This is what makes the START button in the UI actually do something: the
dashboard process owns a TradingEngine (paper broker) and a
MultiAgentSupervisor, and can launch either in a background thread.
"""

from __future__ import annotations

import logging

import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from ts.agents.feed import MarketFeed
from ts.agents.supervisor import MultiAgentSupervisor, SupervisorConfig
from ts.broker.paper import PaperBroker, PaperBrokerConfig
from ts.datafeed import DataFeed, YahooFeed
from ts.engine import EngineConfig, TradingEngine
from ts.risk import RiskConfig
from ts.state import StateStore
from ts.web.server import AppContext


def build_context(state: StateStore, symbols: list[str], interval: str,
                  range_: str, host: str, port: int, open_browser: bool,
                  config: dict | None = None):
    """Create the AppContext with a live engine factory attached.

    ``config`` tunes the human-in-the-loop layer: the auto-approval thresholds,
    how long a prompt waits for an answer, and whether execution starts in
    dry-run. Everything has a safe default.
    """
    config = dict(config or {})
    # --- pick the data source the settings ask for
    from ts.settings import SettingsStore

    _settings = SettingsStore(state.root).load()
    _source = getattr(_settings.instruments, "data_source", "router") or "router"

    if _source == "router":
        from ts.feedrouter import RoutingFeed, SOURCE_NOTES

        route_feed = RoutingFeed(__import__(
            "ts.feedrouter", fromlist=["DataSourceConfig"]).DataSourceConfig(
                source="router", interval=interval, range_=range_))
        logging.getLogger("ts.web.context").info(
            "data source: router -> %s", SOURCE_NOTES["router"][:60])

    feed = route_feed if _source == "router" else YahooFeed(cache_dir=str(ROOT / "data"))

    def fetcher(symbol, range_arg, interval_arg):
        """Fetch one symbol. Works with either a routed or Yahoo feed."""
        from ts.quality import validate

        if _source == "router":
            # a routed ticker (SPX500, BTCUSD) is already the vendor symbol
            try:
                return route_feed.history(symbol, interval=interval_arg, range_=range_arg)
            except Exception:  # noqa: BLE001 - fall through to Yahoo
                pass
        return DataFeed(feed=feed, interval=interval_arg,
                        range_=range_arg).pull([symbol]).get(symbol)

    agent_feed = MarketFeed(feed=feed, interval=interval, range_=range_)
    # 1m data is capped at 7 days by the data source; a shorter range would
    # starve the strategy of its indicator warm-up.
    if interval == "1m" and str(range_) in ("1d", "5d"):
        range_ = "7d"

    holder: dict = {"engine": None, "supervisor": None, "broker": None}
    lock = threading.Lock()

    def starter(config: dict) -> dict:
        mode = str(config.get("mode", "agents")).lower()
        with lock:
            if holder["engine"] is not None and holder["engine"].running:
                return {"started": False, "reason": "trading engine already running"}
            if holder["supervisor"] is not None and getattr(
                    holder["supervisor"], "_thread", None) and \
                    holder["supervisor"]._thread.is_alive():
                return {"started": False, "reason": "agent system already running"}

            capital = float(config.get("capital", 100_000.0) or 100_000.0)
            risk = float(config.get("risk_per_trade", 0.01) or 0.01)
            poll = float(config.get("poll_seconds", 60.0) or 60.0)
            dry_run = bool(config.get("dry_run", True))

            if mode == "paper":
                broker = PaperBroker(
                    price_fn=lambda sym: float(feed.quote(sym, interval).price),
                    config=PaperBrokerConfig(starting_cash=capital, slippage_bps=5.0),
                )
                from ts.forward import ForwardJournal

                engine = TradingEngine(
                    broker=broker,
                    data_fetcher=fetcher,
                    risk_config=RiskConfig(risk_per_trade=risk),
                    config=EngineConfig(
                        poll_seconds=poll,
                        interval=interval,
                        history_range=range_,
                        dry_run=dry_run,
                        use_market_gate=bool(config.get("use_market_gate", True)),
                    ),
                    state_store=state,
                    universe=symbols,
                    kill_switch=lambda: (state.root / "KILL").exists(),
                    forward_journal=ForwardJournal(state.root),
                    notifier=notifier,
                    approvals=approvals,
                    policy=policy,
                )
                holder.update(engine=engine, broker=broker)
                context.engine = engine
                threading.Thread(target=engine.run_forever, name="trade-engine",
                                 daemon=True).start()
                return {
                    "started": True, "mode": "paper", "capital": capital,
                    "risk_per_trade": risk, "poll_seconds": poll,
                    "dry_run": dry_run, "symbols": len(symbols),
                    "note": "paper fills execute in-process; no orders leave this machine",
                }

            supervisor = MultiAgentSupervisor(
                feed=agent_feed,
                symbols=symbols,
                config=SupervisorConfig(
                    cycle_seconds=poll,
                    interval=interval,
                    range_=range_,
                    dry_run=dry_run,
                    min_expectancy=float(config.get("min_expectancy", 0.20) or 0.20),
                    min_confidence=float(config.get("min_confidence", 0.65) or 0.65),
                    kelly_fraction=float(config.get("kelly_fraction", 0.25) or 0.25),
                    max_risk_per_trade=risk,
                ),
                kill_switch=(lambda reason: state.set_killed(True, reason))
                if config.get("kill_on_risk") else None,
                state_store=state,
            )
            holder.update(engine=None, supervisor=supervisor)
            context.agents = supervisor
            supervisor.start()
            return {
                "started": True, "mode": "agents", "capital": capital,
                "risk_per_trade": risk, "cycle_seconds": poll, "dry_run": dry_run,
                "symbols": len(symbols),
                "note": "six agents running; positions are simulated (dry run)",
            }

    def stopper() -> dict:
        with lock:
            stopped = []
            if holder["engine"] is not None:
                holder["engine"].stop()
                stopped.append("trading engine")
                holder["engine"] = None
                context.engine = None
            if holder["supervisor"] is not None:
                holder["supervisor"].stop()
                stopped.append("agent system")
                holder["supervisor"] = None
                context.agents = None
            return {"stopped": stopped, "engine_running": False}

    # --- human-in-the-loop: the engine asks a human only when it is unsure ---
    from ts.agents.policy import AutoApprovePolicy
    from ts.approvals import ApprovalGate
    from ts.notify import NotifyConfig, Notifier

    notifier = Notifier(NotifyConfig())
    policy = AutoApprovePolicy(
        min_expectancy=float(config.get("min_expectancy", 0.35)),
        min_confidence=float(config.get("min_confidence", 0.75)),
        max_auto_risk_pct=float(config.get("max_auto_risk_pct", 0.02)),
    ).clamp()
    approvals = ApprovalGate(
        notifier=notifier,
        timeout_seconds=float(config.get("approval_timeout", 90)),
        require_approval=bool(config.get("require_approval", False)),
    )

    context = AppContext(
        state=state,
        kill_file=state.root / "KILL",
        engine_starter=starter,
        engine_stopper=stopper,
    )

    def data_provider():
        return agent_feed.fetch_all(symbols)

    context.data_provider = data_provider

    def backtester_factory(params: dict):
        from ts.backtest import BacktestConfig, Backtester
        from ts.risk import RiskConfig
        from ts.strategy import StrategyParams

        frames = {s: f for s, f in data_provider().items()}
        return Backtester(
            strategy_params=StrategyParams(),
            risk_config=RiskConfig(risk_per_trade=0.01, max_positions=8),
            config=BacktestConfig(initial_capital=100_000.0),
            market_gate=frames.get("SPY"),
        )

    context.backtester_factory = backtester_factory
    context.approvals = approvals
    context.policy = policy
    context.notifier = notifier
    holder['approvals'] = approvals
    holder['policy'] = policy
    holder['notifier'] = notifier
    return context, holder


if __name__ == "__main__":
    store = StateStore(ROOT / "state")
    ctx, holder = build_context(store, ["BTCUSD", "ETHUSD"], "1h", "60d",
                               "127.0.0.1", 8642, False)
    print("engine_starter present:", ctx.engine_starter is not None)
    print("engine_stopper present:", ctx.engine_stopper is not None)
    print("data_provider present :", ctx.data_provider is not None)
    print("backtester present    : callable =", callable(ctx.backtester_factory))
