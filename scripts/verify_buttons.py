"""Prove the button approval flow: prompt with YES/NO buttons, read the press."""

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

os.environ["TELEGRAM_BOT_TOKEN"] = TOKEN
os.environ["TELEGRAM_CHAT_ID"] = CHAT_ID

from ts.agents.policy import AutoApprovePolicy  # noqa: E402
from ts.approvals import ApprovalGate, Decision  # noqa: E402
from ts.telegram_buttons import ButtonChannel, parse_callback  # noqa: E402

buttons = ButtonChannel(TOKEN, CHAT_ID)
gate = ApprovalGate(notifier=None, timeout_seconds=120.0, max_pending=5)
policy = AutoApprovePolicy(min_expectancy=0.35, min_confidence=0.75)

print("=== 1. the policy decides who gets asked ===")


def decide(symbol, expectancy, confidence, risk=0.01, side="long"):
    needs, tier, reason = policy.evaluate(expectancy, confidence, side=side,
                                          symbol=symbol, risk_pct=risk)
    label = {True: "ASK", False: tier.upper()}[needs]
    print(f"  {symbol:<8} {side:<6} exp={expectancy:+.2f}R conf={confidence:.0%} "
          f"-> {label:<12} {reason}")
    return needs


strong = decide("ETHUSD", 0.40, 0.90)
medium = decide("SOLUSD", 1.85, 0.97)
weak_exp = decide("AUDUSD", 0.08, 0.92)
weak_conf = decide("USDCAD", 0.85, 0.58)
big_risk = decide("BTCUSD", 0.95, 0.97, risk=0.05)
shorting = decide("GBPUSD", 0.90, 0.95, side="short")

print("\n=== 2. the marginal trade gets a button prompt ===")
request = gate.request(
    action="enter",
    title="MARGINAL SETUP - your call",
    body="Expectancy and confidence did not clear the auto-approval bar.",
    fields={
        "symbol": "AUDUSD",
        "side": "long",
        "expectancy": "+0.08R  (needs 0.35R)",
        "confidence": "92%  (needs 75%)",
        "reason": weak_exp,
        "risk": "$100.00 (1.00% of equity)",
    },
)

from ts.approvals import format_request  # noqa: E402

text = format_request(request)
sent = buttons.ask(request.request_id, text,
                   yes_label="YES - execute", no_label="NO - reject")
print(f"  sent {request.request_id} with YES/NO buttons "
      f"(message_id={sent.get('message_id') if sent else 'failed'})")
print("  -> press a button in Telegram")

print("\n=== 3. waiting for a button press ===")
offset = 0
seen: set[int] = set()
deadline = time.time() + 120
answered = False
while time.time() < deadline and request.pending:
    try:
        with urllib.request.urlopen(
                f"https://api.telegram.org/bot{TOKEN}/getUpdates"
                f"?offset={offset}&timeout=20"
                f"&allowed_updates=%5B%22message%22%2C%22callback_query%22%5D",
                timeout=30) as handle:
            payload = json.load(handle)
    except Exception as exc:  # noqa: BLE001
        print(f"  poll error: {exc}")
        time.sleep(2)
        continue

    for update in payload.get("result", []):
        offset = int(update.get("update_id", 0)) + 1
        if update.get("update_id") in seen:
            continue
        seen.add(update.get("update_id"))

        callback = update.get("callback_query")
        if callback:
            data = (callback.get("data") or "")
            parsed = parse_callback(data)
            chat = (callback.get("message") or {}).get("chat") or {}
            print(f"  [button] {data!r} from chat {chat.get('id')}")
            if str(chat.get("id")) != CHAT_ID:
                print("    ignored: wrong chat")
                continue
            if not parsed:
                print("    ignored: not an approval callback")
                continue
            action, request_id = parsed
            if request_id != request.request_id:
                print(f"    ignored: targets {request_id}, not {request.request_id}")
                continue
            buttons.answer_callback(str(callback.get("id")),
                                    "Recording your decision")
            reply = {"approve": "yes", "reject": "no"}[action]
            ok, message = gate.answer(reply, chat_id=str(chat.get("id")))
            print(f"    {message}")
            message_info = callback.get("message") or {}
            buttons.edit_answered(
                str(chat.get("id")), int(message_info.get("message_id", 0)),
                f"*{request.title}*\n\nYou chose "
                f"{'EXECUTE' if action == 'approve' else 'REJECT'} "
                f"(`{request_id}`)")
            answered = True
            break

        message = update.get("message") or {}
        text_msg = (message.get("text") or "").strip()
        if text_msg:
            print(f"  [text] {text_msg!r} -> not a button press, ignored")

    if answered:
        break

print("\n=== 4. result ===")
if request.decision is Decision.APPROVED:
    print(f"  APPROVED by {request.decided_by} in {request.wait_seconds}s")
    print("  the strong trades above already executed; this one executes now:")
    print(f"    {request.fields['side']} {request.fields['symbol']} @ "
          f"risk {request.fields['risk']}")
elif request.decision is Decision.REJECTED:
    print(f"  REJECTED in {request.wait_seconds}s -> no trade")
else:
    gate.expire()
    print("  EXPIRED unanswered -> no trade (silence is never consent)")

print("\n=== 5. journalled ===")
print(json.dumps({k: request.to_dict()[k] for k in
                  ("request_id", "decision", "decided_by", "wait_seconds",
                   "fields")}, indent=2))
print(f"\n  button stats: {buttons.stats()}")
