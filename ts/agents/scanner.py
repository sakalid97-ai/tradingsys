"""Multi-market scanner agent.

Sweeps indices, forex, and crypto for fresh data and publishes MARKET_DATA
snapshots. Deliberately cautious:

* symbols whose data is **stale** for their asset class are *not* published as
  tradable (a weekend index close must not be read as a signal),
* per-symbol failures are isolated and logged rather than aborting the sweep,
* the scan is rate-limited by the caller so Yahoo never gets hammered.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from .base import Agent
from .bus import Topic

log = logging.getLogger(__name__)


@dataclass
class ScanReport:
    """What the last sweep found."""

    at: str = ""
    scanned: list[str] = field(default_factory=list)
    fresh: list[str] = field(default_factory=list)
    stale: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    skipped_classes: dict[str, list[str]] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "at": self.at,
            "scanned": self.scanned,
            "fresh": self.fresh,
            "stale": self.stale,
            "failed": self.failed,
            "skipped_classes": self.skipped_classes,
        }


class ScannerAgent(Agent):
    name = "scanner"
    role = "multi-market monitoring"

    def __init__(self, bus, feed, symbols, max_workers: int = 8) -> None:
        super().__init__(bus)
        self.feed = feed
        self.symbols = list(symbols)
        # I/O-bound fetches, so more workers than cores helps; capped so a
        # 50-symbol universe cannot open 50 sockets at once.
        self.max_workers = max(1, min(int(max_workers), 12))
        # symbols no configured source can serve; reported, not fatal
        self.unavailable: list[str] = []
        self.report = ScanReport()

    def run(self) -> None:
        import datetime as _dt

        # Fetches are independent, so they run concurrently: the wall-clock cost
        # becomes the slowest single symbol rather than the sum of all of them.
        # Results are then re-ordered to match the configured universe so the
        # published sequence -- and therefore every downstream consumer -- stays
        # deterministic.
        from ..altdata import DataSourceError
        from ..parallel import parallel_map

        snapshots = parallel_map(self.feed.fetch, self.symbols,
                                 max_workers=self.max_workers)

        scanned: list[str] = []
        fresh: list[str] = []
        stale: list[str] = []
        failed: list[str] = []
        uncovered: list[str] = []

        for symbol, snap in zip(self.symbols, snapshots):
            if isinstance(snap, DataSourceError):
                # structural: no source can serve this symbol/timeframe
                uncovered.append(symbol)
                continue
            if isinstance(snap, Exception):
                # transient: network, throttling, bad ticker
                failed.append(symbol)
                self.alarm(f"fetch failed for {symbol}: {snap}")
                continue
            if snap is None:
                failed.append(symbol)
                self.alarm(f"fetch failed for {symbol}")
                continue

            scanned.append(symbol)
            summary = snap.summary()
            if snap.is_stale:
                stale.append(symbol)
                self.publish(Topic.MARKET_DATA, {**summary, "tradable": False})
            else:
                fresh.append(symbol)
                self.publish(Topic.MARKET_DATA, {**summary, "tradable": True})

        self.unavailable = uncovered
        self.report = ScanReport(
            at=_dt.datetime.now(_dt.UTC).isoformat(),
            scanned=scanned, fresh=fresh, stale=stale, failed=failed,
            skipped_classes=self.feed.universe_by_class(stale or []),
        )
        self.publish(Topic.AGENT_STATUS, {"agent": self.name, "scan": self.report.to_dict()})


__all__ = ["ScannerAgent", "ScanReport"]
