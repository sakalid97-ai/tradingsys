"""The multi-agent supervisor.

Orchestrates the agents in order each cycle:

    scanner -> analysis -> probability -> edge detection -> guardian -> execution

Each agent is a step; the bus carries state between them; the guardian can veto
before anything reaches execution. Runs in its own thread and publishes an
AGENT_STATUS heartbeat every cycle so the dashboard can show the live state of
every agent.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime

from .analysis import AnalysisAgent
from .bus import MessageBus, Topic
from .edge import EdgeDetectionAgent
from .execution import ExecutionAgent
from .guardian import RiskGuardianAgent, RiskLimits
from .kelly import KellyConfig, KellySizer
from .probability import ProbabilityAgent
from .scanner import ScannerAgent

log = logging.getLogger(__name__)


@dataclass
class SupervisorConfig:
    cycle_seconds: float = 60.0
    max_cycles: int = 0              # 0 = run forever
    interval: str = "1h"
    range_: str = "60d"
    dry_run: bool = True
    publish_every: int = 1

    # strategy thresholds shared by the analysis/edge agents
    min_expectancy: float = 0.20
    min_confidence: float = 0.35
    kelly_fraction: float = 0.25
    max_risk_per_trade: float = 0.01
    max_fraction: float = 0.05
    bankroll: float = 100_000.0    # capital the Kelly sizer budgets against
    atr_stop_mult: float = 2.0
    atr_target_mult: float = 4.0


class MultiAgentSupervisor:
    """Owns the bus, the agents, and the cycle loop."""

    def __init__(self, feed, symbols, broker=None, config=None,
                 kill_switch=None, state_store=None) -> None:
        self.cfg = config or SupervisorConfig()
        self.feed = feed
        self.symbols = list(symbols)
        self.broker = broker
        self.state = state_store
        self.bus = MessageBus()
        self.stop_event = threading.Event()

        kelly_config = KellyConfig(
            kelly_fraction=self.cfg.kelly_fraction,
            max_risk_per_trade=self.cfg.max_risk_per_trade,
            max_fraction=self.cfg.max_fraction,
            bankroll=self.cfg.bankroll,
        )
        self.sizer = KellySizer(kelly_config)

        self.scanner = ScannerAgent(self.bus, self.feed, self.symbols,
                                 max_workers=getattr(self.cfg, "scan_workers", 8))
        self.analysis = AnalysisAgent(self.bus, self.feed)
        self.probability = ProbabilityAgent(self.bus)
        # every agent trade is journalled as forward evidence for the Forward tab
        self.forward_journal = None
        if state_store is not None:
            from ..forward import ForwardJournal

            self.forward_journal = ForwardJournal(state_store.root)
        self.execution = ExecutionAgent(self.bus, broker, self.feed,
                                        dry_run=self.cfg.dry_run)
        self.execution.forward_journal = self.forward_journal
        self.guardian = RiskGuardianAgent(self.bus, RiskLimits(), broker=broker,
                                          kill_switch=kill_switch)
        self.edge = EdgeDetectionAgent(
            self.bus, sizer=self.sizer,
            min_expectancy=self.cfg.min_expectancy,
            min_confidence=self.cfg.min_confidence,
            atr_stop_mult=self.cfg.atr_stop_mult,
            atr_target_mult=self.cfg.atr_target_mult,
        )

        self.agents = [
            self.scanner, self.analysis, self.probability,
            self.edge, self.guardian, self.execution,
        ]
        self.cycles = 0
        self.started_at = datetime.now(UTC)
        self._thread: threading.Thread | None = None
        self.errors: list[dict] = []

    # ------------------------------------------------------------- lifecycle

    def setup(self) -> None:
        for agent in self.agents:
            agent.subscribe_all()
        self.bus.subscribe(Topic.LOG, self._on_log, name="supervisor.log")
        self.bus.subscribe(Topic.RISK, self._on_risk, name="supervisor.risk")
        self.bus.publish(Topic.AGENT_STATUS, {
            "supervisor": "ready", "symbols": self.symbols,
            "dry_run": self.cfg.dry_run,
        })
        if self.state:
            self.state.log("supervisor_ready", symbols=len(self.symbols),
                           dry_run=self.cfg.dry_run)

    def start(self) -> None:
        self.setup()
        self._thread = threading.Thread(target=self._loop, name="agents",
                                        daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self.stop_event.set()
        for agent in self.agents:
            agent.stop()
        if self.state:
            self.state.log("supervisor_stopped", cycles=self.cycles)

    # ------------------------------------------------------------------ loop

    def _loop(self) -> None:
        while not self.stop_event.is_set():
            started = time.perf_counter()
            try:
                self.cycle()
            except Exception as exc:  # noqa: BLE001
                log.exception("cycle failed")
                self.errors.append({"at": datetime.now(UTC).isoformat(),
                                    "error": str(exc)})
                self.bus.publish(Topic.LOG, {"agent": "supervisor", "error": str(exc)})
            elapsed = time.perf_counter() - started
            self._publish_status(elapsed)
            if self.cfg.max_cycles and self.cycles >= self.cfg.max_cycles:
                break
            self.stop_event.wait(self.cfg.cycle_seconds)

    def cycle(self) -> dict:
        """Run one full cycle: scan, analyse, infer, detect, guard, execute."""
        self.scanner.cycle()
        self.analysis.cycle()
        # the handlers above have already published MARKET_DATA / MARKET_REGIME,
        # so the downstream agents' handlers have run too; this drives their
        # status counters and republishes their current state.
        for agent in (self.probability, self.edge, self.guardian, self.execution):
            agent.cycle()

        # approve plans through the guardian, then execute the ones that pass
        approved: list[dict] = []
        for plan in self.edge.plans:
            ok, reason = self.guardian.approve_plan(plan)
            record = {**plan, "guardian_approved": ok, "guardian_reason": reason}
            approved.append(record)
            if ok and plan.get("quantity", 0) > 0:
                self.execution.handle(_FakeMessage(record))
        self.cycles += 1
        self.execution.cycle()
        return {"cycle": self.cycles, "plans": approved}

    # ------------------------------------------------------------- reporting

    def _publish_status(self, elapsed: float) -> None:
        status = {
            "supervisor": {
                "cycles": self.cycles,
                "uptime_seconds": round((datetime.now(UTC) - self.started_at).total_seconds(), 1),
                "dry_run": self.cfg.dry_run,
                "errors": len(self.errors),
                "last_cycle_seconds": round(elapsed, 3),
            },
            "agents": {a.name: a.status.to_dict() for a in self.agents},
            "risk": self.guardian.state.to_dict(),
            "limits": self.guardian.limits.to_dict(),
            "execution": self.execution.snapshot(),
            "plans": self.edge.plans,
            "probabilities": self.probability.latest,
            "regimes": self.analysis.latest,
            "scan": self.scanner.report.to_dict(),
        }
        self.bus.publish(Topic.AGENT_STATUS, {"type": "dashboard", **status})
        if self.state:
            self.state.save_snapshot({
                "mode": "AGENTS-" + ("PAPER" if not self.cfg.dry_run else "DRYRUN"),
                "broker": getattr(self.broker, "name", "none"),
                "dry_run": self.cfg.dry_run,
                "cycles": self.cycles,
                "equity": self.guardian.state.equity,
                "risk": self.guardian.state.to_dict(),
                "positions": self.execution.snapshot()["open_positions"],
                "agent_status": status,
            })
            # full agent state lives in its own file so the dashboard can render
            # it without parsing the (much smaller) snapshot
            self.state.save_agents(status)

    def _on_log(self, message) -> None:
        if self.state:
            self.state.log(str(message.payload.get("agent", "?")),
                           level=str(message.payload.get("level", "INFO")))

    def _on_risk(self, message) -> None:
        if message.payload.get("halt"):
            self.execution.dry_run = True   # stop trading, keep observing

    # ------------------------------------------------------------------ helpers

    def run_cycles(self, n: int) -> list[dict]:
        """Run N cycles synchronously (for tests and CLI use)."""
        self.setup()
        results = []
        for _ in range(max(1, n)):
            result = self.cycle()
            results.append(result)
            # publish status after every cycle so the dashboard has live state
            # even when the loop is driven synchronously from the CLI
            self._publish_status(time.perf_counter())
        return results

    def snapshot(self) -> dict:
        return {
            "cycles": self.cycles,
            "agents": {a.name: a.status.to_dict() for a in self.agents},
            "risk": self.guardian.state.to_dict(),
            "limits": self.guardian.limits.to_dict(),
            "execution": self.execution.snapshot(),
            "plans": self.edge.plans,
            "probabilities": self.probability.latest,
            "regimes": self.analysis.latest,
            "scan": self.scanner.report.to_dict(),
            "bus": self.bus.stats(),
        }


@dataclass
class _FakeMessage:
    """Minimal Message stand-in so agents can be driven directly."""

    payload: dict
    topic: str = Topic.TRADE_PLAN.value


__all__ = ["MultiAgentSupervisor", "SupervisorConfig"]
