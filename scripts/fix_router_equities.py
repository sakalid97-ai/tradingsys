"""Fix the routing feed so equities still work.

The router passed only an Alpaca client, so a non-crypto symbol with no Alpaca
key had no source at all -- equities returned a single bar and the backtest
collapsed to one trade. Binance serves crypto; Yahoo must remain the fallback
for everything else.

Also make the ``limit`` argument optional per source: ``BinanceKlines`` takes it,
``YahooFeed`` does not, and a TypeError from that mismatch was being swallowed
into "no data".
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

# ---- 1. give the router a Yahoo fallback when auto-routing
ok &= patch(ROOT / "ts" / "feedrouter.py", [
    (
        """        source = self.cfg.source
        if source in ("router", "binance", "alpaca"):
            self._router = RouterFeed(alpaca=AlpacaMarketData(key, secret))
        if source in ("router", "yahoo"):
            self._yahoo = YahooFeed(cache_dir=str(self.cfg.cache_dir))""",
        """        source = self.cfg.source
        if source in ("router", "binance", "alpaca"):
            from ts.altdata import RouterFeed

            # Crypto goes to Binance public; everything else needs somewhere to
            # fall back to, so Yahoo is part of the router rather than a
            # separate branch.
            self._router = RouterFeed(
                alpaca=AlpacaMarketData(key, secret),
                yahoo=YahooFeed(cache_dir=str(self.cfg.cache_dir)),
            )
        if source == "yahoo":
            self._yahoo = YahooFeed(cache_dir=str(self.cfg.cache_dir))""",
    ),
    # 2. only pass limit to sources that accept it
    (
        """        errors: list[str] = []
        for source in self._sources():
            try:
                frame = source.history(
                    symbol, interval=interval, range_=range_,
                    limit=self.cfg.max_bars,
                )
            except DataSourceError as exc:
                errors.append(str(exc))
                continue""",
        """        errors: list[str] = []
        for source in self._sources():
            try:
                frame = self._call(source, symbol, interval, range_)
            except DataSourceError as exc:
                errors.append(str(exc))
                continue""",
    ),
    (
        """    def _sources(self) -> list:""",
        """    @staticmethod
    def _call(source, symbol: str, interval: str, range_: str):
        \"\"\"Call a source, passing ``limit`` only if it accepts one.

        BinanceKlines takes ``limit``; YahooFeed does not. A TypeError from
        that mismatch used to be swallowed into a generic "no data", which hid
        the real cause.
        \"\"\"
        try:
            return source.history(symbol, interval=interval, range_=range_, limit=1000)
        except TypeError:
            return source.history(symbol, interval=interval, range_=range_)

    def _sources(self) -> list:""",
    ),
], "feedrouter.py routes equities through Yahoo")

sys.path.insert(0, str(ROOT))
try:
    from ts.feedrouter import RoutingFeed, DataSourceConfig

    feed = RoutingFeed(DataSourceConfig(source="router", interval="1d", range_="5y"))
    for sym in ("BTCUSD", "AAPL", "ETHUSD", "MSFT"):
        try:
            df = feed.history(sym, interval="1d", range_="5y")
            print(f"  {sym:<8} {len(df):>5} bars  {df.index[0]:%Y-%m-%d} -> {df.index[-1]:%Y-%m-%d}")
        except Exception as exc:  # noqa: BLE001
            print(f"  {sym:<8} FAIL: {str(exc)[:70]}")
except Exception as exc:  # noqa: BLE001
    print(f"[FAIL] import error: {exc}")
sys.exit(0 if ok else 1)
