"""Risk management.

Two layers:

**Position level** -- every trade risks exactly ``risk_per_trade`` of current
equity.  Position size is *not* chosen first and risk derived second (that is
backwards and produces wildly different risk per trade across instruments);
here risk is fixed and size is derived from the stop distance::

    risk_dollars  = equity * risk_per_trade
    stop_distance = entry_price - stop_price        (ATR-based)
    quantity      = risk_dollars / stop_distance

**Portfolio level** -- hard limits that override strategy signals, because a
portfolio of individually-correct trades can still ruin an account:

    * ``max_positions``      simultananeous open trades
    * ``max_total_risk``     sum of per-trade risk (caps the correlated blowup)
    * ``daily_loss_limit``   hit it -> flatten and stop for the session
    * ``max_drawdown_halt``  hit it -> freeze new entries entirely
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import pandas as pd


@dataclass
class RiskConfig:
    """Portfolio-wide risk parameters."""

    risk_per_trade: float = 0.01          # 1% of equity at risk per trade
    max_position_pct: float = 0.20        # no single position > 20% of equity
    max_positions: int = 10
    max_total_risk: float = 0.06          # 6 x 1% simultaneously at risk
    max_position_value_pct: float = 0.95  # cash constraint
    daily_loss_limit: float = 0.03        # -3% on the day -> flatten + halt
    max_drawdown_halt: float = 0.15      # -15% from peak -> freeze entries
    allow_fractional: bool = False
    commission_per_share: float = 0.005   # USD
    commission_min: float = 1.0          # minimum commission per order
    slippage_bps: float = 5.0            # modelled slippage, basis points

    def clamp(self) -> "RiskConfig":
        if not (0 < self.risk_per_trade < 0.5):
            raise ValueError("risk_per_trade must be in (0, 0.5)")
        if not (0 < self.max_position_pct <= 1.0):
            raise ValueError("max_position_pct must be in (0, 1]")
        self.max_positions = max(1, int(self.max_positions))
        self.max_total_risk = max(self.risk_per_trade, float(self.max_total_risk))
        return self


@dataclass
class OrderDecision:
    """What the sizer decided, and why (for the journal and for tests)."""

    symbol: str
    side: str                      # "buy" | "sell"
    quantity: int
    intended_price: float
    stop_price: float
    risk_dollars: float
    position_value: float
    fractional_qty: float | None = None   # set when fractional shares are allowed
    approved: bool = True
    rejection: str = ""

    def __str__(self) -> str:
        if not self.approved:
            return f"REJECT {self.symbol}: {self.rejection}"
        return (
            f"{self.side.upper():4} {self.quantity:>6} {self.symbol:<6} "
            f"@{self.intended_price:.2f} stop={self.stop_price:.2f} "
            f"risk={self.risk_dollars:,.0f}"
        )


class PositionSizer:
    """Turns a stop distance into a share quantity for a fixed money risk."""

    def __init__(self, config: RiskConfig | None = None) -> None:
        self.config = (config or RiskConfig()).clamp()

    def quantity_for_risk(
        self,
        equity: float,
        price: float,
        stop_distance: float,
        symbol: str = "?",
        side: str = "buy",
        max_position_pct: float | None = None,
    ) -> OrderDecision:
        """Quantity that risks exactly ``risk_per_trade`` of ``equity``.

        ``stop_distance`` is the distance from entry to stop in price units.
        Returns quantity 0 with ``approved=False`` when the trade is untakeable.
        """
        if equity <= 0:
            return self._reject(symbol, side, price, stop_distance, 0, "equity <= 0")
        if price <= 0:
            return self._reject(symbol, side, price, stop_distance, 0, "price <= 0")
        if stop_distance <= 0:
            return self._reject(symbol, side, price, stop_distance, 0, "stop distance <= 0")

        risk_dollars = equity * self.config.risk_per_trade
        raw_qty = risk_dollars / stop_distance

        cap_pct = self.config.max_position_pct if max_position_pct is None else max_position_pct
        max_value = equity * cap_pct
        max_qty_by_value = max_value / price
        capped_by_size = bool(raw_qty > max_qty_by_value)
        raw_qty = min(raw_qty, max_qty_by_value)

        if self.config.allow_fractional:
            qty = float(raw_qty)
        else:
            qty = float(math.floor(raw_qty))

        if qty <= 0:
            return self._reject(
                symbol,
                side,
                price,
                stop_distance,
                0,
                "risk budget does not buy one share (reduce stop distance or add capital)",
            )

        # When the position-size cap binds, realised risk is below the target;
        # report what is actually risked instead of the target.
        realised_risk = qty * stop_distance

        return OrderDecision(
            symbol=symbol,
            side=side,
            quantity=int(qty) if not self.config.allow_fractional else int(round(qty)),
            fractional_qty=qty if self.config.allow_fractional else None,
            intended_price=float(price),
            stop_price=float(price - stop_distance if side == "buy" else price + stop_distance),
            risk_dollars=float(realised_risk),
            position_value=float(qty) * float(price),
            approved=True,
            rejection="capped by max position size" if capped_by_size else "",
        )

    def _reject(self, symbol, side, price, stop_distance, qty, reason) -> OrderDecision:
        return OrderDecision(
            symbol=symbol,
            side=side,
            quantity=qty,
            fractional_qty=None,
            intended_price=float(price),
            stop_price=float(price - stop_distance if side == "buy" else price + stop_distance),
            risk_dollars=0.0,
            position_value=0.0,
            approved=False,
            rejection=reason,
        )


def decision_value(qty, price: float) -> float:
    return float(qty) * float(price)


@dataclass
class PortfolioRiskState:
    """Snapshot the guard evaluates against."""

    equity: float
    peak_equity: float
    cash: float
    open_risk: float = 0.0          # dollars currently at risk across open trades
    open_positions: int = 0
    day_start_equity: float = 0.0
    halted: bool = False
    halt_reason: str = ""
    halted_day: str = ""

    @property
    def drawdown(self) -> float:
        if self.peak_equity <= 0:
            return 0.0
        return max(0.0, (self.peak_equity - self.equity) / self.peak_equity)

    @property
    def day_pl(self) -> float:
        base = self.day_start_equity or self.equity
        return (self.equity - base) / base if base else 0.0


class PortfolioGuard:
    """The circuit breaker. Every candidate order passes through ``approve``."""

    def __init__(self, config: RiskConfig | None = None) -> None:
        self.config = (config or RiskConfig()).clamp()

    def approve(self, state: PortfolioRiskState, order: OrderDecision) -> OrderDecision:
        """Mutate/approve an :class:`OrderDecision` against portfolio limits."""
        if state.halted:
            order.approved = False
            order.rejection = f"portfolio halted: {state.halt_reason or 'unknown'}"
            return order

        if state.day_start_equity > 0:
            if state.day_pl <= -self.config.daily_loss_limit:
                order.approved = False
                order.rejection = (
                    f"daily loss limit hit ({state.day_pl:.2%} <= "
                    f"{-self.config.daily_loss_limit:.2%})"
                )
                return order

        if state.drawdown >= self.config.max_drawdown_halt:
            order.approved = False
            order.rejection = f"drawdown halt ({state.drawdown:.2%})"
            return order

        if state.open_positions >= self.config.max_positions:
            order.approved = False
            order.rejection = f"max positions ({state.open_positions}>={self.config.max_positions})"
            return order

        if state.open_risk + order.risk_dollars > state.equity * self.config.max_total_risk + 1e-9:
            order.approved = False
            order.rejection = (
                f"max total risk ({state.open_risk / max(state.equity, 1):.2%} existing + "
                f"{order.risk_dollars / max(state.equity, 1):.2%} new > "
                f"{self.config.max_total_risk:.2%})"
            )
            return order

        if order.position_value > state.cash + 1e-9:
            # Reduce to what cash allows, then re-check that it is still 1 share.
            affordable = int(state.cash / order.intended_price) if order.intended_price > 0 else 0
            if affordable <= 0:
                order.approved = False
                order.rejection = f"insufficient cash ({state.cash:.2f} < {order.position_value:.2f})"
                return order
            target_qty = min(order.quantity, affordable)
            shrink(target=order, qty=target_qty, price=order.intended_price, cfg=self.config)
        return order


def shrink(target: OrderDecision, qty: int, price: float, cfg: RiskConfig) -> None:
    """Rescale an approved order to ``qty`` shares, preserving honest risk."""
    target.quantity = int(qty)
    target.fractional_qty = None  # type: ignore[assignment]
    target.position_value = float(qty) * float(price)
    stop_distance = abs(target.intended_price - target.stop_price)
    target.risk_dollars = float(qty) * stop_distance


@dataclass
class DrawdownMonitor:
    """Tracks peak equity and raises the brake when the account degrades.

    ``halt_at`` is the threshold; ``peak_drawdown`` is the worst drawdown
    observed so far.  They are separate fields on purpose -- conflating them
    makes the monitor fire immediately.
    """

    halt_at: float = 0.15
    peak_drawdown: float = 0.0
    peak_equity: float = 0.0
    _triggered: bool = field(default=False, init=False)

    def update(self, equity: float) -> float:
        self.peak_equity = max(self.peak_equity, equity)
        if self.peak_equity > 0:
            self.peak_drawdown = max(
                self.peak_drawdown, (self.peak_equity - equity) / self.peak_equity
            )
        self._triggered = self.peak_drawdown >= self.halt_at
        return self.peak_drawdown

    @property
    def drawdown(self) -> float:
        return self.peak_drawdown

    def should_halt(self) -> bool:
        return self._triggered


@dataclass
class DailyPnlGate:
    """Flattens and stops trading if the session loss exceeds ``limit``."""

    limit: float = 0.03
    day_start_equity: float = 0.0
    _triggered: bool = field(default=False, init=False)

    def open_day(self, equity: float) -> None:
        self.day_start_equity = equity
        self._triggered = False

    def breach(self, equity: float) -> bool:
        if self.day_start_equity <= 0:
            return False
        loss = (equity - self.day_start_equity) / self.day_start_equity
        self._triggered = loss <= -self.limit
        return self._triggered


__all__ = [
    "RiskConfig",
    "OrderDecision",
    "PositionSizer",
    "PortfolioRiskState",
    "PortfolioGuard",
    "DrawdownMonitor",
    "DailyPnlGate",
]
