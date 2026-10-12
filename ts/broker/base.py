"""Broker abstraction.

Any execution venue is adapted to these methods.  Everything returns plain
dicts/lists so the engine and the dashboard never depend on a vendor schema.
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import UTC, datetime


@dataclass
class Position:
    """An open position as every broker reports it."""

    symbol: str
    quantity: int
    side: str                      # "long" | "short"
    entry_price: float = 0.0
    current_price: float = 0.0
    market_value: float = 0.0
    unrealized_pnl: float = 0.0
    stop_price: float | None = None
    risk_dollars: float = 0.0
    bars_held: int = 0
    r_open: float | None = None
    extra: dict = field(default_factory=dict)

    @property
    def direction(self) -> float:
        return 1.0 if self.side == "long" else -1.0

    def mark(self, price: float) -> None:
        self.current_price = float(price)
        self.market_value = self.current_price * self.quantity * self.direction
        if self.entry_price:
            self.unrealized_pnl = (
                self.direction * (self.current_price - self.entry_price) * self.quantity
            )
        if self.risk_dollars and self.risk_dollars > 0:
            self.r_open = self.unrealized_pnl / self.risk_dollars

    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "quantity": self.quantity,
            "side": self.side,
            "entry_price": round(self.entry_price, 4),
            "last": round(self.current_price, 4),
            "stop_price": self.stop_price,
            "risk_dollars": round(self.risk_dollars, 2),
            "unrealized_pnl": round(self.unrealized_pnl, 2),
            "r_open": round(self.r_open, 3) if self.r_open is not None else None,
            "bars_held": self.bars_held,
        }


@dataclass
class Order:
    """An order as the engine constructs it."""

    symbol: str
    quantity: int
    side: str                      # "buy" | "sell"
    type: str = "market"           # market | limit | stop | stop_limit
    limit_price: float | None = None
    stop_price: float | None = None
    time_in_force: str = "day"
    client_id: str = ""

    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "qty": self.quantity,
            "side": self.side,
            "type": self.type,
            "limit_price": self.limit_price,
            "stop_price": self.stop_price,
            "time_in_force": self.time_in_force,
            "client_id": self.client_id,
        }


@dataclass
class Fill:
    """What came back from the venue."""

    symbol: str
    quantity: int
    side: str
    price: float
    fees: float = 0.0
    order_id: str = ""
    filled_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())

    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "quantity": self.quantity,
            "side": self.side,
            "price": round(self.price, 4),
            "fees": round(self.fees, 2),
            "order_id": self.order_id,
            "filled_at": self.filled_at,
        }


class BrokerError(RuntimeError):
    """Venue refused or failed an operation."""


class Broker(ABC):
    """The contract the engine codes against."""

    name: str = "abstract"
    mode: str = "paper"          # "paper" | "live"
    supports_shorting: bool = False

    @abstractmethod
    def account(self) -> dict:
        """Equity, cash, and buying power."""

    @abstractmethod
    def positions(self) -> list[Position]:
        """Currently open positions."""

    @abstractmethod
    def submit(self, order: Order) -> Fill:
        """Place an order and return the fill."""

    @abstractmethod
    def market_price(self, symbol: str) -> float:
        """Latest usable price for sizing and marking."""

    def is_market_open(self) -> bool:
        """Overridden by venues with a session calendar."""
        return True

    def flatten(self, symbol: str | None = None) -> list[Fill]:
        """Close everything, or one symbol. Used by the kill switch."""
        fills: list[Fill] = []
        for pos in self.positions():
            if symbol and pos.symbol != symbol:
                continue
            fills.append(self.submit(Order(
                symbol=pos.symbol, quantity=pos.quantity,
                side="sell" if pos.side == "long" else "buy", type="market")))
        return fills

    def cancel_all(self) -> None:
        """Open-order cancellation. Optional for venues without one."""

    def next_client_id(self) -> str:
        return f"{self.name}-{int(time.time() * 1000)}"
