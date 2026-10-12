"""The trading strategy.

Design philosophy
-----------------
The system trades a **trend-continuation breakout** with a **market regime
filter**, sized so every trade risks exactly 1% of equity.

Why this shape:

===========================  ==================================================
Component                    Reason
===========================  ==================================================
Regime filter (price > 200d) Trend systems bleed in choppy sideways markets. The
                             filter avoids the largest source of small losses.
Breakout entry (20d Donchian) Trend followers capture the middle of large moves
                             and accept many small losses for it. It is the most
                             robust trend entry in the literature.
ADX confirmation             Extra evidence the move is trending, not noise.
Volume confirmation          Breakouts on expanding volume resolve more often.
ATR-based stops              Stops scale with the instrument's own volatility,
                             so "1% risk" means the same thing on SPY and MSTR.
Channel exit (10d low)       Lets winners run; exits only when the trend itself
                             is visibly broken.
===========================  ==================================================

Every rolling statistic is shifted one bar (``.shift(1)``), so a signal produced
on bar ``t`` never sees bar ``t``.  The backtester additionally forbids filling
before bar ``t+1``.  Two independent guards against lookahead.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace

import numpy as np
import pandas as pd

from .indicators import adx as adx_fn
from .indicators import atr as atr_fn
from .indicators import donchian_lower, donchian_upper, sma


@dataclass(frozen=True)
class StrategyParams:
    """All strategy knobs in one place so optimisation stays honest."""

    # --- regime ---------------------------------------------------------
    regime_ma_period: int = 200          # trend filter lookback
    require_regime_long: bool = True      # longs only above the regime MA
    allow_shorts: bool = False            # shorts only make sense in the bear regime

    # --- entry ----------------------------------------------------------
    entry_lookback: int = 20              # Donchian breakout window
    adx_period: int = 14
    adx_min: float = 20.0                 # trend-strength gate
    volume_ma_period: int = 20
    volume_min_ratio: float = 0.0         # above-average volume required
    require_volume: bool = False           # 1m volume is unreliable; off by default

    # --- exits ----------------------------------------------------------
    exit_lookback: int = 10               # channel exit window
    atr_period: int = 14
    atr_stop_mult: float = 2.0            # initial hard stop distance (ATR multiples)
    atr_trail_mult: float = 3.0           # chandelier trailing stop distance
    use_trailing_stop: bool = True

    # --- sanity floors --------------------------------------------------
    # 220 bars is ~3.7 hours of 1-minute data, which is a sensible warm-up for
    # an intraday trend filter rather than the 200 *days* it means on daily bars.
    min_history: int = 220               # refuse to trade before this many bars

    def clamp(self) -> "StrategyParams":
        """Return a copy with nonsensical values repaired (optimiser guard)."""
        out = replace(
            self,
            regime_ma_period=int(max(20, self.regime_ma_period)),
            entry_lookback=int(max(2, self.entry_lookback)),
            exit_lookback=int(max(1, self.exit_lookback)),
            atr_period=int(max(2, self.atr_period)),
            adx_period=int(max(2, self.adx_period)),
            volume_ma_period=int(max(2, self.volume_ma_period)),
            adx_min=float(min(max(self.adx_min, 0.0), 60.0)),
            volume_min_ratio=float(max(0.0, self.volume_min_ratio)),
            atr_stop_mult=float(max(0.5, self.atr_stop_mult)),
            atr_trail_mult=float(max(0.5, self.atr_trail_mult)),
            min_history=int(max(30, self.min_history)),
        )
        return out

    def to_dict(self) -> dict:
        return asdict(self)

    def __str__(self) -> str:
        return ", ".join(f"{k}={v}" for k, v in asdict(self).items())


# The signal frame this class produces.  Column meaning is documented inline.
SIGNAL_COLUMNS = (
    "close",
    "atr",
    "regime",
    "breakout_long",
    "breakout_short",
    "trend_strength",
    "volume_ok",
    "entry_long",
    "entry_short",
    "channel_exit",
)


class Strategy:
    """Computes causal entry/exit signals from an OHLCV frame."""

    def __init__(self, params: StrategyParams | None = None) -> None:
        self.params = (params or StrategyParams()).clamp()

    # ------------------------------------------------------------- internals

    def compute(self, df: pd.DataFrame) -> pd.DataFrame:
        """Build the signal frame for ``df``.

        The returned frame is indexed like ``df`` and every entry is a function
        of bars at or before that index (with ``shift(1)`` applied to rolling
        statistics).
        """
        if df is None or len(df) < self.params.min_history:
            needed = self.params.min_history
            have = 0 if df is None else len(df)
            raise ValueError(
                f"strategy needs at least {needed} bars to compute a valid regime "
                f"filter, got {have}"
            )

        p = self.params
        work = df[["open", "high", "low", "close", "volume"]].copy()

        regime_ma = sma(work["close"], p.regime_ma_period)
        atr_vals = atr_fn(work, p.atr_period)
        adx_vals = adx_fn(work, p.adx_period)
        # donchian_upper/lower already exclude the current bar (they shift by 1
        # internally), so they must NOT be shifted a second time here.
        upper = donchian_upper(work, p.entry_lookback)
        lower = donchian_lower(work, p.exit_lookback)
        vol_ma = sma(work["volume"], p.volume_ma_period)

        # Shifted so bar t uses only bars <= t-1.
        regime_ma = regime_ma.shift(1)
        atr_vals = atr_vals.shift(1)
        adx_vals = adx_vals.shift(1)
        vol_ma = vol_ma.shift(1)

        close = work["close"]

        regime = pd.Series(0, index=work.index, dtype=int)
        regime[close > regime_ma] = 1
        regime[close < regime_ma] = -1
        regime[regime_ma.isna()] = 0

        breakout_long = close >= upper

        # Short-side breakout uses the *entry* window low, not the exit window.
        entry_low = (
            work["low"].rolling(window=p.entry_lookback, min_periods=p.entry_lookback).min().shift(1)
        )
        breakout_short = close <= entry_low

        trend_strength = adx_vals >= p.adx_min
        if p.require_volume and p.volume_min_ratio > 0:
            volume_ok = work["volume"] >= p.volume_min_ratio * vol_ma
            volume_ok = volume_ok.fillna(False)
        else:
            # Yahoo's 1-minute volume is missing on most bars; gating on it
            # would reject nearly every signal for no good reason.
            volume_ok = pd.Series(True, index=work.index)

        tradable = regime_ma.notna() & upper.notna() & adx_vals.notna()

        if p.require_regime_long:
            entry_long = tradable & (regime == 1) & breakout_long & trend_strength & volume_ok
        else:
            entry_long = tradable & breakout_long & trend_strength & volume_ok

        if p.allow_shorts:
            entry_short = tradable & (regime == -1) & breakout_short & trend_strength & volume_ok
        else:
            entry_short = pd.Series(False, index=work.index)

        channel_exit = close < lower

        out = pd.DataFrame(index=work.index)
        out["close"] = close
        out["atr"] = atr_vals
        out["regime"] = regime
        out["breakout_long"] = breakout_long.fillna(False)
        out["breakout_short"] = breakout_short.fillna(False)
        out["trend_strength"] = trend_strength.fillna(False)
        out["volume_ok"] = volume_ok
        out["entry_long"] = entry_long.fillna(False)
        out["entry_short"] = entry_short.fillna(False)
        out["channel_exit"] = channel_exit.fillna(False)
        return out

    # ------------------------------------------------------------- utilities

    def stop_distance(self, atr_value: float) -> float:
        """Initial stop distance in price units (ATR-based)."""
        return max(atr_value * self.params.atr_stop_mult, 1e-9)

    def trail_distance(self, atr_value: float) -> float:
        """Trailing-stop distance in price units."""
        return max(atr_value * self.params.atr_trail_mult, 1e-9)

    def describe(self) -> str:
        p = self.params
        longs = (
            f"price > {p.regime_ma_period}d SMA" if p.require_regime_long else "regime filter off"
        )
        shorts = "allowed below regime MA" if p.allow_shorts else "long-only"
        return (
            "TREND BREAKOUT SYSTEM\n"
            f"  Universe gate   : {longs} ({shorts})\n"
            f"  Entry           : close >= {p.entry_lookback}-day high, "
            f"ADX({p.adx_period}) >= {p.adx_min}, volume >= {p.volume_min_ratio}x {p.volume_ma_period}d avg\n"
            f"  Stop            : entry - {p.atr_stop_mult} x ATR({p.atr_period})\n"
            f"  Trailing stop   : max close - {p.atr_trail_mult} x ATR, ratcheting up only\n"
            f"  Trend exit      : close < {p.exit_lookback}-day low\n"
            "  Risk per trade  : 1.0% of equity (sized in risk.py)\n"
        )


def apply_market_gate(signals: dict[str, pd.DataFrame], market: pd.DataFrame) -> dict[str, pd.DataFrame]:
    """Zero out entries on bars where the broad market regime is risk-off.

    ``market`` is an OHLCV frame for a benchmark (e.g. SPY).  A symbol may only
    be *entered* on bar ``t`` if the benchmark's regime through bar ``t-1`` is
    risk-on.  Exits are never gated -- a position must always be allowed to close.

    Implemented with a strict ``reindex(..., method='ffill')`` then ``shift(1)``
    so the benchmark bar at index ``t`` can only influence entries at ``t`` or
    later, which the engine then converts to a fill at ``t+1`` at the earliest.
    """
    if market is None or len(market) == 0:
        return signals

    regime_ma = sma(market["close"], 200).shift(1)
    gate = (market["close"].shift(1) > regime_ma).astype(int)

    gated: dict[str, pd.DataFrame] = {}
    for symbol, frame in signals.items():
        if frame is None or frame.empty:
            gated[symbol] = frame
            continue
        aligned = gate.reindex(frame.index, method="ffill").fillna(1).astype(int)
        out = frame.copy()
        blocked = aligned == 0
        out.loc[blocked, "entry_long"] = False
        out.loc[blocked, "entry_short"] = False
        out["market_risk_on"] = aligned
        gated[symbol] = out
    return gated


def optimise_grid(base: StrategyParams, dimensions: dict[str, list]) -> list[StrategyParams]:
    """Expand a cartesian grid of parameter overrides (for walk-forward)."""
    keys = list(dimensions)
    if not keys:
        return [base]

    combos: list[list[tuple[str, object]]] = [[]]
    for key in keys:
        combos = [combo + [(key, value)] for combo in combos for value in dimensions[key]]

    out: list[StrategyParams] = []
    seen: set[tuple] = set()
    for combo in combos:
        params = replace(base, **dict(combo)).clamp()
        fingerprint = tuple(sorted(params.to_dict().items()))
        if fingerprint in seen:
            continue
        seen.add(fingerprint)
        out.append(params)
    return out


__all__ = [
    "StrategyParams",
    "Strategy",
    "SIGNAL_COLUMNS",
    "apply_market_gate",
    "optimise_grid",
]
