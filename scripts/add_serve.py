"""Add a headless `serve` mode so the engine runs without a browser.

Right now the engine only exists inside the dashboard process, created when
someone presses START ENGINE. That is exactly wrong for a server: the engine
must run whether or not anyone is looking at it.

`run.py serve` runs the trading engine (and optionally the Telegram channel)
headless, writing state to disk that the dashboard reads. The dashboard becomes
a pure viewer.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

p = ROOT / "ts" / "cli.py"
text = p.read_text(encoding="utf-8")

if "def cmd_serve" in text:
    print("[skip] serve command already present")
    sys.exit(0)

func = '''def cmd_serve(args) -> int:
    """Run the engine headless (for a VPS / always-on box).

    The dashboard becomes a pure viewer: this process owns the trading loop and
    writes state to disk, so it keeps running whether or not anyone has the UI
    open. Telegram runs in-process too when configured.
    """
    from ts.broker.paper import PaperBroker, PaperBrokerConfig
    from ts.datafeed import DataFeed, YahooFeed
    from ts.engine import EngineConfig, TradingEngine
    from ts.forward import ForwardJournal
    from ts.risk import RiskConfig
    from ts.state import SettingsStore, StateStore
    from ts.strategy import StrategyParams

    store = StateStore(ROOT / "state")
    settings = SettingsStore(ROOT / "state").load()
    symbols = load_universe(args.universe)

    feed = YahooFeed(cache_dir=str(ROOT / "data"))

    def fetcher(symbol, range_arg, interval_arg):
        return DataFeed(feed=feed, interval=interval_arg,
                        range_=range_arg).pull([symbol]).get(symbol)

    broker = PaperBroker(
        price_fn=lambda sym: float(feed.quote(sym, args.interval).price),
        config=PaperBrokerConfig(starting_cash=args.capital, slippage_bps=5.0),
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
            poll_seconds=args.cycle_seconds,
            interval=args.interval,
            history_range=args.range,
            dry_run=not args.live,
            use_market_gate=not args.no_gate,
            market_gate_symbol=args.gate,
        ),
        state_store=store,
        universe=symbols,
        kill_switch=lambda: (store.root / "KILL").exists(),
        forward_journal=ForwardJournal(store.root),
    )

    journal = ForwardJournal(store.root)
    if journal.load_baseline() is None:
        journal.capture_baseline_from_state(store)

    print(f"[OK] headless engine: {len(symbols)} symbols, "
          f"{'DRY RUN' if not args.live else 'LIVE ORDERS'}")
    print(f"     cycle every {args.cycle_seconds}s, {args.interval} bars")
    print("     dashboard stays a viewer; Ctrl+C to stop")

    if not args.no_telegram:
        try:
            import os
            import threading

            if os.getenv("TELEGRAM_BOT_TOKEN") and os.getenv("TELEGRAM_CHAT_ID"):
                from ts.commands import CommandChannel, CommandContext, TelegramPoller
                from ts.notify import Notifier, NotifyConfig

                notifier = Notifier(NotifyConfig())
                context = CommandContext(
                    get_status=lambda: {**store.read_snapshot(), "engine_running": True},
                    get_positions=lambda: store.read_snapshot().get("positions", []),
                    get_forward=lambda: journal.summary(),
                    kill=lambda r: store.set_killed(True, r),
                    resume=lambda: store.set_killed(False),
                    pause=lambda: True,
                    start=lambda: True,
                )
                channel = CommandChannel(notifier, context,
                                         allowed_chat_id=os.getenv("TELEGRAM_CHAT_ID"))
                threading.Thread(target=TelegramPoller(channel, notifier).run_forever,
                                 daemon=True).start()
                print("[OK] telegram command channel running in-process")
            else:
                print("[--] telegram not configured (set TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID)")
        except Exception as exc:  # noqa: BLE001 - telegram is optional
            print(f"[!] telegram channel failed to start: {exc}")

    try:
        engine.run_forever()
    except KeyboardInterrupt:
        print("\\n[*] engine stopped")
    return 0


'''

anchor = "def build_parser() -> argparse.ArgumentParser:"
if anchor not in text:
    print("[FAIL] build_parser anchor not found")
    sys.exit(1)
text = text.replace(anchor, func + anchor, 1)

# --- the parser entry
old_sub = '''    p = sub.add_parser("talk", help="run the two-way Telegram command channel")'''
new_sub = '''    p = sub.add_parser("serve", help="run the engine headless (for a VPS)")
    p.add_argument("--universe", help="symbol file (default: config/universe.txt)")
    p.add_argument("--capital", type=float, default=100_000.0)
    p.add_argument("--interval", default="1m", help="bar interval")
    p.add_argument("--range", default="7d")
    p.add_argument("--cycle-seconds", type=float, default=300.0)
    p.add_argument("--risk", type=float, default=0.01)
    p.add_argument("--max-positions", type=int, default=8)
    p.add_argument("--max-risk", type=float, default=0.06)
    p.add_argument("--daily-limit", type=float, default=0.03)
    p.add_argument("--drawdown-halt", type=float, default=0.15)
    p.add_argument("--shorts", action="store_true")
    p.add_argument("--gate", default="SPY")
    p.add_argument("--no-gate", action="store_true")
    p.add_argument("--live", action="store_true",
                   help="submit real orders (requires broker credentials)")
    p.add_argument("--no-telegram", action="store_true")
    p.set_defaults(func=cmd_serve)

    p = sub.add_parser("talk", help="run the two-way Telegram command channel")'''
if new_sub in text:
    print("[skip] serve parser already present")
elif old_sub not in text:
    print("[FAIL] talk parser anchor not found")
else:
    text = text.replace(old_sub, new_sub, 1)
    print("[ok] serve subcommand added")

p.write_text(text, encoding="utf-8")
sys.path.insert(0, str(ROOT))
from ts.cli import main

print("[ok] cli imports clean")
