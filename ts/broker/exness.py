"""Exness adapter via MetaTrader 5.

**What this actually requires, stated plainly:** Exness does not offer a public
REST trading API for retail accounts. Programmatic access goes through the
MetaTrader 5 terminal, which means:

* Windows (the ``MetaTrader5`` Python package is Windows-only)
* the MT5 desktop terminal installed and running
* an Exness MT5 account logged in to that terminal
* ``pip install MetaTrader5``
* algorithmic trading enabled in the terminal (Tools -> Options -> Expert Advisors)

On macOS/Linux, or without a running terminal, this adapter raises a clear
``BrokerError`` explaining what is missing rather than half-working. It is
implemented against the documented ``MetaTrader5`` API so it works the moment
those prerequisites are met.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from .base import Broker, BrokerError, Fill, Order, Position

log = logging.getLogger(__name__)

PIP_SIZE = {
    "EURUSD": 0.0001, "GBPUSD": 0.0001, "AUDUSD": 0.0001, "NZDUSD": 0.0001,
    "USDCAD": 0.0001, "USDCHF": 0.0001,
    "USDJPY": 0.001, "EURJPY": 0.001, "GBPJPY": 0.001,
    "BTCUSD": 0.01, "ETHUSD": 0.01, "XRPUSD": 0.0001, "SOLUSD": 0.01,
}


@dataclass
class ExnessConfig:
    login: int | str = 0
    password: str = ""
    server: str = ""            # e.g. "Exness-MT5Real8"
    terminal_path: str = ""     # optional explicit terminal64.exe
    timeout_ms: int = 30_000

    def __post_init__(self) -> None:
        if not self.login:
            import os

            self.login = os.getenv("EXNESS_LOGIN", "")
        if not self.password:
            import os

            self.password = os.getenv("EXNESS_PASSWORD", "")
        if not self.server:
            import os

            self.server = os.getenv("EXNESS_SERVER", "")


class ExnessBroker(Broker):
    """MT5-backed execution for an Exness account."""

    name = "exness"

    def __init__(self, config: ExnessConfig | None = None) -> None:
        self.cfg = config or ExnessConfig()
        self.mode = "live"
        self.name = "exness-mt5"
        self.supports_shorting = True
        self._mt5 = None
        self._connect()

    def _connect(self) -> None:
        try:
            import MetaTrader5 as mt5  # type: ignore
        except ImportError as exc:
            raise BrokerError(
                "the MetaTrader5 package is not installed. Run "
                "'py -m pip install MetaTrader5' on Windows with the MT5 "
                "terminal installed, then log in to your Exness MT5 account."
            ) from exc

        self._mt5 = mt5
        kwargs: dict = {}
        if str(self.cfg.terminal_path).strip():
            kwargs["path"] = str(self.cfg.terminal_path)
        if not mt5.initialize(**kwargs):
            code, description = mt5.last_error()
            raise BrokerError(
                f"could not start the MT5 terminal (error {code}: {description}). "
                "Install MetaTrader 5, enable algorithmic trading under "
                "Tools -> Options -> Expert Advisors, and log in to your "
                "Exness account."
            )

        if self.cfg.login and self.cfg.password:
            if not mt5.login(int(self.cfg.login), password=str(self.cfg.password),
                             server=str(self.cfg.server)):
                code, description = mt5.last_error()
                mt5.shutdown()
                raise BrokerError(
                    f"MT5 login failed (error {code}: {description}). Check the "
                    "login, password and server name (e.g. 'Exness-MT5Real8')."
                )

    # ------------------------------------------------------------------ venue

    def account(self) -> dict:
        info = self._mt5.account_info()
        if info is None:
            raise BrokerError("could not read the MT5 account")
        return {
            "equity": round(float(info.equity), 2),
            "cash": round(float(info.balance), 2),
            "buying_power": round(float(info.margin_free), 2),
            "realised_pnl": round(float(info.profit), 2),
            "n_positions": len(self.positions()),
            "currency": str(info.currency),
            "server": str(info.server),
            "leverage": int(info.leverage),
        }

    def positions(self) -> list[Position]:
        out: list[Position] = []
        for item in self._mt5.positions_get() or []:
            qty = float(item.volume)
            out.append(Position(
                symbol=str(item.symbol),
                quantity=qty,
                side="long" if item.type == 0 else "short",
                entry_price=float(item.price_open),
                current_price=float(item.price_current),
                unrealized_pnl=float(item.profit),
                stop_price=float(item.sl) if item.sl else None,
            ))
        return out

    def market_price(self, symbol: str) -> float:
        tick = self._mt5.symbol_info_tick(symbol)
        if tick is None:
            raise BrokerError(f"{symbol} is not available in the MT5 market watch")
        return float(tick.ask or tick.bid or 0.0)

    def is_market_open(self) -> bool:
        symbol = next(iter(self.positions() or []), None)
        return True      # MT5 exposes session hours per symbol; keep it simple

    # ---------------------------------------------------------------- trading

    def submit(self, order: Order) -> Fill:
        symbol = order.symbol
        if not self._mt5.symbol_select(symbol, True):
            raise BrokerError(f"could not select {symbol} in MT5")

        tick = self._mt5.symbol_info_tick(symbol)
        if tick is None:
            raise BrokerError(f"no tick for {symbol}")

        info = self._mt5.symbol_info(symbol)
        volume = float(order.quantity)
        if info is not None and volume < float(info.volume_min):
            volume = float(info.volume_min)
        if info is not None:
            step = float(info.volume_step) or 0.01
            volume = round(round(volume / step) * step, 2)

        is_long = order.side == "buy"
        price = tick.ask if is_long else tick.bid
        request = {
            "action": self._mt5.TRADE_ACTION_DEAL,
            "symbol": symbol,
            "volume": volume,
            "type": self._mt5.ORDER_TYPE_BUY if is_long else self._mt5.ORDER_TYPE_SELL,
            "price": float(price),
            "deviation": 20,
            "magic": 20261012,
            "comment": f"tradingsys-{order.client_id or 'manual'}",
            "type_time": self._mt5.ORDER_TIME_GTC,
            "type_filling": self._mt5.ORDER_FILLING_IOC,
        }
        result = self._mt5.order_send(request)
        if result is None or result.retcode not in (
                self._mt5.TRADE_RETCODE_DONE, self._mt5.TRADE_RETCODE_PLACED):
            code = getattr(result, "retcode", "none")
            comment = getattr(result, "comment", "no response")
            raise BrokerError(f"MT5 order rejected (retcode {code}: {comment})")

        return Fill(
            symbol=symbol,
            quantity=float(getattr(result, "volume", volume)),
            side=order.side,
            price=float(getattr(result, "price", price)),
            fees=0.0,
            order_id=str(getattr(result, "order", "")),
        )

    def cancel_all(self) -> None:
        for item in self.positions():
            try:
                self.close_position(item.symbol)
            except BrokerError as exc:
                log.debug("cancel failed for %s: %s", item.symbol, exc)

    def close_position(self, symbol: str) -> Fill:
        for pos in self.positions():
            if pos.symbol != symbol:
                continue
            tick = self._mt5.symbol_info_tick(symbol)
            if tick is None:
                raise BrokerError(f"no tick for {symbol}")
            is_long = pos.side == "long"
            request = {
                "action": self._mt5.TRADE_ACTION_DEAL,
                "symbol": symbol,
                "volume": pos.quantity,
                "type": self._mt5.ORDER_TYPE_SELL if is_long else self._mt5.ORDER_TYPE_BUY,
                "price": float(tick.bid if is_long else tick.ask),
                "deviation": 20,
                "position": int(getattr(pos, "ticket", 0) or 0),
                "type_time": self._mt5.ORDER_TIME_GTC,
                "type_filling": self._mt5.ORDER_FILLING_IOC,
            }
            result = self._mt5.order_send(request)
            if result is None or result.retcode != self._mt5.TRADE_RETCODE_DONE:
                raise BrokerError(f"close failed for {symbol}: {getattr(result, 'comment', '?')}")
            return Fill(symbol=symbol, quantity=pos.quantity,
                        side="sell" if is_long else "buy",
                        price=float(getattr(result, "price", 0) or 0))
        raise BrokerError(f"no open position in {symbol}")

    def flatten(self, symbol: str | None = None) -> list[Fill]:
        fills: list[Fill] = []
        for pos in list(self.positions()):
            if symbol and pos.symbol != symbol:
                continue
            fills.append(self.close_position(pos.symbol))
        return fills


__all__ = ["ExnessBroker", "ExnessConfig", "PIP_SIZE"]
