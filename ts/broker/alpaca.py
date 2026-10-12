"""Alpaca broker adapter -- the real-money path.

Credentials are read from the environment, never from source:

    ALPACA_KEY_ID
    ALPACA_SECRET_KEY
    ALPACA_BASE_URL   (optional; defaults to the paper endpoint)

**The engine refuses to start on the live endpoint unless ``mode='live'`` is
passed explicitly.**  This adapter is written against the documented Alpaca
v2 REST surface using only the standard library, so there is no dependency to
install on the box that runs the trading system.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

from .base import Broker, BrokerError, Fill, Order, Position

PAPER_URL = "https://paper-api.alpaca.markets"
LIVE_URL = "https://api.alpaca.markets"


@dataclass
class AlpacaConfig:
    key_id: str | None = None
    secret_key: str | None = None
    base_url: str | None = None
    mode: str = "paper"            # "paper" | "live"
    timeout: float = 15.0
    max_retries: int = 3


class AlpacaBroker(Broker):
    """REST adapter for Alpaca's v2 trading API."""

    name = "alpaca"

    def __init__(self, config: AlpacaConfig | None = None) -> None:
        self.cfg = config or AlpacaConfig()
        self.cfg.key_id = self.cfg.key_id or os.getenv("ALPACA_KEY_ID")
        self.cfg.secret_key = self.cfg.secret_key or os.getenv("ALPACA_SECRET_KEY")

        if self.cfg.base_url:
            self.base_url = self.cfg.base_url.rstrip("/")
        else:
            self.base_url = LIVE_URL if self.cfg.mode == "live" else PAPER_URL

        # Exact-match the live URL. A substring test is wrong here: the paper
        # endpoint is "paper-api.alpaca.markets", which *contains*
        # "api.alpaca.markets", so `"api.alpaca.markets" in url` is true for the
        # paper endpoint too. That silently classified every paper account as
        # live and tripped the live-trading guard, which is what broke
        # "Test connection" for paper keys.
        normalised = self.base_url.lower()
        if normalised == LIVE_URL:
            self.mode = "live"
        elif normalised == PAPER_URL:
            self.mode = "paper"
        else:
            # an unrecognised URL: guess from its shape, defaulting to the
            # safer reading
            self.mode = "paper" if "paper" in normalised else "live"
        self.name = f"alpaca-{self.mode}"

        if self.mode == "live" and self.cfg.mode != "live":
            raise BrokerError(
                "credentials point at the LIVE endpoint but mode='paper'; "
                "pass mode='live' deliberately to trade real money"
            )
        if self.mode == "live":
            if os.getenv("ALPACA_LIVE_CONFIRMED", "").strip().lower() != "yes":
                raise BrokerError(
                    "live trading refused: set ALPACA_LIVE_CONFIRMED=yes after confirming "
                    "you intend to trade real capital"
                )
        if not self.cfg.key_id or not self.cfg.secret_key:
            raise BrokerError(
                "ALPACA_KEY_ID and ALPACA_SECRET_KEY must be set (environment variables)"
            )

    # --------------------------------------------------------------- plumbing

    def _headers(self) -> dict[str, str]:
        return {
            "APCA-API-KEY-ID": str(self.cfg.key_id),
            "APCA-API-SECRET-KEY": str(self.cfg.secret_key),
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    def _request(self, method: str, path: str, payload: dict | None = None,
                 params: dict | None = None) -> dict | list:
        url = f"{self.base_url}/v2{path}"
        if params:
            url += "?" + urllib.parse.urlencode(params)

        data = json.dumps(payload).encode() if payload is not None else None
        last_err: Exception | None = None

        for attempt in range(self.cfg.max_retries):
            req = urllib.request.Request(url, data=data, method=method,
                                         headers=self._headers())
            try:
                with urllib.request.urlopen(req, timeout=self.cfg.timeout) as resp:
                    body = resp.read().decode("utf-8")
                    return json.loads(body) if body else {}
            except urllib.error.HTTPError as exc:
                detail = ""
                try:
                    detail = exc.read().decode("utf-8", "replace")[:300]
                except (OSError, UnicodeDecodeError):
                    pass
                last_err = BrokerError(f"Alpaca {method} {path} -> HTTP {exc.code}: {detail}")
                if exc.code in (401, 403, 404, 422):
                    raise last_err from exc
                if exc.code == 429:
                    time.sleep(1.0 * (attempt + 1))
                else:
                    time.sleep(0.3 * (attempt + 1))
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                last_err = exc
                time.sleep(0.3 * (attempt + 1))

        raise BrokerError(f"Alpaca request failed after {self.cfg.max_retries} tries: {last_err}")

    # ------------------------------------------------------------------ venue

    def account(self) -> dict:
        acct = self._request("GET", "/account")
        equity = float(acct.get("equity", 0) or 0)
        cash = float(acct.get("cash", 0) or 0)
        return {
            "equity": round(equity, 2),
            "cash": round(cash, 2),
            "buying_power": round(float(acct.get("buying_power", cash) or 0), 2),
            "realised_pnl": 0.0,
            "total_fees": 0.0,
            "n_positions": len(acct.get("portfolio_holdings", []) or []),
            "currency": str(acct.get("currency", "USD")),
        }

    def positions(self) -> list[Position]:
        raw = self._request("GET", "/positions")
        out: list[Position] = []
        for item in raw or []:
            qty = int(float(item.get("qty", 0) or 0))
            if qty == 0:
                continue
            side = str(item.get("side", "long"))
            pos = Position(
                symbol=str(item.get("symbol", "")),
                quantity=abs(qty),
                side="long" if side != "short" else "short",
                entry_price=float(item.get("avg_entry_price", 0) or 0),
                current_price=float(item.get("current_price", 0) or 0),
                market_value=float(item.get("market_value", 0) or 0),
                unrealized_pnl=float(item.get("unrealized_pl", 0) or 0),
                stop_price=None,
            )
            if pos.risk_dollars == 0 and pos.entry_price and pos.current_price:
                pos.risk_dollars = abs(pos.entry_price - pos.current_price) * pos.quantity
            out.append(pos)
        return out

    def market_price(self, symbol: str) -> float:
        try:
            trade = self._request("GET", f"/stocks/{symbol}/trades/latest")
            price = float(trade.get("trade", {}).get("p", 0))
            if price > 0:
                return price
        except BrokerError:
            pass
        quotes = self._request("GET", f"/stocks/{symbol}/quotes/latest")
        quote = quotes.get("quote", {})
        bid = float(quote.get("bp", 0) or 0)
        ask = float(quote.get("ap", 0) or 0)
        price = (bid + ask) / 2 if bid > 0 and ask > 0 else (ask or bid)
        if price <= 0:
            raise BrokerError(f"no tradable price for {symbol}")
        return price

    def is_market_open(self) -> bool:
        clock = self._request("GET", "/clock")
        return bool(clock.get("is_open"))

    # ---------------------------------------------------------------- trading

    def submit(self, order: Order) -> Fill:
        payload: dict = {
            "symbol": order.symbol,
            "qty": str(order.quantity),
            "side": order.side,
            "type": order.type,
            "time_in_force": order.time_in_force,
        }
        if order.type in ("limit", "stop_limit") and order.limit_price:
            payload["limit_price"] = str(round(order.limit_price, 2))
        if order.type in ("stop", "stop_limit") and order.stop_price:
            payload["stop_price"] = str(round(order.stop_price, 2))

        result = self._request("POST", "/orders", payload)
        filled_qty = int(float(result.get("filled_qty", 0) or order.quantity))
        avg_price = float(result.get("filled_avg_price", 0) or 0)
        status = str(result.get("status", ""))

        if status in ("filled", "partially_filled") and not avg_price:
            # Market orders fill on the next poll; read the order back once.
            order_id = str(result.get("id", ""))
            for _ in range(3):
                time.sleep(0.6)
                detail = self._request("GET", f"/orders/{order_id}")
                avg_price = float(detail.get("filled_avg_price", 0) or 0)
                if avg_price > 0:
                    filled_qty = int(float(detail.get("filled_qty", filled_qty) or filled_qty))
                    break
        if not avg_price:
            raise BrokerError(f"order {result.get('id')} not filled (status={status})")

        return Fill(
            symbol=order.symbol,
            quantity=filled_qty,
            side=order.side,
            price=avg_price,
            fees=0.0,
            order_id=str(result.get("id", "")),
        )

    def cancel_all(self) -> None:
        try:
            self._request("DELETE", "/orders")
        except BrokerError as exc:
            print(f"[!] could not cancel open orders: {exc}")


__all__ = ["AlpacaBroker", "AlpacaConfig", "PAPER_URL", "LIVE_URL"]
