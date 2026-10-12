"""Broker registry: what venues are supported and how to build each one.

A single source of truth so the dashboard's connection panel, the engine, and
the live command all agree on which brokers exist, what they need, and whether
they can short. Building a broker is deliberately explicit -- no code path can
construct a live venue by accident.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..settings import BrokerCredential
from .base import Broker, BrokerError


@dataclass
class BrokerDescriptor:
    """How the UI and the factory describe one venue."""

    id: str
    label: str
    asset_classes: list[str]
    supports_shorting: bool
    live_by_default: bool = False     # True = API-only, no simulated mode
    docs_url: str = ""
    requires: list[str] = field(default_factory=list)
    notes: str = ""

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "label": self.label,
            "asset_classes": self.asset_classes,
            "supports_shorting": self.supports_shorting,
            "live_by_default": self.live_by_default,
            "docs_url": self.docs_url,
            "requires": self.requires,
            "notes": self.notes,
        }


BROKERS: tuple[BrokerDescriptor, ...] = (
    BrokerDescriptor(
        id="paper",
        label="Paper (simulated, no account)",
        asset_classes=["index", "forex", "crypto"],
        supports_shorting=True,
        live_by_default=False,
        notes="Fills are simulated against live prices. The default, and the only "
              "mode that needs no credentials.",
    ),
    BrokerDescriptor(
        id="binance",
        label="Binance (spot / USDT-M futures)",
        asset_classes=["crypto", "forex(crypto)"],
        supports_shorting=True,        # futures only
        live_by_default=False,
        docs_url="https://www.binance.com/en/support/faq/how-to-create-api-360002502072",
        requires=["BINANCE_KEY_ID", "BINANCE_SECRET_KEY"],
        notes="Spot and USDT-M futures. Testnet keys work out of the box; set "
              "testnet=False plus BINANCE_LIVE_CONFIRMED=yes for real funds. "
              "Shorting requires the futures endpoint.",
    ),
    BrokerDescriptor(
        id="alpaca",
        label="Alpaca (US equities / crypto)",
        asset_classes=["equity", "crypto"],
        supports_shorting=True,
        live_by_default=False,
        docs_url="https://alpaca.markets/docs/api-references/",
        requires=["ALPACA_KEY_ID", "ALPACA_SECRET_KEY"],
        notes="US equities and crypto. Paper endpoint by default; the live "
              "endpoint additionally needs ALPACA_LIVE_CONFIRMED=yes.",
    ),
    BrokerDescriptor(
        id="exness",
        label="Exness (via MetaTrader 5)",
        asset_classes=["forex", "index", "crypto", "metal"],
        supports_shorting=True,
        live_by_default=True,
        docs_url="https://www.exness.com/",
        requires=["MetaTrader5 package", "MT5 terminal running", "Windows",
                  "algorithmic trading enabled"],
        notes="Exness has no public REST API. Access is through the MT5 terminal: "
              "Windows + terminal installed + logged in + "
              "'py -m pip install MetaTrader5'.",
    ),
    BrokerDescriptor(
        id="generic",
        label="Generic webhook / file bridge",
        asset_classes=["any"],
        supports_shorting=True,
        live_by_default=True,
        notes="Emits orders as JSON to a webhook or a watched directory, for "
              "venues without an API. You wire the receiving side.",
    ),
)


def describe(broker_id: str) -> BrokerDescriptor | None:
    for descriptor in BROKERS:
        if descriptor.id == broker_id:
            return descriptor
    return None


def catalogue() -> list[dict]:
    return [descriptor.to_dict() for descriptor in BROKERS]


def build(credential: BrokerCredential, price_fn=None, starting_cash: float = 100_000.0,
          symbols: list[str] | None = None) -> Broker:
    """Construct a broker from a credential.

    ``paper`` is the only id that needs nothing; every other path is explicit
    and can raise, which is deliberate -- a silent fall-back to paper on a live
    account would be worse than a hard failure.
    """
    broker_id = (credential.broker or "paper").lower()

    if broker_id == "paper":
        from .paper import PaperBroker, PaperBrokerConfig

        if price_fn is None:
            raise BrokerError("paper trading needs a price function")
        return PaperBroker(
            price_fn,
            PaperBrokerConfig(starting_cash=starting_cash),
        )

    if broker_id == "binance":
        from .binance import BinanceBroker, BinanceConfig

        return BinanceBroker(BinanceConfig(
            api_key=credential.key,
            api_secret=credential.secret,
            testnet=credential.testnet,
        ))

    if broker_id == "alpaca":
        from .alpaca import AlpacaBroker, AlpacaConfig

        return AlpacaBroker(AlpacaConfig(
            key_id=credential.key,
            secret_key=credential.secret,
            base_url=None,
            mode="paper" if credential.testnet else "live",
        ))

    if broker_id == "exness":
        from .exness import ExnessBroker, ExnessConfig

        return ExnessBroker(ExnessConfig(
            login=credential.key,
            password=credential.secret,
            server=credential.passphrase,
        ))

    if broker_id == "generic":
        from .generic import GenericBroker, GenericConfig

        return GenericBroker(GenericConfig(
            webhook_url=credential.key,
            outbox=str(credential.secret or "state/orders"),
        ))

    raise BrokerError(
        f"unknown broker {broker_id!r}. Supported: "
        f"{', '.join(d.id for d in BROKERS)}"
    )


__all__ = ["BROKERS", "BrokerDescriptor", "build", "catalogue", "describe"]
