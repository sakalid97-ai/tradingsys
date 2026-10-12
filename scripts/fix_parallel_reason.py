"""Distinguish a failed fetch from an uncovered symbol.

`parallel_map` returned None on any failure, which lost *why*. The scanner needs
to tell "network error, retry later" apart from "no source can serve this
symbol at this timeframe" -- one is transient, one is structural, and reporting
both the same way means a coverage gap looks like an outage.
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

# ---- 1. parallel_map returns the exception, not None
ok &= patch(ROOT / "ts" / "parallel.py", [
    (
        '''def _safe_call(func, item):
    try:
        return func(item)
    except Exception as exc:  # noqa: BLE001 - isolate every failure
        log.debug("item failed: %s", exc)
        return None''',
        '''def _safe_call(func, item):
    """Run ``func(item)``; on failure return the exception object itself.

    Returning the exception rather than None lets the caller tell WHY it failed
    -- "no source can serve this symbol" (structural) and "network error"
    (transient) need different handling and different reporting.
    """
    try:
        return func(item)
    except Exception as exc:  # noqa: BLE001 - isolate every failure
        log.debug("item failed: %s", exc)
        return exc''',
    ),
    (
        '''    Returns a list aligned with ``items``; entries are ``None`` where ``func``
    raised. A failure in one item never affects the others.
    """''',
        '''    Returns a list aligned with ``items``. Entries are normally the result of
    ``func``; where ``func`` raised, the entry is the *exception object*, so the
    caller can inspect ``isinstance`` to learn why. A failure in one item never
    affects the others.
    """''',
    ),
], "parallel.py surfaces the failure reason")

# ---- 2. the scanner classifies it
ok &= patch(ROOT / "ts" / "agents" / "scanner.py", [
    (
        """        for symbol, snap in zip(self.symbols, snapshots):
            if snap is None or isinstance(snap, DataSourceError):
                uncovered.append(symbol)
                continue""",
        """        for symbol, snap in zip(self.symbols, snapshots):
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
                continue""",
    ),
], "scanner.py separates structural from transient")

sys.path.insert(0, str(ROOT))
try:
    from ts.parallel import parallel_map
    from ts.agents.scanner import ScannerAgent

    result = parallel_map(lambda v: 1 / 0 if v == 1 else v * 2, [0, 1, 2])
    print("[ok] failure is surfaced:", type(result[1]).__name__)
    print("[ok] good values intact:", result[0], result[2])
except Exception as exc:  # noqa: BLE001
    print(f"[FAIL] import error: {exc}")
sys.exit(0 if ok else 1)
