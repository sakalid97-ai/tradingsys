"""Multi-asset market data for the agent system: indices, forex, and crypto.

Wraps :class:`ts.datafeed.YahooFeed` and adds what the agents need beyond raw
bars: resampling, per-asset-class price proxies, and staleness detection.
Indices and forex only trade in part-time sessions; the feed records session
awareness so agents do not read a stale index close as a new signal.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pandas as pd

from ..datafeed import DataFeed, YahooFeed
from .instruments import AssetClass, Instrument, resolve, yahoo_symbol

log = logging.getLogger(__name__)

# How long a bar is considered fresh, per asset class.
FRESHNESS: dict[AssetClass, timedelta] = {
    AssetClass.INDEX: timedelta(hours=26),   # overnight sessions + weekends
    AssetClass.FOREX: timedelta(hours=8),
    AssetClass.CRYPTO: timedelta(minutes=30),
}


@dataclass
class MarketSnapshot:
    """One symbol's latest usable state, as the agents see it."""

    symbol: str
    instrument: Instrument
    frame: pd.DataFrame
    fetched_at: datetime
    source: str = "yahoo"

    @property
    def last_bar_time(self) -> pd.Timestamp:
        return self.frame.index[-1]

    @property
    def bar_step_minutes(self) -> float:
        """Median spacing between bars, in minutes."""
        if len(self.frame) < 3:
            return 0.0
        deltas = self.frame.index.to_series().diff().dropna()
        if deltas.empty:
            return 0.0
        step = deltas.median()
        if pd.isna(step) or step <= pd.Timedelta(0):
            return 0.0
        return step.total_seconds() / 60.0

    @property
    def age_seconds(self) -> float:
        return (datetime.now(UTC) - self.last_bar_time.to_pydatetime()).total_seconds()

    @property
    def freshness_limit_seconds(self) -> float:
        """Per-asset-class staleness limit, floored by the bar interval.

        An hourly bar is by definition up to 60 minutes old when it closes, so
        an absolute limit shorter than the bar would mark every bar stale.
        """
        base = FRESHNESS.get(self.instrument.asset_class, timedelta(hours=24)).total_seconds()
        interval_floor = self.bar_step_minutes * 60.0 * 2.5
        return max(base, interval_floor)

    @property
    def is_stale(self) -> bool:
        return self.age_seconds > self.freshness_limit_seconds

    def close(self, n: int = 1) -> float:
        return float(self.frame["close"].iloc[-n])

    def series(self, column: str = "close") -> pd.Series:
        return self.frame[column]

    def summary(self) -> dict:
        return {
            "symbol": self.symbol,
            "asset_class": self.instrument.asset_class.value,
            "bars": len(self.frame),
            "last": self.close(),
            "last_bar": str(self.last_bar_time),
            "age_seconds": round(self.age_seconds, 1),
            "stale": self.is_stale,
            "source": self.source,
        }


class MarketFeed:
    """Pulls and caches history for a universe of indices, FX and crypto."""

    def __init__(self, feed: YahooFeed | None = None, interval: str = "1h",
                 range_: str = "60d") -> None:
        self._feed = feed or YahooFeed(cache_dir="data")
        self.interval = interval
        self.range_ = range_
        self._snapshots: dict[str, MarketSnapshot] = {}

    # ----------------------------------------------------------------- fetching

    def instrument(self, symbol: str) -> Instrument:
        return resolve(symbol)

    def fetch(self, symbol: str, bars: int | None = None,
              use_cache: bool = True) -> MarketSnapshot:
        """Return a snapshot for ``symbol``, or raise if data is unusable."""
        instrument = self.instrument(symbol)
        ticker = yahoo_symbol(symbol)
        # The cache window must match the bar. A fixed 25 minutes meant a 60s
        # cycle at 1m refetched ~126,000 bars/minute across the universe, which
        # a free data endpoint rate-limits -- the engine then hangs mid-cycle
        # and the dashboard reports a stuck 'starting'. Keying freshness to the
        # interval means data is only refreshed once a NEW bar has completed.
        frame = self._feed.history(
            ticker, interval=self.interval, range_=self.range_,
            max_age_minutes=None if use_cache else 0,
        )
        if frame is None or frame.empty:
            raise ValueError(f"no bars for {symbol} (ticker {ticker})")
        if bars and len(frame) > bars:
            frame = frame.tail(bars)
        snap = MarketSnapshot(symbol=symbol, instrument=instrument, frame=frame,
                              fetched_at=datetime.now(UTC))
        self._snapshots[symbol] = snap
        return snap

    def fetch_all(self, symbols, use_cache: bool = True) -> dict[str, MarketSnapshot]:
        out: dict[str, MarketSnapshot] = {}
        for sym in symbols:
            try:
                out[sym] = self.fetch(sym, use_cache=use_cache)
            except Exception as exc:  # noqa: BLE001 - isolation per symbol
                log.warning("fetch failed for %s: %s", sym, exc)
        return out

    def cached(self, symbol: str) -> MarketSnapshot | None:
        return self._snapshots.get(symbol)

    # -------------------------------------------------------------- utilities

    @staticmethod
    def to_data_feed(interval: str, range_: str) -> DataFeed:
        return DataFeed(feed=YahooFeed(cache_dir="data"), interval=interval,
                        range_=range_)

    def universe_by_class(self, symbols) -> dict[str, list[str]]:
        buckets: dict[str, list[str]] = {c.value: [] for c in AssetClass}
        for sym in symbols:
            try:
                buckets[self.instrument(sym).asset_class.value].append(sym)
            except Exception:  # noqa: BLE001
                log.warning("could not classify %s", sym)
        return {k: v for k, v in buckets.items() if v}


__all__ = ["MarketFeed", "MarketSnapshot", "FRESHNESS"]
