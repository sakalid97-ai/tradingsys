"""Inline YES / NO buttons for Telegram approvals.

Typing "yes" into a chat is a poor interface for a decision that moves money:
it is ambiguous, easy to fat-finger, and it tells you nothing about which prompt
you just answered. Inline keyboards fix all three -- the callback data carries
the request id, so pressing YES on the wrong prompt is impossible, and the
button label states what you are approving.
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.parse
import urllib.request

log = logging.getLogger(__name__)

TELEGRAM_API = "https://api.telegram.org"


class ButtonChannel:
    """Sends approval prompts with inline buttons and reads the presses back."""

    def __init__(self, token: str, chat_id: str, timeout: float = 15.0) -> None:
        self.token = token
        self.chat_id = str(chat_id)
        self.timeout = timeout
        self.sent = 0
        self.failed = 0

    # ------------------------------------------------------------------ asking

    def ask(self, request_id: str, text: str, yes_label: str = "YES - execute",
            no_label: str = "NO - reject") -> dict | None:
        """Send a prompt with two buttons bound to this request id.

        The callback data is ``approve:<id>`` / ``reject:<id>``, so the venue
        tells us exactly which request a press belongs to.
        """
        keyboard = {"inline_keyboard": [[
            {"text": yes_label, "callback_data": f"approve:{request_id}"},
            {"text": no_label, "callback_data": f"reject:{request_id}"},
        ]]}
        result = self._api("sendMessage", {
            "chat_id": self.chat_id,
            "text": text,
            "parse_mode": "Markdown",
            "reply_markup": keyboard,
        })
        if result is not None:
            self.sent += 1
        else:
            self.failed += 1
        return result

    def answer_callback(self, callback_query_id: str, text: str = "") -> None:
        """Acknowledge a press so the client stops its spinner."""
        payload: dict = {"callback_query_id": callback_query_id}
        if text:
            payload["text"] = text
        try:
            self._api("answerCallbackQuery", payload)
        except (OSError, ValueError, urllib.error.URLError) as exc:
            log.debug("could not acknowledge the press: %s", exc)

    def edit_answered(self, chat_id: str, message_id: int, text: str) -> None:
        """Replace a prompt once answered, so its buttons disappear."""
        try:
            self._api("editMessageText", {
                "chat_id": chat_id,
                "message_id": message_id,
                "text": text,
                "parse_mode": "Markdown",
                "reply_markup": {"inline_keyboard": []},
            })
        except (OSError, ValueError, urllib.error.URLError) as exc:
            log.debug("could not update the answered prompt: %s", exc)

    # ------------------------------------------------------------------ polling

    def get_updates(self, offset: int = 0, timeout: int = 20) -> list[dict]:
        params = {"offset": offset, "timeout": timeout,
                  "allowed_updates": '["message","callback_query"]'}
        query = "&".join(f"{k}={urllib.parse.quote(str(v))}"
                         for k, v in params.items())
        url = f"{TELEGRAM_API}/bot{self.token}/getUpdates?{query}"
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=timeout + 10) as response:
            body = json.load(response)
        if not isinstance(body, dict) or not body.get("ok", True):
            return []
        return body.get("result") or []

    # ----------------------------------------------------------------- plumbing

    def _api(self, method: str, payload: dict) -> dict | None:
        data = json.dumps(payload).encode()
        req = urllib.request.Request(
            f"{TELEGRAM_API}/bot{self.token}/{method}", data=data, method="POST",
            headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as response:
                body = json.load(response)
            if not isinstance(body, dict) or not body.get("ok", True):
                log.warning("%s rejected: %s", method,
                            body.get("description") if isinstance(body, dict) else body)
                return None
            return body.get("result")
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError,
                OSError, ValueError) as exc:
            log.warning("%s failed: %s", method, exc)
            return None

    def stats(self) -> dict:
        return {"prompts_sent": self.sent, "failed": self.failed,
                "configured": bool(self.token and self.chat_id)}


def parse_callback(data: str) -> tuple[str, str] | None:
    """Split ``approve:ENT-0001`` into ``("approve", "ENT-0001")``.

    Returns None for anything that is not an approval callback.
    """
    if not data or ":" not in data:
        return None
    action, _, request_id = data.partition(":")
    action = action.strip().lower()
    request_id = request_id.strip()
    if action not in ("approve", "reject") or not request_id:
        return None
    if ":" in request_id:
        # malformed -- reject rather than silently targeting a bogus id
        return None
    return action, request_id


__all__ = ["ButtonChannel", "parse_callback"]
