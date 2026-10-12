"""Instrument specifications across indices, forex, and crypto.

Position sizing is meaningless without knowing what one unit of a symbol is
worth. A 1-point move in SPX500 is not remotely the same P&L as a 1-point move
in USDJPY. This module is the single place that knowledge lives, so the sizer
can stay asset-class agnostic.

Conventions (the common CFD/retail-futures ones):

==============  ==========================  ===============================
Asset class     Unit                        Quote
==============  ==========================  ===============================
Index (CFD)     1 lot = 1 USD per point     index points
Forex           1 lot = 100,000 base units  XXXYYY (base / quote)
Crypto          traded in coin units        coin / USD
==============  ==========================  ===============================
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class AssetClass(str, Enum):
    INDEX = "index"
    FOREX = "forex"
    CRYPTO = "crypto"


@dataclass(frozen=True)
class Instrument:
    """Everything the sizing and risk layers need to know about a symbol."""

    symbol: str
    asset_class: AssetClass
    base_currency: str = "USD"
    quote_currency: str = "USD"
    contract_size: float = 1.0        # value per 1.0 of price movement, per 1 lot
    tick_size: float = 0.0001         # smallest meaningful price increment
    pip_size: float = 0.0001          # 1 pip in price terms
    min_lot: float = 0.01             # smallest tradable lot
    lot_step: float = 0.01
    maker_fee: float = 0.0            # as a fraction of notional
    taker_fee: float = 0.0
    swap_long: float = 0.0            # daily financing, fraction of notional
    swap_short: float = 0.0
    session: str = "24/7"             # informational
    leverage: float = 1.0
    metadata: dict = field(default_factory=dict)

    # -------------------------------------------------------------- helpers

    @property
    def is_two_sided(self) -> bool:
        """Can this symbol be sold short?"""
        return self.asset_class in (AssetClass.FOREX, AssetClass.CRYPTO, AssetClass.INDEX)

    @property
    def value_per_point(self) -> float:
        """P&L in quote currency for a 1.0 price move, per one lot."""
        return self.contract_size

    def price_to_units(self, stop_distance: float) -> float:
        """Notional exposure per lot for a stop of ``stop_distance``."""
        return self.contract_size * stop_distance

    def clamp_quantity(self, lots: float) -> float:
        """Round a desired lot size down to a legal, tradable quantity."""
        if lots <= 0:
            return 0.0
        steps = int(lots / self.lot_step) if self.lot_step > 0 else 0
        quantity = steps * self.lot_step
        if quantity < self.min_lot:
            quantity = self.min_lot if lots >= self.min_lot else 0.0
        return round(quantity, 8)

    def notional(self, price: float, lots: float, leverage: float | None = None) -> float:
        """Cash required (or exposure) for ``lots`` at ``price``."""
        notional = price * self.contract_size * lots
        lev = leverage if leverage is not None else self.leverage
        return notional / (lev if lev > 0 else 1.0)

    def fees_for(self, price: float, lots: float) -> float:
        """Round-turn-ish fee for a single fill (taker assumed on entry)."""
        return abs(self.notional(price, lots)) * self.taker_fee

    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "asset_class": self.asset_class.value,
            "base": self.base_currency,
            "quote": self.quote_currency,
            "contract_size": self.contract_size,
            "tick_size": self.tick_size,
            "pip_size": self.pip_size,
            "min_lot": self.min_lot,
            "lot_step": self.lot_step,
            "taker_fee": self.taker_fee,
            "session": self.session,
            "two_sided": self.is_two_sided,
        }


# --------------------------------------------------------------- known symbols
# Sensible defaults for the most commonly traded instruments. Anything not
# listed here falls back to an inferred spec from the symbol string.

_INDEX_DEFAULTS: dict[str, dict] = {
    "SPX500":  {"contract_size": 1.0, "pip_size": 0.1,  "tick_size": 0.1, "session": "23h"},
    "NAS100":  {"contract_size": 1.0, "pip_size": 0.1,  "tick_size": 0.1, "session": "23h"},
    "US30":    {"contract_size": 1.0, "pip_size": 1.0,  "tick_size": 1.0, "session": "23h"},
    "DJI":     {"contract_size": 1.0, "pip_size": 1.0,  "tick_size": 1.0, "session": "23h"},
    "GER40":   {"contract_size": 1.0, "pip_size": 0.1,  "tick_size": 0.1, "session": "15h"},
    "UK100":   {"contract_size": 1.0, "pip_size": 0.1,  "tick_size": 0.1, "session": "10h"},
    "JPN225":  {"contract_size": 1.0, "pip_size": 1.0,  "tick_size": 1.0, "session": "6h"},
    "AUS200":  {"contract_size": 1.0, "pip_size": 0.1,  "tick_size": 0.1, "session": "10h"},
    "SPX":     {"contract_size": 1.0, "pip_size": 0.25, "tick_size": 0.25, "session": "23h"},
    "NDX":     {"contract_size": 1.0, "pip_size": 0.25, "tick_size": 0.25, "session": "23h"},
}

_CRYPTO_DEFAULTS: dict[str, dict] = {
    "BTCUSD": {"contract_size": 1.0, "tick_size": 0.01, "min_lot": 0.0001, "lot_step": 0.0001,
               "taker_fee": 0.001, "maker_fee": 0.001},
    "ETHUSD": {"contract_size": 1.0, "tick_size": 0.01, "min_lot": 0.001, "lot_step": 0.001,
               "taker_fee": 0.001, "maker_fee": 0.001},
    "SOLUSD": {"contract_size": 1.0, "tick_size": 0.01, "min_lot": 0.01, "lot_step": 0.01,
               "taker_fee": 0.001, "maker_fee": 0.001},
    "XRPUSD": {"contract_size": 1.0, "tick_size": 0.0001, "min_lot": 0.1, "lot_step": 0.1,
               "taker_fee": 0.001, "maker_fee": 0.001},
}

_FOREX_TICK = {"JPY": 0.001}
_FOREX_OTHERS = 0.00001


def _forex_spec(symbol: str) -> Instrument:
    base, quote = symbol[:3].upper(), symbol[3:6].upper()
    jpy_pair = "JPY" in (base + quote)
    tick = _FOREX_TICK["JPY"] if jpy_pair else _FOREX_OTHERS
    return Instrument(
        symbol=symbol,
        asset_class=AssetClass.FOREX,
        base_currency=base,
        quote_currency=quote,
        contract_size=100_000.0,     # one standard lot
        tick_size=tick,
        pip_size=tick,               # a pip is one tick for these majors
        min_lot=0.01,
        lot_step=0.01,
        taker_fee=0.0,
        swap_long=0.00008,
        swap_short=0.00010,
        session="24/5",
    )


def resolve(symbol: str, *, asset_class: AssetClass | None = None) -> Instrument:
    """Build an :class:`Instrument` for ``symbol``.

    Detects the asset class from the symbol when it is not supplied:

    * ``=X`` or a 6-letter ``CCYCCY`` code     -> forex
    * ``-USD`` or a known crypto pair          -> crypto
    * ``SPX500``/``NAS100``/``US30`` or ``^X`` -> index
    """
    raw = symbol.strip().upper()
    normalised = raw.replace("=X", "").replace("-USD", "USD").replace("^", "")

    if asset_class is None:
        if raw.endswith("=X") or (len(normalised) == 6 and normalised[:3].isalpha()
                                  and normalised[3:].isalpha()
                                  and normalised in ("EURUSD", "GBPUSD", "USDJPY", "AUDUSD",
                                                     "USDCAD", "USDCHF", "NZDUSD",
                                                     "EURJPY", "GBPJPY", "EURGBP")):
            asset_class = AssetClass.FOREX
        elif raw.endswith("-USD") or normalised.replace("USD", "") in ("BTC", "ETH", "SOL", "XRP"):
            asset_class = AssetClass.CRYPTO
        elif raw.startswith("^") or normalised in _INDEX_DEFAULTS:
            asset_class = AssetClass.INDEX
        else:
            asset_class = AssetClass.CRYPTO     # safe default for derivatives

    if asset_class == AssetClass.FOREX:
        return _forex_spec(normalised)

    if asset_class == AssetClass.CRYPTO:
        defaults = _CRYPTO_DEFAULTS.get(normalised, {})
        return Instrument(
            symbol=normalised,
            asset_class=AssetClass.CRYPTO,
            base_currency=normalised.replace("USD", ""),
            quote_currency="USD",
            contract_size=defaults.get("contract_size", 1.0),
            tick_size=defaults.get("tick_size", 0.01),
            pip_size=defaults.get("tick_size", 0.01),
            min_lot=defaults.get("min_lot", 0.0001),
            lot_step=defaults.get("lot_step", 0.0001),
            taker_fee=defaults.get("taker_fee", 0.0005),
            maker_fee=defaults.get("maker_fee", 0.0002),
            session="24/7",
            **({"contract_size": 0.01} if "BTC" in normalised and "contract_size" not in defaults else {}),
        )

    defaults = _INDEX_DEFAULTS.get(normalised, {"pip_size": 0.1, "tick_size": 0.1})
    return Instrument(
        symbol=normalised,
        asset_class=AssetClass.INDEX,
        contract_size=defaults.get("contract_size", 1.0),
        tick_size=defaults.get("tick_size", 0.1),
        pip_size=defaults.get("pip_size", 0.1),
        min_lot=0.01,
        lot_step=0.01,
        taker_fee=0.00002,
        swap_long=0.00003,
        swap_short=-0.00002,
        session=defaults.get("session", "23h"),
    )


DEFAULT_UNIVERSE: dict[AssetClass, list[str]] = {
    AssetClass.INDEX: ["SPX500", "NAS100", "US30", "GER40", "JPN225"],
    AssetClass.FOREX: ["EURUSD", "GBPUSD", "USDJPY", "AUDUSD", "USDCAD"],
    AssetClass.CRYPTO: ["BTCUSD", "ETHUSD", "SOLUSD", "XRPUSD"],
}


def yahoo_symbol(symbol: str) -> str:
    """Map a trading symbol onto the equivalent Yahoo Finance ticker."""
    normalised = symbol.strip().upper()
    mapping = {
        "SPX500": "^GSPC",
        "SPX": "^GSPC",
        "NAS100": "^NDX",
        "NDX": "^IXIC",
        "US30": "^DJI",
        "DJI": "^DJI",
        "GER40": "^GDAXI",
        "UK100": "^FTSE",
        "JPN225": "^N225",
        "AUS200": "^AXJO",
        "EURUSD": "EURUSD=X",
        "GBPUSD": "GBPUSD=X",
        "USDJPY": "USDJPY=X",
        "AUDUSD": "AUDUSD=X",
        "USDCAD": "USDCAD=X",
        "USDCHF": "USDCHF=X",
        "NZDUSD": "NZDUSD=X",
        "EURJPY": "EURJPY=X",
        "GBPJPY": "GBPJPY=X",
        "EURGBP": "EURGBP=X",
        "BTCUSD": "BTC-USD",
        "ETHUSD": "ETH-USD",
        "SOLUSD": "SOL-USD",
        "XRPUSD": "XRP-USD",
    }
    return mapping.get(normalised, normalised)


__all__ = ["AssetClass", "Instrument", "resolve", "DEFAULT_UNIVERSE", "yahoo_symbol"]
