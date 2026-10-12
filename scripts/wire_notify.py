"""Wire the notifier into the engine, risk guardian and forward journal."""

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
            print(f"    [FAIL] {path.name}: {old[:56]!r}")
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

# ------------------------------------------------------- 1. engine: lifecycle alerts
ok &= patch(ROOT / "ts" / "engine.py", [
    (
        "                forward_journal=None,\n    ) -> None:",
        "                forward_journal=None,\n                notifier=None,\n    ) -> None:",
    ),
    (
        "        self.forward_journal = forward_journal",
        "        self.forward_journal = forward_journal\n        self.notifier = notifier",
    ),
    (
        '''        if self.state:
            self.state.log("engine_started", mode=self.mode, broker=self.broker_name,
                           universe=len(self.universe), dry_run=self.cfg.dry_run)
        print(f"[*] engine running in {self.mode} mode "
              f"({'DRY RUN' if self.cfg.dry_run else 'live orders'})")''',
        '''        if self.state:
            self.state.log("engine_started", mode=self.mode, broker=self.broker_name,
                           universe=len(self.universe), dry_run=self.cfg.dry_run)
        if self.notifier is not None:
            self.notifier.info(
                "Engine started",
                f"Running in {self.mode} mode"
                + (" (DRY RUN)" if self.cfg.dry_run else " with LIVE orders"),
                universe=len(self.universe), broker=self.broker_name,
                interval=self.cfg.interval, _kind="engine_start")
        print(f"[*] engine running in {self.mode} mode "
              f"({'DRY RUN' if self.cfg.dry_run else 'live orders'})")''',
    ),
    (
        '''        except KeyboardInterrupt:
            print("\\n[*] stopping engine")
        finally:
            self.running = False
            if self.state:
                self.state.log("engine_stopped", ticks=self.ticks)''',
        '''        except KeyboardInterrupt:
            print("\\n[*] stopping engine")
        finally:
            self.running = False
            if self.state:
                self.state.log("engine_stopped", ticks=self.ticks)
            if self.notifier is not None:
                self.notifier.warning("Engine stopped",
                                      f"{self.ticks} cycles completed", _kind="engine_stop")''',
    ),
    # a kill switch is the one event that must always interrupt you
    (
        '''        if self.kill_switch and self.kill_switch():
            summary["halted"] = "kill switch engaged"
            self._flatten("kill switch")
            self._publish()
            return summary''',
        '''        if self.kill_switch and self.kill_switch():
            summary["halted"] = "kill switch engaged"
            if self.notifier is not None:
                self.notifier.critical(
                    "KILL SWITCH ENGAGED",
                    "All trading halted and positions flattened.",
                    positions=len(self.managed), _kind="kill_switch")
            self._flatten("kill switch")
            self._publish()
            return summary''',
    ),
], "engine.py notifies on start/stop/kill")

# ------------------------------------------------- 2. guardian: risk-limit alerts
ok &= patch(ROOT / "ts" / "agents" / "guardian.py", [
    (
        "    def __init__(self, bus, limits: RiskLimits | None = None,\n"
        "                 broker=None, kill_switch=None) -> None:",
        "    def __init__(self, bus, limits: RiskLimits | None = None,\n"
        "                 broker=None, kill_switch=None, notifier=None) -> None:",
    ),
    (
        "        self.plans: dict[str, dict] = {}\n        self.subscribe_all()",
        "        self.plans: dict[str, dict] = {}\n"
        "        self.notifier = notifier\n        self.subscribe_all()",
    ),
    (
        '''    def _halt(self, reason: str) -> None:
        if self.state.halted:
            return
        self.state.halted = True
        self.state.halt_reason = reason''',
        '''    def _halt(self, reason: str) -> None:
        if self.state.halted:
            return
        self.state.halted = True
        self.state.halt_reason = reason
        # a risk halt is the single most important thing to be told about
        if self.notifier is not None:
            self.notifier.critical(
                "RISK LIMIT BREACHED",
                reason,
                equity=self.state.equity,
                drawdown=self.state.drawdown,
                day_pl=self.state.day_pl,
                _kind="risk_halt")''',
    ),
], "guardian.py notifies on a breach")

# --------------------------------------- 3. supervisor: pass the notifier through
ok &= patch(ROOT / "ts" / "agents" / "supervisor.py", [
    (
        "    def __init__(self, feed, symbols, broker=None, config=None,\n"
        "                 kill_switch=None, state_store=None) -> None:",
        "    def __init__(self, feed, symbols, broker=None, config=None,\n"
        "                 kill_switch=None, state_store=None, notifier=None) -> None:",
    ),
    (
        "        self.execution.forward_journal = self.forward_journal",
        "        self.execution.forward_journal = self.forward_journal\n"
        "        self.forward_notifier = notifier\n"
        "        if notifier is not None:\n"
        "            self.guardian.notifier = notifier",
    ),
], "supervisor.py passes the notifier through")

sys.path.insert(0, str(ROOT))
try:
    from ts.engine import TradingEngine
    from ts.agents.supervisor import MultiAgentSupervisor
    from ts.agents.guardian import RiskGuardianAgent
    from ts.notify import Notifier, NotifyConfig, Severity
    print("[ok] all modules import cleanly")
except Exception as exc:  # noqa: BLE001
    print(f"[FAIL] import error: {exc}")
sys.exit(0 if ok else 1)
