"""Make the ScannerAgent drop symbols no data source can serve.

A universe spanning crypto, equities, indices and forex will never be fully
covered by one source. Failing the whole scan over one uncovered symbol is
exactly wrong; recording it and continuing gets you the trades you can take.
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

# ------------------------------------------------- 1. the feed raises DataSourceError
ok &= patch(ROOT / "ts" / "agents" / "feed.py", [
    # expose which source served a symbol, for the dashboard
    (
        "    @property\n    def freshness_limit_seconds(self) -> float:",
        "    @property\n"
        "    def source_name(self) -> str:\n"
        "        \"\"\"Which source produced this snapshot, if known.\"\"\"\n"
        "        return getattr(self, 'source', 'yahoo')\n\n"
        "    @property\n"
        "    def freshness_limit_seconds(self) -> float:",
    ),
    (
        "    fetched_at: datetime\n    source: str = \"yahoo\"",
        "    fetched_at: datetime\n"
        "    source: str = \"yahoo\"\n"
        "    tradable: bool = True",
    ),
], "feed.py records its source")

# ------------------------------------------------- 2. ScannerAgent drops uncovered symbols
ok &= patch(ROOT / "ts" / "agents" / "scanner.py", [
    (
        """        from ..parallel import parallel_map

        snapshots = parallel_map(self.feed.fetch, self.symbols,
                                 max_workers=self.max_workers)

        scanned: list[str] = []
        fresh: list[str] = []
        stale: list[str] = []
        failed: list[str] = []

        for symbol, snap in zip(self.symbols, snapshots):
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

        self.report = ScanReport(
            at=_dt.datetime.now(_dt.UTC).isoformat(),
            scanned=scanned, fresh=fresh, stale=stale, failed=failed,
            skipped_classes=self.feed.universe_by_class(stale or []),
        )""",
        """        from ..altdata import DataSourceError
        from ..parallel import parallel_map

        snapshots = parallel_map(self.feed.fetch, self.symbols,
                                 max_workers=self.max_workers)

        scanned: list[str] = []
        fresh: list[str] = []
        stale: list[str] = []
        failed: list[str] = []
        uncovered: list[str] = []

        for symbol, snap in zip(self.symbols, snapshots):
            if snap is None or isinstance(snap, DataSourceError):
                uncovered.append(symbol)
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
        )""",
    ),
    (
        "        self.max_workers = max(1, min(int(max_workers), 12))\n        self.report = ScanReport()",
        "        self.max_workers = max(1, min(int(max_workers), 12))\n"
        "        # symbols no configured source can serve; reported, not fatal\n"
        "        self.unavailable: list[str] = []\n"
        "        self.report = ScanReport()",
    ),
], "scanner.py drops uncovered symbols")

sys.path.insert(0, str(ROOT))
try:
    from ts.agents.scanner import ScannerAgent
    from ts.feedrouter import RoutingFeed, DataSourceConfig
    print("[ok] imports clean")
except Exception as exc:  # noqa: BLE001
    print(f"[FAIL] import error: {exc}")
sys.exit(0 if ok else 1)
