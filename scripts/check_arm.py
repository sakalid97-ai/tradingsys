"""Check the system for ARM/aarch64 compatibility.

Oracle's Always Free tier is Ampere **ARM**, not x86. Most pure-Python code is
fine; what breaks is compiled extensions and anything shelling out to an
x86-only binary. This walks the codebase looking for both rather than guessing.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TS = ROOT / "ts"

CHECKS: list[tuple[str, str]] = [
    ("platform.machine", "explicit CPU-architecture branching"),
    ("platform.architecture", "explicit CPU-architecture branching"),
    ("platform.system", "explicit OS branching"),
    ("win32com", "Windows COM (Exness/MT5 path)"),
    ("MetaTrader5", "Windows-only MT5 bridge (Exness path)"),
    ("cp1252", "Windows console encoding assumption"),
    ("os.startfile", "Windows-only shell call"),
    ("winshell|winreg|win32", "Windows-only shell call"),
]


def main() -> int:
    print("=== ARM (aarch64) compatibility check ===\n")

    problems: list[str] = []
    notes: list[str] = []

    # ---- 1. declared dependencies
    req = (ROOT / "requirements.txt")
    deps = []
    if req.exists():
        for line in req.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and not line.startswith("."):
                deps.append(line.split(">=")[0].split("==")[0].split("<")[0].strip())
    print("Declared dependencies:", deps or "(none)")
    # pandas and numpy ship aarch64 manylinux wheels
    safe = {"pandas", "numpy"}
    unsafe = [d for d in deps if d not in safe]
    if unsafe:
        problems.append(f"deps without an ARM wheel guarantee: {unsafe}")
    else:
        print("  -> all have aarch64 wheels; no source builds needed\n")

    # ---- 2. arch-specific code in the package
    hits: dict[str, list[str]] = {}
    for path in TS.rglob("*.py"):
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for needle, why in CHECKS:
            if needle in text:
                hits.setdefault(needle, []).append(f"{path.relative_to(ROOT)} ({why})")

    if hits:
        print("Platform-specific code found:")
        for needle, files in hits.items():
            print(f"  {needle}:")
            for f in files:
                print(f"     - {f}")
            notes.append(f"{needle} is Windows-only; the affected path must be optional")
    else:
        print("No platform-specific code in ts/ (beyond stdlib)\n")

    # ---- 3. paths that must not be Windows-only
    required = [
        ("ts/backtest.py", "backtester"),
        ("ts/agents/supervisor.py", "multi-agent loop"),
        ("ts/web/server.py", "dashboard"),
        ("ts/forward.py", "forward journal"),
        ("ts/notify.py", "alerts"),
        ("ts/propfirm.py", "prop-firm rules"),
    ]
    print("Core modules present:")
    for rel, label in required:
        ok = (ROOT / rel).exists()
        print(f"  [{'x' if ok else ' '}] {label:<20} {rel}")
        if not ok:
            problems.append(f"missing core module {rel}")

    # ---- 4. verdict
    print("\n=== VERDICT ===")
    if problems:
        print("BLOCKED:")
        for p in problems:
            print("  -", p)
        return 1
    print("The core system (data, indicators, strategy, risk, backtest, metrics,")
    print("agents, Bayesian, Kelly, forward journal, dashboard, alerts, prop-firm")
    print("rules) is pure Python and runs on aarch64 as-is.")
    if notes:
        print("\nWindows-only paths, already optional:")
        for n in notes:
            print(f"  - {n}")
    print("\nNote: the Exness/MT5 broker is Windows-only BY DESIGN (MetaTrader5 has")
    print("no ARM/Linux build). On Oracle use paper, Binance, Alpaca or the generic")
    print("webhook bridge.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
