"""End-to-end: a mixed crypto/equity/forex universe through the routing scanner."""

from __future__ import annotations

import io
import sys
import time
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from ts.agents.feed import MarketFeed
from ts.agents.scanner import ScannerAgent
from ts.agents.bus import MessageBus, Topic
from ts.feedrouter import RoutingFeed, DataSourceConfig, SOURCE_NOTES

print("=== sources ===")
for name, note in SOURCE_NOTES.items():
    print(f"  {name:<8} {note[:80]}")

print("\n=== routing feed, 1m, mixed universe ===")
feed = MarketFeed(
    feed=RoutingFeed(DataSourceConfig(source="router", interval="1m", range_="7d")),
    interval="1m",
    range_="7d",
)
symbols = ["BTCUSD", "ETHUSD", "SOLUSD", "XRPUSD", "AAPL", "SPX500", "EURUSD", "GBPUSD"]

bus = MessageBus()
published: list[dict] = []
bus.subscribe(Topic.MARKET_DATA.value, lambda m: published.append(m.payload))

scanner = ScannerAgent(bus, feed, symbols, max_workers=8)
t0 = time.perf_counter()
scanner.run()
elapsed = time.perf_counter() - t0

print(f"  wall clock      : {elapsed:.1f}s")
print(f"  symbols scanned : {len(scanner.report.scanned)}/{len(symbols)}")
print(f"  fresh           : {len(scanner.report.fresh)}")
print(f"  unavailable     : {scanner.unavailable}")
print(f"  failed          : {scanner.report.failed}")
print(f"  published       : {len(published)}")

if scanner.report.scanned:
    for payload in published:
        src = payload.get("source", "?")
        print(f"    {payload['symbol']:<8} {payload['last']:>12,.2f}  source={src}  "
              f"tradable={payload['tradable']}")

print("\n=== signal generation on the routed data ===")
from ts.strategy import Strategy, StrategyParams

for symbol in scanner.report.scanned[:4]:
    snap = feed.fetch(symbol)
    if snap is None:
        continue
    sig = Strategy(StrategyParams(min_history=150)).compute(snap.frame)
    print(f"  {symbol:<8} bars={len(snap.frame):>5}  entry_long={int(sig['entry_long'].sum()):>3}  "
          f"atr={float(sig.atr.dropna().iloc[-1]):.4f}")

print("\n=== conclusion ===")
print(f"  {len(scanner.unavailable)} symbol(s) have no free 1m source:")
for s in scanner.unavailable:
    print(f"    - {s}")
print("  -> they were dropped, and the rest still scanned. No error.")
