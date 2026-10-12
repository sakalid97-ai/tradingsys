"""Wire conditional approval + button prompts into the dashboard engine builder.

This is what makes it real: pressing START ENGINE now launches an engine that
decides for itself when it is unsure, and only interrupts you then.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def patch(path: Path, subs, label: str) -> bool:
    text = path.read_text(encoding="utf-8")
    applied = skipped = failed = 0
    for old, new in subs:
        if new and new in text:
            skipped += 1
            continue
        if old not in text:
            print(f"    [FAIL] {path.name}: {old[:58]!r}")
            failed += 1
            continue
        text = text.replace(old, new, 1)
        applied += 1
    if not failed:
        path.write_text(text, encoding="utf-8")
    print(f"[{'ok' if not failed else 'FAIL'}] {label}: "
          f"{applied} applied, {skipped} present, {failed} failed")
    return not failed


ok = True

# ------------------------------------------------------------- 1. the gate
ok &= patch(ROOT / "ts" / "web" / "context.py", [
    (
        "    context = AppContext(\n        state=store,",
        "    # --- human-in-the-loop: the engine asks only when it is unsure ---\n"
        "    from ts.agents.policy import AutoApprovePolicy\n"
        "    from ts.approvals import ApprovalGate\n"
        "    from ts.notify import Notifier, NotifyConfig\n\n"
        "    notifier = Notifier(NotifyConfig())\n"
        "    policy = AutoApprovePolicy(\n"
        "        min_expectancy=float(config.get('min_expectancy', 0.35)),\n"
        "        min_confidence=float(config.get('min_confidence', 0.75)),\n"
        "        max_auto_risk_pct=float(config.get('max_auto_risk_pct', 0.02)),\n"
        "    ).clamp()\n"
        "    approvals = ApprovalGate(\n"
        "        notifier=notifier,\n"
        "        timeout_seconds=float(config.get('approval_timeout', 90)),\n"
        "        require_approval=bool(config.get('require_approval', False)),\n"
        "    )\n\n"
        "    context = AppContext(\n        state=store,",
    ),
    (
        "    context.backtester_factory = backtester_factory\n    return context, holder",
        "    context.backtester_factory = backtester_factory\n"
        "    # exposed so the dashboard can render the approval queue\n"
        "    context.approvals = approvals\n"
        "    context.policy = policy\n"
        "    context.notifier = notifier\n"
        "    return context, holder",
    ),
], "context.py builds the approval gate")

# ------------------------------------------- 2. the engine consults the policy
ok &= patch(ROOT / "ts" / "engine.py", [
    (
        "                kill_switch=lambda: (state.root / \"KILL\").exists(),\n"
        "                    forward_journal=ForwardJournal(state.root),\n"
        "                )",
        "                kill_switch=lambda: (state.root / \"KILL\").exists(),\n"
        "                    forward_journal=ForwardJournal(state.root),\n"
        "                    approvals=(holder.get(\"approvals\")\n"
        "                               if isinstance(holder, dict) else None),\n"
        "                    policy=(holder.get(\"policy\")\n"
        "                            if isinstance(holder, dict) else None),\n"
        "                )",
    ),
], "engine.py receives the policy and gate")

ok &= patch(ROOT / "ts" / "engine.py", [
    (
        "                forward_journal=None,\n                notifier=None,\n    ) -> None:",
        "                forward_journal=None,\n"
        "                notifier=None,\n"
        "                approvals=None,\n"
        "                policy=None,\n    ) -> None:",
    ),
    (
        "        self.notifier = notifier",
        "        self.notifier = notifier\n"
        "        # when set, the engine asks a human only for unconvincing plans\n"
        "        self.approvals = approvals\n"
        "        self.policy = policy",
    ),
], "engine.py accepts the policy and gate")

sys.path.insert(0, str(ROOT))
try:
    from ts.web.context import build_context
    from ts.engine import TradingEngine
    from ts.agents.policy import AutoApprovePolicy
    from ts.approvals import ApprovalGate
    print("[ok] all modules import cleanly")
except Exception as exc:  # noqa: BLE001
    print(f"[FAIL] import error: {exc}")
sys.exit(0 if ok else 1)
