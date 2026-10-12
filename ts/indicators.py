"""Technical indicators.

Every indicator in this module is *causal*: the value at index ``i`` is computed
only from data up to and including index ``i``.  Callers must additionally apply
``.shift(1)`` when an indicator is used to make a trading decision on the same
bar, which the strategy layer does.
"""

from __future__ import annotations

import numpy as np
import pandas as pd


def sma(series: pd.Series, period: int) -> pd.Series:
    """Simple moving average."""
    if period < 1:
        raise ValueError("period must be >= 1")
    return series.rolling(window=period, min_periods=period).mean()


def ema(series: pd.Series, period: int) -> pd.Series:
    """Exponential moving average."""
    if period < 1:
        raise ValueError("period must be >= 1")
    return series.ewm(span=period, min_periods=period, adjust=False).mean()


def true_range(df: pd.DataFrame) -> pd.Series:
    """True range of a bar: max(high-low, |high-prev_close|, |low-prev_close|)."""
    prev_close = df["close"].shift(1)
    ranges = pd.concat(
        [
            df["high"] - df["low"],
            (df["high"] - prev_close).abs(),
            (df["low"] - prev_close).abs(),
        ],
        axis=1,
    )
    return ranges.max(axis=1)


def atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """Average True Range (Wilder smoothing, the industry-standard variant)."""
    tr = true_range(df)
    return tr.ewm(alpha=1.0 / period, min_periods=period, adjust=False).mean()


def rolling_high(series: pd.DataFrame | pd.Series, period: int) -> pd.Series:
    """Highest value of ``series`` over the trailing ``period`` bars (inclusive)."""
    col = series["high"] if isinstance(series, pd.DataFrame) else series
    return col.rolling(window=period, min_periods=period).max()


def rolling_low(series: pd.DataFrame | pd.Series, period: int) -> pd.Series:
    """Lowest value of ``series`` over the trailing ``period`` bars (inclusive)."""
    col = series["low"] if isinstance(series, pd.DataFrame) else series
    return col.rolling(window=period, min_periods=period).min()


def donchian_upper(df: pd.DataFrame, period: int) -> pd.Series:
    """Donchian upper channel. Shifted one bar so the current bar is excluded."""
    return df["high"].rolling(window=period, min_periods=period).max().shift(1)


def donchian_lower(df: pd.DataFrame, period: int) -> pd.Series:
    """Donchian lower channel. Shifted one bar so the current bar is excluded."""
    return df["low"].rolling(window=period, min_periods=period).min().shift(1)


def adx(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """Average Directional Index, Wilder smoothed.

    Measures trend strength (0-100) independent of direction.  Values above
    ~20-25 indicate a trending market; below that, a choppy/ranging one.
    """
    high, low, close = df["high"], df["low"], df["close"]

    up_move = high.diff()
    down_move = -low.diff()

    plus_dm = pd.Series(
        np.where((up_move > down_move) & (up_move > 0), up_move, 0.0), index=df.index
    )
    minus_dm = pd.Series(
        np.where((down_move > up_move) & (down_move > 0), down_move, 0.0), index=df.index
    )

    tr_smoothed = true_range(df).ewm(alpha=1.0 / period, min_periods=period, adjust=False).mean()

    plus_di = 100.0 * plus_dm.ewm(alpha=1.0 / period, min_periods=period, adjust=False).mean() / tr_smoothed
    minus_di = 100.0 * minus_dm.ewm(alpha=1.0 / period, min_periods=period, adjust=False).mean() / tr_smoothed

    dx = 100.0 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0.0, np.nan)
    return dx.ewm(alpha=1.0 / period, min_periods=period, adjust=False).mean()


def rsi(series: pd.Series, period: int = 14) -> pd.Series:
    """Relative Strength Index, Wilder smoothed."""
    delta = series.diff()
    gain = delta.clip(lower=0.0)
    loss = (-delta).clip(lower=0.0)
    avg_gain = gain.ewm(alpha=1.0 / period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1.0 / period, min_periods=period, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0.0, np.nan)
    return 100.0 - (100.0 / (1.0 + rs))


def realized_volatility(df: pd.DataFrame, period: int = 20, annualization: int | None = None) -> pd.Series:
    """Annualised realised volatility of close-to-close log returns."""
    log_ret = np.log(df["close"] / df["close"].shift(1))
    vol = log_ret.rolling(window=period, min_periods=period).std(ddof=0)
    if annualization:
        vol = vol * np.sqrt(annualization)
    return vol


def bollinger_bands(series: pd.Series, period: int = 20, num_std: float = 2.0):
    """(middle, upper, lower) Bollinger bands."""
    middle = sma(series, period)
    sigma = series.rolling(window=period, min_periods=period).std(ddof=0)
    return middle, middle + num_std * sigma, middle - num_std * sigma


def enrich(df: pd.DataFrame) -> pd.DataFrame:
    """Return a copy of ``df`` with the standard indicator set attached.

    Columns added: ``tr``, ``atr``, ``sma_trend``, ``donchian_upper``,
    ``donchian_lower``, ``adx``, ``rsi``, ``vol_sma``.
    """
    out = df.copy()
    out["tr"] = true_range(out)
    out["atr"] = out["tr"].ewm(alpha=1 / 14, min_periods=14, adjust=False).mean()
    out["sma_trend"] = sma(out["close"], 200)
    out["donchian_upper"] = donchian_upper(out, 20)
    out["donchian_lower"] = donchian_lower(out, 10)
    out["adx"] = adx(out, 14)
    out["rsi"] = rsi(out["close"], 14)
    out["vol_sma"] = sma(out["volume"], 20)
    return out
