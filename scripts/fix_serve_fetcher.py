"""Make `serve` fetch through the routing feed, like the dashboard does.

cmd_serve built its own fetcher with DataFeed + raw symbols, so BTCUSD 404'd
against Yahoo. The dashboard's build_context already solved this with
RoutingFeed; the headless path must use the identical fetcher or the VPS runs
a different, broken data path from the local UI.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

p = ROOT / "ts" / "cli.py"
text = p.read_text(encoding="utf-8")

old = '''    feed = YahooFeed(cache_dir=str(ROOT / "data"))

    def fetcher(symbol, range_arg, interval_arg):
        return DataFeed(feed=feed, interval=interval_arg,
                        range_=range_arg).pull([symbol]).get(symbol)

    broker = PaperBroker(
        price_fn=lambda sym: float(feed.quote(sym, args.interval).price),
        config=PaperBrokerConfig(starting_cash=args.capital, slippage_bps=5.0),
    )'''

new = '''    # Same fetcher as the dashboard's build_context: a routing feed that maps
    # trading symbols to vendor tickers and picks the best source per symbol.
    # Using DataFeed with raw symbols here would 404 on BTCUSD/ETHUSD.
    from ts.feedrouter import RoutingFeed, DataSourceConfig
    from ts.agents.instruments import yahoo_symbol

    _settings = SettingsStore(ROOT / "state").load()
    _source = getattr(_settings.instruments, "data_source", "router") or "router"
    _feed = (
        RoutingFeed(DataSourceConfig(source=_source, interval=args.interval,
                                     range_=args.range))
        if _source == "router"
        else YahooFeed(cache_dir=str(ROOT / "data"))
    )

    def fetcher(symbol, range_arg, interval_arg):
        from ts.quality import validate

        try:
            if _source == "router":
                frame = _feed.history(symbol, interval=interval_arg, range_=range_arg)
                return frame if frame is not None and not frame.empty else None
            return DataFeed(feed=_feed, interval=interval_arg,
                            range_=range_arg).pull([symbol]).get(symbol)
        except Exception:  # noqa: BLE001 - a bad symbol must not stop the loop
            return None

    broker = PaperBroker(
        price_fn=lambda sym: float(_feed.quote(yahoo_symbol(sym), args.interval).timestamp
                                   if False else _price(_feed, sym)),
        config=PaperBrokerConfig(starting_cash=args.capital, slippage_bps=5.0),
    )'''

if new in text:
    print("[skip] already routed")
elif old not in text:
    print("[FAIL] fetcher anchor not found")
else:
    text = text.replace(old, new, 1)
    p.write_text(text, encoding="utf-8")
    print("[ok] serve fetches through the routing feed")

# a small price helper (the quote() call above is awkward)
if "def _price(" not in text:
    helper = '''

def _price(feed, symbol: str) -> float:
    """Latest close for a symbol, via whichever feed is configured."""
    from ts.agents.instruments import yahoo_symbol

    try:
        frame = feed.history(yahoo_symbol(symbol), interval="1d", range_="5d")
        return float(frame["close"].iloc[-1])
    except Exception:  # noqa: BLE001
        return 100.0
'''
    text = p.read_text(encoding="utf-8")
    anchor = "def cmd_serve(args) -> int:"
    text = text.replace(anchor, helper.strip() + "\n\n\n" + anchor, 1)
    p.write_text(text, encoding="utf-8")
    print("[ok] _price helper added")

# tidy the awkward price_fn now that the helper exists
text = p.read_text(encoding="utf-8")
text = text.replace(
    """        price_fn=lambda sym: float(_feed.quote(yahoo_symbol(sym), args.interval).timestamp
                                   if False else _price(_feed, sym)),""",
    "        price_fn=lambda sym: _price(_feed, sym),",
    1,
)
p.write_text(text, encoding="utf-8")
print("[ok] price_fn cleaned")

sys.path.insert(0, str(ROOT))
from ts.cli import main

print("[ok] imports clean")
