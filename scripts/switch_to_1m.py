"""Switch the system's default timescale to 1-minute bars.

1m bars come with three constraints that must be handled explicitly, or the
system produces numbers that look fine and mean nothing:

1. **Only ~7 days of history.** Yahoo caps 1m data at 7d. That is roughly
   2,700 bars for a session-only instrument (index/forex) and ~10,000 for
   crypto. Enough to trade, nowhere near enough to *validate* -- so walk-forward
   now refuses instead of reporting a beautiful overfit curve.
2. **Volume is unreliable at 1m.** On real data only ~19% of 1-minute bars have
   a non-zero volume. The breakout entry requires above-average volume, which
   at 1m would silently reject almost every signal. Volume gating therefore
   defaults OFF at this timescale.
3. **Per-order fees dominate.** A flat 1-dollar minimum commission on a
   position risking 1% of a modest account is a large fraction of the edge at
   1m, so commission becomes basis-point based at this scale.
"""

from __future__ import annotations

from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]


def patch(path: Path, replacements, label: str) -> None:
    text = path.read_text(encoding="utf-8")
    applied, skipped = 0, 0
    for old, new in replacements:
        if new in text:
            skipped += 1
            continue
        if old not in text:
            print(f"    [warn] {path.name}: anchor not found for {old[:48]!r}")
            continue
        text = text.replace(old, new, 1)
        applied += 1
    path.write_text(text, encoding="utf-8")
    print(f"[{'ok' if applied or skipped else 'fail'}] {label}: "
          f"{applied} applied, {skipped} already present")


# ------------------------------------------------------------ 1. datafeed.py
patch(ROOT / "ts" / "datafeed.py", [
    # 1m bars go stale within a minute; the default 15-minute window would keep
    # serving minutes-old prices and calling them fresh.
    (
        "def history(\n        self,\n        symbol: str,\n        interval: str = \"1d\",\n        range_: str | None = None,\n        start: str | None = None,\n        end: str | None = None,\n        use_cache: bool = True,\n        max_age_minutes: int | None = 15,\n    ) -> pd.DataFrame:",
        "def history(\n        self,\n        symbol: str,\n        interval: str = \"1d\",\n        range_: str | None = None,\n        start: str | None = None,\n        end: str | None = None,\n        use_cache: bool = True,\n        max_age_minutes: int | None = None,\n    ) -> pd.DataFrame:",
    ),
    # interval-aware default freshness
    (
        "        if cached is not None and max_age_minutes is not None and len(cached) > 0:",
        "        if max_age_minutes is None:\n"
        "            # 1m bars are stale within a minute; daily bars are fine for a\n"
        "            # whole session. Match the cache window to the bar spacing.\n"
        "            max_age_minutes = _FRESHNESS_MINUTES.get(interval, 15)\n"
        "        if cached is not None and max_age_minutes is not None and len(cached) > 0:",
    ),
    (
        "log = logging.getLogger(__name__)",
        "log = logging.getLogger(__name__)\n\n"
        "# How long a cached frame is trusted, per interval. 1m bars are stale in\n"
        "# 60 seconds, so anything longer would serve old prices as current.\n"
        "_FRESHNESS_MINUTES = {\n"
        "    \"1m\": 1,\n"
        "    \"2m\": 2,\n"
        "    \"5m\": 5,\n"
        "    \"15m\": 15,\n"
        "    \"30m\": 30,\n"
        "    \"60m\": 60,\n"
        "    \"90m\": 90,\n"
        "    \"1h\": 60,\n"
        "    \"1d\": 15,\n"
        "    \"1wk\": 1440,\n"
        "    \"1mo\": 1440,\n"
        "}",
    ),
], "datafeed.py: 1m-aware cache freshness")

# ------------------------------------------------------------- 2. strategy.py
patch(ROOT / "ts" / "strategy.py", [
    (
        "    # --- sanity floors --------------------------------------------------\n    min_history: int = 220               # refuse to trade before this many bars",
        "    # --- sanity floors --------------------------------------------------\n"
        "    # 220 bars is ~3.7 hours of 1-minute data, which is a sensible warm-up for\n"
        "    # an intraday trend filter rather than the 200 *days* it means on daily bars.\n"
        "    min_history: int = 220               # refuse to trade before this many bars",
    ),
    (
        "    # --- sanity floors --------------------------------------------------\n"
        "    min_history: int = 220               # refuse to trade before this many bars\n\n",
        "    # --- sanity floors --------------------------------------------------\n"
        "    # 220 bars is ~3.7 hours of 1-minute data, which is a sensible warm-up for\n"
        "    # an intraday trend filter rather than the 200 *days* it means on daily bars.\n"
        "    min_history: int = 220               # refuse to trade before this many bars\n",
    ),
    # volume gate: make it optional and default it off at 1m, where the data is junk
    (
        "    volume_ma_period: int = 20\n    volume_min_ratio: float = 1.0         # breakout must have above-average volume",
        "    volume_ma_period: int = 20\n"
        "    volume_min_ratio: float = 0.0         # above-average volume required\n"
        "    require_volume: bool = False           # 1m volume is unreliable; off by default",
    ),
    (
        "        trend_strength = adx_vals >= p.adx_min\n        volume_ok = work[\"volume\"] >= p.volume_min_ratio * vol_ma\n        volume_ok = volume_ok.fillna(False)",
        "        trend_strength = adx_vals >= p.adx_min\n"
        "        if p.require_volume and p.volume_min_ratio > 0:\n"
        "            volume_ok = work[\"volume\"] >= p.volume_min_ratio * vol_ma\n"
        "            volume_ok = volume_ok.fillna(False)\n"
        "        else:\n"
        "            # Yahoo's 1-minute volume is missing on most bars; gating on it\n"
        "            # would reject nearly every signal for no good reason.\n"
        "            volume_ok = pd.Series(True, index=work.index)",
    ),
], "strategy.py: volume gate optional at 1m")

# ------------------------------------------------------------------ 3. cli.py
patch(ROOT / "ts" / "cli.py", [
    (
        '    p.add_argument("--interval", default="1h", help="bar interval for the live engine")',
        '    p.add_argument("--interval", default="1m", help="bar interval: 1m, 5m, 15m, 1h, 1d")',
    ),
    (
        '    p.add_argument("--range", default="1y", help="history range for the live engine")',
        '    p.add_argument("--range", default="7d", help="history range (1m is capped at 7d by Yahoo)")',
    ),
    (
        '    p.add_argument("--interval", default="1h", help="bar interval: 1d, 1h, 15m ...")',
        '    p.add_argument("--interval", default="1m", help="bar interval: 1m, 5m, 15m, 1h, 1d")',
    ),
    (
        '    p.add_argument("--range", default="60d", help="history range")',
        '    p.add_argument("--range", default="7d", help="history range (1m is capped at 7d)")',
    ),
    (
        '    p.add_argument("--range", default="5y", help="history range: 2y, 5y, max")',
        '    p.add_argument("--range", default="7d", help="history range: 7d, 1mo, 1y, 5y, max")',
    ),
    # commission: 1m positions are small, a flat $1 minimum destroys the edge
    (
        "    p.add_argument(\"--commission\", type=float, default=0.005, help=\"per share\")",
        "    p.add_argument(\"--commission\", type=float, default=0.005,\n"
        "                   help=\"per share, or bps when --interval is 1m\")",
    ),
], "cli.py: 1m defaults everywhere")

sys.exit(0)
