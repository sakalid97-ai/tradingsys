"""Point cmd_dashboard at the real engine factory (context.build_context)."""

from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
cli = ROOT / "ts" / "cli.py"
text = cli.read_text(encoding="utf-8")

if "build_context" in text:
    print("[skip] cli.py already uses build_context")
    sys.exit(0)

old = """    context = AppContext(state=store, backtester_factory=backtester_factory,
                         data_provider=data_provider, kill_file=store.root / "KILL")"""
new = """    # A real, in-process engine so the dashboard's START button works.
    from ts.web.context import build_context

    context, holder = build_context(
        state=store,
        symbols=symbols,
        interval=args.interval,
        range_=args.range,
        host=args.host,
        port=args.port,
        open_browser=not args.no_browser,
    )"""

if old not in text:
    print("[FAIL] cli.py: AppContext construction anchor not found")
    sys.exit(1)

text = text.replace(old, new, 1)
# the old local definitions are now unused
text = text.replace("""    def fetcher(symbol, range_, interval):
        return DataFeed(feed=feed, interval=interval, range_=range_).pull([symbol]).get(symbol)

""", "", 1)
cli.write_text(text, encoding="utf-8")
print("[ok]   cli.py: dashboard now uses build_context with a real engine")
sys.exit(0)
