"""Reproduce the stuck START button: time the /api/engine/start call."""

from __future__ import annotations

import io
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

BASE = "http://127.0.0.1:8642"


def get(path, timeout=25):
    with urllib.request.urlopen(BASE + path, timeout=timeout) as r:
        return json.load(r)


def post(path, body, timeout=25):
    data = json.dumps(body).encode()
    req = urllib.request.Request(BASE + path, data=data, method="POST",
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def status() -> dict:
    return get("/api/status", timeout=10)


print("=== before ===")
s = status()
print(f"  mode={s['mode']}  engine_running={s['engine_running']}")
print(f"  ctx.agents is {'set' if s.get('engine_running') else '?'}")

print("\n=== POST /api/engine/start (timed) ===")
started = time.perf_counter()
try:
    result = post("/api/engine/start", {"mode": "agents", "dry_run": True,
                                        "poll_seconds": 300}, timeout=180)
    elapsed = time.perf_counter() - started
    print(f"  returned in {elapsed:.1f}s -> {json.dumps(result)[:200]}")
except Exception as exc:  # noqa: BLE001
    elapsed = time.perf_counter() - started
    print(f"  FAILED after {elapsed:.1f}s: {type(exc).__name__}: {exc}")

print("\n=== status right after ===")
s = status()
print(f"  mode={s['mode']}  engine_running={s['engine_running']}")

print("\n=== agents endpoint ===")
try:
    a = get("/api/agents", timeout=20)
    sup = a.get("supervisor") or {}
    cycling = [n for n, st in (a.get("agents") or {}).items()
               if (st or {}).get("cycles", 0) > 0]
    print(f"  supervisor.cycles={sup.get('cycles')}  cycling={cycling}")
    print(f"  plans={len(a.get('plans') or [])}  posteriors={list((a.get('probabilities') or {}))}")
except Exception as exc:  # noqa: BLE001
    print(f"  FAILED: {exc}")

print("\n=== the thread the status check looks for ===")
try:
    a = get("/api/agents", timeout=20)
    print("  /api/agents returned data -> the supervisor IS running")
except Exception as exc:  # noqa: BLE001
    print(f"  /api/agents failed -> the supervisor is NOT running: {exc}")
