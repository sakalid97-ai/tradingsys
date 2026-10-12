"""Fix two real parser bugs + the test dummy handler signatures."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# --------------------------------------------------------------- parser fixes
p = ROOT / "ts" / "commands.py"
text = p.read_text(encoding="utf-8")

# BUG 1: "run" was an alias for /start, so "/run backtest" was swallowed by the
# start branch and never reached the CLI-command handler.
old_start = '    if head in ("start", "go", "run"):\\n        return "start", args'
new_start = ('    if head in ("start", "go"):\\n        return "start", args')
if new_start in text:
    print("[skip] start aliases already fixed")
elif old_start not in text:
    print("[FAIL] start alias anchor not found")
    sys.exit(1)
else:
    text = text.replace(old_start, new_start, 1)
    print("[ok] removed 'run' from the /start aliases so /run reaches the CLI")

# BUG 2: replying "no" to a confirmation prompt raised "unknown command"
# instead of cancelling.
old_confirm = '    if head in ("yes", "y", "confirm") and context is not None:\\n        return "confirm", args'
new_confirm = ('    # a yes/no reply is only meaningful when a confirmation is pending,\n'
               '    # but must always parse to *something* rather than erroring\n'
               '    if head in ("yes", "y", "confirm", "no", "n", "cancel"):\n'
               '        return "confirm", args')
if new_confirm in text:
    print("[skip] yes/no parsing already fixed")
elif old_confirm not in text:
    print("[FAIL] yes/no anchor not found")
    sys.exit(1)
else:
    text = text.replace(old_confirm, new_confirm, 1)
    print("[ok] 'no' now parses as a cancel instead of an unknown command")
p.write_text(text, encoding="utf-8")

# ---------------------------------------------------------------- test fixes
t = ROOT / "tests" / "test_commands.py"
jtext = t.read_text(encoding="utf-8")
subs = [
    ('set_risk=self._wrap("set_risk", lambda: True),',
     'set_risk=self._wrap("set_risk", lambda *a: True),'),
    ('kill=self._wrap("kill", lambda r: True),',
     'kill=self._wrap("kill", lambda *a: True),'),
]
fixed = 0
for old, new in subs:
    if new in jtext:
        continue
    if old not in jtext:
        print(f"[FAIL] test anchor not found: {old[:40]!r}")
        sys.exit(1)
    jtext = jtext.replace(old, new, 1)
    fixed += 1
t.write_text(jtext, encoding="utf-8")
print(f"[ok] test dummies: {fixed} handler signature(s) fixed")
sys.exit(0)
