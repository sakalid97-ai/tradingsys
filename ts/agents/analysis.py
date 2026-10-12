"""Market analysis agent.

Turns raw bars into the features the other agents argue about. This is
deliberately *not* a strategy -- it reports state, it does not decide. A single
feature being "good" is never a reason to trade; only the combination that the
edge detector validates is.

Features produced per symbol:

=================  ==========================================================
``regime``         +1 trending up, -1 trending down, 0 unclear
``regime_strength``  ADX(14) scaled 0..1
``volatility``     ATR as a fraction of price
``trend_quality``  R^2 of close vs time over the window
``momentum``       z-score of short momentum vs long
``efficiency``     Kaufman efficiency ratio (path / displacement)
``stale``          whether the data is too old to trade
=================  ==========================================================
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
import pandas as pd

from ..indicators import adx as adx_fn
from ..indicators import atr as atr_fn
from ..indicators import donchian_lower, donchian_upper, sma
from .base import Agent
from .bus import Topic


@dataclass
class MarketAnalysis:
    """A symbol's analysis at one point in time."""

    symbol: str
    asset_class: str
    price: float
    regime: int
    regime_strength: float
    volatility: float
    trend_quality: float
    momentum: float
    efficiency: float
    breakout_up: bool
    breakout_down: bool
    atr: float
    sma_fast: float
    sma_slow: float
    stale: bool
    bars: int

    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "asset_class": self.asset_class,
            "price": round(self.price, 6),
            "regime": self.regime,
            "regime_label": {1: "UP", -1: "DOWN", 0: "FLAT"}[self.regime],
            "regime_strength": round(self.regime_strength, 3),
            "volatility": round(self.volatility, 5),
            "trend_quality": round(self.trend_quality, 3),
            "momentum": round(self.momentum, 4),
            "efficiency": round(self.efficiency, 4),
            "breakout_up": self.breakout_up,
            "breakout_down": self.breakout_down,
            "atr": round(self.atr, 6),
            "stale": self.stale,
            "bars": self.bars,
        }


def _clip(value: float, lo: float = 0.0, hi: float = 1.0) -> float:
    if value != value:  # NaN
        return lo
    return max(lo, min(hi, value))


class MarketAnalyzer:
    """Pure functions over a bar frame; kept separate so agents stay thin."""

    def __init__(self, fast: int = 20, slow: int = 50, momentum_fast: int = 14,
                 momentum_slow: int = 40, breakout: int = 20) -> None:
        self.fast = fast
        self.slow = slow
        self.momentum_fast = momentum_fast
        self.momentum_slow = momentum_slow
        self.breakout = breakout

    def analyse(self, snapshot) -> MarketAnalysis:
        frame = snapshot.frame
        close = frame["close"]
        price = float(close.iloc[-1])

        atr = atr_fn(frame, 14)
        atr_val = float(atr.iloc[-1]) if len(atr.dropna()) else 0.0
        volatility = atr_val / price if price else 0.0

        adx_val = float(adx_fn(frame, 14).iloc[-1] or 0.0)
        adx_val = 0.0 if adx_val != adx_val else adx_val
        regime_strength = _clip(adx_val / 40.0)

        fast_ma = float(sma(close, self.fast).iloc[-1] or price)
        slow_ma = float(sma(close, self.slow).iloc[-1] or price)
        price_above_slow = price > slow_ma
        fast_above_slow = fast_ma > slow_ma

        if price_above_slow and fast_above_slow:
            regime = 1
        elif (not price_above_slow) and (not fast_above_slow):
            regime = -1
        else:
            regime = 0

        upper = donchian_upper(frame, self.breakout)
        lower = donchian_lower(frame, self.breakout)
        breakout_up = bool(price >= float(upper.iloc[-1])) if upper.notna().iloc[-1] else False
        breakout_down = bool(price <= float(lower.iloc[-1])) if lower.notna().iloc[-1] else False

        return MarketAnalysis(
            symbol=snapshot.symbol,
            asset_class=snapshot.instrument.asset_class.value,
            price=price,
            regime=regime,
            regime_strength=regime_strength,
            volatility=volatility,
            trend_quality=self._r2(close.tail(self.slow)),
            momentum=self._momentum_z(close, self.momentum_fast, self.momentum_slow),
            efficiency=self._efficiency_ratio(close.tail(self.breakout)),
            breakout_up=breakout_up,
            breakout_down=breakout_down,
            atr=atr_val,
            sma_fast=fast_ma,
            sma_slow=slow_ma,
            stale=snapshot.is_stale,
            bars=len(frame),
        )

    @staticmethod
    def _r2(series: pd.Series) -> float:
        """R^2 of a linear fit -- how much of the move is trend, not noise."""
        values = series.dropna()
        if len(values) < 10:
            return 0.0
        y = values.to_numpy(dtype=float)
        x = np.arange(len(y), dtype=float)
        if y.std() == 0:
            return 0.0
        corr = np.corrcoef(x, y)[0, 1]
        return _clip(float(corr * corr))

    @staticmethod
    def _momentum_z(close: pd.Series, fast: int, slow: int) -> float:
        """z-score of short-window momentum against long-window distribution."""
        values = close.dropna()
        if len(values) < slow:
            return 0.0
        fast_ret = float(values.iloc[-1] / values.iloc[-fast - 1] - 1.0)
        slow_rets = values.pct_change(slow).dropna()
        if len(slow_rets) < 20 or slow_rets.std() == 0:
            return 0.0
        return _clip((fast_ret - slow_rets.mean()) / (slow_rets.std() * 3.0), -1.0, 1.0)

    @staticmethod
    def _efficiency_ratio(series: pd.Series) -> float:
        """Kaufman efficiency ratio: net move / total path travelled."""
        values = series.dropna().to_numpy(dtype=float)
        if len(values) < 5:
            return 0.0
        path = float(np.abs(np.diff(values)).sum())
        displacement = float(abs(values[-1] - values[0]))
        if path == 0:
            return 0.0
        return _clip(displacement / path)


class AnalysisAgent(Agent):
    """Publishes a MARKET_REGIME message per symbol on every cycle."""

    name = "analysis"
    role = "market analysis"
    subscribes = (Topic.MARKET_DATA.value,)

    def __init__(self, bus, feed=None) -> None:
        super().__init__(bus)
        self.analyzer = MarketAnalyzer()
        self.feed = feed
        self.latest: dict[str, dict] = {}
        self.subscribe_all()

    def run(self) -> None:
        if self.feed is None:
            return
        symbols = list(self._symbols())
        if not symbols:
            return
        for symbol in symbols:
            snap = self.feed.cached(symbol)
            if snap is None:
                continue
            analysis = self.analyzer.analyse(snap)
            payload = analysis.to_dict()
            self.latest[symbol] = payload
            self.publish(Topic.MARKET_REGIME, payload)

    def _symbols(self) -> set[str]:
        return set(self.latest.keys())

    def handle(self, message) -> None:
        """A new market data snapshot triggers a re-analysis of that symbol."""
        symbol = message.payload.get("symbol")
        if not symbol or self.feed is None:
            return
        snap = self.feed.cached(symbol)
        if snap is None:
            try:
                snap = self.feed.fetch(symbol)
            except Exception:  # noqa: BLE001
                return
        try:
            payload = self.analyzer.analyse(snap).to_dict()
        except Exception as exc:  # noqa: BLE001
            self.alarm(f"analysis failed for {symbol}: {exc}")
            return
        self.latest[symbol] = payload
        self.publish(Topic.MARKET_REGIME, payload)


__all__ = ["MarketAnalyzer", "MarketAnalysis", "AnalysisAgent"]
