"""Agent base class.

Each agent owns one responsibility, runs its own update cycle, and communicates
only through the bus. Lifecycle is explicit: ``start()`` -> ``cycle()`` ...
-> ``stop()``, so the supervisor can run them in parallel threads and a hung
agent can never take the system down.
"""

from __future__ import annotations

import logging
import threading
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import UTC, datetime

from .bus import MessageBus, Topic


@dataclass
class AgentStatus:
    """What the dashboard shows for one agent."""

    name: str
    role: str = ""
    running: bool = False
    cycles: int = 0
    last_run: str | None = None
    last_error: str | None = None
    last_duration_ms: float = 0.0
    alarms: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "role": self.role,
            "running": self.running,
            "cycles": self.cycles,
            "last_run": self.last_run,
            "last_error": self.last_error,
            "last_duration_ms": round(self.last_duration_ms, 2),
            "alarms": self.alarms[-5:],
        }


class Agent(ABC):
    """Base class for every agent in the system."""

    name = "agent"
    role = "generic"
    subscribes: tuple[str, ...] = ()

    def __init__(self, bus: MessageBus) -> None:
        self.bus = bus
        self.log = logging.getLogger(f"agent.{self.name}")
        self.status = AgentStatus(name=self.name, role=self.role)
        self._stop_event = threading.Event()
        self._subscriptions = []

    # ------------------------------------------------------------- lifecycle

    def start(self) -> None:
        """Hook for setup work; call from the supervisor thread."""
        self.status.running = True
        self.status.alarms = []
        self.on_start()

    def stop(self) -> None:
        self._stop_event.set()
        self.status.running = False
        self.on_stop()

    def on_start(self) -> None:  # pragma: no cover - optional
        """Optional override."""

    def on_stop(self) -> None:  # pragma: no cover - optional
        """Optional override."""

    def subscribe_all(self) -> None:
        for topic in self.subscribes:
            handler = getattr(self, "handle", None)
            if handler is None:
                continue
            self._subscriptions.append(
                self.bus.subscribe(topic, handler, name=f"{self.name}->{topic}")
            )

    def unsubscribe_all(self) -> None:
        for sub in self._subscriptions:
            self.bus.unsubscribe(sub)
        self._subscriptions.clear()

    # ----------------------------------------------------------------- cycle

    def cycle(self) -> None:
        """One unit of work. The supervisor calls this on a schedule."""
        started = time.perf_counter()
        try:
            self.run()
            self.status.cycles += 1
            self.status.last_run = datetime.now(UTC).isoformat()
            self.status.last_error = None
        except Exception as exc:  # noqa: BLE001 - an agent must never die
            self.status.last_error = f"{type(exc).__name__}: {exc}"
            self.status.alarms.append(self.status.last_error)
            self.log.exception("cycle failed")
            self.bus.publish(Topic.LOG, {
                "agent": self.name, "error": self.status.last_error,
            }, source=self.name)
        finally:
            self.status.last_duration_ms = (time.perf_counter() - started) * 1000.0

    @abstractmethod
    def run(self) -> None:
        """The agent's actual work. Must be safe to call repeatedly."""

    # --------------------------------------------------------------- helpers

    def publish(self, topic: Topic | str, payload: dict) -> None:
        return self.bus.publish(topic, payload, source=self.name)

    def alarm(self, message: str) -> None:
        self.status.alarms.append(message)
        if len(self.status.alarms) > 200:
            self.status.alarms = self.status.alarms[-200:]
        self.bus.publish(Topic.LOG, {"agent": self.name, "alarm": message}, source=self.name)


__all__ = ["Agent", "AgentStatus"]
