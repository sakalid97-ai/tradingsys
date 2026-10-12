"""Clean up cmd_dashboard: drop dead code and add the engine-control args."""

from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[1]
cli = ROOT / "ts" / "cli.py"
text = cli.read_text(encoding="utf-8")

start = text.index("def cmd_dashboard(args) -> int:")
end = text.index("def cmd_report(args) -> int:")
new_func = '''def cmd_dashboard(args) -> int:
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
        print("\\n[*] stopping")
        server.stop()
    return 0


'''
text = text[:start] + new_func + text[end:]

# --- dashboard subparser needs --interval / --range
old_parser = '''    p.add_argument("--no-browser", action="store_true")
    p.set_defaults(func=cmd_dashboard)'''
new_parser = '''    p.add_argument("--no-browser", action="store_true")
    p.add_argument("--interval", default="1d", help="bar interval for the live engine")
    p.add_argument("--range", default="1y", help="history range for the live engine")
    p.set_defaults(func=cmd_dashboard)'''
if old_parser in text:
    text = text.replace(old_parser, new_parser, 1)
    print("[ok]   dashboard subparser: --interval and --range added")
else:
    print("[warn] dashboard subparser anchor not found")

cli.write_text(text, encoding="utf-8")
print("[ok]   cmd_dashboard rewritten")

sys.path.insert(0, str(ROOT))
try:
    from ts.cli import main  # noqa: F401
    print("[ok]   cli.py imports cleanly")
except Exception as exc:  # noqa: BLE001
    print(f"[FAIL] import error: {exc}")
sys.exit(0)
