"""In-process message bus for the multi-agent system.

Agents never call each other directly -- they publish and subscribe on topics.
That gives three properties the whole design leans on:

* **Loose coupling.** The Bayesian agent does not know the edge detector
  exists. Swapping either out cannot break the other.
* **Order independence.** Agents finish their work whenever they finish; a slow
  network call in the scanner never blocks execution of an already-decided plan.
* **Auditability.** Every message is a typed envelope kept in a ring buffer, so
  the dashboard can show exactly what the system was thinking and when.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from collections import defaultdict, deque
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from enum import Enum
from typing import Any, Callable

log = logging.getLogger(__name__)


class Topic(str, Enum):
    """Everything the agents talk about."""

    MARKET_DATA = "market.data"          # new bars / quotes
    MARKET_REGIME = "market.regime"     # per-symbol regime assessment
    SIGNAL = "strategy.signal"          # directional opportunity
    PROBABILITY = "bayes.probability"  # posterior updates
    TRADE_PLAN = "edge.tradeplan"       # sized, validated plan
    ORDER = "execution.order"          # submitted order
    FILL = "execution.fill"            # confirmed fill
    POSITION = "execution.position"     # position update
    RISK = "risk.alert"               # risk event / halt
    AGENT_STATUS = "agent.status"       # heartbeat
    LOG = "system.log"


def topic_key(topic: "Topic | str") -> str:
    """Normalise a topic to its canonical string value.

    ``Topic`` subclasses ``str`` and ``Enum``. In Python 3.11+,
    ``str(Topic.X)`` returns ``"Topic.X"`` -- not ``"market.data"`` -- so
    publishing with the enum while subscribing with ``.value`` matched nothing
    and the whole agent pipeline silently did nothing. Always route topic keys
    through this helper instead of calling ``str`` on a topic.
    """
    if isinstance(topic, Topic):
        return topic.value
    return str(topic)


@dataclass
class Message:
    topic: str
    payload: dict
    source: str = "system"
    ts: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    seq: int = 0

    def to_dict(self) -> dict:
        return {
            "topic": self.topic,
            "source": self.source,
            "ts": self.ts,
            "seq": self.seq,
            "payload": self.payload,
        }


class Subscription:
    """A handler registered for one topic."""

    __slots__ = ("topic", "handler", "name")

    def __init__(self, topic: Topic | str, handler: Callable[[Message], Any],
                 name: str = "") -> None:
        self.topic = str(topic)
        self.handler = handler
        self.name = name or getattr(handler, "__name__", "anon")


class MessageBus:
    """Thread-safe pub/sub bus with a rolling journal of every message."""

    def __init__(self, journal_size: int = 2000) -> None:
        self._subs: dict[str, list[Subscription]] = defaultdict(list)
        self._lock = threading.RLock()
        self._seq = 0
        self._journal: deque[Message] = deque(maxlen=journal_size)
        self._published = 0
        self._errors: list[dict] = []

    # ------------------------------------------------------------- publishing

    def publish(self, topic: Topic | str, payload: dict, source: str = "system") -> Message:
        key = topic_key(topic)
        with self._lock:
            self._seq += 1
            seq = self._seq
            subscribers = list(self._subs.get(key, ()))
            message = Message(topic=key, payload=payload, source=source, seq=seq)
        self._journal.append(message)
        self._published += 1

        for sub in subscribers:
            try:
                sub.handler(message)
            except Exception as exc:  # noqa: BLE001 - one bad agent must not
                # stall the rest of the system
                log.exception("subscriber %s failed on %s", sub.name, topic)
                self._errors.append({
                    "at": message.ts, "subscriber": sub.name,
                    "topic": key, "error": str(exc),
                })
        return message

    # ------------------------------------------------------------ subscribing

    def subscribe(self, topic: Topic | str, handler: Callable[[Message], Any],
                  name: str = "") -> Subscription:
        key = topic_key(topic)
        sub = Subscription(key, handler, name)
        with self._lock:
            self._subs[key].append(sub)
        return sub

    def unsubscribe(self, sub: Subscription) -> None:
        with self._lock:
            bucket = self._subs.get(sub.topic)
            if bucket and sub in bucket:
                bucket.remove(sub)

    # -------------------------------------------------------------- inspection

    def journal(self, limit: int = 100, topic: str | None = None) -> list[dict]:
        key = None if topic is None else topic_key(topic)
        with self._lock:
            items = list(self._journal)
        if key:
            items = [m for m in items if m.topic == key]
        return [m.to_dict() for m in items[-limit:]]

    def stats(self) -> dict:
        with self._lock:
            return {
                "published": self._published,
                "subscriptions": {t: len(v) for t, v in self._subs.items()},
                "journalled": len(self._journal),
                "errors": len(self._errors),
            }


# --------------------------------------------------------------- snapshot I/O


def snapshot_message(message: Message) -> str:
    """JSON for logs and the dashboard."""
    try:
        return json.dumps(message.to_dict(), default=str)
    except (TypeError, ValueError):  # pragma: no cover
        return str(message.to_dict())


def utcnow() -> str:
    return datetime.now(UTC).isoformat()


def now_ts() -> float:
    return time.time()


__all__ = ["Topic", "Message", "Subscription", "MessageBus", "snapshot_message",
           "topic_key"]
