"""Bar-by-bar event-driven backtester.

Invariants the engine guarantees (each is asserted by the test suite):

1. **No lookahead.** A signal computed from bar ``t`` can only be filled at bar
   ``t+1`` at the earliest, and is filled at that bar's *open*.
2. **Cash is a first-class state variable** -- never negative, and share counts
   are whole numbers unless fractional trading is enabled.
3. **Every order costs money.** Fills are charged commission and slippage, both
   in the direction that hurts.
4. **Stops are evaluated intrabar** using the bar's low (long) / high (short);
   a gap through the stop fills at the gap price, not the stop price.
5. **Stops outrank channel exits** within the same bar (worst case first), and
   exits outrank entries when cash is scarce.
6. **Trailing stops ratchet only**, and only using closing prices.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field

import pandas as pd

from .metrics import compute_metrics, monte_carlo_trades
from .risk import (
    DailyPnlGate,
    DrawdownMonitor,
    OrderDecision,
    PortfolioGuard,
    PortfolioRiskState,
    RiskConfig,
)
from .strategy import Strategy, StrategyParams
from .trades import BacktestResult, Trade

log = logging.getLogger(__name__)


@dataclass
class BacktestConfig:
    """Execution and cost assumptions."""

    initial_capital: float = 100_000.0
    slippage_bps: float = 5.0
    commission_per_share: float = 0.005
    commission_min: float = 1.0
    allow_short: bool = False
    order_expiry_bars: int = 5
    # Restrict the *trading* window while still computing indicators over the
    # full history. Used by walk-forward validation so out-of-sample runs get
    # proper indicator warm-up without leaking in-sample information into
    # trades. ``None`` means unbounded.
    trade_from: object | None = None
    trade_until: object | None = None


@dataclass
class _Order:
    """An instruction queued from bar ``t`` to be filled on a later bar."""

    symbol: str
    side: str                      # "buy" = enter long, "sell" = exit long
    signal_bar: pd.Timestamp
    atr_at_signal: float = 0.0
    reason: str = "signal"
    bars_waiting: int = 0


@dataclass
class _Position:
    symbol: str
    side: str                      # "long" | "short"
    quantity: int
    entry_price: float
    entry_cost: float              # cash paid on entry (shares + entry fee)
    entry_time: pd.Timestamp
    stop_price: float              # currently active stop (may have ratcheted)
    initial_stop: float            # original stop, fixed for R accounting
    atr_at_entry: float
    equity_at_entry: float
    trail_stop: float = 0.0
    extreme_close: float = 0.0     # best close seen, for the chandelier
    bars_held: int = 0
    mae: float = 0.0               # max adverse excursion (price units)
    mfe: float = 0.0               # max favourable excursion (price units)

    @property
    def stop_distance(self) -> float:
        return abs(self.entry_price - self.initial_stop)


def commission(qty: int, price: float, per_share: float, minimum: float) -> float:
    """Commission for one fill: per-share cost, floored at ``minimum``."""
    if qty <= 0:
        return 0.0
    return max(minimum, per_share * qty)


def _to_utc_ts(value):
    """Normalise a trade-window bound to a UTC Timestamp.

    pd.Timestamp(x, tz="UTC") raises when x already carries tzinfo, and the
    walk-forward layer passes live Timestamps straight off the bar index, so
    handle both naive and aware inputs.
    """
    if value is None:
        return None
    ts = pd.Timestamp(value)
    if ts.tzinfo is None:
        return ts.tz_localize("UTC")
    return ts.tz_convert("UTC")


def apply_slippage(price: float, side: str, bps: float) -> float:
    """Price adjusted against the trader for every fill."""
    adj = price * bps / 10_000.0
    return price + adj if side == "buy" else price - adj


class Backtester:
    """Replays bars chronologically; orders fill on the following bar's open."""

    def __init__(
        self,
        strategy_params: StrategyParams | None = None,
        risk_config: RiskConfig | None = None,
        config: BacktestConfig | None = None,
        market_gate: pd.DataFrame | None = None,
    ) -> None:
        self.strategy_params = (strategy_params or StrategyParams()).clamp()
        self.risk = (risk_config or RiskConfig()).clamp()
        self.cfg = config or BacktestConfig()
        self.market_gate = market_gate
        self.strategy = Strategy(self.strategy_params)
        self.guard = PortfolioGuard(self.risk)

    # ------------------------------------------------------------------- main

    def run(self, data: dict[str, pd.DataFrame]) -> BacktestResult:
        if not data:
            raise ValueError("no data supplied to the backtester")

        usable = {
            s: d
            for s, d in data.items()
            if d is not None and len(d) >= self.strategy_params.min_history
        }
        skipped = sorted(s for s in data if s not in usable)
        for sym in skipped:
            log.warning("skipping %s: only %s bars", sym, 0 if data.get(sym) is None else len(data[sym]))
        if not usable:
            raise ValueError("no symbol had enough history to trade")

        signals = self._build_signals(usable)
        bars = {s: f for s, f in usable.items()}
        calendar = self._calendar(bars)

        # --- trading window (walk-forward restricts this; None means unbounded)
        trade_from = _to_utc_ts(self.cfg.trade_from)
        trade_until = _to_utc_ts(self.cfg.trade_until)

        def tradable_bar(t: pd.Timestamp) -> bool:
            """True if orders may be *placed or filled* on this bar."""
            if trade_from is not None and t < trade_from:
                return False
            if trade_until is not None and t > trade_until:
                return False
            return True

        cash = float(self.cfg.initial_capital)
        peak_equity = cash
        day_start_equity = cash
        day_mark = None

        positions: dict[str, _Position] = {}
        orders: list[_Order] = []
        trades: list[Trade] = []
        rows: list[dict] = []

        dd_monitor = DrawdownMonitor(halt_at=self.risk.max_drawdown_halt)
        daily_gate = DailyPnlGate(limit=self.risk.daily_loss_limit)
        halted = False
        halt_reason = ""

        def equity_at(t: pd.Timestamp) -> float:
            total = cash
            for sym, pos in positions.items():
                frame = bars[sym]
                if t in frame.index:
                    total += pos.quantity * float(frame.loc[t, "close"])
            return total

        for bar in calendar:
            # ------------------------------------------------- per-session bookkeeping
            session = bar.normalize()
            if day_mark is None:
                day_mark, day_start_equity = session, equity_at(bar)
                daily_gate.open_day(day_start_equity)
            elif session != day_mark:
                day_mark = session
                day_start_equity = equity_at(bar)
                daily_gate.open_day(day_start_equity)

            orders = self._expire_orders(orders)

            # Outside the trading window no order may be placed or filled. This is
            # what lets walk-forward validation compute indicators over the full
            # history while only trading the out-of-sample segment.
            in_window = tradable_bar(bar)

            # --- 1. intrabar stops on positions carried in from the prior bar
            if not halted and in_window:
                for sym in list(positions):
                    pos = positions.get(sym)
                    frame = bars[sym]
                    if pos is None or bar not in frame.index or frame.index.get_loc(bar) < 1:
                        continue
                    pnl = self._check_stop(pos, frame.loc[bar], bar, positions, trades, cash)
                    cash += pnl

            # --- 2. queued exits fill at this bar's open
            if not halted and in_window:
                for order in [o for o in orders if o.side == "sell" and o.symbol in positions]:
                    frame = bars[order.symbol]
                    if bar not in frame.index:
                        continue
                    if order.signal_bar >= bar:
                        continue
                    pos = positions[order.symbol]
                    fill = apply_slippage(float(frame.loc[bar, "open"]), "sell", self.cfg.slippage_bps)
                    cash += self._close(pos, fill, bar, order.reason, positions, trades, cash)
                    orders.remove(order)

            # --- 3. queued entries fill at this bar's open
            if not halted and in_window:
                for order in [o for o in orders if o.side == "buy" and o.symbol not in positions]:
                    frame = bars[order.symbol]
                    if bar not in frame.index or order.signal_bar >= bar:
                        continue
                    price = float(frame.loc[bar, "open"])
                    entry_price = apply_slippage(price, "buy", self.cfg.slippage_bps)
                    decision = self._size(equity_at(bar), entry_price, order.atr_at_signal,
                                          order.symbol, cash)
                    state = PortfolioRiskState(
                        equity=equity_at(bar),
                        peak_equity=peak_equity,
                        cash=cash,
                        open_risk=sum(p.quantity * p.stop_distance for p in positions.values()),
                        open_positions=len(positions),
                        day_start_equity=day_start_equity,
                    )
                    decision = self.guard.approve(state, decision)
                    if not decision.approved or decision.quantity <= 0:
                        log.debug("entry rejected %s: %s", order.symbol, decision.rejection)
                        orders.remove(order)
                        continue
                    stop_dist = self.strategy.stop_distance(order.atr_at_signal)
                    fee = commission(decision.quantity, entry_price, self.cfg.commission_per_share,
                                     self.cfg.commission_min)
                    positions[order.symbol] = _Position(
                        symbol=order.symbol,
                        side="long",
                        quantity=int(decision.quantity),
                        entry_price=entry_price,
                        entry_cost=decision.quantity * entry_price + fee,
                        entry_time=bar,
                        stop_price=entry_price - stop_dist,
                        initial_stop=entry_price - stop_dist,
                        atr_at_entry=order.atr_at_signal,
                        equity_at_entry=equity_at(bar),
                        trail_stop=entry_price - stop_dist,
                        extreme_close=float(frame.loc[bar, "close"]),
                    )
                    cash -= decision.quantity * entry_price + fee
                    orders.remove(order)

                    # A position filled at the open can be stopped inside the
                    # very bar that filled it.
                    pnl = self._check_stop(positions[order.symbol], frame.loc[bar], bar,
                                           positions, trades, cash)
                    cash += pnl

            # --- 4. circuit breakers
            current_equity = equity_at(bar)
            dd = dd_monitor.update(current_equity)
            peak_equity = max(peak_equity, current_equity)

            if not halted:
                if daily_gate.breach(current_equity):
                    halted, halt_reason = True, f"daily loss limit {self.risk.daily_loss_limit:.1%}"
                elif dd >= self.risk.max_drawdown_halt:
                    halted, halt_reason = True, f"drawdown {dd:.1%} >= {self.risk.max_drawdown_halt:.1%}"

            if halted:
                for sym in list(positions):
                    frame = bars[sym]
                    if bar in frame.index:
                        pos = positions[sym]
                        fill = apply_slippage(float(frame.loc[bar, "close"]), "sell", self.cfg.slippage_bps)
                        cash += self._close(pos, fill, bar, f"halt:{halt_reason}", positions, trades, cash)
                orders = []

            # --- 5. mark to market, trail stops, advance holding time
            invested = sum(p.quantity * float(bars[p.symbol].loc[bar, "close"])
                           for p in positions.values() if bar in bars[p.symbol].index)
            for sym, pos in positions.items():
                frame = bars[sym]
                if bar in frame.index:
                    self._update_trail(pos, signals[sym], bar)
                    pos.bars_held += 1

            rows.append({
                "time": bar,
                "equity": cash + invested,
                "cash": cash,
                "positions_value": invested,
                "n_positions": len(positions),
                "in_market": 1 if positions else 0,
                "day_ret": (cash + invested - day_start_equity) / day_start_equity if day_start_equity else 0.0,
                "drawdown": dd,
            })

            # --- 6. signals on this close become orders for a later bar
            if not halted and in_window:
                for sym, frame in signals.items():
                    if bar not in frame.index:
                        continue
                    sig = frame.loc[bar]
                    if bool(sig.get("entry_long", False)) and sym not in positions:
                        self._queue(orders, sym, "buy", bar, float(sig.get("atr") or 0.0), "breakout")
                    elif (bool(sig.get("entry_short", False)) and self.cfg.allow_short
                          and sym not in positions):
                        self._queue(orders, sym, "sell", bar, float(sig.get("atr") or 0.0), "breakdown")

                    if sym in positions and bool(sig.get("channel_exit", False)):
                        self._queue(orders, sym, "sell", bar, 0.0, "channel_exit")

        if not rows:
            raise ValueError("backtest produced no equity rows")

        eq = pd.DataFrame(rows).set_index("time").sort_index()
        eq["daily_ret"] = eq["equity"].pct_change().fillna(0.0)

        metrics = compute_metrics(eq, trades, risk_per_trade=self.risk.risk_per_trade)
        metrics["monte_carlo"] = monte_carlo_trades(trades, risk_per_trade=self.risk.risk_per_trade)
        metrics["skipped_symbols"] = skipped
        metrics["halted"] = halted
        metrics["halt_reason"] = halt_reason
        metrics["final_equity"] = round(float(eq["equity"].iloc[-1]), 2)
        metrics["total_fees"] = round(sum(t.fees for t in trades), 2)

        return BacktestResult(
            equity_curve=eq,
            trades=trades,
            metrics=metrics,
            params=self.strategy_params,
            risk_config=self.risk,
            signals=signals,
            config={
                "initial_capital": self.cfg.initial_capital,
                "slippage_bps": self.cfg.slippage_bps,
                "commission_per_share": self.cfg.commission_per_share,
                "commission_min": self.cfg.commission_min,
                "risk_per_trade": self.risk.risk_per_trade,
                "max_positions": self.risk.max_positions,
                "max_total_risk": self.risk.max_total_risk,
                "daily_loss_limit": self.risk.daily_loss_limit,
                "max_drawdown_halt": self.risk.max_drawdown_halt,
                "allow_short": self.cfg.allow_short,
            },
            notes=[
                f"{len(usable)} symbol(s) traded, {len(skipped)} skipped for short history",
                "entries fill at the bar after the signal, at the open, with slippage",
                "stops are evaluated intrabar; gap-throughs fill at the gap price",
            ],
        )

    # -------------------------------------------------------------- internals

    def _build_signals(self, data: dict[str, pd.DataFrame]) -> dict[str, pd.DataFrame]:
        from .strategy import apply_market_gate

        raw = {sym: self.strategy.compute(frame) for sym, frame in data.items()}
        return apply_market_gate(raw, self.market_gate) if self.market_gate is not None else raw

    @staticmethod
    def _calendar(data: dict[str, pd.DataFrame]) -> pd.DatetimeIndex:
        idx: pd.DatetimeIndex | None = None
        for frame in data.values():
            idx = frame.index if idx is None else idx.union(frame.index)
        if idx is None:
            raise ValueError("no bars to build a calendar from")
        return idx.sort_values()

    def _expire_orders(self, orders: list[_Order]) -> list[_Order]:
        kept: list[_Order] = []
        for order in orders:
            if order.bars_waiting >= self.cfg.order_expiry_bars:
                log.debug("order expired: %s %s (%s)", order.symbol, order.side, order.reason)
                continue
            order.bars_waiting += 1
            kept.append(order)
        return kept

    @staticmethod
    def _queue(orders, symbol, side, signal_bar, atr_value, reason) -> None:
        """Append an order unless an identical one is already pending."""
        for existing in orders:
            if existing.symbol == symbol and existing.side == side and existing.reason == reason:
                return
        orders.append(_Order(symbol=symbol, side=side, signal_bar=signal_bar,
                             atr_at_signal=atr_value, reason=reason))

    def _size(self, equity: float, price: float, atr_value: float, symbol: str,
              cash: float) -> OrderDecision:
        """Shares that risk 1% of *account equity*, capped by the cash we hold."""
        stop_distance = self.strategy.stop_distance(atr_value)
        if stop_distance <= 0 or price <= 0:
            return OrderDecision(symbol=symbol, side="buy", quantity=0, intended_price=price,
                                 stop_price=price, risk_dollars=0.0, position_value=0.0,
                                 approved=False, rejection="invalid stop distance")

        from .risk import PositionSizer

        decision = PositionSizer(self.risk).quantity_for_risk(
            equity=equity, price=price, stop_distance=stop_distance, symbol=symbol, side="buy"
        )
        # Never spend more than the cash actually available.
        if decision.approved:
            affordable = int(cash / price)
            if decision.quantity > affordable:
                if affordable <= 0:
                    decision.approved = False
                    decision.rejection = f"insufficient cash ({cash:.2f})"
                else:
                    stop_dist = abs(decision.intended_price - decision.stop_price)
                    decision.quantity = affordable
                    decision.position_value = affordable * price
                    decision.risk_dollars = affordable * stop_dist
        return decision

    def _check_stop(self, pos, row, bar, positions, trades, cash) -> float:
        """Exit if this bar's range reached the stop. Returns realised P&L (0 if none)."""
        high, low, open_ = float(row["high"]), float(row["low"]), float(row["open"])
        stop = pos.stop_price
        if pos.side == "long":
            if low > stop:
                pos.mae = max(pos.mae, pos.entry_price - low)
                pos.mfe = max(pos.mfe, high - pos.entry_price)
                return 0.0
            gap = open_ if open_ < stop else stop
            fill = apply_slippage(gap, "sell", self.cfg.slippage_bps)
        else:
            if high < stop:
                pos.mae = max(pos.mae, high - pos.entry_price)
                pos.mfe = max(pos.mfe, pos.entry_price - low)
                return 0.0
            gap = open_ if open_ > stop else stop
            fill = apply_slippage(gap, "buy", self.cfg.slippage_bps)
        return self._close(pos, fill, bar, "stop", positions, trades, cash)

    def _close(self, pos, fill_price, bar, reason, positions, trades, cash) -> float:
        """Realise a position, book both fee legs, append a Trade.

        Returns the **cash delta** (proceeds after exit fees).  Callers add it to
        cash; the entry cost was already deducted when the position was opened.
        ``equity_after`` is reported as account equity at entry plus realised
        P&L, which is the meaningful before/after comparison for grading a trade.
        """
        direction = 1.0 if pos.side == "long" else -1.0
        exit_fee = commission(pos.quantity, fill_price, self.cfg.commission_per_share,
                              self.cfg.commission_min)
        entry_fee = max(self.cfg.commission_min, self.cfg.commission_per_share * pos.quantity)
        gross = direction * (fill_price - pos.entry_price) * pos.quantity
        proceeds = pos.quantity * fill_price - exit_fee
        net = gross - entry_fee - exit_fee

        trades.append(Trade(
            symbol=pos.symbol,
            side=pos.side,
            quantity=pos.quantity,
            entry_time=pos.entry_time,
            entry_price=pos.entry_price,
            exit_time=bar,
            exit_price=float(fill_price),
            stop_price=pos.initial_stop,
            initial_risk=pos.quantity * pos.stop_distance,
            fees=entry_fee + exit_fee,
            slippage=fill_price - pos.entry_price,
            exit_reason=reason,
            bars_held=pos.bars_held,
            equity_before=pos.equity_at_entry,
            equity_after=pos.equity_at_entry + net,
            mae=pos.mae,
            mfe=pos.mfe,
        ))
        positions.pop(pos.symbol, None)
        return proceeds

    def _update_trail(self, pos: _Position, frame: pd.DataFrame, bar: pd.Timestamp) -> None:
        """Ratchet the trailing stop using only closes; never loosens."""
        if bar not in frame.index or frame.index.get_loc(bar) < 1:
            return
        close = float(frame["close"].loc[bar])
        atr = float(frame["atr"].loc[bar]) if "atr" in frame.columns else pos.atr_at_entry
        if not atr or atr <= 0:
            return
        distance = self.strategy.trail_distance(atr)
        if pos.side == "long":
            pos.extreme_close = max(pos.extreme_close or close, close)
            candidate = pos.extreme_close - distance
            pos.trail_stop = max(pos.trail_stop, candidate)
        else:
            pos.extreme_close = min(pos.extreme_close or close, close)
            candidate = pos.extreme_close + distance
            pos.trail_stop = min(pos.trail_stop, candidate)
        pos.stop_price = pos.trail_stop


__all__ = ["Backtester", "BacktestConfig", "commission", "apply_slippage"]
