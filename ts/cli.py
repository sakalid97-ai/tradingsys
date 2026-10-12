"""Command line interface.

    py run.py backtest    -- re-run the strategy over history, save results
    py run.py dashboard   -- start the web dashboard
    py run.py paper       -- paper-trade the strategy live (simulated fills)
    py run.py live        -- real-money trading (OPT-IN, never the default)
    py run.py report      -- print metrics from the state store
    py run.py cache clear -- drop cached bars
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_UNIVERSE = [
    "SPY", "QQQ", "IWM",
    "AAPL", "MSFT", "NVDA", "AMD", "META", "GOOGL", "AMZN",
    "AVGO", "TSLA", "NFLX", "CRM", "PLTR",
]

LOG_FORMAT = "%(asctime)s %(levelname)-7s %(name)s | %(message)s"


def _make_console_safe() -> None:
    """Avoid a UnicodeEncodeError on a cp1252 Windows console.

    The CLI prints arrows and box-drawing characters; a default Windows console
    would crash with 'charmap codec can\'t encode' the first time one appears.
    Reconfiguring stdout with errors="replace" turns that into a '?' instead.
    """
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError, ValueError):  # redirected/piped output
        pass


def setup_logging(verbose: bool = False) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format=LOG_FORMAT,
        datefmt="%Y-%m-%d %H:%M:%S",
    )


UNIVERSE_FILES = ("config/universe.txt", "config/instruments.txt")


def default_universe_path() -> Path | None:
    """The universe file used when none was named on the command line."""
    for candidate in UNIVERSE_FILES:
        path = ROOT / candidate
        if path.exists():
            return path
    return None


def load_universe(path: str | None) -> list[str]:
    """Read a symbol file (one per line, ``#`` comments allowed)."""
    if not path:
        default = default_universe_path()
        if default is not None:
            return load_universe(str(default))
        return DEFAULT_UNIVERSE
    file = Path(path)
    if not file.exists():
        raise SystemExit(f"universe file not found: {path}")
    symbols = []
    # utf-8-sig strips a byte-order mark, which otherwise becomes part of the
        # first symbol and makes every lookup for it fail
    for line in file.read_text(encoding="utf-8-sig").splitlines():
        line = line.split("#")[0].strip().upper()
        if line:
            symbols.append(line)
    if not symbols:
        raise SystemExit(f"universe file {path} contained no symbols")
    return symbols


# --------------------------------------------------------------------- commands


def cmd_backtest(args) -> int:
    from ts.backtest import BacktestConfig, Backtester
    from ts.datafeed import DataFeed, YahooFeed
    from ts.risk import RiskConfig
    from ts.state import StateStore
    from ts.strategy import StrategyParams

    symbols = load_universe(args.universe)
    feed = YahooFeed(cache_dir=str(ROOT / "data"))
    # Trading symbols must be mapped to their data-source tickers: SPX500 -> ^GSPC,
    # EURUSD -> EURUSD=X, BTCUSD -> BTC-USD. The agent layer has always done this;
    # the backtest path was passing trading names straight through and 404ing.
    from ts.agents.instruments import yahoo_symbol

    tickers = {s: yahoo_symbol(s) for s in symbols}
    print(f"[*] fetching {len(symbols)} symbols ({args.range}, {args.interval}) ...")
    pulled = DataFeed(feed=feed, interval=args.interval, range_=args.range).pull(
        list(tickers.values()))
    # restore the trading keys so results, journals and the dashboard stay symbol-agnostic
    data = {}
    for symbol, ticker in tickers.items():
        if ticker in pulled:
            data[symbol] = pulled.pop(ticker)
    unresolved = [s for s in symbols if s not in data]
    if unresolved:
        print(f"[!] unresolved: {unresolved}")
    if not data:
        print("[!] no data retrieved")
        return 1

    longest = max(len(f) for f in data.values())
    span_days = (max(f.index[-1] for f in data.values())
                 - min(f.index[0] for f in data.values())).days
    print(f"[*] {len(data)} symbols, longest {longest:,} bars ({span_days} days)")
    if longest < 1200 or span_days < 60:
        print(f"[!] WARNING: {longest:,} bars over {span_days} days is too little to validate. "
              "The result below can tell you the strategy TRADES, not whether it is PROFITABLE.")
    backtester = Backtester(
        strategy_params=StrategyParams(
            entry_lookback=args.entry_lookback,
            adx_min=args.adx,
            atr_stop_mult=args.stop,
            atr_trail_mult=args.trail,
            allow_shorts=args.shorts,
        ),
        risk_config=RiskConfig(
            risk_per_trade=args.risk,
            max_positions=args.max_positions,
            max_total_risk=args.max_risk,
            daily_loss_limit=args.daily_limit,
            max_drawdown_halt=args.drawdown_halt,
        ),
        config=BacktestConfig(
            initial_capital=args.capital,
            slippage_bps=args.slippage,
            commission_per_share=args.commission,
        ),
        market_gate=data.get(args.gate) if args.gate else None,
    )
    print("[*] running backtest ...")
    result = backtester.run(data)
    print(result.summary())

    store = StateStore(ROOT / "state")
    store.save_backtest(result)
    # A backtest replaces the recorded history rather than appending to it,
    # so repeated runs do not double-count trades.
    for path in (store.trades_path, store.equity_path):
        try:
            path.unlink()
        except OSError:
            pass
    for trade in result.trades_frame().to_dict("records"):
        store.append_trade(trade)
    for idx, row in result.equity_curve.iterrows():
        store.append_equity({"t": str(idx), "equity": round(float(row["equity"]), 2)})
    store.log("cli_backtest", trades=result.metrics.get("n_trades"),
              expectancy_R=result.metrics.get("expectancy_R"))
    print(f"[OK] saved to {store.root}")
    return 0


def cmd_dashboard(args) -> int:
    """Start the web dashboard, with real start/stop control of the engine."""
    from ts.state import StateStore
    from ts.web.context import build_context
    from ts.web.server import DashboardServer, ServerConfig

    symbols = load_universe(args.universe)
    store = StateStore(ROOT / "state")

    # build_context attaches a live engine factory: the dashboard's START and
    # STOP buttons create a real TradingEngine / MultiAgentSupervisor in this
    # process rather than reporting a placeholder status.
    context, holder = build_context(
        state=store,
        symbols=symbols,
        interval=args.interval,
        range_=args.range,
        host=args.host,
        port=args.port,
        open_browser=not args.no_browser,
    )

    server = DashboardServer(context, ServerConfig(
        host=args.host, port=args.port, open_browser=not args.no_browser))

    store.log("dashboard_started", port=args.port, universe=len(symbols),
              interval=args.interval, range_=args.range)
    print(f"[OK] dashboard -> http://{args.host}:{args.port}/")
    print(f"     universe: {len(symbols)} symbols, {args.interval} bars, {args.range} history")
    print("     press Ctrl+C to stop")
    try:
        server.start(blocking=True)
    except KeyboardInterrupt:
        print("\n[*] stopping")
        server.stop()
    return 0


def cmd_report(args) -> int:
    from ts.state import StateStore

    store = StateStore(ROOT / "state")
    bt = store.read_backtest()
    if bt:
        m = bt.get("metrics", {})
        print("=" * 60)
        print("LAST BACKTEST")
        print("=" * 60)
        for key in ("total_return", "cagr", "max_drawdown", "sharpe", "expectancy_R",
                    "win_rate", "n_trades", "profit_factor"):
            print(f"  {key:<18} {m.get(key)}")
    else:
        print("[!] no backtest in state")
    live = store.live_metrics()
    print(f"\n  live trades        {live.get('n_trades', 0)}")
    print(f"  live expectancy    {live.get('expectancy_R', 0)}R")
    print(f"  live total P&L     {live.get('total_pnl', 0)}")
    return 0


def cmd_cache(args) -> int:
    from ts.datafeed import YahooFeed

    feed = YahooFeed(cache_dir=str(ROOT / "data"))
    if args.cache_command == "clear":
        removed = feed.clear_cache()
        print(f"[OK] removed {removed} cached file(s)")
    else:
        files = sorted(feed.cache_dir.glob("*.csv"))
        print(f"[i] {len(files)} cached file(s) in {feed.cache_dir}")
        for path in files[: args.limit]:
            print("   ", path.name)
    return 0


def cmd_paper(args) -> int:
    """Simulated live trading: real data, real signal logic, simulated fills."""
    from ts.broker.paper import PaperBroker, PaperBrokerConfig
    from ts.datafeed import DataFeed, YahooFeed
    from ts.engine import EngineConfig, TradingEngine
    from ts.risk import RiskConfig
    from ts.state import StateStore
    from ts.strategy import StrategyParams

    symbols = load_universe(args.universe)
    feed = YahooFeed(cache_dir=str(ROOT / "data"))
    store = StateStore(ROOT / "state")

    broker = PaperBroker(
        price_fn=lambda sym: float(feed.quote(sym, args.interval).price),
        config=PaperBrokerConfig(
            starting_cash=args.capital,
            slippage_bps=args.slippage,
            commission_per_share=args.commission,
        ),
    )

    engine = TradingEngine(
        broker=broker,
        data_fetcher=fetcher,
        strategy_params=StrategyParams(allow_shorts=args.shorts),
        risk_config=RiskConfig(
            risk_per_trade=args.risk,
            max_positions=args.max_positions,
            max_total_risk=args.max_risk,
            daily_loss_limit=args.daily_limit,
            max_drawdown_halt=args.drawdown_halt,
        ),
        config=EngineConfig(
            poll_seconds=args.interval_seconds,
            interval=args.interval,
            dry_run=args.dry_run,
            use_market_gate=not args.no_gate,
            market_gate_symbol=args.gate,
        ),
        state_store=store,
        universe=symbols,
        kill_switch=lambda: (store.root / "KILL").exists(),
    )

    print(f"[*] paper trading {len(symbols)} symbols, capital {args.capital:,.0f}")
    if args.dry_run:
        print("[*] DRY RUN -- signals are computed and logged, no orders are sent")
    engine.run_forever(max_ticks=args.ticks)
    return 0


def cmd_live(args) -> int:
    print("[!] live trading requires an explicit --i-understand flag and broker keys.")
    print("    nothing was traded.")
    return 1


# ------------------------------------------------------------------------ main


def load_agent_universe(path: str | None) -> list[str]:
    """Read symbols for the multi-asset agent system."""
    default = str(ROOT / "config" / "instruments.txt")
    symbols = load_universe(path or default)
    return symbols


def cmd_agents(args) -> int:
    """Run the multi-agent system over indices, forex, and crypto."""
    from ts.agents.supervisor import MultiAgentSupervisor, SupervisorConfig
    from ts.agents.feed import MarketFeed
    from ts.state import StateStore

    symbols = load_agent_universe(args.universe)
    store = StateStore(ROOT / "state")

    print(f"[*] multi-agent system: {len(symbols)} instruments")
    for sym in symbols:
        from ts.agents.instruments import resolve
        try:
            inst = resolve(sym)
            print(f"      {sym:<10} {inst.asset_class.value:<7} "
                  f"contract={inst.contract_size:g} tick={inst.tick_size:g} "
                  f"two_sided={inst.is_two_sided}")
        except Exception as exc:  # noqa: BLE001
            print(f"      {sym:<10} (unresolved: {exc})")

    feed = MarketFeed(interval=args.interval, range_=args.range)
    supervisor = MultiAgentSupervisor(
        feed=feed, symbols=symbols,
        config=SupervisorConfig(
            cycle_seconds=args.cycle_seconds,
            interval=args.interval,
            range_=args.range,
            dry_run=not args.live,
            min_expectancy=args.min_expectancy,
            min_confidence=args.min_confidence,
            kelly_fraction=args.kelly_fraction,
            max_risk_per_trade=args.risk,
            bankroll=args.capital,
            atr_stop_mult=args.stop,
            atr_target_mult=args.target,
        ),
        kill_switch=(lambda reason, s=store: s.set_killed(True, reason))
        if args.kill_on_risk else None,
        state_store=store,
    )

    if args.cycles:
        results = supervisor.run_cycles(args.cycles)
        last = results[-1] if results else {"plans": []}
        print(f"\n[OK] completed {len(results)} cycle(s)")
        plans = last.get("plans", [])
        if plans:
            print("\n[plan]")
            for plan in plans:
                if not plan.get("guardian_approved"):
                    verdict = f"-> NOT SIZED ({plan.get('guardian_reason', 'edge detector')[:44]})"
                elif (plan.get("quantity") or 0) > 0:
                    verdict = f"-> EXECUTED (risk {plan.get('kelly_risk', 0):.0f})"
                else:
                    verdict = "-> STANDING BY"
                print(f"      {plan['symbol']:<9} {plan.get('side', '?'):<6} "
                      f"exp={plan.get('expectancy_r', 0):+.3f}R "
                      f"payoff={plan.get('payoff', 0):.2f} "
                      f"p={plan.get('p_win', 0):.3f} "
                      f"grade={plan.get('edge_grade', '?')} {verdict}")
        else:
            print("      no opportunities passed the gates this cycle")
        snapshot = supervisor.snapshot()
        print("\n[agent status]")
        for name, status in snapshot["agents"].items():
            err = ("ERR: " + status["last_error"]) if status["last_error"] else "ok"
            print(f"      {name:<14} cycles={status['cycles']:<4} "
                  f"{status['last_duration_ms']:>8.1f}ms  {err}")
        print(f"\n[risk] {snapshot['risk']}")
        exec_summary = snapshot["execution"]["summary"]
        print(f"[execution] open={exec_summary['open']} closed={exec_summary['closed']} "
              f"win_rate={exec_summary['win_rate']:.2%} "
              f"dry_run={exec_summary['dry_run']}")
        return 0

    print("[*] starting continuous loop (Ctrl+C to stop)")
    supervisor.start()
    try:
        while True:
            time.sleep(5)
    except KeyboardInterrupt:
        print("\n[*] stopping")
        supervisor.stop()
    return 0


def cmd_walkforward(args) -> int:
    """Out-of-sample validation: optimise in-sample, trade out-of-sample."""
    import time as _time

    from ts.backtest import BacktestConfig, Backtester
    from ts.datafeed import DataFeed, YahooFeed
    from ts.metrics import monte_carlo_trades
    from ts.risk import RiskConfig
    from ts.state import StateStore
    from ts.strategy import StrategyParams
    from ts.state import dumps
    from ts.walkforward import WalkForward, WalkForwardConfig

    symbols = load_universe(args.universe)
    feed = YahooFeed(cache_dir=str(ROOT / "data"))
    print(f"[*] fetching {len(symbols)} symbols ({args.range}) ...")
    data = DataFeed(feed=feed, interval=args.interval, range_=args.range).pull(symbols)
    if not data:
        print("[!] no data retrieved")
        return 1
    print(f"[*] {len(data)} symbols, longest {max(len(f) for f in data.values())} bars")

    if args.reference:
        print("[*] fitting reference parameters on the FULL sample (for contrast only)")
        first_bar = min(frame.index[0] for frame in data.values())
        last_bar = max(frame.index[-1] for frame in data.values())
        ref_params, _, _ = WalkForward(
            data, WalkForwardConfig(n_folds=1, grid=args_grid()), RiskConfig(),
            StrategyParams(),
        )._optimise(first_bar, last_bar)
        full = Backtester(
            strategy_params=ref_params,
            risk_config=RiskConfig(risk_per_trade=args.risk),
            config=BacktestConfig(initial_capital=args.capital),
            market_gate=data.get(args.gate),
        ).run(data)
        print(f"\n[reference] in-sample-optimised, FULL sample:")
        print(f"            expectancy {full.metrics.get('expectancy_R')}R  "
              f"trades {full.metrics.get('n_trades')}  "
              f"CAGR {full.metrics.get('cagr', 0):.2%}  "
              f"maxDD {full.metrics.get('max_drawdown', 0):.2%}")
        print("            (this is the number a naive backtest would show -- it is "
              "optimistic by construction)")

    grid = {
        "entry_lookback": [int(x) for x in args.entry_lookback.split(",")],
        "adx_min": [float(x) for x in args.adx.split(",")],
        "atr_stop_mult": [float(x) for x in args.stop.split(",")],
        "atr_trail_mult": [float(x) for x in args.trail.split(",")],
    }
    combos = (len(grid["entry_lookback"]) * len(grid["adx_min"])
              * len(grid["atr_stop_mult"]) * len(grid["atr_trail_mult"]))
    print(f"[*] grid: {combos} combinations per fold, "
          f"{args.folds} folds -> up to {combos * args.folds} backtests")
    print("[*] this takes a while ...\n")

    started = _time.time()
    wf = WalkForward(
        data=data,
        config=WalkForwardConfig(
            n_folds=args.folds,
            in_sample_ratio=args.is_ratio,
            purge_bars=args.purge,
            objective=args.objective,
            rolling=not args.anchored,
            grid=grid,
            slippage_bps=args.slippage,
        ),
        risk_config=RiskConfig(
            risk_per_trade=args.risk,
            max_positions=args.max_positions,
            max_total_risk=args.max_risk,
            daily_loss_limit=args.daily_limit,
            max_drawdown_halt=args.drawdown_halt,
        ),
        base_params=StrategyParams(allow_shorts=args.shorts),
        market_gate=data.get(args.gate),
    )
    try:
        result = wf.run(initial_capital=args.capital)
    except ValueError as exc:
        if "refusing to validate" in str(exc):
            print(f"\n[!] {exc}\n")
            print("    Validation refused. A grid search on this little data always finds")
            print("    something that looks brilliant and means nothing -- that is the single")
            print("    most common way a backtest lies.\n")
            print("    What you CAN do with 1m bars:")
            print("      * trade it:      py run.py agents --interval 1m --range 7d")
            print("      * paper it:     py run.py paper --interval 1m --ticks 10")
            print("      * validate on daily bars:  py run.py walkforward --interval 1d --range 2y")
            return 2
        raise
    print()
    print(result.summary())

    print("\n  PARAMETER STABILITY (high spread = the optimiser is chasing noise)")
    stability = result.parameter_stability()
    for key, stats in stability.items():
        if stats.get("numeric"):
            print(f"    {key:<16} values={stats['values']}  spread={stats['spread']}")
        else:
            print(f"    {key:<16} {stats['values']}")

    if result.warnings:
        print("\n  WARNINGS")
        for warning in result.warnings:
            print(f"    - {warning}")

    mc = monte_carlo_trades(result.oos_trades,
                            risk_per_trade=args.risk, n_runs=args.monte_carlo)
    if mc:
        print(f"\n  MONTE CARLO on out-of-sample trades ({mc['n_trades_per_run']} trades/run)")
        print(f"    median ending {mc['p50_ending']}x   "
              f"p5 {mc['p5_ending']}x   p95 {mc['p95_ending']}x")
        print(f"    median max drawdown {mc['p50_max_dd']:.1%}   "
              f"p95 {mc['p95_max_dd']:.1%}")
        print(f"    probability of finishing positive {mc['prob_finish_positive']:.1%}")

    store = StateStore(ROOT / "state")
    store.save_snapshot({
        "mode": "WALKFORWARD",
        "walkforward": {
            "saved_at": datetime.now(UTC).isoformat(),
            "metrics": result.metrics,
            "folds": [f.to_dict() for f in result.folds],
            "stability": stability,
            "warnings": result.warnings,
            "monte_carlo": mc,
            "seconds": round(_time.time() - started, 1),
        },
    })
    cache = ROOT / "state" / "walkforward.json"
    cache.write_text(dumps({
        "metrics": result.metrics,
        "folds": [f.to_dict() for f in result.folds],
        "stability": stability,
        "warnings": result.warnings,
        "monte_carlo": mc,
        "equity": [
            {"t": str(i), "equity": round(float(r["equity"]), 2)}
            for i, r in result.oos_equity.iterrows()
        ],
        "saved_at": datetime.now(UTC).isoformat(),
    }), encoding="utf-8")
    print(f"\n[OK] completed in {_time.time() - started:.0f}s; results saved to "
          f"{cache}")
    return 0


def cmd_talk(args) -> int:
    """Run the two-way Telegram command channel.

    Long-polls Telegram, parses each message as a command and applies it to the
    trading system. Requires TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID.
    """
    import os

    from ts.commands import CommandChannel, CommandContext, TelegramPoller
    from ts.notify import Notifier, NotifyConfig
    from ts.state import StateStore

    token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "")
    if not token or not chat_id:
        print("[!] TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID are required.")
        print("    1. message @BotFather on Telegram -> /newbot -> copy the token")
        print("    2. message @userinfobot -> copy your numeric chat id")
        print("    3. set them, e.g.  $env:TELEGRAM_BOT_TOKEN = '...'")
        return 1
    if not args.allow_any_chat and str(args.allow_chat or chat_id) != str(chat_id):
        pass

    store = StateStore(ROOT / "state")
    notifier = Notifier(NotifyConfig())
    if not notifier.cfg.telegram_enabled:
        print("[!] the notifier did not pick up the credentials")
        return 1

    from ts.settings import SettingsStore

    settings_store = SettingsStore(store.root)

    def _engine():
        from ts.web.context import build_context
        return None

    def get_status():
        snap = store.read_snapshot() or {}
        snap["engine_running"] = bool(snap.get("cycles", 0)) or bool(
            snap.get("positions"))
        return snap

    def get_positions():
        return store.read_snapshot().get("positions", [])

    def get_risk():
        r = settings_store.load().risk
        out = {k: getattr(r, k) for k in
               ("risk_per_trade", "max_total_risk", "daily_loss_limit",
                "max_drawdown_halt", "max_fraction")}
        out["kelly_fraction"] = settings_store.load().strategy.kelly_fraction
        out["risk"] = r.risk_per_trade
        out["daily"] = r.daily_loss_limit
        out["dd"] = r.max_drawdown_halt
        return out

    def get_forward():
        from ts.forward import ForwardJournal

        return ForwardJournal(store.root).summary()

    def set_risk(key, value):
        strategy_keys = {"kelly": "kelly_fraction", "expectancy": "min_expectancy",
                         "confidence": "min_confidence"}
        if key in strategy_keys:
            return bool(settings_store.update("strategy",
                                              {strategy_keys[key]: float(value)}))
        risk_field = {"risk": "risk_per_trade", "max_risk": "max_total_risk",
                      "daily": "daily_loss_limit",
                      "dd": "max_drawdown_halt"}.get(key)
        if risk_field is None:
            return False
        return bool(settings_store.update("risk", {risk_field: float(value)}))

    def kill(reason="telegram command"):
        store.set_killed(True, reason)
        return True

    def resume():
        store.set_killed(False)
        return True

    def pause():
        return True     # the engine reads the KILL file; pause is advisory here

    def start():
        return True

    def run_cli(command, cli_args):
        from ts.web.console import ConsoleRunner

        try:
            code, run = ConsoleRunner(log_path=store.root / "console.log").start(
                command, cli_args or {})
        except Exception as exc:  # noqa: BLE001
            return {"error": str(exc)}
        return {"ok": code == 202, "started": code == 202}

    context = CommandContext(
        get_status=get_status, get_positions=get_positions, get_risk=get_risk,
        get_forward=get_forward, set_risk=set_risk, kill=kill, resume=resume,
        pause=pause, start=start, run_cli=run_cli,
    )
    channel = CommandChannel(notifier, context, allowed_chat_id=chat_id)
    poller = TelegramPoller(channel, notifier)

    print(f"[OK] command channel listening on Telegram chat {chat_id}")
    print("     send /help to your bot to list the commands")
    notifier.info("Command channel online",
                  "Send /help to see what you can do.",
                  chat_id=chat_id, _kind="talk_start")
    try:
        poller.run_forever()
    except KeyboardInterrupt:
        print("\n[*] command channel stopped")
        poller.stop()
    return 0


def args_grid() -> dict:
    return {"entry_lookback": [15, 20, 30, 40, 55]}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="tradingsys",
        description="Autonomous trend-following trading system",
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("backtest", help="run the strategy over history")
    p.add_argument("--universe", help="file of symbols, one per line")
    p.add_argument("--capital", type=float, default=100_000.0)
    p.add_argument("--interval", default="1m", help="bar interval: 1m, 5m, 15m, 1h, 1d")
    p.add_argument("--range", default="7d",
                   help="history range: 7d, 1mo, 1y, 5y, max (1m is capped at 7d)")
    p.add_argument("--risk", type=float, default=0.01, help="fraction risked per trade")
    p.add_argument("--entry-lookback", type=int, default=20)
    p.add_argument("--adx", type=float, default=20.0)
    p.add_argument("--stop", type=float, default=2.0, help="initial stop, ATR multiples")
    p.add_argument("--trail", type=float, default=3.0, help="trailing stop, ATR multiples")
    p.add_argument("--shorts", action="store_true")
    p.add_argument("--max-positions", type=int, default=8)
    p.add_argument("--max-risk", type=float, default=0.06)
    p.add_argument("--daily-limit", type=float, default=0.03)
    p.add_argument("--drawdown-halt", type=float, default=0.15)
    p.add_argument("--slippage", type=float, default=5.0, help="bps")
    p.add_argument("--commission", type=float, default=0.005,
                   help="per share, or bps when --interval is 1m")
    p.add_argument("--gate", default="SPY", help="regime benchmark symbol")
    p.set_defaults(func=cmd_backtest)

    p = sub.add_parser("dashboard", help="start the web dashboard")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8642)
    p.add_argument("--universe", help="file of symbols, one per line")
    p.add_argument("--no-browser", action="store_true")
    p.add_argument("--interval", default="1m", help="bar interval: 1m, 5m, 15m, 1h, 1d")
    p.add_argument("--range", default="7d", help="history range (1m is capped at 7d by Yahoo)")
    p.set_defaults(func=cmd_dashboard)

    p = sub.add_parser("report", help="print stored metrics")
    p.set_defaults(func=cmd_report)

    p = sub.add_parser("cache", help="manage the local bar cache")
    cache_sub = p.add_subparsers(dest="cache_command", required=True)
    clear = cache_sub.add_parser("clear")
    clear.set_defaults(cache_command="clear")
    ls = cache_sub.add_parser("list")
    ls.add_argument("--limit", type=int, default=50)
    ls.set_defaults(cache_command="list")
    p.set_defaults(func=cmd_cache)

    p = sub.add_parser("paper", help="simulated live trading")
    p.add_argument("--universe", help="file of symbols, one per line")
    p.add_argument("--capital", type=float, default=100_000.0)
    p.add_argument("--interval-seconds", type=float, default=60.0,
                   help="seconds between engine ticks")
    p.add_argument("--ticks", type=int, default=0, help="stop after N ticks (0 = forever)")
    p.add_argument("--dry-run", action="store_true",
                   help="compute and log signals, never submit orders")
    p.add_argument("--risk", type=float, default=0.01)
    p.add_argument("--max-positions", type=int, default=8)
    p.add_argument("--max-risk", type=float, default=0.06)
    p.add_argument("--daily-limit", type=float, default=0.03)
    p.add_argument("--drawdown-halt", type=float, default=0.15)
    p.add_argument("--slippage", type=float, default=5.0)
    p.add_argument("--commission", type=float, default=0.005)
    p.add_argument("--shorts", action="store_true")
    p.add_argument("--gate", default="SPY")
    p.add_argument("--no-gate", action="store_true", help="disable the market regime filter")
    p.set_defaults(func=cmd_paper)

    p = sub.add_parser("live", help="real-money trading (requires explicit opt-in)")
    p.add_argument("--i-understand", action="store_true")
    p.set_defaults(func=cmd_live)

    p = sub.add_parser("talk", help="run the two-way Telegram command channel")
    p.add_argument("--allow-chat", default=None,
                   help="chat id permitted to send commands (defaults to TELEGRAM_CHAT_ID)")
    p.add_argument("--allow-any-chat", action="store_true",
                   help="DANGEROUS: accept commands from any chat id")
    p.set_defaults(func=cmd_talk)

    p = sub.add_parser("agents", help="run the multi-agent system (indices, forex, crypto)")
    p.add_argument("--interval", default="1m",
                       help="bar interval: 1m, 5m, 15m, 1h, 1d")
    p.add_argument("--range", default="7d",
                       help="history range (1m is capped at 7d)")
    p.add_argument("--capital", type=float, default=100_000.0,
                   help="bankroll the Kelly sizer budgets against")
    p.add_argument("--universe", help="symbol file (default: config/instruments.txt)")
    p.add_argument("--cycles", type=int, default=1,
                   help="run N cycles and exit (0 = run forever)")
    p.add_argument("--cycle-seconds", type=float, default=60.0)
    p.add_argument("--dry-run", action="store_true", default=True,
                   help="plan and log, never submit (default)")
    p.add_argument("--live", action="store_true",
                   help="submit real orders through the broker")
    p.add_argument("--min-expectancy", type=float, default=0.20)
    p.add_argument("--min-confidence", type=float, default=0.65,
                   help="minimum posterior probability that the direction is right")
    p.add_argument("--kelly-fraction", type=float, default=0.25)
    p.add_argument("--risk", type=float, default=0.01, help="max risk per trade")
    p.add_argument("--stop", type=float, default=2.0, help="initial stop, ATR multiples")
    p.add_argument("--target", type=float, default=4.0, help="target, ATR multiples")
    p.add_argument("--kill-on-risk", action="store_true",
                   help="engage the kill switch when a risk limit is breached")
    p.set_defaults(func=cmd_agents)

    p = sub.add_parser("walkforward", help="out-of-sample validation (optimise, then trade unseen data)")
    wf = p.add_argument_group("walk-forward")
    wf.add_argument("--universe", help="file of symbols, one per line")
    wf.add_argument("--capital", type=float, default=100_000.0)
    wf.add_argument("--interval", default="1m",
                   help="bar interval: 1m, 5m, 15m, 1h, 1d")
    wf.add_argument("--range", default="7d", help="history range: 7d, 1mo, 1y, 5y, max")
    wf.add_argument("--folds", type=int, default=5)
    wf.add_argument("--is-ratio", type=float, default=0.7,
                    help="fraction of each fold used for in-sample fitting")
    wf.add_argument("--purge", type=int, default=25,
                    help="bars dropped between in-sample and out-of-sample to stop indicator leakage")
    wf.add_argument("--objective", default="sharpe",
                    choices=["sharpe", "expectancy", "cagr"])
    wf.add_argument("--anchored", action="store_true",
                    help="grow the in-sample window instead of rolling it")
    wf.add_argument("--entry-lookback", default="15,20,30,40,55")
    wf.add_argument("--adx", default="15,20,25,30")
    wf.add_argument("--stop", default="1.5,2,2.5,3")
    wf.add_argument("--trail", default="2,3,4")
    wf.add_argument("--risk", type=float, default=0.01)
    wf.add_argument("--max-positions", type=int, default=8)
    wf.add_argument("--max-risk", type=float, default=0.06)
    wf.add_argument("--daily-limit", type=float, default=0.03)
    wf.add_argument("--drawdown-halt", type=float, default=0.15)
    wf.add_argument("--slippage", type=float, default=5.0)
    wf.add_argument("--gate", default="SPY")
    wf.add_argument("--shorts", action="store_true")
    wf.add_argument("--monte-carlo", type=int, default=2000)
    wf.add_argument("--reference", action="store_true",
                    help="also fit on the full sample, to show how optimistic that is")
    p.set_defaults(func=cmd_walkforward)

    return parser


def main(argv: list[str] | None = None) -> int:
    _make_console_safe()
    parser = build_parser()
    args = parser.parse_args(argv)
    setup_logging(args.verbose)
    sys.path.insert(0, str(ROOT))
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
