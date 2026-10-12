"""Two-way command channel: send a Telegram message, the system applies it.

The notifier pushes alerts out; this module lets the *trader* push commands
back in. A stopped engine, a breached risk limit, or a filled position can now
be acted on from a phone without opening the dashboard.

Commands are parsed from plain text::

    /status            full state: equity, positions, risk, drift verdict
    /positions         open positions only
    /kill             emergency stop -- flatten and halt
    /resume           clear the kill switch
    /pause            stop taking new trades, keep managing open ones
    /start           resume taking trades
    /set risk 0.005   change the risk per trade
    /set kelly 0.15   change the Kelly fraction
    /forward          forward-tracking verdict (predicted vs realised)
    /run backtest     run a CLI command
    /help             list the commands

Design rules that matter for something on the money path:

1. **Every command is validated before it runs.** Unknown command, bad
   argument, or out-of-range value returns an error message and changes
   nothing. A typo cannot silently loosen a risk limit.
2. **Dangerous commands are gated.** ``/kill`` works from anywhere. Changing a
   *risk limit upward* requires the reply to be confirmed, because a typo in
   ``/set risk 0.5`` should not quietly quintuple your exposure.
3. **Every action is journalled** with who asked, what, and whether it worked.
4. **Unknown senders are ignored entirely** -- if a chat id is configured, only
   that id may issue commands. Otherwise anyone who finds the bot could trade
   your account.
"""

from __future__ import annotations

import logging
import re
import shlex
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum
from typing import Callable

from ts.notify import Notifier, NotifyConfig, Severity, _escape

log = logging.getLogger(__name__)


class CommandError(ValueError):
    """The user's message could not be understood."""


@dataclass
class CommandContext:
    """Everything a command needs, injected so this module stays decoupled."""

    get_status: Callable[[], dict] | None = None
    get_positions: Callable[[], list] | None = None
    get_risk: Callable[[], dict] | None = None
    get_forward: Callable[[], dict] | None = None
    set_risk: Callable[[str, float], bool] | None = None
    kill: Callable[[str], bool] | None = None
    resume: Callable[[], bool] | None = None
    pause: Callable[[], bool] | None = None
    start: Callable[[], bool] | None = None
    run_cli: Callable[[str, dict | None], dict] | None = None
    get_commands: Callable[[], list[str]] | None = None

    def available(self) -> list[str]:
        """Commands whose handler is actually wired up."""
        result = []
        for name in ("get_status", "get_positions", "get_risk", "get_forward"):
            if getattr(self, name) is not None:
                result.append({ "get_status": "status",
                                "get_positions": "positions",
                                "get_risk": "risk",
                                "get_forward": "forward"}[name])
        for name, command in (("kill", "kill"), ("resume", "resume"),
                              ("pause", "pause"), ("start", "start")):
            if getattr(self, name) is not None:
                result.append(command)
        if self.set_risk is not None:
            result.append("set")
        if self.run_cli is not None:
            result.append("run")
        return result


# numeric bounds: a command may not push a limit outside these
RISK_BOUNDS = {"risk": (0.0005, 0.20), "kelly": (0.05, 1.0),
               "max_risk": (0.001, 0.50), "daily": (0.005, 0.20),
               "dd": (0.02, 0.60), "expectancy": (0.0, 1.0),
               "confidence": (0.5, 0.99)}
# keys that, when raised, need a confirming reply
CONFIRM_KEYS = {"risk", "max_risk", "daily", "dd"}
_pending_confirmations: dict[int, tuple[str, float, float]] = {}


def parse(text: str, context: CommandContext | None = None) -> tuple[str, dict]:
    """Parse a message into (command, args). Raises CommandError if unusable."""
    if not text or not text.strip():
        raise CommandError("empty message")

    cleaned = text.strip()
    # strip the @botname suffix Telegram appends in group chats
    cleaned = re.sub(r"@\w+\b", "", cleaned, count=1).strip()

    parts = shlex.split(cleaned)
    if not parts:
        raise CommandError("could not parse that message")

    head = parts[0].lower().lstrip("/")
    args: dict = {}

    if head in ("help", "commands", "?"):
        return "help", {"available": context.available() if context else []}

    if head in ("status", "state"):
        return "status", args

    if head in ("positions", "pos", "trades"):
        return "positions", args

    if head in ("forward", "drift", "journal"):
        return "forward", args

    if head in ("risk", "limits"):
        return "risk", args

    if head in ("kill", "stop", "flatten"):
        return "kill", {"reason": " ".join(parts[1:]) or "telegram command"}

    if head in ("resume", "unhalt", "continue"):
        return "resume", args

    if head in ("pause", "no_new", "hold"):
        return "pause", args

    if head in ("start", "go"):
        return "start", args

    if head == "set" and len(parts) >= 3:
        key = parts[1].lower()
        try:
            value = float(parts[2])
        except ValueError as exc:
            raise CommandError(f"'{parts[2]}' is not a number") from exc
        if key not in RISK_BOUNDS:
            raise CommandError(
                f"cannot set '{key}'. Settable: {', '.join(sorted(RISK_BOUNDS))}")
        low, high = RISK_BOUNDS[key]
        if not low <= value <= high:
            raise CommandError(f"{key} must be between {low} and {high}")
        return "set", {"key": key, "value": value}

    if head == "run" and len(parts) >= 2:
        return "run", {"command": parts[1],
                       "args": _parse_run_args(parts[2:])}

    if head in ("yes", "y", "confirm", "no", "n", "cancel"):
        args["answer"] = "no" if head in ("no", "n", "cancel") else "yes"
        return "confirm", args

    raise CommandError(
        f"unknown command '{head}'. Send /help for the list.")


def _parse_run_args(tokens: list[str]) -> dict:
    out: dict = {}
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token.startswith("--"):
            key = token[2:].replace("-", "_")
            if index + 1 < len(tokens) and not tokens[index + 1].startswith("--"):
                out[key] = tokens[index + 1]
                index += 2
            else:
                out[key] = True
                index += 1
        else:
            index += 1
    return out


class CommandChannel:
    """Bidirectional Telegram control, built on the notifier."""

    HELP = """*Commands*

/status - equity, positions, risk, uptime
/positions - open positions only
/forward - predicted vs realised performance
/risk - current limits and the firm profile
/kill - emergency stop, flatten everything
/resume - clear the kill switch
/pause - stop opening trades, keep managing
/start - resume opening trades
/set risk 0.005 - change a limit
   settable: risk, kelly, max_risk, daily, dd,
             expectancy, confidence
/run backtest - run any dashboard command
/help - this message

Limits only move inside safe bounds, and raising a risk
limit needs a confirming reply."""

    def __init__(self, notifier: Notifier, context: CommandContext,
                 allowed_chat_id: str | None = None) -> None:
        self.notifier = notifier
        self.ctx = context
        self.allowed_chat_id = str(allowed_chat_id) if allowed_chat_id else None
        self.handled = 0
        self.rejected = 0
        self.history: list[dict] = []

    # ------------------------------------------------------------------ intake

    def handle(self, chat_id: str, text: str) -> str:
        """Process one incoming message. Returns the reply text."""
        if self.allowed_chat_id and str(chat_id) != self.allowed_chat_id:
            log.warning("ignoring command from unauthorised chat id")
            self.rejected += 1
            self._record(chat_id, text, "unauthorised", "")
            return "This bot is not authorised for your chat id."

        try:
            command, args = parse(text, self.ctx)
        except CommandError as exc:
            self.rejected += 1
            self._record(chat_id, text, "error", str(exc))
            return f"⚠️ {_escape(str(exc))}"

        try:
            reply = self._dispatch(command, args)
        except Exception as exc:  # noqa: BLE001 - never crash the poll loop
            log.exception("command failed")
            self.rejected += 1
            self._record(chat_id, text, command, f"failed: {exc}")
            return f"🚨 `{command}` failed: {_escape(str(exc))}"

        self.handled += 1
        self._record(chat_id, text, command, reply[:80])
        return reply

    def _dispatch(self, command: str, args: dict) -> str:
        if command == "help":
            return self.HELP

        if command == "status":
            data = self._require("get_status")() or {}
            lines = ["*System status*", ""]
            for key in ("mode", "engine_running", "equity", "day_pnl",
                        "drawdown", "n_positions", "killed", "uptime_seconds"):
                if key in data:
                    lines.append(f"• {_escape(str(key))}: {_escape(self._fmt(key, data[key]))}")
            verdict = (self._maybe_forward() or {}).get("verdict")
            if verdict:
                lines += ["", f"Forward: {_escape(verdict)}"]
            return "\n".join(lines)

        if command == "positions":
            positions = self._maybe_positions() or []
            if not positions:
                return "No open positions."
            lines = ["*Open positions*", ""]
            for pos in positions:
                r = pos.get("r_open")
                lines.append(
                    f"• `{_escape(str(pos.get('symbol', '?')))}` "
                    f"{_escape(str(pos.get('side', '')))} "
                    f"{_escape(str(pos.get('quantity', '')))} "
                    f"@ {_escape(str(pos.get('entry_price', '')))} "
                    f"R={_escape(f'{r:.2f}' if r is not None else 'n/a')} "
                    f"P&L {_escape(str(pos.get('unrealized_pnl', '')))}")
            return "\n".join(lines)

        if command == "forward":
            data = self._maybe_forward()
            if not data or not data.get("n_trades"):
                return "No forward trades recorded yet."
            lines = [
                "*Forward tracking*", "",
                f"• trades: {data.get('n_trades')}",
                f"• win rate: {_escape(str(data.get('win_rate')))}",
                f"• expectancy: {_escape(str(data.get('expectancy_r')))}R",
                f"• total P&L: {_escape(str(data.get('total_pnl')))}",
                f"• max drawdown: {_escape(str(data.get('max_drawdown')))}",
                "", f"{_escape(data.get('verdict', ''))}",
            ]
            return "\n".join(lines)

        if command == "risk":
            data = self._maybe_risk() or {}
            if not data:
                return "Risk state unavailable."
            lines = ["*Risk limits*", ""]
            for key, value in data.items():
                lines.append(f"• {_escape(str(key))}: {_escape(self._fmt(key, value))}")
            return "\n".join(lines)

        if command == "kill":
            if self._require("kill")(args.get("reason", "telegram command")):
                self.notifier.critical("Kill switch engaged via Telegram",
                                       args.get("reason", ""), _kind="kill_switch")
                return "🚨 Kill switch engaged. Positions flattened, trading halted."
            return "⚠️ Kill switch could not be engaged - check the logs."

        if command == "resume":
            return "✅ Kill switch cleared." if self._require("resume")() \
                else "⚠️ Could not clear the kill switch."

        if command == "pause":
            return "⏸ Paused - no new trades, open positions still managed." \
                if self._require("pause")() else "⚠️ Could not pause."

        if command == "start":
            return "▶️ Resumed - taking new trades." if self._require("start")() \
                else "⚠️ Could not resume."

        if command == "set":
            return self._set(args)

        if command == "confirm":
            return self._confirm(str(args.get("answer", "yes")).lower())

        if command == "run":
            args_dict = args.get("args") or {}
            if self.ctx.run_cli is None:
                return "⚠️ Command execution is not wired up here."
            result = self.ctx.run_cli(args["command"], args_dict)
            if result.get("started") or result.get("ok"):
                return f"▶️ Started `{_escape(args['command'])}`"
            return f"⚠️ {_escape(str(result.get('error', 'could not start')))}"

        raise CommandError(f"unknown command '{command}'")

    # ------------------------------------------------------------------- set

    def _set(self, args: dict) -> str:
        key, value = args["key"], args["value"]
        if self.ctx.set_risk is None:
            return "⚠️ Changing limits is not wired up here."

        # raising a risk limit needs a confirming reply
        current = (self._maybe_risk() or {}).get(key)
        if current is not None:
            try:
                current_value = float(current)
            except (TypeError, ValueError):
                current_value = None
            if current_value is not None and key in CONFIRM_KEYS and value > current_value:
                _pending_confirmations[id(self)] = (key, current_value, value)
                return (f"⚠️ That raises `{key}` from {current_value:.4f} to "
                        f"{value:.4f}. Reply *yes* to confirm, or anything else "
                        "to cancel.")

        if self.ctx.set_risk(key, value):
            return f"✅ `{key}` set to {value}."
        return f"⚠️ Could not set `{key}` - the value may be rejected."

    def _confirm(self, answer: str = "yes") -> str:
        """Apply or discard a pending limit change.

        Any reply other than an explicit yes cancels. A typo while confirming
        must never raise a risk limit.
        """
        pending = _pending_confirmations.pop(id(self), None)
        if pending is None:
            return "Nothing is waiting for confirmation."
        if answer not in ("yes", "y", "confirm"):
            key = pending[0]
            return f"Cancelled - `{key}` is unchanged."
        key, old_value, new_value = pending
        if self.ctx.set_risk(key, new_value):
            return f"✅ `{key}` raised from {old_value:.4f} to {new_value:.4f}."
        return f"⚠️ Could not apply `{key}` = {new_value}."

    # ---------------------------------------------------------------- helpers

    def _require(self, name: str):
        handler = getattr(self.ctx, name, None)
        if handler is None:
            raise CommandError(f"'{name}' is not available on this installation")
        return handler

    def _maybe_status(self) -> dict:
        return self.ctx.get_status() if self.ctx.get_status else {}

    def _maybe_positions(self) -> list:
        return self.ctx.get_positions() if self.ctx.get_positions else []

    def _maybe_risk(self) -> dict:
        return self.ctx.get_risk() if self.ctx.get_risk else {}

    def _maybe_forward(self) -> dict:
        return self.ctx.get_forward() if self.ctx.get_forward else {}

    @staticmethod
    def _fmt(key: str, value) -> str:
        if isinstance(value, float):
            if key.endswith(("_pct", "_limit", "risk", "dd", "daily", "kelly")) \
                    and -1 < value < 1:
                return f"{value:.2%}"
            return f"{value:,.2f}"
        if isinstance(value, bool):
            return "yes" if value else "no"
        return str(value)

    def _record(self, chat_id: str, text: str, command: str, reply: str) -> None:
        self.history.append({
            "at": datetime.now(UTC).isoformat(), "chat_id": str(chat_id),
            "message": text[:200], "command": command, "reply": reply[:200],
        })
        if len(self.history) > 200:
            self.history = self.history[-200:]


class TelegramPoller:
    """Long-polls Telegram for new messages and feeds them to the channel."""

    def __init__(self, channel: CommandChannel, notifier: Notifier,
                 poll_timeout: int = 30) -> None:
        self.channel = channel
        self.notifier = notifier
        self.poll_timeout = poll_timeout
        self._offset = 0
        self._stop = False

    @property
    def _token(self) -> str:
        return self.notifier.cfg.telegram_token

    def _api(self, method: str, params: dict) -> dict:
        import json
        import urllib.parse
        import urllib.request

        url = f"https://api.telegram.org/bot{self._token}/{method}"
        if params:
            url += "?" + urllib.parse.urlencode(params)
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=self.poll_timeout + 10) as response:
            body = response.read().decode("utf-8")
            return json.loads(body) if body else {}

    def poll_once(self) -> int:
        """Fetch and handle one batch. Returns how many messages were handled."""
        data = self._api("getUpdates", {
            "offset": self._offset, "timeout": self.poll_timeout,
            "allowed_updates": '["message"]',
        })
        updates = data.get("result") or []
        handled = 0
        for update in updates:
            self._offset = int(update.get("update_id", 0)) + 1
            message = update.get("message") or {}
            chat = message.get("chat") or {}
            text = (message.get("text") or "").strip()
            if not text:
                continue
            reply = self.channel.handle(str(chat.get("id", "")), text)
            self._reply(str(chat.get("id", "")), reply)
            handled += 1
        return handled

    def _reply(self, chat_id: str, text: str) -> None:
        try:
            self._api("sendMessage", {"chat_id": chat_id, "text": text,
                                      "parse_mode": "Markdown"})
        except Exception as exc:  # noqa: BLE001
            log.warning("could not send reply: %s", exc)

    def run_forever(self) -> None:
        self._stop = False
        log.info("telegram command channel polling")
        while not self._stop:
            try:
                self.poll_once()
            except Exception as exc:  # noqa: BLE001 - keep polling
                log.warning("poll failed: %s", exc)
                time.sleep(5)

    def stop(self) -> None:
        self._stop = True


__all__ = [
    "CommandChannel", "CommandContext", "CommandError", "TelegramPoller",
    "parse", "RISK_BOUNDS",
]
