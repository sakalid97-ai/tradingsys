"""A source returning one bar must not be treated as success.

Alpaca's stock endpoint with these credentials returns a single bar. The router
saw a non-empty frame, declared victory, and served it -- so a universe with
1256 bars of Yahoo data available got 1 bar from Alpaca instead, and the
backtest collapsed to one trade.

Fix: a source that returns fewer than ``min_bars`` is a failure, and the router
falls through to the next one. The default floor is 200 because the strategy's
200-period regime filter cannot compute on less than that anyway.
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

# ---- 1. RouterFeed: add a min-bars floor
ok &= patch(ROOT / "ts" / "altdata.py", [
    (
        '''    binance: BinanceKlines | None = None
    alpaca: AlpacaMarketData | None = None
    yahoo: object | None = None
    cache_dir: Path | None = None

    def __post_init__(self) -> None:
        self.binance = self.binance or BinanceKlines()
        self.alpaca = self.alpaca or AlpacaMarketData()''',
        '''    binance: BinanceKlines | None = None
    alpaca: AlpacaMarketData | None = None
    yahoo: object | None = None
    cache_dir: Path | None = None
    # A source returning fewer bars than this is treated as a failure, not as
    # success. Below ~200 bars the 200-period regime filter cannot compute, so
    # such a frame is useless even though it is non-empty.
    min_bars: int = 200

    def __post_init__(self) -> None:
        self.binance = self.binance or BinanceKlines()
        self.alpaca = self.alpaca or AlpacaMarketData()''',
    ),
    (
        '''            if frame is None or frame.empty:
                errors.append(f"{source.name}: empty")
                continue
            return frame''',
        '''            if frame is None or frame.empty:
                errors.append(f"{source.name}: empty")
                continue
            if len(frame) < self.min_bars:
                errors.append(f"{source.name}: only {len(frame)} bars (need {self.min_bars})")
                continue
            return frame''',
    ),
], "RouterFeed enforces a minimum-viable-data floor")

sys.path.insert(0, str(ROOT))
from ts.feedrouter import RoutingFeed, DataSourceConfig

feed = RoutingFeed(DataSourceConfig(source="router", interval="1d", range_="5y"))
for sym in ("BTCUSD", "AAPL", "MSFT", "ETHUSD"):
    try:
        df = feed.history(sym, interval="1d", range_="5y")
        print(f"  {sym:<8} {len(df):>5} bars  {df.index[0]:%Y-%m-%d} -> {df.index[-1]:%Y-%m-%d}")
    except Exception as exc:  # noqa: BLE001
        print(f"  {sym:<8} FAIL: {str(exc)[:70]}")
sys.exit(0 if ok else 1)
