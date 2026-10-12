"""Market data.

Primary source is the Yahoo Finance chart endpoint, which is fetched with
``urllib`` from the standard library -- no third-party network dependency, so
the system is deployable anywhere Python runs.  Everything is cached to CSV on
disk so repeated backtests are instant and never re-hit the network.

For live trading, ``DataFeed.feed`` returns bars whose *last* timestamp is
compared against wall-clock time; ``staleness_seconds`` lets the engine refuse
to trade on stale data rather than acting on a frozen snapshot.
"""

from __future__ import annotations

import csv
import json
import logging
import os
import random
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pandas as pd

from .quality import validate

log = logging.getLogger(__name__)

# How long a cached frame is trusted, per interval. 1m bars are stale in
# 60 seconds, so anything longer would serve old prices as current.
_FRESHNESS_MINUTES = {
    "1m": 1,
    "2m": 2,
    "5m": 5,
    "15m": 15,
    "30m": 30,
    "60m": 60,
    "90m": 90,
    "1h": 60,
    "1d": 15,
    "1wk": 1440,
    "1mo": 1440,
}

# How many times to retry a cache write against a racing process.
_CACHE_WRITE_RETRIES = 4

_ENDPOINTS = (
    "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}",
    "https://query2.finance.yahoo.com/v8/finance/chart/{symbol}",
)

_INTERVALS = ("1m", "2m", "5m", "15m", "30m", "60m", "90m", "1h", "1d", "5d", "1wk", "1mo", "3mo")

# Yahoo caps intraday history; beyond these ranges the API silently truncates.
_MAX_RANGE = {
    "1m": "7d",
    "2m": "60d",
    "5m": "60d",
    "15m": "60d",
    "30m": "60d",
    "60m": "730d",
    "90m": "60d",
    "1h": "730d",
    "1d": "max",
    "1wk": "max",
    "1mo": "max",
    "3mo": "max",
}


@dataclass
class Quote:
    """Latest-tick snapshot used by the live engine."""

    symbol: str
    price: float
    timestamp: datetime
    currency: str = "USD"

    def age_seconds(self) -> float:
        return (datetime.now(UTC) - self.timestamp).total_seconds()


class DataError(RuntimeError):
    """Raised when data cannot be retrieved or fails validation."""


class YahooFeed:
    """Download and cache OHLCV history from Yahoo Finance."""

    def __init__(
        self,
        cache_dir: str | Path = "data",
        timeout: float = 20.0,
        max_retries: int = 4,
        user_agent: str = "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
    ) -> None:
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.timeout = timeout
        self.max_retries = max_retries
        self.user_agent = user_agent

    # ---------------------------------------------------------------- network

    def _get(self, symbol: str, params: dict) -> dict:
        last_err: Exception | None = None
        for attempt in range(self.max_retries):
            url_template = _ENDPOINTS[attempt % len(_ENDPOINTS)]
            url = url_template.format(symbol=urllib.parse.quote(symbol, safe=""))
            query = urllib.parse.urlencode(params)
            full_url = f"{url}?{query}"
            req = urllib.request.Request(
                full_url,
                headers={"User-Agent": self.user_agent, "Accept": "application/json"},
            )
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    return json.load(resp)
            except urllib.error.HTTPError as exc:
                last_err = exc
                if exc.code in (401, 403, 404):
                    raise DataError(f"{symbol}: HTTP {exc.code} from data source") from exc
                if exc.code == 429:
                    time.sleep(1.5 * (attempt + 1))
                else:
                    time.sleep(0.4 * (attempt + 1))
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                last_err = exc
                time.sleep(0.4 * (attempt + 1) + random.random() * 0.3)
        raise DataError(f"{symbol}: data request failed after {self.max_retries} tries ({last_err})")

    # ------------------------------------------------------------------ cache

    def _cache_path(self, symbol: str, interval: str) -> Path:
        safe = symbol.replace("/", "_").replace("^", "_").replace("=", "_")
        return self.cache_dir / f"{safe}__{interval}.csv"

    def _load_cache(self, symbol: str, interval: str) -> pd.DataFrame | None:
        path = self._cache_path(symbol, interval)
        if not path.exists():
            return None
        try:
            df = pd.read_csv(path, index_col=0, parse_dates=True)
            df.index = pd.to_datetime(df.index, utc=True)
            return df if not df.empty else None
        except (OSError, ValueError, pd.errors.ParserError) as exc:
            log.warning("cache unreadable for %s: %s", path, exc)
            return None

    def _save_cache(self, symbol: str, interval: str, df: pd.DataFrame) -> None:
        path = self._cache_path(symbol, interval)
        # A unique temp name per process AND thread: the dashboard and a
        # backtest can both fetch the same symbol at the same time, and a
        # shared ".tmp" makes one of them fail with Permission denied.
        # _TEMP_SUFFIX_GUARD
        unique = f"{os.getpid()}-{threading.get_ident()}"
        tmp = path.with_name(f"{path.stem}.{unique}.tmp")
        # A cache write that fails is not an error worth shouting about: the
        # bars are already fetched and in memory. Windows raises
        # PermissionError / WinError 32 when another process holds the same
        # file, so retry briefly, then give up quietly.
        last_error: OSError | None = None
        for attempt in range(_CACHE_WRITE_RETRIES):
            try:
                df.to_csv(tmp)
                tmp.replace(path)
                return
            except OSError as exc:
                last_error = exc
                time.sleep(0.05 * (attempt + 1))
                try:
                    tmp.unlink()
                except OSError:
                    pass
        log.debug("could not write cache %s (another process is using it): %s",
                  path, last_error)

    # ----------------------------------------------------------------- parsing

    @staticmethod
    def _parse(payload: dict, symbol: str) -> pd.DataFrame:
        chart = payload.get("chart")
        if not chart or chart.get("error"):
            raise DataError(f"{symbol}: {chart.get('error') if chart else 'empty response'}")
        result = chart["result"]
        if not result:
            raise DataError(f"{symbol}: no result")
        result = result[0]

        meta = result.get("meta", {})
        gmt_offset = int(meta.get("gmtoffset", 0) or 0)
        granularity = str(meta.get("dataGranularity", "1d"))

        timestamps = result.get("timestamp") or []
        quote = result["indicators"]["quote"][0]
        frame = pd.DataFrame(
            {
                "open": quote.get("open", []),
                "high": quote.get("high", []),
                "low": quote.get("low", []),
                "close": quote.get("close", []),
                "volume": quote.get("volume", []),
            },
            index=pd.to_datetime(timestamps, unit="s", utc=True),
        )
        frame = frame[frame.index.notna()]

        # Daily/weekly bars are stamped at UTC midnight of the *session* date in
        # the exchange's local zone; shift them so the bar label matches the
        # session the trader actually sees.
        if granularity in ("1d", "1wk", "1mo", "3mo", "5d") and gmt_offset:
            frame.index = frame.index.map(lambda ts: (ts + pd.Timedelta(seconds=gmt_offset)).normalize())
            frame.index = pd.to_datetime(frame.index, utc=True)

        frame["symbol"] = meta.get("symbol", symbol)
        return frame

    # --------------------------------------------------------------- fetching

    def history(
        self,
        symbol: str,
        interval: str = "1d",
        range_: str | None = None,
        start: str | None = None,
        end: str | None = None,
        use_cache: bool = True,
        max_age_minutes: int | None = None,
    ) -> pd.DataFrame:
        """Fetch history for ``symbol``.

        ``range_`` uses Yahoo ranges (1mo, 6mo, 1y, 5y, max).  Alternatively
        pass ``start``/``end`` as ``YYYY-MM-DD``.  Bars already on disk and
        newer than ``max_age_minutes`` are reused instead of re-fetched.
        """
        if interval not in _INTERVALS:
            raise ValueError(f"interval must be one of {_INTERVALS}, got {interval!r}")

        cached = self._load_cache(symbol, interval) if use_cache else None
        if max_age_minutes is None:
            # 1m bars are stale within a minute; daily bars are fine for a
            # whole session. Match the cache window to the bar spacing.
            max_age_minutes = _FRESHNESS_MINUTES.get(interval, 15)
        if cached is not None and max_age_minutes is not None and len(cached) > 0:
            age = datetime.now(UTC) - cached.index[-1]
            if age <= timedelta(minutes=max_age_minutes):
                return cached

        params: dict[str, str | int] = {"interval": interval, "includePrePost": "false"}
        if range_:
            params["range"] = range_
        else:
            start_dt = pd.Timestamp(start, tz="UTC") if start else pd.Timestamp("1970-01-01", tz="UTC")
            end_dt = pd.Timestamp(end, tz="UTC") if end else pd.Timestamp.now(tz="UTC")
            params["period1"] = int(start_dt.timestamp())
            params["period2"] = int(end_dt.timestamp())
        if "range" not in params and interval in _MAX_RANGE:
            params["range"] = _MAX_RANGE[interval]

        payload = self._get(symbol, params)
        fresh = self._parse(payload, symbol)
        fresh = validate(fresh, symbol)

        if fresh.empty:
            if cached is not None and not cached.empty:
                log.warning("%s: live fetch empty, falling back to cache", symbol)
                return cached
            raise DataError(f"{symbol}: no usable bars returned")

        if cached is not None and not cached.empty:
            merged = pd.concat([cached, fresh])
            merged = merged[~merged.index.duplicated(keep="last")].sort_index()
        else:
            merged = fresh

        if use_cache:
            self._save_cache(symbol, interval, merged)
        return merged

    def quote(self, symbol: str, interval: str = "1d") -> Quote:
        """Most recent bar as a :class:`Quote` (used by the live engine)."""
        df = self.history(symbol, interval=interval, range_="5d")
        if df.empty:
            raise DataError(f"{symbol}: no bars for quote")
        last_ts = df.index[-1]
        price = float(df["close"].iloc[-1])
        currency = "USD"
        try:
            currency = str(df["symbol"].iloc[-1]) if "symbol" in df else "USD"
        except (KeyError, IndexError):  # pragma: no cover
            pass
        return Quote(symbol=symbol, price=price, timestamp=last_ts.to_pydatetime(), currency=currency)

    def staleness(self, symbol: str, interval: str = "1d") -> float:
        """Seconds since the last bar; raises if the series is empty."""
        df = self.history(symbol, interval=interval, range_="5d")
        if df.empty:
            raise DataError(f"{symbol}: no bars")
        return (datetime.now(UTC) - df.index[-1]).total_seconds()

    def clear_cache(self, symbol: str | None = None, interval: str | None = None) -> int:
        """Delete cached CSVs. Returns the number of files removed."""
        pattern = f"{symbol or '*'}__{interval or '*'}.csv" if symbol else "*.csv"
        files = list(self.cache_dir.glob(pattern)) if symbol else list(self.cache_dir.glob(pattern))
        removed = 0
        for path in files:
            try:
                path.unlink()
                removed += 1
            except OSError:
                pass
        return removed


# --------------------------------------------------------------------- helpers


def csv_feed(path: str | Path, symbol: str | None = None) -> pd.DataFrame:
    """Load a local CSV (Date,Open,High,Low,Close,Volume) as a validated frame."""
    path = Path(path)
    if not path.exists():
        raise DataError(f"csv not found: {path}")
    df = pd.read_csv(path)
    df.columns = [c.strip().title() for c in df.columns]
    date_col = "Date" if "Date" in df.columns else df.columns[0]
    df[date_col] = pd.to_datetime(df[date_col], utc=True, errors="coerce")
    df = df.dropna(subset=[date_col]).set_index(date_col).sort_index()
    return validate(df, symbol or path.stem)


@dataclass
class DataFeed:
    """Convenience wrapper: pull a batch of symbols with error isolation."""

    feed: YahooFeed
    interval: str = "1d"
    range_: str | None = None
    start: str | None = None
    end: str | None = None
    use_cache: bool = True

    def pull(self, symbols) -> dict[str, pd.DataFrame]:
        out: dict[str, pd.DataFrame] = {}
        problems: list[str] = []
        for symbol in symbols:
            try:
                out[symbol] = self.feed.history(
                    symbol,
                    interval=self.interval,
                    range_=self.range_,
                    start=self.start,
                    end=self.end,
                    use_cache=self.use_cache,
                )
            except (DataError, ValueError, OSError) as exc:
                problems.append(f"{symbol}: {exc}")
                log.error("data pull failed for %s: %s", symbol, exc)
        if not out and problems:
            raise DataError("every symbol failed: " + " | ".join(problems))
        if problems:
            log.warning("%d symbol(s) skipped", len(problems))
        return out
