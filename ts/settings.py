"""Persistent settings: credentials, strategy choice, instruments, risk.

Everything the dashboard's settings panels control lives here and is written to
``state/settings.json``.  Design rules that matter:

* **Secrets stay out of the repo.** Credentials are read from environment
  variables first; a file is only written when you explicitly save them, and it
  lives under ``state/`` which is never committed.
* **Every panel is one dataclass.** The UI edits a structure, not a pile of
  individual endpoints, so there is exactly one place defaults live.
* **Nothing is live by default.** ``broker`` starts as ``paper`` and
  ``dry_run`` starts as ``True``; enabling real money takes several deliberate
  steps rather than one checkbox.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path

log = logging.getLogger(__name__)

SETTINGS_VERSION = 1

# --------------------------------------------------------------- credentials


@dataclass
class BrokerCredential:
    """How to authenticate against one venue.

    ``key`` and ``secret`` are resolved from the environment when available, so
    the normal deployment is "export the keys, never write them to disk". A file
    copy is only used when ``store_credentials`` is set explicitly.
    """

    broker: str = "paper"              # paper | binance | alpaca | exness | generic
    key: str = ""
    secret: str = ""
    passphrase: str = ""               # some venues need a third field
    testnet: bool = True
    store_credentials: bool = False
    account_label: str = ""

    def resolve(self) -> "BrokerCredential":
        """Fill empty fields from the matching environment variables."""
        prefix = self.broker.upper()
        env_map = {
            "key": f"{prefix}_KEY_ID",
            "secret": f"{prefix}_SECRET_KEY",
            "passphrase": f"{prefix}_PASSPHRASE",
        }
        for fieldname, envvar in env_map.items():
            if not getattr(self, fieldname):
                value = os.getenv(envvar)
                if value:
                    setattr(self, fieldname, value)
        if not self.key:
            self.key = os.getenv("ALPACA_KEY_ID", "")
        if not self.secret:
            self.secret = os.getenv("ALPACA_SECRET_KEY", "")
        return self

    def redacted(self) -> dict:
        """Safe to show in the UI and to log."""

        def mask(value: str) -> str:
            if not value:
                return ""
            if len(value) <= 4:
                return "*" * len(value)
            return value[:2] + "*" * (len(value) - 6) + value[-4:]

        return {
            "broker": self.broker,
            "key_set": bool(self.key),
            "key_masked": mask(self.key),
            "secret_set": bool(self.secret),
            "passphrase_set": bool(self.passphrase),
            "testnet": self.testnet,
            "stored_in_file": self.store_credentials,
            "source": "environment" if (not self.store_credentials and self.key) else "file",
        }


# ------------------------------------------------------------------ strategy


@dataclass
class StrategySettings:
    """Which strategy runs and how it behaves."""

    # two strategies are available; both can run at once
    run_trend_breakout: bool = True
    run_multi_agent: bool = True

    # --- trend breakout parameters
    entry_lookback: int = 20
    exit_lookback: int = 10
    regime_ma_period: int = 200
    adx_period: int = 14
    adx_min: float = 20.0
    atr_period: int = 14
    atr_stop_mult: float = 2.0
    atr_trail_mult: float = 3.0
    allow_shorts: bool = False
    require_volume: bool = False      # 1m volume is unreliable; off by default

    # --- multi-agent parameters
    min_expectancy: float = 0.20      # R units required before sizing
    min_confidence: float = 0.65      # posterior probability required
    kelly_fraction: float = 0.25      # fractional Kelly
    min_observations: int = 10        # analyst calls before it may influence

    # --- cycle
    poll_seconds: float = 60.0

    def effective(self) -> dict:
        """Merge into the parameter objects each engine expects."""
        return {
            "trend": {
                "entry_lookback": self.entry_lookback,
                "exit_lookback": self.exit_lookback,
                "regime_ma_period": self.regime_ma_period,
                "adx_period": self.adx_period,
                "adx_min": self.adx_min,
                "atr_period": self.atr_period,
                "atr_stop_mult": self.atr_stop_mult,
                "atr_trail_mult": self.atr_trail_mult,
                "allow_shorts": self.allow_shorts,
                "require_volume": self.require_volume,
            },
            "agents": {
                "min_expectancy": self.min_expectancy,
                "min_confidence": self.min_confidence,
                "kelly_fraction": self.kelly_fraction,
            },
        }


# ------------------------------------------------------------- instruments


@dataclass
class InstrumentSettings:
    """Which markets, which timeframe, and how big the universe is."""

    timeframe: str = "1m"
    range_: str = "7d"
    asset_classes: list[str] = field(
        default_factory=lambda: ["index", "forex", "crypto"])
    symbols: list[str] = field(default_factory=list)   # empty = use the class defaults
    max_symbols: int = 20
    use_market_gate: bool = True
    market_gate_symbol: str = "SPY"

    def normalise(self) -> "InstrumentSettings":
        valid = {"1m", "2m", "5m", "15m", "30m", "60m", "1h", "1d", "1wk"}
        if self.timeframe not in valid:
            self.timeframe = "1m"
        # 1m data is capped at 7 days by the source; longer ranges silently
        # come back with less data than expected
        if self.timeframe == "1m" and self.range_ not in ("1d", "5d", "7d"):
            self.range_ = "7d"
        self.asset_classes = [c for c in self.asset_classes
                              if c in ("index", "forex", "crypto")]
        if not self.asset_classes:
            self.asset_classes = ["crypto"]
        self.symbols = [s.strip().upper() for s in self.symbols if s.strip()]
        self.max_symbols = max(1, min(int(self.max_symbols), 50))
        return self


# --------------------------------------------------------------------- risk


@dataclass
class RiskSettings:
    """Everything the risk panels control."""

    risk_per_trade: float = 0.01        # fraction of equity risked per trade
    max_position_pct: float = 0.20      # no single position above this
    max_positions: int = 8
    max_total_risk: float = 0.06        # simultaneous open risk
    max_fraction: float = 0.05          # Kelly cap as a fraction of bankroll
    daily_loss_limit: float = 0.03      # -3% on the session -> flatten
    max_drawdown_halt: float = 0.15     # -15% from peak -> freeze entries
    bankroll: float = 100_000.0
    capital: float = 100_000.0          # starting capital for paper/backtest

    # --- execution costs
    slippage_bps: float = 5.0
    commission_per_share: float = 0.005
    commission_min: float = 1.0

    # --- safety
    dry_run: bool = True                # plan and log, never submit
    kill_on_risk: bool = True           # engage the kill switch automatically

    def clamp(self) -> "RiskSettings":
        self.risk_per_trade = float(min(max(self.risk_per_trade, 0.0005), 0.2))
        self.max_position_pct = float(min(max(self.max_position_pct, 0.01), 1.0))
        self.max_positions = int(max(1, min(self.max_positions, 40)))
        self.max_total_risk = float(max(self.max_total_risk, self.risk_per_trade))
        self.max_fraction = float(min(max(self.max_fraction, 0.001), 0.5))
        self.daily_loss_limit = float(max(self.daily_loss_limit, 0.005))
        self.max_drawdown_halt = float(max(self.max_drawdown_halt, 0.02))
        self.bankroll = float(max(self.bankroll, 100.0))
        self.capital = float(max(self.capital, 100.0))
        self.slippage_bps = float(max(self.slippage_bps, 0.0))
        return self


# ----------------------------------------------------------------- settings


@dataclass
class Settings:
    """The whole configuration, exactly as the dashboard edits it."""

    version: int = SETTINGS_VERSION
    broker: BrokerCredential = field(default_factory=BrokerCredential)
    strategy: StrategySettings = field(default_factory=StrategySettings)
    instruments: InstrumentSettings = field(default_factory=InstrumentSettings)
    risk: RiskSettings = field(default_factory=RiskSettings)
    updated_at: str = ""
    updated_by: str = ""

    def clamp(self) -> "Settings":
        self.broker.resolve()
        self.strategy = self.strategy
        self.instruments.normalise()
        self.risk.clamp()
        if not self.updated_at:
            self.updated_at = datetime.now(UTC).isoformat()
        return self

    def redacted(self) -> dict:
        """A copy with the secret masked -- safe to render and to log."""
        payload = asdict(self)
        payload["broker"] = self.broker.redacted()
        return payload


DEFAULT_SETTINGS = Settings()


def _coerce(payload: dict) -> Settings:
    """Build a Settings from a (possibly partial) dict, keeping defaults."""
    settings = Settings()
    for section in ("broker", "strategy", "instruments", "risk"):
        if isinstance(payload.get(section), dict):
            target = getattr(settings, section)
            for key, value in payload[section].items():
                if hasattr(target, key):
                    setattr(target, key, value)
    return settings


class SettingsStore:
    """Reads and writes ``state/settings.json``.

    Writes are atomic (temp file + replace) so a crash mid-save cannot leave a
    truncated file that the engine then refuses to parse.
    """

    def __init__(self, root: str | Path = "state") -> None:
        self.path = Path(root) / "settings.json"
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def load(self) -> Settings:
        if not self.path.exists():
            return Settings()
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            log.warning("could not read settings (%s); using defaults", exc)
            return Settings()
        if not isinstance(payload, dict):
            return Settings()
        settings = _coerce(payload)
        settings.updated_at = str(payload.get("updated_at", ""))
        settings.updated_by = str(payload.get("updated_by", ""))
        # credentials are never loaded from a file unless it was saved with them
        if not settings.broker.store_credentials:
            settings.broker = BrokerCredential(
                broker=settings.broker.broker,
                testnet=settings.broker.testnet,
                account_label=settings.broker.account_label,
                store_credentials=False,
            ).resolve()
        return settings.clamp()

    def save(self, settings: Settings, updated_by: str = "dashboard") -> Settings:
        settings.clamp()
        settings.updated_by = updated_by
        settings.updated_at = datetime.now(UTC).isoformat()

        payload = asdict(settings)
        # only persist the secret when explicitly asked to
        if not settings.broker.store_credentials:
            payload["broker"].pop("secret", None)
            payload["broker"].pop("passphrase", None)

        tmp = self.path.with_suffix(".tmp")
        with tmp.open("w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2)
        tmp.replace(self.path)
        return settings

    def update(self, section: str, values: dict, updated_by: str = "dashboard") -> Settings:
        settings = self.load()
        if not hasattr(settings, section):
            raise KeyError(f"unknown settings section {section!r}")
        target = getattr(settings, section)
        for key, value in values.items():
            if not hasattr(target, key):
                raise KeyError(f"unknown key {section}.{key}")
            setattr(target, key, value)
        return self.save(settings, updated_by=updated_by)


__all__ = [
    "Settings",
    "SettingsStore",
    "BrokerCredential",
    "StrategySettings",
    "InstrumentSettings",
    "RiskSettings",
    "DEFAULT_SETTINGS",
]
