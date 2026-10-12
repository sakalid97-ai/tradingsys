"""Verify the Telegram alert + command channel with the real credentials."""

from __future__ import annotations

import io
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

TOKEN = "8688505731:AAF-kYLwzsXEDmkMKQTrL7SmD2w-rHQRFlE"
CHAT_ID = "573029836"
BASE = f"https://api.telegram.org/bot{TOKEN}"


def api(method: str, params: dict | None = None, timeout: float = 20.0):
    url = f"{BASE}/{method}"
    data = None
    if params is not None:
        data = json.dumps(params).encode()
    req = urllib.request.Request(url, data=data, method="POST" if data else "GET",
                                 headers={"Content-Type": "application/json"} if data else {})
    with urllib.request.urlopen(req, timeout=timeout) as handle:
        return json.load(handle)


print("=== 1. getMe ===")
me = api("getMe")
print(f"  @{me['result']['username']}")

print("\n=== 2. send a plain alert ===")
response = api("sendMessage", {
    "chat_id": CHAT_ID,
    "text": "Autonomous Trading System\n\nThe command channel is now live. Send /help to "
            "your bot to see what you can do.",
    "parse_mode": "Markdown",
})
print(f"  sent: message_id={response['result']['message_id']}")

print("\n=== 3. drive the command channel in-process ===")
os.environ["TELEGRAM_BOT_TOKEN"] = TOKEN
os.environ["TELEGRAM_CHAT_ID"] = CHAT_ID

from ts.commands import CommandChannel, CommandContext
from ts.notify import Notifier, NotifyConfig
from ts.settings import SettingsStore
from ts.state import StateStore
from ts.forward import ForwardJournal

store = StateStore(ROOT / "state")
settings_store = SettingsStore(store.root)
notifier = Notifier(NotifyConfig())
print(f"  telegram configured: {notifier.cfg.telegram_enabled}")

context = CommandContext(
    get_status=lambda: {**store.read_snapshot(), "engine_running": False},
    get_positions=lambda: store.read_snapshot().get("positions", []),
    get_risk=lambda: {
        "risk": settings_store.load().risk.risk_per_trade,
        "daily": settings_store.load().risk.daily_loss_limit,
        "dd": settings_store.load().risk.max_drawdown_halt,
        "kelly_fraction": settings_store.load().strategy.kelly_fraction,
        "max_risk": settings_store.load().risk.max_total_risk,
    },
    get_forward=lambda: ForwardJournal(store.root).summary(),
    set_risk=lambda k, v: bool(settings_store.update("risk", {k: v})),
    kill=lambda reason: store.set_killed(True, reason),
    resume=lambda: store.set_killed(False),
    pause=lambda: True,
    start=lambda: True,
)
channel = CommandChannel(notifier, context, allowed_chat_id=CHAT_ID)

print("\n=== 4. exercise each command ===")
for command in ("/help", "/status", "/positions", "/risk", "/forward"):
    reply = channel.handle(CHAT_ID, command)
    print(f"  {command:<12} -> {reply[:90]!r}")

print("\n=== 5. push one reply back to the chat ===")
api("sendMessage", {"chat_id": CHAT_ID,
                    "text": "*Command channel verified*\n\n" + channel.handle(
                        CHAT_ID, "/status"),
                    "parse_mode": "Markdown"})
print("  status reply delivered")

print("\n=== 6. unauthorised sender is ignored ===")
intruder = CommandChannel(notifier, context, allowed_chat_id=CHAT_ID)
reply = intruder.handle("999999999", "/kill")
print(f"  from another chat: {reply[:60]!r}")
print("  nothing was executed (kill switch set:", (ROOT / "state" / "KILL").exists(), ")")

print("\nDONE - check your Telegram for two messages.")
