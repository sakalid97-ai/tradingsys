"""Paper broker -- simulated execution against a live price feed.

This is the default broker and the only one ever started without an explicit
opt-in.  Fills happen at the current market price plus configurable slippage,
commissions are charged per fill, and position/P&L accounting is exactly the
same arithmetic the backtester uses -- so paper results are directly comparable
to backtest results.
"""

from __future__ import annotations

import random
from dataclasses import dataclass

from .base import Broker, BrokerError, Fill, Order, Position


@dataclass
class PaperBrokerConfig:
    starting_cash: float = 100_000.0
    slippage_bps: float = 5.0
    commission_per_share: float = 0.005
    commission_min: float = 1.0
    allow_shorting: bool = False
    deterministic: bool = True   # seeds slippage jitter for reproducible runs


class PaperBroker(Broker):
    """Executes orders against prices supplied by a quote function."""

    name = "paper"
    mode = "paper"
    supports_shorting = False

    def __init__(self, price_fn, config: PaperBrokerConfig | None = None) -> None:
        self.price_fn = price_fn                # callable(symbol) -> float
        self.cfg = config or PaperBrokerConfig()
        self.supports_shorting = self.cfg.allow_shorting
        self._rng = random.Random(1234 if self.cfg.deterministic else None)
        self.cash = float(self.cfg.starting_cash)
        self.realised_pnl = 0.0
        self.total_fees = 0.0
        self._positions: dict[str, Position] = {}
        self._client_seq = 0

    # ------------------------------------------------------------------ venue

    def market_price(self, symbol: str) -> float:
        price = self.price_fn(symbol)
        if not price or price <= 0:
            raise BrokerError(f"no price for {symbol}")
        return float(price)

    def account(self) -> dict:
        equity = self.cash + sum(p.market_value for p in self._positions.values())
        return {
            "equity": round(equity, 2),
            "cash": round(self.cash, 2),
            "buying_power": round(self.cash, 2),
            "realised_pnl": round(self.realised_pnl, 2),
            "total_fees": round(self.total_fees, 2),
            "n_positions": len(self._positions),
            "currency": "USD",
        }

    def positions(self) -> list[Position]:
        out: list[Position] = []
        for pos in self._positions.values():
            try:
                pos.mark(self.market_price(pos.symbol))
            except BrokerError:
                pass
            out.append(pos)
        return out

    def is_market_open(self) -> bool:
        return True   # the engine enforces the trading calendar itself

    # ---------------------------------------------------------------- trading

    def submit(self, order: Order) -> Fill:
        if order.quantity <= 0:
            raise BrokerError("quantity must be positive")
        price = self.market_price(order.symbol)

        if order.side == "buy":
            fill_price = self._with_slippage(price, order.side)
            fee = max(self.cfg.commission_min,
                      self.cfg.commission_per_share * order.quantity)
            cost = order.quantity * fill_price + fee
            if cost > self.cash + 1e-6:
                raise BrokerError(f"insufficient cash for {order.symbol}: "
                                  f"need {cost:.2f}, have {self.cash:.2f}")

            existing = self._positions.get(order.symbol)
            if existing and existing.side == "short":
                self._cover(order, price)
                existing = None
            if existing and existing.side == "long":
                # Scale in: average the entry price.
                total_qty = existing.quantity + order.quantity
                existing.entry_price = (
                    existing.entry_price * existing.quantity + fill_price * order.quantity
                ) / total_qty
                existing.quantity = total_qty
                existing.mark(fill_price)
            else:
                self._positions[order.symbol] = Position(
                    symbol=order.symbol,
                    quantity=order.quantity,
                    side="long",
                    entry_price=fill_price,
                    stop_price=order.stop_price,
                    risk_dollars=(abs(fill_price - order.stop_price) * order.quantity
                                  if order.stop_price else 0.0),
                )
                self._positions[order.symbol].mark(fill_price)
            self.cash -= cost
            self.total_fees += fee
        else:
            pos = self._positions.get(order.symbol)
            if pos is None:
                raise BrokerError(f"no position in {order.symbol} to sell")
            if pos.side == "short":
                self._cover(order, price)
            else:
                qty = min(order.quantity, pos.quantity)
                fill_price = self._with_slippage(price, order.side)
                exit_fee = max(self.cfg.commission_min,
                               self.cfg.commission_per_share * qty)
                entry_fee = max(self.cfg.commission_min,
                                self.cfg.commission_per_share * qty)
                fee = exit_fee  # the fee reported on this fill's execution
                self.realised_pnl += (fill_price - pos.entry_price) * qty - entry_fee - exit_fee
                self.cash += qty * fill_price - exit_fee
                self.total_fees += exit_fee
                pos.quantity -= qty
                pos.mark(fill_price)
                if pos.quantity <= 0:
                    del self._positions[order.symbol]

        self._client_seq += 1
        fill = Fill(
            symbol=order.symbol,
            quantity=order.quantity,
            side=order.side,
            price=round(price, 4),
            fees=round(fee, 2),
            order_id=order.client_id or f"paper-{self._client_seq}",
        )
        return fill

    def _cover(self, order: Order, price: float) -> None:
        pos = self._positions[order.symbol]
        qty = min(order.quantity, pos.quantity)
        fill_price = self._with_slippage(price, order.side)
        fee = max(self.cfg.commission_min, self.cfg.commission_per_share * qty)
        self.realised_pnl += (pos.entry_price - fill_price) * qty - fee
        self.cash += qty * fill_price - fee
        self.total_fees += fee
        pos.quantity -= qty
        if pos.quantity <= 0:
            del self._positions[order.symbol]

    def _with_slippage(self, price: float, side: str) -> float:
        """Slippage plus a little jitter, always against the fill."""
        jitter = 0.0 if self.cfg.deterministic else self._rng.uniform(0, 0.4)
        bps = self.cfg.slippage_bps * (1.0 + jitter)
        adj = price * bps / 10_000.0
        return round(price + adj if side == "buy" else price - adj, 4)

    # ------------------------------------------------------------- reporting

    def set_stop(self, symbol: str, stop_price: float) -> None:
        pos = self._positions.get(symbol)
        if pos:
            pos.stop_price = round(float(stop_price), 4)

    def snapshot(self) -> dict:
        acct = self.account()
        acct["positions"] = [p.to_dict() for p in self.positions()]
        return acct


__all__ = ["PaperBroker", "PaperBrokerConfig"]
