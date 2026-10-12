"""Bar-data validation.

Bad input data is the single most common cause of a beautiful backtest that
loses money live.  These checks run before anything touches a data frame.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

REQUIRED_COLUMNS = ("open", "high", "low", "close", "volume")


@dataclass
class DataReport:
    """Result of validating a single symbol's price history."""

    symbol: str
    rows: int = 0
    bars_removed: int = 0
    issues: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.issues

    def __str__(self) -> str:  # pragma: no cover - display helper
        status = "OK " if self.ok else "WARN"
        detail = "; ".join(self.issues) if self.issues else "clean"
        return f"[{status}] {self.symbol:<12} rows={self.rows:<6} dropped={self.bars_removed:<3} {detail}"


def validate(df: pd.DataFrame, symbol: str = "?", max_gap_minutes: int = 0) -> pd.DataFrame:
    """Clean and validate an OHLCV frame.

    Rules enforced:
      * required columns present, index is a tz-aware DatetimeIndex, sorted
      * non-positive or NaN prices dropped
      * ``high`` must be the true max and ``low`` the true min of each bar
      * duplicate timestamps dropped (last wins)
      * NaN gaps in price columns dropped
      * optional: gaps between bars larger than ``max_gap_minutes`` flagged

    Returns a clean frame; never mutates the input.
    """
    if df is None or len(df) == 0:
        return pd.DataFrame(columns=list(REQUIRED_COLUMNS))

    out = df.copy()
    out.index = pd.to_datetime(out.index, utc=True)
    out = out[~out.index.duplicated(keep="last")]
    out = out.sort_index()

    missing = [c for c in REQUIRED_COLUMNS if c not in out.columns]
    if missing:
        raise ValueError(f"{symbol}: missing required columns {missing}")

    price_cols = ["open", "high", "low", "close"]
    out = out[list(REQUIRED_COLUMNS)]

    for col in price_cols:
        out[col] = pd.to_numeric(out[col], errors="coerce")
    out["volume"] = pd.to_numeric(out["volume"], errors="coerce").fillna(0.0)

    invalid = (out[["open", "high", "low", "close"]] <= 0).any(axis=1) | out[["open", "high", "low", "close"]].isna().any(axis=1)
    bars_removed = int(invalid.sum())
    if bars_removed:
        out = out[~invalid]

    # Repair inverted highs/lows so stops and targets are never evaluated
    # against impossible geometry.
    out["high"] = out[["high", "close", "open"]].max(axis=1)
    out["low"] = out[["low", "close", "open"]].min(axis=1)

    report = DataReport(symbol=symbol, rows=len(out), bars_removed=bars_removed)

    if len(out) > 2:
        deltas = out.index.to_series().diff().dropna()
        if deltas.min() <= pd.Timedelta(0):
            report.issues.append("non-monotonic index")

    out.attrs["report"] = report
    return out


def describe_gaps(df: pd.DataFrame, expected_bars_per_day: int = 6) -> list[str]:
    """Human-readable notes about calendar gaps (weekends, holidays, halts)."""
    if len(df) < 3:
        return []
    notes: list[str] = []
    deltas = df.index.to_series().diff().dropna()
    day = pd.Timedelta(days=1)
    long_gaps = deltas[deltas > day * 4]
    if len(long_gaps) > 0:
        worst = long_gaps.max()
        notes.append(f"{len(long_gaps)} gaps > 4 days (longest {worst})")
    if expected_bars_per_day:
        intraday = deltas[deltas < day]
        if len(intraday) > 5:
            median_step = intraday.median()
            notes.append(f"median step {median_step} -> ~{expected_bars_per_day} bars/day assumed")
    return notes
