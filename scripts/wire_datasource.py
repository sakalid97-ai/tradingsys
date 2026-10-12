"""Add the data-source selector to settings, the API, and the Setup panel."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

ok = True


def patch(path: Path, subs, label: str) -> bool:
    global ok
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
    ok = ok and not failed
    return not failed


# ---------------------------------------------------------- 1. the setting
patch(ROOT / "ts" / "settings.py", [
    (
        "    timeframe: str = \"1m\"\n    range_: str = \"7d\"",
        "    timeframe: str = \"1m\"\n"
        "    range_: str = \"7d\"\n"
        "    # router | yahoo | binance | alpaca\n"
        "    data_source: str = \"router\"",
    ),
    (
        "    def to_dict(self) -> dict:\n        return {\n            \"symbol\": self.symbol,",
        "    def to_dict(self) -> dict:\n        return {\n            \"symbol\": self.symbol,",
    ),
], "settings.py: data_source field")

# --------------------------------------------------- 2. the API serves the catalogue
patch(ROOT / "ts" / "web" / "server.py", [
    (
        '''        if path == "/api/instruments":
            return 200, self._instruments()''',
        '''        if path == "/api/instruments":
            return 200, self._instruments()
        if path == "/api/data-sources":
            return 200, {"sources": _data_sources()}''',
    ),
    (
        "def _propfirm_registry():",
        '''def _data_sources():
    """The available data sources and what each one covers."""
    from ..feedrouter import SOURCE_NOTES

    return [
        {"id": key, "label": label, **note} for key, label in [
            ("router", "Auto (recommended)"),
            ("binance", "Binance public"),
            ("alpaca", "Alpaca market data"),
            ("yahoo", "Yahoo Finance"),
        ] for note in [dict(zip(("note",), (SOURCE_NOTES.get(key, ""),)))]
    ]


def _propfirm_registry():''',
    ),
], "server.py: /api/data-sources")

sys.path.insert(0, str(ROOT))
try:
    from ts.settings import Settings
    from ts.web.server import _data_sources
    from ts.feedrouter import SOURCE_NOTES

    s = Settings()
    print("[ok] settings default source:", s.instruments.data_source)
    print("[ok] sources:", [d["id"] for d in _data_sources()])
except Exception as exc:  # noqa: BLE001
    print(f"[FAIL] import error: {exc}")
sys.exit(0 if ok else 1)
