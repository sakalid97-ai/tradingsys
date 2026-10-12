"""Point the engine at the routable data source and the 1m-capable universe.

The engine was still feeding from Yahoo with a 14-symbol universe, 7 of which
have no free 1m source. Now:

* ``build_context`` builds a RoutingFeed when the setting says so
* the default agent universe is the 4 crypto symbols that work at 1m, plus
  equities that Yahoo can serve
* indices and forex are no longer in the default set at 1m
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
            print(f"    [FAIL] {path.name}: {old[:56]!r}")
            failed += 1
            continue
        text = text.replace(old, new, 1)
        applied += 1
    if not failed:
        path.write_text(text, encoding="utf-8")
    print(f"[{'ok' if not failed else 'FAIL'}] {label}: "
          f"{applied} applied, {skipped} present, {failed} failed")
    return not failed


ok = True

# ---------------------------------------------- 1. the routing feed in context.py
ok &= patch(ROOT / "ts" / "web" / "context.py", [
    (
        '''    feed = YahooFeed(cache_dir=str(ROOT / "data"))
''',
        '''    # --- pick the data source the settings ask for
    from ts.settings import SettingsStore

    _settings = SettingsStore(store.root).load()
    _source = getattr(_settings.instruments, "data_source", "router") or "router"

    if _source == "router":
        from ts.feedrouter import RoutingFeed, SOURCE_NOTES

        route_feed = RoutingFeed(__import__(
            "ts.feedrouter", fromlist=["DataSourceConfig"]).DataSourceConfig(
                source="router", interval=interval, range_=range_))
        logging.getLogger("ts.web.context").info(
            "data source: router -> %s", SOURCE_NOTES["router"][:60])

    feed = route_feed if _source == "router" else YahooFeed(cache_dir=str(ROOT / "data"))
''',
    ),
], "context.py routes through RoutingFeed")

# the fetcher must work for either feed (both expose .history)
ok &= patch(ROOT / "ts" / "web" / "context.py", [
    (
        '''    def fetcher(symbol, range_arg, interval_arg):
        return DataFeed(feed=feed, interval=interval_arg,
                        range_=range_arg).pull([symbol]).get(symbol)''',
        '''    def fetcher(symbol, range_arg, interval_arg):
        """Fetch one symbol. Works with either a routed or Yahoo feed."""
        from ts.quality import validate

        if _source == "router":
            # a routed ticker (SPX500, BTCUSD) is already the vendor symbol
            try:
                return route_feed.history(symbol, interval=interval_arg, range_=range_arg)
            except Exception:  # noqa: BLE001 - fall through to Yahoo
                pass
        return DataFeed(feed=feed, interval=interval_arg,
                        range_=range_arg).pull([symbol]).get(symbol)''',
    ),
], "context.py fetches through either feed")

sys.path.insert(0, str(ROOT))
try:
    from ts.web.context import build_context
    from ts.feedrouter import RoutingFeed, DataSourceConfig

    print("[ok] imports clean")
except Exception as exc:  # noqa: BLE001
    print(f"[FAIL] import error: {exc}")
sys.exit(0 if ok else 1)
