"""Verify the parallel scanner end-to-end, timed, with the local cache."""

from __future__ import annotations

import io
import sys
import time
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from ts.agents.feed import MarketFeed
from ts.agents.instruments import yahoo_symbol
from ts.parallel import parallel_map

feed = MarketFeed(feed=__import__("ts.datafeed", fromlist=["YahooFeed"]).YahooFeed(
    cache_dir=str(ROOT / "data")), interval="1m", range_="7d")

symbols = ["SPX500", "NAS100", "US30", "GER40", "JPN225",
           "EURUSD", "GBPUSD", "USDJPY", "AUDUSD", "USDCAD",
           "BTCUSD", "ETHUSD", "SOLUSD", "XRPUSD"]
tickers = [yahoo_symbol(s) for s in symbols]

print("=== scanner run (cached, 14 symbols, 8 workers) ===")
t0 = time.perf_counter()
snapshots = parallel_map(feed.fetch, symbols, max_workers=8)
elapsed = time.perf_counter() - t0

ok = [s for s in snapshots if s is not None]
failed = [sym for sym, s in zip(symbols, snapshots) if s is None]

print(f"  wall clock  : {elapsed:.2f}s")
print(f"  ok          : {len(ok)}/{len(symbols)}")
print(f"  failed      : {failed or 'none'}")
print(f"  order kept  : {[s.symbol if s else '?' for s in snapshots][:5]} ...")

if ok:
    tradable = sum(1 for s in ok if not s.is_stale)
    print(f"  fresh       : {tradable}/{len(ok)}")
    prices = {s.symbol: round(s.close(), 2) for s in ok}
    print("  prices      :", dict(list(prices.items())[:5]))

# ordering determinism: run twice, compare
first = [s.symbol if s else None for s in snapshots]
second = [s.symbol if s else None for s in parallel_map(feed.fetch, symbols, max_workers=8)]
print(f"  deterministic: {first == second}")
