"""Prove the approval round-trip: ask over Telegram, then apply the reply."""

from __future__ import annotations

import io
import json
import os
import sys
import time
import urllib.request
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

TOKEN = "8688505731:AAF-kYLwzsXEDmkMKQTrL7SmD2w-rHQRFlE"
CHAT_ID = "573029836"
BASE = f"https://api.telegram.org/bot{TOKEN}"

os.environ["TELEGRAM_BOT_TOKEN"] = TOKEN
os.environ["TELEGRAM_CHAT_ID"] = CHAT_ID

from ts.approvals import ApprovalGate, Decision, format_request  # noqa: E402
from ts.notify import Notifier, NotifyConfig  # noqa: E402


def send(text: str):
    data = json.dumps({"chat_id": CHAT_ID, "text": text,
                       "parse_mode": "Markdown"}).encode()
    req = urllib.request.Request(f"{BASE}/sendMessage", data=data,
                                 headers={"Content-Type": "application/json"},
                                 method="POST")
    with urllib.request.urlopen(req, timeout=20) as handle:
        return json.load(handle)["result"]["message_id"]


notifier = Notifier(NotifyConfig())
print(f"telegram configured: {notifier.cfg.telegram_enabled}")

gate = ApprovalGate(notifier=notifier, timeout_seconds=120.0, max_pending=5)

print("\n=== 1. ask for approval (as the engine would before an entry) ===")
request = gate.request(
    action="enter",
    title="APPROVE ENTRY: long BTCUSD",
    body="20-day breakout, regime risk-on, expectancy +1.77R",
    fields={
        "symbol": "BTCUSD",
        "side": "long",
        "quantity": "4.03",
        "price": "83,317.44",
        "stop": "82,255.37",
        "target": "83,193.31",
        "risk": "$250.00 (1.00% of equity)",
        "expectancy": "+1.77R",
    },
    severity="trade",
)
print(f"  queued {request.request_id}, expires in {int(request.remaining())}s")
print("  message sent -- check Telegram")

print("\n=== 2. poll for your reply ===")
print("  reply YES or NO to the bot (or leave it and it will expire)")
offset = 0
seen: set[str] = set()
deadline = time.time() + 120
while time.time() < deadline and request.pending:
    try:
        url = f"{BASE}/getUpdates?offset={offset}&timeout=20"
        with urllib.request.urlopen(url, timeout=30) as handle:
            payload = json.load(handle)
    except Exception as exc:  # noqa: BLE001
        print(f"  poll error: {exc}")
        time.sleep(2)
        continue

    for update in payload.get("result", []):
        offset = int(update.get("update_id", 0)) + 1
        message = update.get("message") or {}
        chat = message.get("chat") or {}
        text = (message.get("text") or "").strip()
        if not text or update.get("update_id") in seen:
            continue
        seen.add(update.get("update_id"))
        if str(chat.get("id")) != CHAT_ID:
            print(f"  [ignored] {text!r} from {chat.get('id')}")
            continue
        ok, reply = gate.answer(text, chat_id=str(chat.get("id")))
        print(f"  you said: {text!r} -> {reply}")

if request.decision is Decision.APPROVED:
    print(f"\n=== 3. RESULT: APPROVED by {request.decided_by} "
          f"after {request.wait_seconds}s ===")
    print(f"  executing exactly as proposed: {request.fields}")
elif request.decision is Decision.REJECTED:
    print(f"\n=== 3. RESULT: REJECTED after {request.wait_seconds}s ===")
    print("  no trade placed -- your call")
else:
    gate.expire()
    print("\n=== 3. RESULT: EXPIRED unanswered ===")
    print("  no trade placed -- silence is never consent")

print("\n=== 4. what would be journalled ===")
print(json.dumps(request.to_dict(), indent=2)[:600])
send(f"*Approval gate verified*\n\n`{request.request_id}` -> "
     f"{request.decision.value if request.decision else 'pending'}\n"
     f"waited {request.wait_seconds}s")
