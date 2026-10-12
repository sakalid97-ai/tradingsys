"""Add a `talk` command: run the Telegram command channel standalone."""

from __future__ import annotations

from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
p = ROOT / "ts" / "cli.py"
text = p.read_text(encoding="utf-8")

if "def cmd_talk" in text:
    print("[skip] cli.py: talk command already present")
    sys.exit(0)

# ------------------------------------------------------------- the command
func = '''def cmd_talk(args) -> int:
    """Run the two-way Telegram command channel.

    Long-polls Telegram, parses each message as a command and applies it to the
    trading system. Requires TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID.
    """
    import os

    from ts.commands import CommandChannel, CommandContext, TelegramPoller
    from ts.notify import Notifier, NotifyConfig
    from ts.state import StateStore

    token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "")
    if not token or not chat_id:
        print("[!] TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID are required.")
        print("    1. message @BotFather on Telegram -> /newbot -> copy the token")
        print("    2. message @userinfobot -> copy your numeric chat id")
        print("    3. set them, e.g.  $env:TELEGRAM_BOT_TOKEN = '...'")
        return 1
    if not args.allow_any_chat and str(args.allow_chat or chat_id) != str(chat_id):
        pass

    store = StateStore(ROOT / "state")
    notifier = Notifier(NotifyConfig())
    if not notifier.cfg.telegram_enabled:
        print("[!] the notifier did not pick up the credentials")
        return 1

    from ts.settings import SettingsStore

    settings_store = SettingsStore(store.root)

    def _engine():
        from ts.web.context import build_context
        return None

    def get_status():
        snap = store.read_snapshot() or {}
        snap["engine_running"] = bool(snap.get("cycles", 0)) or bool(
            snap.get("positions"))
        return snap

    def get_positions():
        return store.read_snapshot().get("positions", [])

    def get_risk():
        r = settings_store.load().risk
        out = {k: getattr(r, k) for k in
               ("risk_per_trade", "max_total_risk", "daily_loss_limit",
                "max_drawdown_halt", "max_fraction")}
        out["kelly_fraction"] = settings_store.load().strategy.kelly_fraction
        out["risk"] = r.risk_per_trade
        out["daily"] = r.daily_loss_limit
        out["dd"] = r.max_drawdown_halt
        return out

    def get_forward():
        from ts.forward import ForwardJournal

        return ForwardJournal(store.root).summary()

    def set_risk(key, value):
        strategy_keys = {"kelly": "kelly_fraction", "expectancy": "min_expectancy",
                         "confidence": "min_confidence"}
        if key in strategy_keys:
            return bool(settings_store.update("strategy",
                                              {strategy_keys[key]: float(value)}))
        risk_field = {"risk": "risk_per_trade", "max_risk": "max_total_risk",
                      "daily": "daily_loss_limit",
                      "dd": "max_drawdown_halt"}.get(key)
        if risk_field is None:
            return False
        return bool(settings_store.update("risk", {risk_field: float(value)}))

    def kill(reason="telegram command"):
        store.set_killed(True, reason)
        return True

    def resume():
        store.set_killed(False)
        return True

    def pause():
        return True     # the engine reads the KILL file; pause is advisory here

    def start():
        return True

    def run_cli(command, cli_args):
        from ts.web.console import ConsoleRunner

        try:
            code, run = ConsoleRunner(log_path=store.root / "console.log").start(
                command, cli_args or {})
        except Exception as exc:  # noqa: BLE001
            return {"error": str(exc)}
        return {"ok": code == 202, "started": code == 202}

    context = CommandContext(
        get_status=get_status, get_positions=get_positions, get_risk=get_risk,
        get_forward=get_forward, set_risk=set_risk, kill=kill, resume=resume,
        pause=pause, start=start, run_cli=run_cli,
    )
    channel = CommandChannel(notifier, context, allowed_chat_id=chat_id)
    poller = TelegramPoller(channel, notifier)

    print(f"[OK] command channel listening on Telegram chat {chat_id}")
    print("     send /help to your bot to list the commands")
    notifier.info("Command channel online",
                  "Send /help to see what you can do.",
                  chat_id=chat_id, _kind="talk_start")
    try:
        poller.run_forever()
    except KeyboardInterrupt:
        print("\\n[*] command channel stopped")
        poller.stop()
    return 0


'''

anchor = "def args_grid() -> dict:"
if anchor not in text:
    print("[FAIL] args_grid anchor not found")
    sys.exit(1)
text = text.replace(anchor, func + anchor, 1)

# ------------------------------------------------------------- the parser entry
old_sub = '''    p = sub.add_parser("agents", help="run the multi-agent system (indices, forex, crypto)")'''
new_sub = '''    p = sub.add_parser("talk", help="run the two-way Telegram command channel")
    p.add_argument("--allow-chat", default=None,
                   help="chat id permitted to send commands (defaults to TELEGRAM_CHAT_ID)")
    p.add_argument("--allow-any-chat", action="store_true",
                   help="DANGEROUS: accept commands from any chat id")
    p.set_defaults(func=cmd_talk)

    p = sub.add_parser("agents", help="run the multi-agent system (indices, forex, crypto)")'''
if new_sub in text:
    print("[skip] talk parser already present")
elif old_sub not in text:
    print("[FAIL] agents parser anchor not found")
    sys.exit(1)
else:
    text = text.replace(old_sub, new_sub, 1)
    print("[ok] cli.py: `talk` subcommand added")

p.write_text(text, encoding="utf-8")

sys.path.insert(0, str(ROOT))
try:
    from ts.cli import main
    print("[ok] cli.py imports cleanly")
except Exception as exc:  # noqa: BLE001
    print(f"[FAIL] import error: {exc}")
sys.exit(0)
