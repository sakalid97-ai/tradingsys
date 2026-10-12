"""Execution and position-management agent.

Owns the actual trade lifecycle once a plan is approved:

* submits the entry through the broker,
* immediately places and then ratchets the stop,
* manages the target (trailing or fixed),
* closes when either is hit and records the trade,
* publishes FILL / POSITION / ORDER / TRADE_PLAN results onto the bus.

Management happens here rather than in the broker so that the same stop logic
runs identically in backtest, paper, and live modes.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime

import pandas as pd

from .base import Agent
from .bus import Topic

log = logging.getLogger(__name__)


@dataclass
class ManagedTrade:
    """An open trade with its live stop and target."""

    symbol: str
    side: str
    quantity: float
    entry_price: float
    stop_price: float
    initial_stop: float
    target_price: float
    risk_dollars: float
    atr: float
    opened_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    realized_pnl: float = 0.0
    last_price: float = 0.0
    # Timestamp of the bar that was already closed when the entry was placed.
    # Exits must only ever be evaluated on bars completed AFTER this one,
    # otherwise the bar that triggered the entry can be used to close it --
    # textbook lookahead.
    entry_bar_time: str | None = None

    @property
    def direction(self) -> float:
        return 1.0 if self.side == "long" else -1.0

    def open_r(self, price: float | None = None) -> float:
        px = price if price is not None else self.last_price
        if self.risk_dollars <= 0:
            return 0.0
        return (self.direction * (px - self.entry_price) * self.quantity) / self.risk_dollars

    def unrealized(self, price: float | None = None) -> float:
        px = price if price is not None else self.last_price
        return self.direction * (px - self.entry_price) * self.quantity

    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "side": self.side,
            "quantity": round(self.quantity, 8),
            "entry_price": round(self.entry_price, 6),
            "stop_price": round(self.stop_price, 6),
            "target_price": round(self.target_price, 6),
            "last_price": round(self.last_price, 6),
            "risk_dollars": round(self.risk_dollars, 2),
            "open_r": round(self.open_r(), 3),
            "unrealized_pnl": round(self.unrealized(), 2),
            "opened_at": self.opened_at,
        }


class ExecutionAgent(Agent):
    """Takes approved TRADE_PLANs and manages them to completion."""

    name = "execution"
    role = "execution and trade management"
    subscribes = (Topic.TRADE_PLAN.value, Topic.MARKET_DATA.value)

    def __init__(self, bus, broker, feed, dry_run: bool = True,
                 use_trailing: bool = True, trail_atr_mult: float = 2.5) -> None:
        super().__init__(bus)
        self.broker = broker
        self.feed = feed
        self.dry_run = dry_run
        self.use_trailing = use_trailing
        self.trail_atr_mult = trail_atr_mult
        self.managed: dict[str, ManagedTrade] = {}
        self.closed: list[dict] = []
        # optional: auto-journal every closed trade for the Forward tab
        self.forward_journal = None
        self.subscribe_all()

    # --------------------------------------------------------------- lifecycle

    def handle(self, message) -> None:
        topic = str(message.topic)
        if topic == Topic.TRADE_PLAN.value:
            self._maybe_enter(message.payload)
        elif topic == Topic.MARKET_DATA.value:
            self._update_prices(message.payload)

    def _maybe_enter(self, plan: dict) -> None:
        symbol = plan.get("symbol")
        side = plan.get("side")
        quantity = plan.get("quantity") or 0.0
        if not symbol or not side or quantity <= 0:
            return
        if symbol in self.managed:
            self.publish(Topic.LOG, {"agent": self.name, "note": f"already in {symbol}"})
            return
        if plan.get("reason", "").startswith("rejected"):
            return

        entry_price = float(plan.get("price", 0.0) or 0.0)
        if entry_price <= 0:
            try:
                entry_price = self.feed.fetch(symbol).close()
            except Exception:  # noqa: BLE001
                self.alarm(f"no price for {symbol}")
                return

        # Record the timestamp of the bar that was already closed when we
        # entered, so exits are only ever evaluated on strictly newer bars.
        entry_bar_time = None
        cached = self.feed.cached(symbol)
        if cached is not None and not cached.frame.empty:
            entry_bar_time = str(cached.last_bar_time)

        stop_distance = float(plan.get("stop_distance", 0.0) or 0.0)
        target_distance = float(plan.get("target_distance", 0.0) or 0.0)
        direction = 1.0 if side == "long" else -1.0
        stop_price = entry_price + direction * stop_distance
        target_price = entry_price + direction * target_distance
        risk_dollars = float(plan.get("kelly_risk", 0.0) or 0.0)

        if self.dry_run:
            log.info("[dry-run] would %s %s qty=%s @%.6f stop=%.6f target=%.6f",
                     side, symbol, quantity, entry_price, stop_price, target_price)
            self.publish(Topic.LOG, {
                "agent": self.name, "dry_run": True, "side": side, "symbol": symbol,
                "quantity": quantity, "price": entry_price, "stop": stop_price,
                "target": target_price,
            })
        else:
            try:
                fill = self._submit(symbol, side, quantity)
                entry_price = float(fill.price)
                stop_price = entry_price + direction * stop_distance
            except Exception as exc:  # noqa: BLE001
                self.alarm(f"entry failed for {symbol}: {exc}")
                self.publish(Topic.RISK, {"agent": self.name, "symbol": symbol,
                                          "error": str(exc)})
                return

        self.managed[symbol] = ManagedTrade(
            symbol=symbol, side=side, quantity=quantity, entry_price=entry_price,
            stop_price=stop_price, initial_stop=stop_price, target_price=target_price,
            risk_dollars=risk_dollars, atr=float(plan.get("atr", 0.0) or 0.0),
            last_price=entry_price, entry_bar_time=entry_bar_time,
        )
        self.publish(Topic.ORDER, {"symbol": symbol, "side": side, "quantity": quantity,
                                   "price": entry_price, "dry_run": self.dry_run})
        self.publish(Topic.FILL, {"symbol": symbol, "side": side, "quantity": quantity,
                                  "price": entry_price, "stop": stop_price,
                                  "target": target_price, "dry_run": self.dry_run})
        self.publish(Topic.POSITION, self.managed[symbol].to_dict())

    def _submit(self, symbol: str, side: str, quantity: float):
        from ..broker.base import Order

        return self.broker.submit(Order(
            symbol=symbol, quantity=int(quantity) if float(quantity).is_integer()
            else float(quantity), side="buy" if side == "long" else "sell",
            type="market"))

    # -------------------------------------------------------------- management

    def _update_prices(self, payload: dict) -> None:
        symbol = payload.get("symbol")
        price = payload.get("last")
        if not symbol or price is None:
            return
        trade = self.managed.get(symbol)
        if trade is None:
            return
        trade.last_price = float(price)

        if self.use_trailing and trade.atr > 0:
            self._ratchet_stop(trade, trade.atr)

        self._check_exit(trade)

    def _ratchet_stop(self, trade: ManagedTrade, atr: float) -> None:
        distance = atr * self.trail_atr_mult
        if trade.side == "long":
            candidate = trade.last_price - distance
            if candidate > trade.stop_price:
                trade.stop_price = candidate
        else:
            candidate = trade.last_price + distance
            if candidate < trade.stop_price:
                trade.stop_price = candidate

    def _last_bar(self, symbol: str, after_ts: str | None = None) -> tuple[float, float, float]:
        """(high, low, close) of the most recent bar completed after ``after_ts``.

        Returns ``(0, 0, 0)`` when there is no newer bar, which the caller must
        treat as "nothing to do yet" rather than as a zero price.
        """
        snap = self.feed.cached(symbol)
        if snap is None:
            try:
                snap = self.feed.fetch(symbol)
            except Exception:  # noqa: BLE001
                return 0.0, 0.0, 0.0
        if snap is None or snap.frame.empty:
            return 0.0, 0.0, 0.0

        frame = snap.frame
        if after_ts:
            cutoff = pd.Timestamp(after_ts)
            newer = frame[frame.index > cutoff]
            if newer.empty:
                return 0.0, 0.0, 0.0
            frame = newer
        try:
            row = frame.iloc[-1]
            return float(row["high"]), float(row["low"]), float(row["close"])
        except (KeyError, IndexError, ValueError):
            return 0.0, 0.0, 0.0

    def _check_exit(self, trade: ManagedTrade) -> None:
        """Exit on the stop or target, evaluated intrabar where possible.

        Using the bar's high/low rather than its close means a spike through the
        stop or target inside the bar is honoured -- the same rule the backtester
        applies, so paper and backtested exits stay comparable.
        """
        high, low, close = self._last_bar(trade.symbol, trade.entry_bar_time)
        if high <= 0.0 and low <= 0.0:
            # No newer bar has closed since the entry -- do not evaluate exits,
            # and above all do not reuse the entry bar's own range.
            return
        trade.last_price = close or trade.last_price

        if trade.side == "long":
            hit_stop = low <= trade.stop_price
            hit_target = high >= trade.target_price
        else:
            hit_stop = high >= trade.stop_price
            hit_target = low <= trade.target_price
        if not (hit_stop or hit_target):
            return

        # A stop fills at the stop; a target fills at the target. When both
        # were touched in the same bar, assume the worse (the stop) came first.
        exit_reason = "stop" if hit_stop else "target"
        exit_price = trade.stop_price if hit_stop else trade.target_price
        trade.last_price = exit_price

        pnl = trade.unrealized()
        record = {
            "symbol": trade.symbol, "side": trade.side, "quantity": trade.quantity,
            "entry_price": trade.entry_price, "exit_price": exit_price,
            "stop_price": trade.initial_stop, "exit_reason": exit_reason,
            "pnl": pnl, "r_multiple": trade.open_r(), "dry_run": self.dry_run,
            "closed_at": datetime.now(UTC).isoformat(),
        }

        if not self.dry_run:
            try:
                self._submit(trade.symbol, trade.side, trade.quantity)
            except Exception as exc:  # noqa: BLE001
                self.alarm(f"exit failed for {trade.symbol}: {exc}")

        self.closed.append(record)
        self._journal(record)
        self.managed.pop(trade.symbol, None)
        self.publish(Topic.POSITION, {"symbol": trade.symbol, "closed": True,
                                      "reason": exit_reason, "pnl": pnl,
                                      "exit_price": exit_price})
        self.publish(Topic.FILL, {**record, "exit": True})

    def _journal(self, record: dict) -> None:
        """Write one closed trade to the forward journal, if one is attached."""
        if self.forward_journal is None:
            return
        try:
            from ..forward import ForwardTrade

            self.forward_journal.record(ForwardTrade(
                symbol=record["symbol"],
                side=record["side"],
                quantity=float(record["quantity"]),
                entry_price=float(record["entry_price"]),
                exit_price=float(record["exit_price"]),
                r_multiple=float(record["r_multiple"]),
                net_pnl=float(record["pnl"]),
                entry_time=str(record.get("opened_at", "")),
                exit_time=str(record.get("closed_at", "")),
                exit_reason=record.get("exit_reason", ""),
                strategy="multi_agent",
                broker="paper" if self.dry_run else "live",
            ))
        except (KeyError, TypeError, ValueError) as exc:
            log.debug("could not journal agent trade: %s", exc)

    # ---------------------------------------------------------------- reporting

    def run(self) -> None:
        """Periodic reconciliation: mark every open trade and publish state."""
        for symbol, trade in list(self.managed.items()):
            try:
                snap = self.feed.fetch(symbol)
                trade.last_price = snap.close()
            except Exception:  # noqa: BLE001
                continue
            if self.use_trailing and trade.atr > 0:
                self._ratchet_stop(trade, trade.atr)
            self._check_exit(trade)
        self.publish(Topic.AGENT_STATUS, {
            "agent": self.name,
            "open": len(self.managed),
            "closed": len(self.closed),
            "dry_run": self.dry_run,
        })

    def snapshot(self) -> dict:
        wins = [c for c in self.closed if c["pnl"] > 0]
        losses = [c for c in self.closed if c["pnl"] <= 0]
        return {
            "open_positions": [t.to_dict() for t in self.managed.values()],
            "closed_trades": list(reversed(self.closed[-200:])),
            "summary": {
                "open": len(self.managed),
                "closed": len(self.closed),
                "wins": len(wins),
                "losses": len(losses),
                "win_rate": round(len(wins) / len(self.closed), 4) if self.closed else 0.0,
                "realized_pnl": round(sum(c["pnl"] for c in self.closed), 2),
                "dry_run": self.dry_run,
            },
        }


__all__ = ["ExecutionAgent", "ManagedTrade"]
