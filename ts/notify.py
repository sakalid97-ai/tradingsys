"""Notifications: Telegram (and generic webhooks).

Every alert the trading system raises can be sent off-box. Two reasons that
matters:

* **The engine may run on a VPS you are not watching.** A kill switch or a
  daily-loss breach needs to reach you wherever you are, not sit in a console
  you are not reading.
* **A message you do not see is not an alert.** A log line in a file is
  evidence; a Telegram message is an interruption.

Delivery is best-effort by design: a notification failure must never stop
trading, so every send is wrapped and a failure is counted rather than raised.

Credentials come from the environment (``TELEGRAM_BOT_TOKEN``,
``TELEGRAM_CHAT_ID``) or from the constructor. Nothing is written to disk by
this module, and the bot token is never logged.
"""

from __future__ import annotations

import json
import logging
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum

log = logging.getLogger(__name__)

TELEGRAM_API = "https://api.telegram.org"
# Telegram rejects anything over 4096 characters; keep well clear of it
MAX_MESSAGE = 3500


class Severity(str, Enum):
    INFO = "info"
    TRADE = "trade"
    WARN = "warning"
    CRITICAL = "critical"


@dataclass
class NotifyConfig:
    """Where alerts go."""

    telegram_token: str = ""
    telegram_chat_id: str = ""
    webhook_url: str = ""
    # Never send more than this many messages per minute, so a stuck loop
    # cannot turn into a rate-limited failure -- or a phone buzzing all night.
    rate_limit_per_minute: int = 20
    # Minimum seconds between two messages of the same kind
    cooldown_seconds: float = 5.0
    # Events worth interrupting you for. Everything else is still journalled.
    min_severity: Severity = Severity.INFO

    def __post_init__(self) -> None:
        self.telegram_token = self.telegram_token or os.getenv("TELEGRAM_BOT_TOKEN", "")
        self.telegram_chat_id = str(
            self.telegram_chat_id or os.getenv("TELEGRAM_CHAT_ID", "") or "")

    @property
    def telegram_enabled(self) -> bool:
        return bool(self.telegram_token and self.telegram_chat_id)

    @property
    def webhook_enabled(self) -> bool:
        return bool(self.webhook_url)

    @property
    def any_enabled(self) -> bool:
        return self.telegram_enabled or self.webhook_enabled

    def redacted(self) -> dict:
        token = self.telegram_token
        masked = (token[:6] + "…" + token[-4:]) if len(token) > 12 else ("set" if token else "")
        return {
            "telegram_configured": self.telegram_enabled,
            "telegram_token_masked": masked,
            "telegram_chat_id": self.telegram_chat_id or None,
            "webhook_configured": self.webhook_enabled,
            "min_severity": self.min_severity.value,
            "rate_limit_per_minute": self.rate_limit_per_minute,
        }


def coerce_severity(value) -> Severity:
    """Accept a Severity, or its string name, and normalise to the enum."""
    if isinstance(value, Severity):
        return value
    try:
        return Severity(str(value).lower())
    except ValueError:
        return Severity.INFO


SEVERITY_ORDER = {
    Severity.INFO: 0,
    Severity.TRADE: 1,
    Severity.WARN: 2,
    Severity.CRITICAL: 3,
}


def format_alert(title: str, body: str, severity=Severity.INFO,
                 fields: dict | None = None) -> str:
    """Build a plain-text message. Markdown-safe: no unsupported entities."""
    severity = coerce_severity(severity)
    icon = {
        Severity.INFO: "ℹ️",
        Severity.TRADE: "📈",
        Severity.WARN: "⚠️",
        Severity.CRITICAL: "🚨",
    }[severity]
    lines = [f"{icon} *{_escape(title)}*", ""]
    if body:
        lines += [_escape(body), ""]
    for key, value in (fields or {}).items():
        lines.append(f"• {_escape(str(key))}: {_escape(str(value))}")
    lines += ["", f"`{datetime.now(UTC).strftime('%Y-%m-%d %H:%M:%S')} UTC`"]
    text = "\n".join(lines)
    return text[:MAX_MESSAGE]


def _escape(text: str) -> str:
    """Escape the characters Telegram's Markdown parser treats specially."""
    for char in ("_", "*", "`", "["):
        text = text.replace(char, "\\" + char)
    return text


class Notifier:
    """Sends alerts to Telegram and/or a webhook. Never raises."""

    def __init__(self, config: NotifyConfig | None = None) -> None:
        self.cfg = config or NotifyConfig()
        self._sent_times: list[float] = []
        self._last_by_kind: dict[str, float] = {}
        self.sent_count = 0
        self.failed_count = 0
        self.suppressed_count = 0
        self.history: list[dict] = []

    # ------------------------------------------------------------------ gating

    def _allowed(self, kind: str, severity: Severity) -> bool:
        if SEVERITY_ORDER[severity] < SEVERITY_ORDER[self.cfg.min_severity]:
            self.suppressed_count += 1
            return False

        now = time.monotonic()
        window = [t for t in self._sent_times if now - t < 60.0]
        if len(window) >= self.cfg.rate_limit_per_minute:
            self.suppressed_count += 1
            log.debug("notification rate limit reached; suppressing %s", kind)
            return False

        last = self._last_by_kind.get(kind)
        if last is not None and (now - last) < self.cfg.cooldown_seconds:
            self.suppressed_count += 1
            return False
        return True

    # ----------------------------------------------------------------- sending

    def send(self, title: str, body: str = "", severity=Severity.INFO,
             fields: dict | None = None, kind: str = "alert") -> bool:
        """Deliver one alert. Returns True if at least one channel accepted it.

        ``severity`` accepts either a :class:`Severity` or its string name, so
        callers do not have to import the enum just to raise an alert.
        """
        if not self.cfg.any_enabled:
            return False
        severity = coerce_severity(severity)
        if not self._allowed(kind, severity):
            return False

        message = format_alert(title, body, severity, fields)
        delivered = False

        if self.cfg.telegram_enabled:
            if self._send_telegram(message):
                delivered = True

        if self.cfg.webhook_enabled:
            if self._send_webhook({
                "title": title, "body": body, "severity": severity.value,
                "fields": fields or {}, "text": message,
                "at": datetime.now(UTC).isoformat(),
            }):
                delivered = True

        now = time.monotonic()
        self._sent_times.append(now)
        self._last_by_kind[kind] = now
        if delivered:
            self.sent_count += 1
            self.history.append({"kind": kind, "severity": severity.value,
                                 "title": title, "at": datetime.now(UTC).isoformat()})
        else:
            self.failed_count += 1
        return delivered

    # convenience wrappers so call sites read clearly
    def info(self, title, body="", **fields):
        return self.send(title, body, Severity.INFO, fields, kind=fields.get("_kind", "info"))

    def trade(self, title, body="", **fields):
        return self.send(title, body, Severity.TRADE, fields, kind="trade")

    def warning(self, title, body="", **fields):
        return self.send(title, body, Severity.WARN, fields, kind="warning")

    def critical(self, title, body="", **fields):
        return self.send(title, body, Severity.CRITICAL, fields, kind="critical")

    # ----------------------------------------------------------------- channels

    def _send_telegram(self, message: str) -> bool:
        url = f"{TELEGRAM_API}/bot{self.cfg.telegram_token}/sendMessage"
        payload = {
            "chat_id": self.cfg.telegram_chat_id,
            "text": message,
            "parse_mode": "Markdown",
            "disable_web_page_preview": True,
        }
        try:
            self._post(url, payload)
            return True
        except (OSError, ValueError, urllib.error.URLError) as exc:
            # never log the token: the URL is redacted before it reaches the log
            log.warning("telegram send failed: %s", _redact(str(exc), self.cfg.telegram_token))
            return False

    def _send_webhook(self, payload: dict) -> bool:
        try:
            self._post(self.cfg.webhook_url, payload)
            return True
        except (OSError, ValueError, urllib.error.URLError) as exc:
            log.warning("webhook send failed: %s", exc)
            return False

    @staticmethod
    def _post(url: str, payload: dict, timeout: float = 10.0) -> dict:
        data = json.dumps(payload).encode()
        req = urllib.request.Request(url, data=data, method="POST",
                                     headers={"Content-Type": "application/json",
                                              "User-Agent": "tradingsys/1.0"})
        with urllib.request.urlopen(req, timeout=timeout) as response:
            body = response.read().decode("utf-8")
            parsed = json.loads(body) if body else {}
            if not parsed.get("ok", True):
                raise ValueError(f"endpoint rejected the message: {parsed.get('description')}")
            return parsed

    # ---------------------------------------------------------------- reporting

    def stats(self) -> dict:
        return {
            **self.cfg.redacted(),
            "sent": self.sent_count,
            "failed": self.failed_count,
            "suppressed": self.suppressed_count,
            "recent": list(reversed(self.history[-10:])),
        }

    def test(self) -> tuple[bool, str]:
        """Send a test message so the user can confirm credentials work."""
        if not self.cfg.telegram_enabled:
            return False, ("Telegram is not configured. Set TELEGRAM_BOT_TOKEN and "
                           "TELEGRAM_CHAT_ID, or pass them to NotifyConfig.")
        delivered = self.send(
            "Trading system connected",
            "This is a test message. If you can read it, alerts will reach you.",
            Severity.INFO, {"source": "manual test"}, kind="test",
        )
        if delivered:
            return True, "test message sent"
        return False, "delivery failed - check the bot token and chat id"


def _redact(text: str, secret: str) -> str:
    if not secret:
        return text
    return text.replace(secret, "***")


__all__ = ["Notifier", "NotifyConfig", "Severity", "format_alert"]
