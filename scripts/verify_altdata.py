"""Verify the alternative data sources solve Yahoo's rate-limit death."""

from __future__ import annotations

import io
import sys
import time
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from ts.altdata import BinanceKlines, DataSourceError, RouterFeed

print("=== 1. Binance public klines (no key) ===")
bn = BinanceKlines()
for sym in ("BTCUSD", "ETHUSD", "SOLUSD", "XRPUSD"):
    t0 = time.perf_counter()
    try:
        df = bn.history(sym, interval="1m", range_="7d", limit=1000)
        el = time.perf_counter() - t0
        print(f"  {sym:<8} {len(df):>5} bars  {df.index[0]:%Y-%m-%d %H:%M} -> "
              f"{df.index[-1]:%m-%d %H:%M}  {el:.2f}s  close={df['close'].iloc[-1]:,.2f}")
    except DataSourceError as exc:
        print(f"  {sym:<8} FAILED: {exc}")

print("\n=== 2. Alpaca crypto bars (no key) ===")
from ts.altdata import AlpacaMarketData

ap = AlpacaMarketData()
for sym in ("BTC/USD", "ETH/USD"):
    t0 = time.perf_counter()
    try:
        df = ap.history(sym, interval="1m", limit=500)
        print(f"  {sym:<8} {len(df):>5} bars  {time.perf_counter()-t0:.2f}s  "
              f"close={df['close'].iloc[-1]:,.2f}")
    except DataSourceError as exc:
        print(f"  {sym:<8} FAILED: {str(exc)[:80]}")

print("\n=== 3. Alpaca stock bars (key required) ===")
for sym in ("AAPL", "SPY"):
    try:
        df = ap.history(sym, interval="1m", limit=100)
        print(f"  {sym:<8} {len(df)} bars")
    except DataSourceError as exc:
        print(f"  {sym:<8} {str(exc)[:70]}")

print("\n=== 4. full universe through the router ===")
router = RouterFeed(alpaca=AlpacaMarketData())
universe = ["BTCUSD", "ETHUSD", "SOLUSD", "XRPUSD", "AAPL", "MSFT", "NVDA",
            "SPX500", "NAS100", "EURUSD", "GBPUSD"]
buckets = router.classify(universe)
for bucket, syms in buckets.items():
    print(f"  {bucket:<12} ({len(syms)}): {syms}")

print("\n  fetching the crypto + stock legs:")
t0 = time.perf_counter()
ok = failed = 0
for sym in buckets["crypto"] + buckets["stocks"]:
    try:
        df = router.history(sym, interval="1m", range_="7d", limit=500)
        ok += 1
    except DataSourceError as exc:
        failed += 1
        print(f"    {sym}: {str(exc)[:60]}")
print(f"  -> {ok} ok, {failed} failed, in {time.perf_counter()-t0:.1f}s")
print(f"  -> unsupported (no free 1m source): {buckets['unsupported']}")

