"""Binance adapter (spot and futures, live or testnet).

Signed with HMAC SHA256 using only the standard library, so the trading box
needs no vendor SDK.

Safety: the adapter never constructs on a *live* endpoint unless
``testnet=False`` is passed deliberately **and** the environment asserts it.
Testnet (``https://testnet.binance.vision``) is the default, and its keys are
free to generate from the Binance testnet UI.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

from .base import Broker, BrokerError, Fill, Order, Position

log = logging.getLogger(__name__)

LIVE_SPOT = "https://api.binance.com"
TEST_SPOT = "https://testnet.binance.vision"
LIVE_FUTURES = "https://fapi.binance.com"
TEST_FUTURES = "https://testnet.binancefuture.com"


@dataclass
class BinanceConfig:
    api_key: str = ""
    api_secret: str = ""
    futures: bool = False
    testnet: bool = True
    recv_window: int = 5_000
    timeout: float = 15.0

    def __post_init__(self) -> None:
        self.api_key = self.api_key or os.getenv("BINANCE_KEY_ID", "")
        self.api_secret = self.api_secret or os.getenv("BINANCE_SECRET_KEY", "")

    @property
    def base_url(self) -> str:
        if self.futures:
            return TEST_FUTURES if self.testnet else LIVE_FUTURES
        return TEST_SPOT if self.testnet else LIVE_SPOT

    @property
    def is_live(self) -> bool:
        """Exact endpoint match, never a substring test.

        Binance's futures live host is "fapi.binance.com", which contains
        "api.binance.com" as a substring -- so a substring check would misread
        a live account as spot (or vice versa). Compare against the known set.
        """
        return self.base_url in (LIVE_SPOT, LIVE_FUTURES)


def sign(secret: str, query: str) -> str:
    """HMAC SHA256 signature for a Binance query string."""
    if not secret:
        raise BrokerError("a Binance API secret is required to sign requests")
    return hmac.new(secret.encode(), query.encode(), hashlib.sha256).hexdigest()


def build_query(params: dict) -> str:
    """URL-encode parameters in the deterministic order Binance expects."""
    return urllib.parse.urlencode(sorted(params.items()))


class BinanceBroker(Broker):
    """Spot/futures adapter for Binance."""

    name = "binance"

    def __init__(self, config: BinanceConfig | None = None) -> None:
        self.cfg = config or BinanceConfig()
        self.mode = "live" if self.cfg.is_live else "paper"
        self.name = f"binance-{'futures' if self.cfg.futures else 'spot'}-{self.mode}"
        self.supports_shorting = self.cfg.futures

        if self.cfg.is_live and os.getenv("BINANCE_LIVE_CONFIRMED", "").lower() != "yes":
            raise BrokerError(
                "this configuration points at the LIVE Binance endpoint. Set "
                "BINANCE_LIVE_CONFIRMED=yes once you accept that real funds are at risk."
            )
        if not self.cfg.api_key or not self.cfg.api_secret:
            raise BrokerError(
                "Binance requires BINANCE_KEY_ID and BINANCE_SECRET_KEY. Generate "
                "them at binance.com (or testnet.binance.vision for the testnet)."
            )

    # --------------------------------------------------------------- plumbing

    def _headers(self) -> dict[str, str]:
        return {"X-MBX-APIKEY": self.cfg.api_key, "Content-Type": "application/json"}

    def _request(self, method: str, path: str, params: dict | None = None,
                 signed: bool = False):
        query = build_query(params or {})
        if signed:
            params = dict(params or {})
            params["timestamp"] = int(time.time() * 1000)
            params["recvWindow"] = self.cfg.recv_window
            query = build_query(params)
            query += f"&signature={sign(self.cfg.api_secret, query)}"

        url = f"{self.cfg.base_url}{path}"
        if query:
            url += f"?{query}"
        data = json.dumps(params).encode() if method == "POST" and not signed else None
        if method == "POST" and not signed:
            url = f"{self.cfg.base_url}{path}"

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
            raise BrokerError(f"Binance {method} {path} -> HTTP {exc.code}: {detail}") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise BrokerError(f"Binance request failed: {exc}") from exc

    # ------------------------------------------------------------------ venue

    def server_time(self) -> int:
        data = self._request("GET", "/api/v3/time")
        return int(data.get("serverTime", 0))

    def account(self) -> dict:
        data = self._request("GET", "/api/v3/account", signed=True)
        balances = data.get("balances", [])
        free = sum(float(b.get("free", 0) or 0) for b in balances)
        locked = sum(float(b.get("locked", 0) or 0) for b in balances)
        return {
            "equity": round(free + locked, 8),
            "cash": round(free, 8),
            "buying_power": round(free, 8),
            "n_positions": len([b for b in balances
                                if float(b.get("free", 0) or 0) > 0]),
            "currency": "USDT",
            "raw_symbols": [b["asset"] for b in balances if float(b.get("free", 0) or 0) > 0],
        }

    def positions(self) -> list[Position]:
        """Binance spot has no short inventory; only futures report positions."""
        if not self.cfg.futures:
            return []
        data = self._request("GET", "/fapi/v2/positionRisk", signed=True)
        out: list[Position] = []
        for item in data or []:
            amount = float(item.get("positionAmt", 0) or 0)
            if abs(amount) < 1e-12:
                continue
            out.append(Position(
                symbol=str(item.get("symbol", "")),
                quantity=abs(amount),
                side="long" if amount > 0 else "short",
                entry_price=float(item.get("entryPrice", 0) or 0),
                current_price=float(item.get("markPrice", 0) or 0),
                unrealized_pnl=float(item.get("unRealizedProfit", 0) or 0),
            ))
        return out

    def market_price(self, symbol: str) -> float:
        data = self._request("GET", "/api/v3/ticker/price", {"symbol": symbol})
        price = float(data.get("price", 0) or 0)
        if price <= 0:
            raise BrokerError(f"no price for {symbol}")
        return price

    def is_market_open(self) -> bool:
        # Binance trades 24/7; the engine's own calendar is the constraint
        return True

    # ---------------------------------------------------------------- trading

    def submit(self, order: Order) -> Fill:
        if order.type != "market":
            raise BrokerError("only market orders are supported by this adapter")
        params = {
            "symbol": self._normalise_symbol(order.symbol),
            "side": "BUY" if order.side == "buy" else "SELL",
            "type": "MARKET",
            "quantity": self._format_qty(order.quantity, order.symbol),
        }
        data = self._request("POST", "/api/v3/order", params, signed=True)
        return Fill(
            symbol=order.symbol,
            quantity=float(data.get("executedQty", order.quantity) or order.quantity),
            side=order.side,
            price=float((data.get("fills") or [{}])[0].get("price", 0) or 0)
            or float(data.get("cummulativeQuoteQty", 0) or 0),
            fees=float(sum(float(f.get("commission", 0) or 0)
                           for f in (data.get("fills") or []))),
            order_id=str(data.get("orderId", "")),
        )

    def cancel_all(self) -> None:
        for symbol in ("BTCUSDT", "ETHUSDT"):
            try:
                self._request("DELETE", "/api/v3/openOrders",
                              {"symbol": symbol}, signed=True)
            except BrokerError as exc:
                log.debug("cancel failed for %s: %s", symbol, exc)

    def _normalise_symbol(self, symbol: str) -> str:
        """Trading symbol -> Binance pair. BTCUSD -> BTCUSDT."""
        normalised = symbol.strip().upper().replace("/", "").replace("-", "")
        for quote in ("USDT", "USDC", "BUSD"):
            if normalised.endswith(quote):
                return normalised
        if normalised.endswith("USD"):
            return normalised[:-3] + "USDT"
        return normalised

    def _format_qty(self, quantity: float, symbol: str) -> str:
        """Binance rejects quantities with too many decimal places."""
        step = 0.00001
        for candidate in (1.0, 0.1, 0.01, 0.001, 0.0001, 0.00001, 0.000001):
            if quantity >= candidate * 10:
                step = candidate
                break
        rounded = round(quantity, int(round(-math_log10(step))))
        return f"{rounded:.8f}".rstrip("0").rstrip(".") or "0"


def math_log10(step: float) -> float:
    import math

    return math.log10(step) if step > 0 else 0.0


__all__ = ["BinanceBroker", "BinanceConfig", "sign", "build_query"]
