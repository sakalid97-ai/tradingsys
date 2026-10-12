"""Repair server.py (orphaned _run_backtest body) and import dumps in cli.py."""

from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]

server = ROOT / "ts" / "web" / "server.py"
text = server.read_text(encoding="utf-8")

orphan = '''        return {"agents": {}, "note": "no agent system has run yet"}
        if self.ctx.backtester_factory is None or self.ctx.data_provider is None:'''
fixed = '''        return {"agents": {}, "note": "no agent system has run yet"}

    def _run_backtest(self, body: dict) -> tuple[int, dict]:
        """Start a backtest on a worker thread so the UI never blocks."""
        if self.ctx.backtester_factory is None or self.ctx.data_provider is None:'''

if orphan not in text:
    print("[skip] server.py: orphan already repaired or not found")
else:
    text = text.replace(orphan, fixed, 1)
    server.write_text(text, encoding="utf-8")
    print("[ok]   server.py: restored _run_backtest as its own method")

cli = ROOT / "ts" / "cli.py"
text = cli.read_text(encoding="utf-8")
if "from ts.state import dumps" in text:
    print("[skip] cli.py: dumps already imported")
elif "dumps(" in text:
    text = text.replace(
        "from ts.strategy import StrategyParams\nfrom ts.walkforward import WalkForward, WalkForwardConfig",
        "from ts.state import StateStore, dumps\nfrom ts.strategy import StrategyParams\nfrom ts.walkforward import WalkForward, WalkForwardConfig",
        1,
    )
    # remove the now-duplicated StateStore import on the following line
    text = text.replace(
        "from ts.state import StateStore\nfrom ts.state import StateStore, dumps",
        "from ts.state import StateStore, dumps",
        1,
    )
    cli.write_text(text, encoding="utf-8")
    print("[ok]   cli.py: dumps imported")
else:
    print("[skip] cli.py: no dumps usage")

print("\ncompile check:")
sys.exit(0)
