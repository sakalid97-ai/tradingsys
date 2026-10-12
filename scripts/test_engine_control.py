"""Launch the dashboard detached and prove the START/STOP engine control works.

Run:  py tradingsys\\scripts\\test_engine_control.py
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PORT = 8646


def get(path: str):
    with urllib.request.urlopen(f"http://127.0.0.1:{PORT}{path}", timeout=25) as r:
        return json.load(r)


def post(path: str, body: dict):
    req = urllib.request.Request(
        f"http://127.0.0.1:{PORT}{path}",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=90) as r:
        return r.status, json.load(r)


def wait_up(seconds: float = 45) -> bool:
    deadline = time.time() + seconds
    while time.time() < deadline:
        try:
            get("/api/status")
            return True
        except (urllib.error.URLError, OSError, TimeoutError):
            time.sleep(1.0)
    return False


def main() -> int:
    # stop any previous instance on this port
    subprocess.run(["powershell", "-NoProfile", "-Command",
                    f"$c=Get-NetTCPConnection -LocalPort {PORT} -State Listen "
                    f"-ErrorAction SilentlyContinue; if($c){{$c | "
                    f"Select-Object -ExpandProperty OwningProcess -Unique | "
                    f"ForEach-Object {{ Stop-Process -Id $_ -Force }}}}"
                    ], capture_output=True)
    time.sleep(2)

    print("[*] launching the dashboard ...")
    env_launcher = ("import os; os.environ['PYTHONPATH']="
                    f"r'{ROOT}'")
    subprocess.Popen(
        [sys.executable, str(ROOT / "run.py"), "dashboard",
         "--port", str(PORT), "--no-browser", "--interval", "1h", "--range", "60d"],
        cwd=str(ROOT),
        stdout=open(ROOT.parent / "dashboard_test.log", "w"),
        stderr=subprocess.STDOUT,
    )
    if not wait_up():
        print("[FAIL] dashboard did not come up; see dashboard_test.log")
        return 1
    print("[ok] dashboard is up")

    status = get("/api/status")
    print(f"[*] before: mode={status['mode']} running={status['engine_running']}")
    if status["engine_running"]:
        print("[!] an engine is already running; stopping it first")
        post("/api/engine/stop", {})

    print("[*] clicking START ENGINE (multi-agent) ...")
    code, result = post("/api/engine/start",
                        {"mode": "agents", "dry_run": True, "poll_seconds": 60})
    print(f"[ok] HTTP {code}: {json.dumps(result)[:200]}")

    print("[*] waiting for the first full agent cycle ...")
    agents = {}
    for _ in range(120):
        time.sleep(2)
        agents = get("/api/agents")
        cycling = [name for name, s in (agents.get("agents") or {}).items()
                   if (s or {}).get("cycles", 0) > 0]
        # a full cycle ends when the supervisor has published its own status
        if (agents.get("supervisor") or {}).get("cycles") and len(cycling) >= 6:
            break

    sup = agents.get("supervisor") or {}
    cycling = [name for name, s in (agents.get("agents") or {}).items()
               if (s or {}).get("cycles", 0) > 0]
    print(f"[ok] cycles={sup.get('cycles')}  agents cycling: {cycling}")
    print(f"[ok] plans={len(agents.get('plans') or [])}  "
          f"posteriors={list((agents.get('probabilities') or {}).keys())}")
    print(f"[ok] risk halted={(agents.get('risk') or {}).get('halted')}")

    status = get("/api/status")
    print(f"[*] after start: mode={status['mode']} running={status['engine_running']}")

    print("[*] clicking STOP ENGINE ...")
    code, result = post("/api/engine/stop", {})
    print(f"[ok] HTTP {code}: {json.dumps(result)[:200]}")
    time.sleep(4)
    status = get("/api/status")
    print(f"[*] after stop: mode={status['mode']} running={status['engine_running']}")

    ok = bool(
        status.get("engine_running") is False
        and len(cycling) >= 6
        and any((s or {}).get("cycles", 0) > 0 for s in (agents.get("agents") or {}).values())
    )
    print("\n" + ("PASS" if ok else "FAIL") + ": engine start/stop control works")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
