"""The autonomous trading engine.

One :meth:`TradingEngine.tick` does everything a discretionary trader would do
during a session, in order:

1. **Safety** -- honour the kill switch, daily loss limit, and drawdown halt.
2. **Observe** -- refresh market data for the whole universe.
3. **Manage** -- for every open position: ratchet trailing stops, evaluate
   intrabar stop-outs, and check the trend-break exit.
4. **Size** -- for every fresh signal: risk exactly 1% of equity, then apply
   portfolio caps and the cash constraint.
5. **Execute** -- submit market orders through the broker.
6. **Record** -- publish a full snapshot to the state store for the dashboard.

The loop never touches the dashboard or the backtester, so a UI crash cannot
stop trading and a trading exception cannot take the UI down.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime

import pandas as pd

from .broker.base import Broker, Order, Position
from .metrics import compute_metrics
from .risk import DailyPnlGate, DrawdownMonitor, PortfolioGuard, PortfolioRiskState, RiskConfig
from .strategy import Strategy, StrategyParams, apply_market_gate
from .forward import ForwardTrade
from .trades import Trade

log = logging.getLogger(__name__)


@dataclass
class EngineConfig:
    poll_seconds: float = 30.0
    interval: str = "1d"
    history_range: str = "1y"
    market_gate_symbol: str | None = "SPY"
    use_market_gate: bool = True
    max_orders_per_tick: int = 4
    dry_run: bool = False           # True = plan and log, but never submit
    session_timezone: str = "America/New_York"
    flatten_on_close: bool = True   # close everything at session end


@dataclass
class ManagedPosition:
    """A position the engine owns and manages stops for."""

    symbol: str
    side: str
    quantity: int
    entry_price: float
    entry_time: datetime
    stop_price: float
    initial_stop: float
    initial_risk: float
    atr_at_entry: float
    equity_at_entry: float
    highest_close: float = 0.0
    bars_held: int = 0
    last_price: float = 0.0

    @property
    def direction(self) -> float:
        return 1.0 if self.side == "long" else -1.0

    def r_open(self, price: float) -> float | None:
        if self.initial_risk <= 0:
            return None
        pnl = self.direction * (price - self.entry_price) * self.quantity
        return pnl / self.initial_risk

    def to_position(self, qty_override: int | None = None) -> Position:
        return Position(
            symbol=self.symbol,
            quantity=qty_override or self.quantity,
            side=self.side,
            entry_price=self.entry_price,
            current_price=self.last_price,
            stop_price=self.stop_price,
            risk_dollars=self.initial_risk,
            bars_held=self.bars_held,
            r_open=self.r_open(self.last_price),
        )


class TradingEngine:
    """Drives strategy, risk, and execution on a loop."""

    def __init__(
        self,
        broker: Broker,
        data_fetcher,
        strategy_params: StrategyParams | None = None,
        risk_config: RiskConfig | None = None,
        config: EngineConfig | None = None,
        state_store=None,
        universe: list[str] | None = None,
        kill_switch=None,
        forward_journal=None,
        notifier=None,
        approvals=None,
        policy=None,
    ) -> None:
        self.broker = broker
        self.fetch = data_fetcher            # callable(symbol, range, interval) -> DataFrame
        self.params = (strategy_params or StrategyParams()).clamp()
        self.risk = (risk_config or RiskConfig()).clamp()
        self.cfg = config or EngineConfig()
        self.state = state_store
        self.universe = list(universe or [])
        self.kill_switch = kill_switch        # callable() -> bool
        # When supplied, every closed trade is journalled here automatically.
        # Backtests never reach it -- a backtest is a prediction, not evidence.
        self.forward_journal = forward_journal
        self.notifier = notifier
        # when set, the engine asks a human only for unconvincing plans
        self.approvals = approvals
        self.policy = policy

        self.strategy = Strategy(self.params)
        self.guard = PortfolioGuard(self.risk)
        self.dd_monitor = DrawdownMonitor(halt_at=self.risk.max_drawdown_halt)
        self.daily_gate = DailyPnlGate(limit=self.risk.daily_loss_limit)

        self.managed: dict[str, ManagedPosition] = {}
        self.trades: list[Trade] = []
        self.frames: dict[str, pd.DataFrame] = {}
        self.running = False
        self.session_day: str | None = None
        self.peak_equity = 0.0
        self.ticks = 0
        self.last_error: str | None = None
        self.errors: list[dict] = []

    # ------------------------------------------------------------------ metrics

    @property
    def mode(self) -> str:
        return getattr(self.broker, "mode", "paper").upper()

    @property
    def broker_name(self) -> str:
        return getattr(self.broker, "name", "unknown")

    @property
    def open_positions(self) -> list[dict]:
        acct = self.broker.account()
        self._mark_managed(acct)
        out = []
        for pos in self.managed.values():
            out.append(pos.to_position().to_dict())
        return out

    def _mark_managed(self, acct: dict) -> None:
        for mp in self.managed.values():
            mp.last_price = self._latest_close(mp.symbol)

    def _latest_close(self, symbol: str) -> float:
        frame = self.frames.get(symbol)
        if frame is not None and len(frame):
            try:
                return float(frame["close"].iloc[-1])
            except (KeyError, IndexError, ValueError):
                pass
        try:
            return self.broker.market_price(symbol)
        except Exception:  # noqa: BLE001 - fall back to the broker's own price
            return 0.0

    # --------------------------------------------------------------------- tick

    def tick(self) -> dict:
        """One full cycle. Returns a summary dict for logging."""
        self.ticks += 1
        summary: dict = {"tick": self.ticks, "at": datetime.now(UTC).isoformat()}

        if self.kill_switch and self.kill_switch():
            summary["halted"] = "kill switch engaged"
            self._flatten("kill switch")
            self._publish()
            return summary

        # ---- data
        try:
            self.frames = self._refresh_data()
        except Exception as exc:  # noqa: BLE001
            self._record_error("data refresh", exc)
            summary["error"] = str(exc)
            return summary

        if not self.frames:
            summary["error"] = "no data"
            return summary

        acct = self.broker.account()
        equity = float(acct.get("equity", 0) or 0)
        cash = float(acct.get("cash", 0) or 0)
        if equity <= 0:
            summary["error"] = "equity unknown"
            return summary

        # ---- session bookkeeping
        today = datetime.now(UTC).date().isoformat()
        if self.session_day != today:
            self.session_day = today
            self.peak_equity = equity
            self.daily_gate.open_day(equity)

        self.peak_equity = max(self.peak_equity, equity)
        drawdown = self.dd_monitor.update(equity)

        # ---- sync managed positions with what the broker actually holds
        self._sync_positions(equity)

        # ---- exits first, so capital is released before entries are sized
        exits = self._manage_exits()
        summary["exits"] = exits

        # ---- circuit breakers
        halt_reason = None
        if self.daily_gate.breach(equity):
            halt_reason = f"daily loss limit {self.risk.daily_loss_limit:.1%}"
        elif drawdown >= self.risk.max_drawdown_halt:
            halt_reason = f"drawdown {drawdown:.1%} >= {self.risk.max_drawdown_halt:.1%}"
        if halt_reason:
            summary["halted"] = halt_reason
            self._flatten(halt_reason)
            self._publish(extra={"halt_reason": halt_reason})
            if self.state:
                self.state.log("engine_halt", level="WARN", reason=halt_reason)
            return summary

        # ---- entries
        entries = self._find_entries(equity, cash)
        summary["entries"] = entries

        self._publish()
        return summary

    # ------------------------------------------------------------------ pieces

    def _refresh_data(self) -> dict[str, pd.DataFrame]:
        from .datafeed import DataFeed

        out: dict[str, pd.DataFrame] = {}
        for symbol in self.universe:
            try:
                frame = self.fetch(symbol, self.cfg.history_range, self.cfg.interval)
                if frame is not None and len(frame) >= self.params.min_history:
                    out[symbol] = frame
            except Exception as exc:  # noqa: BLE001 - one bad symbol must not stop the day
                self._record_error(f"fetch {symbol}", exc)
        if self.cfg.use_market_gate and self.cfg.market_gate_symbol not in out:
            try:
                gate = self.fetch(self.cfg.market_gate_symbol,
                                  self.cfg.history_range, self.cfg.interval)
                if gate is not None:
                    out[self.cfg.market_gate_symbol] = gate
            except Exception as exc:  # noqa: BLE001
                self._record_error("fetch gate", exc)
        return out

    def _sync_positions(self, equity: float) -> None:
        broker_positions = {p.symbol: p for p in self.broker.positions()}

        for symbol in list(self.managed):
            if symbol not in broker_positions:
                mp = self.managed.pop(symbol)
                if self.state:
                    self.state.log("position_closed_externally", symbol=symbol,
                                   qty=mp.quantity)
                self._record_closed_trade(mp, self._latest_close(symbol), "broker_exit")

        for symbol, pos in broker_positions.items():
            if symbol in self.managed:
                continue
            frame = self.frames.get(symbol)
            atr = 0.0
            if frame is not None and "atr" in frame.columns:
                try:
                    atr = float(frame["atr"].iloc[-1])
                except (KeyError, IndexError, ValueError):
                    atr = 0.0
            stop_dist = self.strategy.stop_distance(atr or pos.entry_price * 0.02)
            mp = ManagedPosition(
                symbol=symbol,
                side=pos.side,
                quantity=pos.quantity,
                entry_price=pos.entry_price,
                entry_time=datetime.now(UTC),
                stop_price=pos.entry_price + (1.0 if pos.side == "short" else -1.0) * stop_dist,
                initial_stop=pos.entry_price + (1.0 if pos.side == "short" else -1.0) * stop_dist,
                initial_risk=pos.quantity * stop_dist,
                atr_at_entry=atr,
                equity_at_entry=equity,
                highest_close=self._latest_close(symbol),
            )
            self.managed[symbol] = mp
            if self.state:
                self.state.log("position_adopted", symbol=symbol, side=pos.side,
                               qty=pos.quantity, price=pos.entry_price)

    def _manage_exits(self) -> list[dict]:
        exits: list[dict] = []
        for symbol in list(self.managed):
            mp = self.managed.get(symbol)
            if mp is None:
                continue
            frame = self.frames.get(symbol)
            if frame is None or len(frame) < 2:
                continue
            mp.bars_held += 1
            try:
                row = frame.iloc[-1]
                high, low, close = float(row["high"]), float(row["low"]), float(row["close"])
            except (KeyError, IndexError, ValueError):
                continue

            mp.last_price = close
            self._ratchet_trail(mp, frame)

            hit_stop = low <= mp.stop_price if mp.side == "long" else high >= mp.stop_price
            channel_exit = False
            try:
                channel_exit = bool(self._signal_for(symbol, frame).get("channel_exit", False))
            except (ValueError, KeyError, IndexError):
                channel_exit = False

            reason = None
            if hit_stop:
                reason = "stop"
            elif channel_exit:
                reason = "channel_exit"
            if reason is None:
                continue

            try:
                fill = self.broker.submit(Order(symbol=symbol, quantity=mp.quantity,
                                               side="sell" if mp.side == "long" else "buy",
                                               type="market", stop_price=mp.stop_price))
            except Exception as exc:  # noqa: BLE001
                self._record_error(f"exit {symbol}", exc)
                continue

            self._record_closed_trade(mp, fill.price, reason)
            exits.append({"symbol": symbol, "reason": reason, "price": fill.price,
                          "qty": fill.quantity, "fees": fill.fees})
            if self.state:
                self.state.log("exit", symbol=symbol, reason=reason, price=fill.price)
        return exits

    def _ratchet_trail(self, mp: ManagedPosition, frame: pd.DataFrame) -> None:
        if not self.params.use_trailing_stop:
            return
        try:
            close = float(frame["close"].iloc[-1])
        except (KeyError, IndexError, ValueError):
            return
        atr = mp.atr_at_entry
        if "atr" in frame.columns:
            try:
                atr = float(frame["atr"].iloc[-1])
            except (KeyError, IndexError, ValueError):
                pass
        if not atr or atr <= 0:
            return

        distance = self.strategy.trail_distance(atr)
        if mp.side == "long":
            mp.highest_close = max(mp.highest_close or close, close)
            candidate = mp.highest_close - distance
            mp.stop_price = max(mp.stop_price, candidate)
        else:
            mp.highest_close = min(mp.highest_close or close, close)
            candidate = mp.highest_close + distance
            mp.stop_price = min(mp.stop_price, candidate)

    def _signal_for(self, symbol: str, frame: pd.DataFrame) -> pd.Series:
        sig = self.strategy.compute(frame)
        if self.cfg.use_market_gate and self.cfg.market_gate_symbol:
            market = self.frames.get(self.cfg.market_gate_symbol)
            if market is not None:
                sig = apply_market_gate({symbol: sig}, market).get(symbol, sig)
        return sig.iloc[-1]

    def _needs_approval(self, symbol: str, frame, equity: float) -> bool:
        """Consult the auto-approval policy; queue a question if unconvincing.

        Returns True when the trade should NOT auto-execute. A strong plan
        returns False and the entry proceeds untouched; a marginal one is
        queued for a human answer instead.
        """
        if self.policy is None or self.approvals is None:
            return False
        try:
            payload = self._signal_for(symbol, frame)
            if not bool(payload.get("entry_long", False)):
                return False
        except (ValueError, KeyError, IndexError):
            return False

        expectancy_r = float(payload.get("expectancy_r", 0.0) or 0.0)
        confidence = float(payload.get("confidence", 0.5) or 0.5)
        atr = float(payload.get("atr", 0.0) or 0.0)
        try:
            price = float(frame["close"].iloc[-1])
        except (IndexError, ValueError, KeyError):
            return False
        stop_dist = self.strategy.stop_distance(atr)
        risk_pct = (stop_dist / price) if price > 0 else 0.0

        needs, tier, reason = self.policy.evaluate(
            expectancy_r, confidence, side="long", symbol=symbol,
            risk_pct=risk_pct)
        if not needs:
            log.debug("%s auto-approved (%s): %s", symbol, tier, reason)
            return False

        try:
            self.approvals.request(
                action="enter",
                title=f"APPROVE ENTRY: long {symbol}",
                body=reason,
                fields={
                    "symbol": symbol,
                    "side": "long",
                    "expectancy": f"{expectancy_r:+.2f}R",
                    "confidence": f"{confidence:.0%}",
                    "risk": f"{risk_pct:.2%} of equity",
                    "tier": tier,
                },
            )
        except RuntimeError as exc:
            self.alarm(f"approval queue is full: {exc}")
        return True

    def _find_entries(self, equity: float, cash: float) -> list[dict]:
        entries: list[dict] = []
        state = PortfolioRiskState(
            equity=equity,
            peak_equity=self.peak_equity,
            cash=cash,
            open_risk=sum(mp.initial_risk for mp in self.managed.values()),
            open_positions=len(self.managed),
            day_start_equity=self.daily_gate.day_start_equity or equity,
        )

        for symbol, frame in self.frames.items():
            if symbol == self.cfg.market_gate_symbol:
                continue
            if len(entries) >= self.cfg.max_orders_per_tick:
                break
            if symbol in self.managed:
                continue
            # --- ask a human only when this plan is not convincing on its own
            if self.policy is not None and self.approvals is not None and \
                    self._needs_approval(symbol, frame, equity):
                continue
            try:
                sig = self._signal_for(symbol, frame)
            except (ValueError, KeyError, IndexError):
                continue

            if not bool(sig.get("entry_long", False)):
                continue
            if self.params.allow_shorts and bool(sig.get("entry_short", False)):
                continue   # shorting off by default

            price = float(frame["close"].iloc[-1])
            atr = float(sig.get("atr") or 0.0)
            stop_dist = self.strategy.stop_distance(atr)
            if stop_dist <= 0:
                continue

            from .risk import PositionSizer

            decision = PositionSizer(self.risk).quantity_for_risk(
                equity=equity, price=price, stop_distance=stop_dist,
                symbol=symbol, side="buy")
            decision = self.guard.approve(state, decision)
            if not decision.approved or decision.quantity <= 0:
                log.debug("entry rejected %s: %s", symbol, decision.rejection)
                continue

            stop_price = price - stop_dist
            order = Order(symbol=symbol, quantity=decision.quantity, side="buy",
                          type="market", stop_price=round(stop_price, 4),
                          client_id=f"eng-{datetime.now(UTC).strftime('%H%M%S')}-{symbol}")

            if self.cfg.dry_run:
                entries.append({"symbol": symbol, "qty": decision.quantity, "dry_run": True})
                if self.state:
                    self.state.log("signal", symbol=symbol, qty=decision.quantity, dry_run=True)
                continue

            try:
                fill = self.broker.submit(order)
            except Exception as exc:  # noqa: BLE001
                self._record_error(f"entry {symbol}", exc)
                continue

            self.managed[symbol] = ManagedPosition(
                symbol=symbol,
                side="long",
                quantity=fill.quantity,
                entry_price=fill.price,
                entry_time=datetime.now(UTC),
                stop_price=stop_price,
                initial_stop=stop_price,
                initial_risk=fill.quantity * stop_dist,
                atr_at_entry=atr,
                equity_at_entry=equity,
                highest_close=float(frame["close"].iloc[-1]),
            )
            state.open_risk += fill.quantity * stop_dist
            state.open_positions += 1
            state.cash = cash - fill.quantity * fill.price

            entries.append({"symbol": symbol, "qty": fill.quantity, "price": fill.price,
                            "stop": round(stop_price, 4)})
            if self.state:
                self.state.log("entry", symbol=symbol, qty=fill.quantity,
                               price=fill.price, stop=round(stop_price, 4),
                               risk=round(fill.quantity * stop_dist, 2))
        return entries

    # -------------------------------------------------------------- utilities

    def _flatten(self, reason: str) -> None:
        if self.cfg.dry_run:
            return
        for symbol in list(self.managed):
            mp = self.managed[symbol]
            try:
                fill = self.broker.submit(Order(symbol=symbol, quantity=mp.quantity,
                                               side="sell" if mp.side == "long" else "buy",
                                               type="market"))
                self._record_closed_trade(mp, fill.price, f"flatten:{reason}")
                if self.state:
                    self.state.log("flatten", symbol=symbol, price=fill.price, reason=reason)
            except Exception as exc:  # noqa: BLE001
                self._record_error(f"flatten {symbol}", exc)

    def _record_closed_trade(self, mp: ManagedPosition, exit_price: float, reason: str) -> None:
        direction = mp.direction
        gross = direction * (exit_price - mp.entry_price) * mp.quantity
        net = gross
        equity_after = mp.equity_at_entry + net
        trade = Trade(
            symbol=mp.symbol,
            side=mp.side,
            quantity=mp.quantity,
            entry_time=pd.Timestamp(mp.entry_time),
            entry_price=mp.entry_price,
            exit_time=pd.Timestamp(datetime.now(UTC)),
            exit_price=float(exit_price),
            stop_price=mp.initial_stop,
            initial_risk=mp.initial_risk,
            fees=0.0,
            exit_reason=reason,
            bars_held=mp.bars_held,
            equity_before=mp.equity_at_entry,
            equity_after=equity_after,
        )
        self.trades.append(trade)
        payload = trade.to_dict()
        payload["exit_time"] = str(payload["exit_time"])
        payload["entry_time"] = str(payload["entry_time"])
        if self.state:
            self.state.append_trade(payload)
        if self.forward_journal is not None:
            try:
                self.forward_journal.record(
                    ForwardTrade.from_engine_trade(
                        trade, "trend_breakout", getattr(self.broker, "mode", "paper")))
            except (TypeError, ValueError, AttributeError) as exc:
                log.warning("could not journal trade %s: %s", trade.symbol, exc)

    def _publish(self, extra: dict | None = None) -> None:
        if not self.state:
            return
        acct = self.broker.account()
        equity = float(acct.get("equity", 0) or 0)
        day_start = self.daily_gate.day_start_equity or equity
        snapshot = {
            "mode": self.mode,
            "broker": self.broker_name,
            "ticks": self.ticks,
            "equity": round(equity, 2),
            "cash": round(float(acct.get("cash", 0) or 0), 2),
            "day_pnl": round(equity - day_start, 2),
            "day_return_pct": round((equity - day_start) / day_start, 5) if day_start else 0.0,
            "total_return_pct": self._total_return(),
            "drawdown": round(self.dd_monitor.peak_drawdown, 5),
            "positions": [mp.to_position().to_dict() for mp in self.managed.values()],
            "risk": {
                "risk_per_trade": self.risk.risk_per_trade,
                "max_positions": self.risk.max_positions,
                "open_risk": round(sum(mp.initial_risk for mp in self.managed.values()), 2),
                "n_positions": len(self.managed),
                "daily_loss_limit": self.risk.daily_loss_limit,
                "drawdown_halt": self.risk.max_drawdown_halt,
            },
            "n_trades": len(self.trades),
        }
        if extra:
            snapshot.update(extra)
        self.state.save_snapshot(snapshot)
        self.state.append_equity({"t": datetime.now(UTC).isoformat(),
                                  "equity": round(equity, 2)})

    def _total_return(self) -> float:
        acct = self.broker.account()
        equity = float(acct.get("equity", 0) or 0)
        start = float(self.__dict__.get("_start_equity", 0) or 0)
        if not start:
            start = float(getattr(self.broker, "cfg", None).starting_cash if hasattr(
                self.broker, "cfg") else 100_000.0)
            self.__dict__["_start_equity"] = start
        return (equity - start) / start if start > 0 else 0.0

    def _record_error(self, where: str, exc: Exception) -> None:
        msg = f"{where}: {type(exc).__name__}: {exc}"
        self.last_error = msg
        self.errors.append({"at": datetime.now(UTC).isoformat(), "where": where,
                            "error": str(exc)})
        if len(self.errors) > 200:
            self.errors = self.errors[-200:]
        log.error(msg)
        if self.state:
            self.state.log("engine_error", level="ERROR", where=where, error=str(exc))

    # --------------------------------------------------------------------- loop

    def run_forever(self, max_ticks: int | None = None) -> None:
        """Run until stopped. ``max_ticks`` is for tests and demos."""
        self.running = True
        if self.state:
            self.state.log("engine_started", mode=self.mode, broker=self.broker_name,
                           universe=len(self.universe), dry_run=self.cfg.dry_run)
        print(f"[*] engine running in {self.mode} mode "
              f"({'DRY RUN' if self.cfg.dry_run else 'live orders'})")
        try:
            ticks = 0
            while self.running:
                summary = self.tick()
                print(f"    tick {summary.get('tick')}: "
                      f"entries={len(summary.get('entries') or [])} "
                      f"exits={len(summary.get('exits') or [])} "
                      f"positions={len(self.managed)}"
                      + (f"  [{summary['halted']}]" if summary.get("halted") else "")
                      + (f"  ERR: {summary['error']}" if summary.get("error") else ""))
                ticks += 1
                if max_ticks and ticks >= max_ticks:
                    break
                time.sleep(self.cfg.poll_seconds)
        except KeyboardInterrupt:
            print("\n[*] stopping engine")
        finally:
            self.running = False
            if self.state:
                self.state.log("engine_stopped", ticks=self.ticks)

    def stop(self) -> None:
        self.running = False


__all__ = ["TradingEngine", "EngineConfig", "ManagedPosition"]
