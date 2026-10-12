import io, sys
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

"""Verify the Telegram bot and recover the chat id from getUpdates."""

import json
import urllib.error
import urllib.request

TOKEN = "8688505731:AAF-kYLwzsXEDmkMKQTrL7SmD2w-rHQRFlE"
BASE = f"https://api.telegram.org/bot{TOKEN}"


def call(method: str, url: str):
    try:
        with urllib.request.urlopen(url, timeout=30) as handle:
            return json.load(handle)
    except urllib.error.HTTPError as exc:
        return {"ok": False, "error": f"HTTP {exc.code}"}
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        return {"ok": False, "error": str(exc)}


print("=== getMe ===")
me = call("getMe", f"{BASE}/getMe")
if me.get("ok"):
    result = me["result"]
    print(f"  ok: @{result.get('username')}  ({result.get('first_name')})")
    print(f"  can join groups: {result.get('can_join_groups')}")
else:
    print(f"  FAILED: {me.get('error')}")

print("\n=== getUpdates ===")
updates = call("getUpdates", f"{BASE}/getUpdates?timeout=0&limit=10")
if not updates.get("ok"):
    print(f"  FAILED: {updates.get('error')}")
else:
    results = updates.get("result") or []
    if not results:
        print("  no messages yet - cannot recover the chat id automatically")
        print("  ACTION: open Telegram -> search @trdns_Bot -> press Start -> send /status")
    else:
        chats = {}
        for update in results:
            message = update.get("message") or {}
            chat = message.get("chat") or {}
            chats[chat.get("id")] = chat
        for chat_id, chat in chats.items():
            print(f"  chat_id={chat_id}  name={chat.get('first_name')}  "
                  f"username=@{chat.get('username')}")
        print("\n  set it with:")
        print(f"    $env:TELEGRAM_CHAT_ID = \"{list(chats)[0]}\"")
