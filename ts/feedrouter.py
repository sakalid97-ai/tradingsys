"""A ``YahooFeed``-compatible feed that routes per symbol.

``MarketFeed`` (used by the ScannerAgent) already accepts any object exposing
``history(symbol, interval, range)``. This class implements exactly that and
picks the best source per symbol, so routing needs no changes downstream.

Sources:

======================  ==========  =========================
``source``              Covers      Notes
======================  ==========  =========================
``router``              per-symbol  Binance public for crypto, Yahoo otherwise
``yahoo``               everything  rate-limited at 1m over many symbols
``binance``             crypto      public, no API key
``alpaca``              crypto+equities  crypto public, equities need a key
======================  ==========  =========================

A symbol no source can serve raises ``DataSourceError``; the scanner records it
under ``source_unavailable`` and keeps scanning the rest.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd

from ts.altdata import AlpacaMarketData, BinanceKlines, DataSourceError, RouterFeed
from ts.datafeed import YahooFeed
from ts.quality import validate

log = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parents[1]

SOURCE_NOTES = {
    "router": (
        "Routes each symbol to the best available source. Crypto goes to "
        "Binance public (no API key); everything else falls back to Yahoo. "
        "Symbols no source can serve are dropped, not fatal."
    ),
    "yahoo": "Yahoo Finance. Rate-limited at 1m across many symbols.",
    "binance": "Binance public klines. Crypto only, no API key required.",
    "alpaca": "Alpaca market data. Crypto is public; equities need a free key.",
}


@dataclass
class DataSourceConfig:
    """Which source the system feeds from."""

    source: str = "router"        # router | yahoo | binance | alpaca
    alpaca_key: str = ""
    alpaca_secret: str = ""
    range_: str = "7d"
    interval: str = "1m"
    max_bars: int = 1000
    cache_dir: Path = field(default_factory=lambda: ROOT / "data")


def _stored_alpaca_credentials() -> tuple[str, str]:
    """Read Alpaca keys from the settings layer, if stored there."""
    try:
        from ts.settings import SettingsStore

        cred = SettingsStore(ROOT / "state").load().broker
        if cred.broker == "alpaca" and cred.key:
            return cred.key, cred.secret or ""
    except (ImportError, OSError, ValueError, AttributeError, KeyError):
        log.debug("no stored Alpaca credentials")
    return "", ""



class RoutingFeed:
    """``YahooFeed``-compatible feed that routes each symbol to a source."""

    name = "routing_feed"
    display = "routed (best available)"

    def __init__(self, config: DataSourceConfig | None = None) -> None:
        self.cfg = config or DataSourceConfig()
        key, secret = self.cfg.alpaca_key, self.cfg.alpaca_secret
        if not key and self.cfg.source in ("router", "alpaca"):
            key, secret = _stored_alpaca_credentials()
        self.cfg.alpaca_key, self.cfg.alpaca_secret = key, secret

        self._router: RouterFeed | None = None
        self._yahoo: YahooFeed | None = None
        self._binance: BinanceKlines | None = None
        self._alpaca: AlpacaMarketData | None = None

        source = self.cfg.source
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
            self._yahoo = YahooFeed(cache_dir=str(self.cfg.cache_dir))

    # ------------------------------------------------------------------ feed

    def history(self, symbol: str, interval: str = "1m", range_: str = "7d",
                **kwargs) -> pd.DataFrame:
        """Return a validated frame, or raise ``DataSourceError``."""
        if self._router is None and self._yahoo is None:
            raise DataSourceError(f"{symbol}: no data source configured")

        errors: list[str] = []
        for source in self._sources():
            try:
                frame = self._call(source, symbol, interval, range_)
            except DataSourceError as exc:
                errors.append(str(exc))
                continue
            if frame is None or frame.empty:
                errors.append(f"{symbol}: {source.name} returned nothing")
                continue
            frame = validate(frame, symbol)
            if frame.empty:
                errors.append(f"{symbol}: no valid bars from {source.name}")
                continue
            return frame
        raise DataSourceError(f"{symbol}: no source could serve it ({'; '.join(errors[:3])})")

    @staticmethod
    def _call(source, symbol: str, interval: str, range_: str):
        """Call a source, passing ``limit`` only if it accepts one.

        BinanceKlines takes ``limit``; YahooFeed does not. A TypeError from
        that mismatch used to be swallowed into a generic "no data", which hid
        the real cause.
        """
        try:
            return source.history(symbol, interval=interval, range_=range_, limit=1000)
        except TypeError:
            return source.history(symbol, interval=interval, range_=range_)

    def _sources(self) -> list:
        if self.cfg.source == "yahoo" and self._yahoo is not None:
            return [self._yahoo]
        if self.cfg.source == "binance" and self._router is not None:
            return [self._router]          # RouterFeed already prefers Binance for crypto
        if self.cfg.source == "alpaca" and self._router is not None:
            return [self._router]
        if self._router is not None:
            return [self._router]
        return [self._yahoo] if self._yahoo is not None else []

    def quote(self, symbol: str, interval: str = "1d") -> pd.Timestamp:
        frame = self.history(symbol, interval=interval, range_="5d")
        return frame.index[-1]


def resolve_feed(source: str, range_: str = "7d", interval: str = "1m",
                 cache_dir: Path | None = None) -> tuple[RoutingFeed, str]:
    """Build a feed for the requested source plus a human-readable note."""
    cfg = DataSourceConfig(source=source, range_=range_, interval=interval)
    if cache_dir:
        cfg.cache_dir = Path(cache_dir)
    return RoutingFeed(cfg), SOURCE_NOTES.get(source, "")


__all__ = ["RoutingFeed", "DataSourceConfig", "resolve_feed", "SOURCE_NOTES"]
