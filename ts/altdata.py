"""Alternative data sources that solve Yahoo's rate-limit problem.

A 1-minute strategy over ~14 symbols needs ~126,000 bars per cycle. Yahoo's
free endpoint rate-limits that, and the engine hangs mid-cycle -- which is
exactly what was measured. These sources do not.

======================  ==========  ==================  ======================
Source                  Key?        Covers              Rate limit
======================  ==========  ==================  ======================
Binance public klines   **No**      crypto              weight-based, generous
Alpaca crypto bars      **No**      crypto              free with any account
Alpaca stock bars       free key    US equities         200 req/min
======================  ==========  ==================  ======================

Honest coverage limits:

* **No free source carries real FX spot** (EURUSD) or **index CFDs**
  (SPX500, NAS100, US30). Binance's EURUSDT is EUR priced in USDT -- a proxy,
  not the interbank pair. Using it as EURUSD is a decision, not an oversight.
* For forex and indices, MT5 (Windows, via a broker) or a paid feed is the
  answer; at 1m resolution there is no good free option.

:class:`RouterFeed` picks the best available source per symbol and exposes the
same ``history(symbol, interval, range)`` interface as ``YahooFeed``, so it is a
drop-in replacement.
"""

from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path

import pandas as pd

log = logging.getLogger(__name__)

BINANCE_REST = "https://api.binance.com"
ALPACA_DATA = "https://data.alpaca.markets"

# Binance interval -> our internal interval name
_BINANCE_INTERVALS = {
    "1m": "1m", "2m": "2m", "3m": "3m", "5m": "5m", "15m": "15m", "30m": "30m",
    "1h": "1h", "2h": "2h", "4h": "4h", "6h": "6h", "8h": "8h", "12h": "12h",
    "1d": "1d", "3d": "3d", "1w": "1w", "1M": "1M",
}

# Binance kline array -> OHLCV column names
_KLINE_COLUMNS = [
    "open_time", "open", "high", "low", "close", "volume",
    "close_time", "quote_volume", "trades", "taker_base", "taker_quote", "ignore",
]


class DataSourceError(RuntimeError):
    """A source could not satisfy the request."""


def _http_json(url: str, headers: dict | None = None, timeout: float = 20.0,
               method: str = "GET", payload: dict | None = None):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            body = response.read().decode("utf-8", "replace")
            return json.loads(body) if body else {}
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", "replace")[:200]
        except (OSError, UnicodeDecodeError):
            pass
        raise DataSourceError(f"HTTP {exc.code} from {url}: {detail}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise DataSourceError(f"cannot reach {url}: {exc}") from exc


# ------------------------------------------------------------------- Binance


class BinanceKlines:
    """Public Binance spot klines. No API key required."""

    name = "binance_public"
    display = "Binance (public klines)"

    def history(self, symbol: str, interval: str = "1m", range_: str = "7d",
                limit: int = 1000, **kwargs) -> pd.DataFrame:
        binance_interval = _BINANCE_INTERVALS.get(interval)
        if binance_interval is None:
            raise DataSourceError(f"Binance has no {interval!r} bars")

        pair = self._normalise(symbol)
        # NOTE: passing BOTH startTime and limit returns the oldest `limit` bars
        # after startTime -- not the most recent -- which made a 7d range look
        # 6 days stale. Use limit alone to get the newest bars, which is what a
        # trading system actually wants.
        params = {"symbol": pair, "interval": binance_interval, "limit": int(limit)}
        url = f"{BINANCE_REST}/api/v3/klines?{urllib.parse.urlencode(params)}"
        payload = _http_json(url)
        if not isinstance(payload, list) or not payload:
            raise DataSourceError(f"{pair}: no klines returned")
        return self._to_frame(payload)

    @staticmethod
    def _normalise(symbol: str) -> str:
        s = symbol.strip().upper().replace("/", "").replace("-", "")
        for suffix in ("USDT", "USDC", "USD", "BUSD"):
            if s.endswith(suffix) and len(s) > len(suffix):
                return s[: -len(suffix)] + "USDT"
        return s + "USDT"

    @staticmethod
    def _start_ms(range_: str) -> int | None:
        ranges = {"1d": 1, "7d": 7, "30d": 30, "60d": 60, "90d": 90, "1y": 365,
                  "2y": 730, "5y": 1825}
        days = ranges.get(str(range_))
        if not days:
            return None
        return int((time.time() - days * 86400) * 1000)

    @staticmethod
    def _to_frame(payload: list) -> pd.DataFrame:
        frame = pd.DataFrame(payload, columns=_KLINE_COLUMNS)
        # index must be a plain array: giving pandas a Series here makes it
        # reindex the column Series by label against the new index, which yields
        # an all-NaN frame.
        index = pd.to_datetime(frame["open_time"].astype("int64"), unit="ms", utc=True).to_numpy()
        out = pd.DataFrame(
            {
                "open": frame["open"].astype(float).to_numpy(),
                "high": frame["high"].astype(float).to_numpy(),
                "low": frame["low"].astype(float).to_numpy(),
                "close": frame["close"].astype(float).to_numpy(),
                "volume": frame["volume"].astype(float).to_numpy(),
            },
            index=index,
        )
        return out[~out.index.duplicated(keep="last")].sort_index()


# -------------------------------------------------------------------- Alpaca


class AlpacaMarketData:
    """Alpaca market data: crypto is public, equities need a free key."""

    name = "alpaca_data"
    display = "Alpaca market data"

    def __init__(self, key: str = "", secret: str = "") -> None:
        self.key = key
        self.secret = secret

    @property
    def _headers(self) -> dict[str, str]:
        return {"APCA-API-KEY-ID": self.key, "APCA-API-SECRET-KEY": self.secret,
                "Accept": "application/json"}

    def history(self, symbol: str, interval: str = "1m", range_: str = "7d",
                limit: int = 1000, **kwargs) -> pd.DataFrame:
        s = symbol.strip().upper()
        if self._is_crypto(s):
            return self._crypto(s, interval, limit)
        return self._stocks(s, interval, limit)

    # Known FX majors. A 6-letter code ending in USD is NOT automatically
    # crypto -- EURUSD/GBPUSD are forex pairs, and routing them to a crypto
    # venue would silently serve EURUSDT as if it were EURUSD.
    FX_PAIRS = frozenset({
        "EURUSD", "GBPUSD", "USDJPY", "USDCHF", "AUDUSD", "NZDUSD",
        "USDCAD", "EURJPY", "GBPJPY", "EURGBP", "AUDJPY", "CADJPY",
        "CHFJPY", "EURCHF", "GBPCHF", "AUDNZD",
    })

    # Base currencies that mean "crypto base, USD quote". A bare USD quote is
    # ambiguous, so classify by the BASE rather than the quote: BTCUSD is crypto,
    # EURUSD is not.
    KNOWN_COINS = frozenset({
        "BTC", "ETH", "SOL", "XRP", "BNB", "ADA", "DOGE", "AVAX", "LINK",
        "DOT", "LTC", "MATIC", "ATOM", "UNI", "AAVE", "XLM", "ETC", "TRX",
        "NEAR", "APT", "ARB", "OP", "SUI", "SEI", "FIL", "INJ", "TIA",
        "RNDR", "IMX", "HBAR", "ALGO", "VET", "SAND", "MANA", "EGLD",
        "FTM", "GALA", "CRV", "MKR", "SNX", "COMP", "ENS", "LDO", "RUNE",
    })

    @classmethod
    def _is_crypto(cls, symbol: str) -> bool:
        s = symbol.replace("/", "").replace("-", "").upper()
        if s in cls.FX_PAIRS:
            return False
        for quote in ("USDT", "USDC", "BUSD", "USD"):
            if s.endswith(quote) and len(s) > len(quote):
                base = s[: -len(quote)]
                if quote == "USD":
                    return base in cls.KNOWN_COINS
                return True
        return False

    def _crypto(self, symbol: str, interval: str, limit: int) -> pd.DataFrame:
        pair = symbol.replace("-", "/").replace("USDT", "USD")
        if "/" not in pair:
            pair = pair[:-3] + "/USD"
        # Alpaca's timeframe names are 1Min/5Min/1Hour/1Day, NOT "1m" -- sending
        # the internal name straight through returns HTTP 400.
        timeframe = {"1m": "1Min", "2m": "2Min", "5m": "5Min", "15m": "15Min",
                     "30m": "30Min", "1h": "1Hour", "2h": "2Hour", "4h": "4Hour",
                     "1d": "1Day", "1w": "1Week", "1M": "1Month"}[interval]
        # the slash in BTC/USD must be quoted or Alpaca returns 400
        url = (f"{ALPACA_DATA}/v1beta3/crypto/us/bars?"
               f"symbols={urllib.parse.quote(pair)}&timeframe={timeframe}&limit={int(limit)}")
        payload = _http_json(url, self._headers)
        bars = (payload.get("bars") or {}).get(pair)
        if not bars:
            raise DataSourceError(f"{pair}: no bars returned")
        index = pd.to_datetime([b["t"] for b in bars], utc=True).to_numpy()
        out = pd.DataFrame(
            {
                "open": [float(b["o"]) for b in bars],
                "high": [float(b["h"]) for b in bars],
                "low": [float(b["l"]) for b in bars],
                "close": [float(b["c"]) for b in bars],
                "volume": [float(b["v"]) for b in bars],
            },
            index=index,
        )
        return out.sort_index()

    def _stocks(self, symbol: str, interval: str, limit: int) -> pd.DataFrame:
        if not self.key:
            raise DataSourceError(
                "Alpaca stock data needs a key (free at alpaca.markets); "
                "crypto bars are public")
        timeframe = {"1m": "1Min", "5m": "5Min", "15m": "15Min", "1h": "1Hour",
                     "1d": "1Day"}.get(interval)
        if timeframe is None:
            raise DataSourceError(f"Alpaca has no {interval!r} stock bars")
        url = (f"{ALPACA_DATA}/v2/stocks/{urllib.parse.quote(symbol)}/bars?"
               f"timeframe={timeframe}&limit={int(limit)}&adjustment=raw")
        payload = _http_json(url, self._headers)
        bars = payload.get("bars") or {}
        if not bars:
            raise DataSourceError(f"{symbol}: no bars returned")
        index = pd.to_datetime([b["t"] for b in bars], utc=True)
        out = pd.DataFrame(
            {
                "open": [float(b["o"]) for b in bars],
                "high": [float(b["h"]) for b in bars],
                "low": [float(b["l"]) for b in bars],
                "close": [float(b["c"]) for b in bars],
                "volume": [float(b["v"]) for b in bars],
            },
            index=index,
        )
        return out.sort_index()


# --------------------------------------------------------------------- router


@dataclass
class RouterFeed:
    """Picks the best source per symbol, exposing one ``history`` interface."""

    binance: BinanceKlines | None = None
    alpaca: AlpacaMarketData | None = None
    yahoo: object | None = None
    cache_dir: Path | None = None

    def __post_init__(self) -> None:
        self.binance = self.binance or BinanceKlines()
        self.alpaca = self.alpaca or AlpacaMarketData()

    def history(self, symbol: str, interval: str = "1m", range_: str = "7d",
                **kwargs) -> pd.DataFrame:
        """Try each source in order of quality, cheapest network first."""
        candidates = self._order(symbol)
        errors: list[str] = []
        for source in candidates:
            try:
                frame = source.history(symbol, interval=interval, range_=range_, **kwargs)
            except (DataSourceError, ValueError, KeyError) as exc:
                errors.append(f"{source.name}: {exc}")
                continue
            if frame is None or frame.empty:
                errors.append(f"{source.name}: empty")
                continue
            return frame
        raise DataSourceError(f"{symbol}: every source failed -> " + " | ".join(errors))

    def _order(self, symbol: str) -> list:
        """Sources in preference order, with unavailable ones dropped."""
        s = symbol.strip().upper().replace("/", "").replace("-", "")
        is_crypto = self.alpaca._is_crypto(s) if hasattr(self.alpaca, "_is_crypto") else s.endswith("USD")
        if is_crypto:
            # public klines first: no key, best rate limits
            order = [self.binance, self.alpaca, self.yahoo]
        elif self.alpaca.key:
            order = [self.alpaca, self.yahoo]
        else:
            order = [self.yahoo]
        return [src for src in order if src is not None]

    def classify(self, symbols) -> dict[str, list[str]]:
        """Split a universe by which source can serve it."""
        crypto, keyed, unsupported = [], [], []
        for symbol in symbols:
            if self.alpaca._is_crypto(symbol) if isinstance(self.alpaca, AlpacaMarketData) \
                    else symbol.upper().endswith("USD"):
                crypto.append(symbol)
            elif self.alpaca.key:
                keyed.append(symbol)
            else:
                unsupported.append(symbol)
        return {"crypto": crypto, "stocks": keyed, "unsupported": unsupported}


__all__ = [
    "BinanceKlines", "AlpacaMarketData", "RouterFeed", "DataSourceError",
]
