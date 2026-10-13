"""Make the dashboard a true viewer of a headless engine.

Two failures on the VPS path:

1. `_status()` derived mode and engine_running only from an in-process engine
   or supervisor. On a VPS there is none -- the engine runs headless via
   `run.py serve` and the dashboard is a separate process. The header therefore
   said IDLE / STOPPED while the engine was actively trading.

2. The headless engine writes ``ticks`` in its snapshot; the dashboard reads
   ``cycles``. Different components, different key names for the same number.

Fix: when no in-process engine exists, the dashboard infers both from the state
file -- mode from ``snapshot.mode``, and engine_running from whether that file
was updated recently. That is exactly what a viewer should do, and it makes the
UI honest on a VPS instead of implying the system is down.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

p = ROOT / "ts" / "web" / "server.py"
text = p.read_text(encoding="utf-8")

if "STALE_ENGINE_SECONDS" in text:
    print("[skip] already fixed")
    sys.exit(0)

old = '''        # "Running" means the worker thread is alive, NOT that it has finished a
        # cycle. `cycles` is only incremented at the END of a cycle, and the
        # first one spends 5-20s fetching the universe -- so keying the status
        # on it reported STOPPED while the engine was mid-flight.
        agents = self.ctx.agents
        thread = getattr(agents, "_thread", None)
        supervisor_alive = bool(thread is not None and thread.is_alive())
        running = bool(getattr(engine, "running", False)) or supervisor_alive

        mode = str(getattr(engine, "mode", "IDLE")) if engine else "IDLE"
        if agents is not None:
            mode = ("AGENTS-DRYRUN" if agents.cfg.dry_run else "AGENTS-LIVE") \\
                if supervisor_alive else "IDLE"
            if supervisor_alive and not getattr(agents, "cycles", 0):
                # alive but mid-first-cycle: say so rather than implying IDLE
                mode = mode + " (starting)"'''

new = '''        # "Running" means the worker is alive, NOT that it finished a cycle.
        # `cycles`/`ticks` is incremented at the END of a cycle, and the first
        # one spends 5-20s fetching the universe -- so keying the status on it
        # reported STOPPED while the engine was mid-flight.
        agents = self.ctx.agents
        thread = getattr(agents, "_thread", None)
        supervisor_alive = bool(thread is not None and thread.is_alive())
        running = bool(getattr(engine, "running", False)) or supervisor_alive

        mode = str(getattr(engine, "mode", "IDLE")) if engine else "IDLE"
        if agents is not None:
            mode = ("AGENTS-DRYRUN" if agents.cfg.dry_run else "AGENTS-LIVE") \\
                if supervisor_alive else "IDLE"
            if supervisor_alive and not getattr(agents, "cycles", 0):
                # alive but mid-first-cycle: say so rather than implying IDLE
                mode = mode + " (starting)"

        # --- VPS path: no in-process engine, so read the state file
        if engine is None and agents is None:
            snapshot = self.ctx.state.read_snapshot() or {}
            snap_mode = str(snapshot.get("mode") or "")
            if snap_mode:
                # infer liveness from freshness: a state file that stopped being
                # written means the engine is gone, whatever the file claims
                updated = snapshot.get("updated_at")
                age = self._snapshot_age_seconds(updated)
                running = age is not None and age <= STALE_ENGINE_SECONDS
                if snap_mode == "AGENTS-DRYRUN":
                    mode = "AGENTS-DRYRUN"
                elif snap_mode == "AGENTS-LIVE":
                    mode = "AGENTS-LIVE"
                else:
                    mode = snap_mode
                if not running:
                    mode = "STOPPED (stale state)"'''

if old not in text:
    print("[FAIL] status anchor not found")
    sys.exit(1)
text = text.replace(old, new, 1)

# the freshness helper + constant
anchor = "def _catalogue():"
helper = '''# How long a snapshot may go unwritten before the engine is presumed dead.
# Cycles are minutes apart, so this is deliberately generous.
STALE_ENGINE_SECONDS = 900.0


def _snapshot_age_seconds(iso: str | None):
    """Age of a snapshot's updated_at, or None if unparseable/absent."""
    if not iso:
        return None
    try:
        stamp = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
    except ValueError:
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=UTC)
    return max(0.0, (datetime.now(UTC) - stamp).total_seconds())


def _catalogue():'''
if anchor not in text:
    print("[FAIL] _catalogue anchor not found")
    sys.exit(1)
text = text.replace(anchor, helper, 1)

# the JS reads `cycles`; the engine writes `ticks`. Normalise on read.
text = text.replace(
    '''        snap["equity_series"] = series''',
    '''        # the headless engine writes "ticks"; the dashboard reads "cycles"
        snap["cycles"] = snap.get("cycles", snap.get("ticks"))
        snap["equity_series"] = series''',
    1,
)
p.write_text(text, encoding="utf-8")
print("[ok] server.py: dashboard reads headless state; ticks/cycles normalised")

sys.path.insert(0, str(ROOT))
try:
    from ts.web.server import DashboardServer, _snapshot_age_seconds

    print("[ok] imports clean")
    print("[ok] age helper works:", _snapshot_age_seconds(datetime.now(UTC).isoformat()) is not None)
except Exception as exc:  # noqa: BLE001
    print(f"[FAIL] import error: {exc}")
sys.exit(0)
