"""Generic webhook / file-bridge broker.

For venues with no usable API: orders are written as JSON to a watched
directory and optionally POSTed to a webhook. The receiving side -- your own
script, a bridge service, or an EA -- does the actual execution and reports
fills back by appending to the same outbox.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path

from .base import Broker, BrokerError, Fill, Order

log = logging.getLogger(__name__)


@dataclass
class GenericConfig:
    webhook_url: str = ""
    outbox: str = "state/orders"
    poll_seconds: float = 0.25
    timeout: float = 10.0

    def __post_init__(self) -> None:
        Path(self.outbox).mkdir(parents=True, exist_ok=True)


class GenericBroker(Broker):
    """Writes order requests; never claims a fill it did not observe."""

    name = "generic"
    mode = "live"

    def __init__(self, config: GenericConfig | None = None) -> None:
        self.cfg = config or GenericConfig()
        self.supports_shorting = True
        if not self.cfg.webhook_url and not self.cfg.outbox:
            raise BrokerError("the generic broker needs a webhook URL or an outbox path")

    # ------------------------------------------------------------------ venue

    def account(self) -> dict:
        return {
            "equity": 0.0,
            "cash": 0.0,
            "buying_power": 0.0,
            "n_positions": 0,
            "currency": "n/a",
            "note": "the generic bridge cannot report balances; poll your venue",
        }

    def positions(self) -> list:
        return []

    def market_price(self, symbol: str) -> float:
        raise BrokerError(
            "the generic bridge has no price feed; use MarketFeed for prices and "
            "this broker only for order delivery")

    def is_market_open(self) -> bool:
        return True

    # ---------------------------------------------------------------- trading

    def submit(self, order: Order) -> Fill:
        payload = order.to_dict()
        payload["order_id"] = self.next_client_id()
        payload["submitted_at"] = time.time()

        if self.cfg.webhook_url:
            self._post(payload)
        path = Path(self.cfg.outbox) / f"{payload['order_id']}.json"
        path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

        # A webhook 202 does not mean the trade filled. Report what we know:
        # the order was delivered, the fill is not observed.
        return Fill(
            symbol=order.symbol,
            quantity=order.quantity,
            side=order.side,
            price=0.0,
            fees=0.0,
            order_id=payload["order_id"],
        )

    def _post(self, payload: dict) -> None:
        import urllib.request

        data = json.dumps(payload).encode()
        req = urllib.request.Request(
            self.cfg.webhook_url, data=data, method="POST",
            headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=self.cfg.timeout) as response:
                response.read()
        except Exception as exc:  # noqa: BLE001
            log.warning("webhook delivery failed: %s", exc)


__all__ = ["GenericBroker", "GenericConfig"]
