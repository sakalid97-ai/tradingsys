"""Add the settings / broker API to the dashboard server."""

from __future__ import annotations

from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
server = ROOT / "ts" / "web" / "server.py"
text = server.read_text(encoding="utf-8")

if "/api/settings" in text:
    print("[skip] server.py: settings API already present")
    sys.exit(0)

# --- routes
old = '        if path == "/api/agents":\n            return 200, self._agents()'
new = '''        if path == "/api/agents":
            return 200, self._agents()
        if path == "/api/settings" and method == "GET":
            return 200, self.ctx.settings.redacted()
        if path == "/api/settings" and method == "POST":
            return self._save_settings(body)
        if path == "/api/brokers":
            return 200, {"brokers": _catalogue()}
        if path == "/api/broker/connect" and method == "POST":
            return self._broker_connect(body)
        if path == "/api/broker/disconnect" and method == "POST":
            return self._broker_disconnect()
        if path == "/api/broker/test" and method == "POST":
            return self._broker_test(body)
        if path == "/api/instruments":
            return 200, self._instruments()'''
if old not in text:
    print("[FAIL] /api/agents route anchor not found")
    sys.exit(1)
text = text.replace(old, new, 1)

# --- handlers
anchor = "    def _run_backtest(self, body: dict) -> tuple[int, dict]:"
handlers = '''    # -------------------------------------------------------------- settings

    def _save_settings(self, body: dict) -> tuple[int, dict]:
        """Persist a settings update from the dashboard.

        A section name limits what can change, so a bad payload from one panel
        cannot corrupt another panel's values.
        """
        from ..settings import SettingsStore

        section = str(body.get("section", "")).strip()
        values = body.get("values") or {}
        if section not in ("broker", "strategy", "instruments", "risk"):
            return 400, {"error": "section must be broker, strategy, instruments or risk"}
        if not isinstance(values, dict):
            return 400, {"error": "values must be an object"}
        try:
            store = SettingsStore(self.ctx.state.root)
            updated = store.update(section, values)
        except KeyError as exc:
            return 400, {"error": str(exc)}
        except (OSError, ValueError) as exc:
            return 500, {"error": str(exc)}
        self.ctx.settings = updated
        if self.state:
            self.state.log("settings_updated", section=section, keys=list(values))
        return 200, {"ok": True, "settings": updated.redacted()}

    def _instruments(self) -> dict:
        """The tradable universe with per-class defaults."""
        from ..agents.instruments import DEFAULT_UNIVERSE

        return {
            "defaults": {c.value: list(s) for c, s in DEFAULT_UNIVERSE.items()},
            "selected": self.ctx.settings.instruments.symbols,
            "timeframe": self.ctx.settings.instruments.timeframe,
            "range": self.ctx.settings.instruments.range_,
            "asset_classes": self.ctx.settings.instruments.asset_classes,
        }

    # --------------------------------------------------------------- brokers

    def _broker_connect(self, body: dict) -> tuple[int, dict]:
        """Save credentials; does not open a connection by itself."""
        from ..settings import SettingsStore

        allowed = ("broker", "key", "secret", "passphrase", "testnet",
                   "store_credentials", "account_label")
        values = {k: v for k, v in (body or {}).items() if k in allowed}
        if not values.get("broker"):
            return 400, {"error": "broker id is required"}
        try:
            store = SettingsStore(self.ctx.state.root)
            updated = store.update("broker", values)
        except (KeyError, OSError, ValueError) as exc:
            return 400, {"error": str(exc)}
        self.ctx.settings = updated
        redacted = updated.broker.redacted()
        return 200, {
            "ok": True,
            "broker": redacted,
            "note": ("credentials stored on disk under state/" if values.get(
                "store_credentials") else "credentials held for this session only; "
                                          "environment variables are preferred"),
        }

    def _broker_disconnect(self) -> tuple[int, dict]:
        from ..settings import SettingsStore

        store = SettingsStore(self.ctx.state.root)
        updated = store.update("broker", {"key": "", "secret": "", "passphrase": "",
                                          "account_label": ""})
        self.ctx.settings = updated
        return 200, {"ok": True, "broker": updated.broker.redacted()}

    def _broker_test(self, body: dict) -> tuple[int, dict]:
        """Attempt a real connection and read the account back.

        Returns a structured result rather than a bare error so the UI can show
        *why* a connection failed -- which for Exness is nearly always a missing
        MT5 terminal.
        """
        from ..broker import registry
        from ..settings import SettingsStore

        store = SettingsStore(self.ctx.state.root)
        settings = store.load()
        broker_id = str((body or {}).get("broker") or settings.broker.broker).lower()

        descriptor = registry.describe(broker_id)
        if descriptor is None:
            return 404, {"ok": False, "error": f"unknown broker {broker_id!r}",
                         "supported": registry.catalogue()}
        if broker_id == "paper":
            return 200, {"ok": True, "broker": "paper",
                         "account": {"equity": settings.risk.capital, "cash": settings.risk.capital,
                                     "currency": "USD", "note": "simulated"}}

        credential = settings.broker.resolve()
        credential.broker = broker_id
        try:
            broker = registry.build(credential,
                                    price_fn=lambda s: self._test_price(s),
                                    starting_cash=settings.risk.capital)
            account = broker.account()
        except Exception as exc:  # noqa: BLE001 - surface every failure mode
            problem = str(exc)
            hint = ""
            if broker_id == "exness":
                hint = ("Exness has no REST API. This needs Windows, the MetaTrader 5 "
                        "terminal installed and running, an Exness MT5 account logged "
                        "in, algorithmic trading enabled, and "
                        "'py -m pip install MetaTrader5'.")
            elif broker_id == "binance":
                hint = ("Use testnet keys from testnet.binance.vision, or set "
                        "BINANCE_LIVE_CONFIRMED=yes to allow the live endpoint.")
            elif broker_id == "alpaca":
                hint = ("Use paper keys from alpaca.markets, or set "
                        "ALPACA_LIVE_CONFIRMED=yes for the live endpoint.")
            return 200, {"ok": False, "broker": broker_id, "error": problem,
                         "hint": hint, "requires": descriptor.requires}

        return 200, {"ok": True, "broker": broker.name, "mode": broker.mode,
                     "account": account, "redacted": credential.redacted()}

    def _test_price(self, symbol: str) -> float:
        """A price for connection tests, so the paper path has one."""
        try:
            if self.ctx.data_provider is None:
                raise BrokerError("no price source configured")
            frames = self.ctx.data_provider()
            if symbol in frames:
                return float(frames[symbol]["close"].iloc[-1])
            first = next(iter(frames.values()), None)
            if first is not None:
                return float(first["close"].iloc[-1])
        except Exception:  # noqa: BLE001
            pass
        return 100.0

'''
if anchor not in text:
    print("[FAIL] _run_backtest anchor not found")
    sys.exit(1)
text = text.replace(anchor, handlers + anchor, 1)

# --- imports + the settings on the context
text = text.replace(
    "from ..state import StateStore, dumps",
    "from ..broker.base import BrokerError\nfrom ..settings import Settings\nfrom ..settings import SettingsStore\nfrom ..state import StateStore, dumps",
    1,
)
text = text.replace(
    "    kill_file: Path = field(default=Path(\"tradingsys/state/KILL\"))",
    "    kill_file: Path = field(default=Path(\"tradingsys/state/KILL\"))\n"
    "    settings: Settings = field(default_factory=Settings)",
    1,
)
text = text.replace(
    "STATIC_DIR = Path(__file__).resolve().parent / \"static\"",
    "STATIC_DIR = Path(__file__).resolve().parent / \"static\"\n\n\n"
    "def _catalogue():\n"
    "    from ..broker import registry\n\n"
    "    return registry.catalogue()",
    1,
)
server.write_text(text, encoding="utf-8")
print("[ok]   server.py: settings + broker API added")

sys.path.insert(0, str(ROOT))
try:
    from ts.web.server import DashboardServer  # noqa: F401
    print("[ok]   server.py imports cleanly")
except Exception as exc:  # noqa: =None
    print(f"[FAIL] import error: {exc}")
sys.exit(0)
