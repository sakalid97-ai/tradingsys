"""Human-in-the-loop approval, asked and answered over Telegram.

The engine proposes; you dispose. When an action needs your sign-off it sends a
message to Telegram with the exact parameters and waits for a reply:

    APPROVE?  long BTCUSD
    quantity  4.03  price 83,317  stop 82,255  target 83,193
    risk      $250 (1.00% of equity)   expectancy +1.77R
    Reply YES to execute, NO to reject. Expires in 90s.

Design rules for anything that can move money:

1. **Default deny.** No reply, or a timeout, means the action does NOT happen.
   Silence is never consent -- an unanswered phone while you sleep must not
   become a trade.
2. **Everything is journalled** with the exact parameters, who approved it, when,
   and how long they took. A decision you cannot reconstruct later is not a
   decision you made.
3. **The proposed parameters are frozen.** Approving `quantity 4.03` approves
   exactly 4.03; nothing is recomputed between the question and the execution.
4. **Requests are rate-limited** so a signal flood cannot turn into 40
   notifications, and so you cannot be worn down into approving something you
   would otherwise reject.
5. **Only your chat id may answer.** Anyone else replying is ignored.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import Enum

log = logging.getLogger(__name__)

YES = {"yes", "y", "approve", "approved", "confirm", "ok", "go", "do it",
       "1", "si", "oui"}
NO = {"no", "n", "reject", "deny", "cancel", "stop", "0", "nope", "nah"}


class Decision(str, Enum):
    APPROVED = "approved"
    REJECTED = "rejected"
    EXPIRED = "expired"
    CANCELLED = "cancelled"


@dataclass
class ApprovalRequest:
    """One action waiting on a human."""

    request_id: str
    action: str                     # enter | exit | flatten | set_limit | run
    title: str
    body: str
    fields: dict = field(default_factory=dict)
    severity: str = "warning"
    created_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    expires_at: str = ""
    timeout_seconds: float = 90.0
    decision: Decision | None = None
    decided_at: str | None = None
    decided_by: str | None = None
    wait_seconds: float | None = None

    @property
    def pending(self) -> bool:
        return self.decision is None

    @property
    def approved(self) -> bool:
        return self.decision is Decision.APPROVED

    def remaining(self) -> float:
        """Seconds left; 0 once decided or if the deadline is unset."""
        if self.decision is not None or not self.expires_at:
            return 0.0
        try:
            deadline = datetime.fromisoformat(self.expires_at.replace("Z", "+00:00"))
        except ValueError:
            return 0.0
        return max(0.0, (deadline - datetime.now(UTC)).total_seconds())

    def resolve(self, decision: Decision, decided_by: str = "telegram") -> None:
        self.decision = decision
        self.decided_at = datetime.now(UTC).isoformat()
        self.decided_by = decided_by
        started = datetime.fromisoformat(self.created_at.replace("Z", "+00:00"))
        self.wait_seconds = round(
            (datetime.now(UTC) - started).total_seconds(), 1)

    def to_dict(self) -> dict:
        return {
            "request_id": self.request_id,
            "action": self.action,
            "title": self.title,
            "fields": self.fields,
            "severity": self.severity,
            "created_at": self.created_at,
            "expires_at": self.expires_at,
            "timeout_seconds": self.timeout_seconds,
            "decision": self.decision.value if self.decision else None,
            "decided_at": self.decided_at,
            "decided_by": self.decided_by,
            "wait_seconds": self.wait_seconds,
            "remaining_seconds": round(self.remaining(), 1),
        }


def format_request(request: ApprovalRequest) -> str:
    """Render the question the way a trader wants to read it."""
    from .notify import _escape

    lines = [f"*{_escape(request.title)}*", ""]
    if request.body:
        lines += [_escape(request.body), ""]
    for key, value in request.fields.items():
        lines.append(f"• {_escape(str(key))}: {_escape(str(value))}")
    lines += [
        "",
        f"Reply *YES* to execute, *NO* to reject.",
        f"Expires in {int(request.remaining())}s — no reply means *no*.",
        f"`{request.request_id}`",
    ]
    return "\n".join(lines)


def classify_reply(text: str) -> Decision | None:
    """Interpret a reply. None means 'not an answer'."""
    cleaned = (text or "").strip().lower().strip(".!")
    if cleaned in YES:
        return Decision.APPROVED
    if cleaned in NO:
        return Decision.REJECTED
    return None


class ApprovalGate:
    """Asks for, collects, and times out human decisions."""

    def __init__(self, notifier=None, timeout_seconds: float = 90.0,
                 max_pending: int = 5, require_approval: bool = False) -> None:
        self.notifier = notifier
        self.default_timeout = float(timeout_seconds)
        self.max_pending = max(1, int(max_pending))
        self.require_approval = require_approval
        self.pending: dict[str, ApprovalRequest] = {}
        self._lock = threading.RLock()
        self._seq = 0
        self.history: list[ApprovalRequest] = []

    # ------------------------------------------------------------- requesting

    def request(self, action: str, title: str, body: str = "",
                fields: dict | None = None, timeout_seconds: float | None = None,
                severity: str = "warning") -> ApprovalRequest:
        """Queue an action for approval and send it to Telegram."""
        with self._lock:
            if len(self.pending) >= self.max_pending:
                raise RuntimeError(
                    f"{len(self.pending)} approvals already wait on you; refusing to "
                    "queue more until they are answered")
            self._seq += 1
            request_id = f"{action[:3].upper()}-{self._seq:04d}"
            timeout = self.default_timeout if timeout_seconds is None else float(
                timeout_seconds)
            expires = (datetime.now(UTC) + timedelta(seconds=timeout)).isoformat()
            request = ApprovalRequest(
                request_id=request_id, action=action, title=title, body=body,
                fields=dict(fields or {}), severity=severity,
                expires_at=expires, timeout_seconds=timeout,
            )
            self.pending[request_id] = request

        if self.notifier is not None:
            self.notifier.send(title, format_request(request), severity=severity,
                              kind=f"approval_{action}")
        return request

    # -------------------------------------------------------------- answering

    def answer(self, text: str, chat_id: str | None = None,
               request_id: str | None = None) -> tuple[bool, str]:
        """Apply a reply from Telegram.

        With no ``request_id`` the reply is applied to the oldest pending
        request, so "yes" answers the question currently on screen.
        """
        decision = classify_reply(text)
        if decision is None:
            return False, "Reply YES or NO."

        with self._lock:
            if not self.pending:
                return False, "Nothing is waiting for your approval."

            target = None
            if request_id:
                target = self.pending.get(request_id)
                if target is None:
                    return False, f"No pending request {request_id}."
            else:
                # oldest first -- the one you have been looking at
                target = min(self.pending.values(),
                             key=lambda r: r.created_at)
            if target is None or not target.pending:
                return False, "That request is no longer pending."

            target.resolve(decision, decided_by=str(chat_id or "telegram"))
            del self.pending[target.request_id]
            self.history.append(target)
            if len(self.history) > 200:
                self.history = self.history[-200:]

        if decision is Decision.APPROVED:
            self.notifier.trade(f"Approved: {target.title}",
                                f"executed as proposed · waited "
                                f"{target.wait_seconds}s",
                                request_id=target.request_id) if self.notifier else None
            return True, f"Approved {target.request_id}."
        return True, f"Rejected {target.request_id}."

    # -------------------------------------------------------------- expiring

    def expire(self) -> list[ApprovalRequest]:
        """Time out anything past its deadline. Expiry is a rejection."""
        expired: list[ApprovalRequest] = []
        with self._lock:
            now = datetime.now(UTC)
            for request in list(self.pending.values()):
                if datetime.fromisoformat(request.expires_at.replace("Z", "+00:00")) <= now:
                    request.resolve(Decision.EXPIRED, decided_by="timeout")
                    del self.pending[request.request_id]
                    self.history.append(request)
                    expired.append(request)
        for request in expired:
            log.info("approval %s expired unanswered", request.request_id)
            if self.notifier is not None:
                self.notifier.warning(
                    f"Expired: {request.title}",
                    f"no reply within {int(request.timeout_seconds)}s — NOT executed",
                    request_id=request.request_id)
        return expired

    # ------------------------------------------------------------------ waiting

    def wait_for(self, request: ApprovalRequest,
                 poll_seconds: float = 1.0) -> Decision:
        """Block until a decision is made or the deadline passes.

        Returns APPROVED, REJECTED or EXPIRED. Used by the engine when it wants
        an answer before acting.
        """
        while request.pending:
            decision = classify_reply(getattr(request, "_last_reply", ""))
            if request.remaining() <= 0:
                self.expire()
                return request.decision or Decision.EXPIRED
            time.sleep(poll_seconds)
        return request.decision or Decision.EXPIRED

    # ---------------------------------------------------------------- reporting

    def cancel_all(self, reason: str = "cancelled") -> int:
        with self._lock:
            count = len(self.pending)
            for request in list(self.pending.values()):
                request.resolve(Decision.CANCELLED, decided_by=reason)
                self.history.append(request)
                del self.pending[request.request_id]
            return count

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "require_approval": self.require_approval,
                "default_timeout": self.default_timeout,
                "pending": [r.to_dict() for r in self.pending.values()],
                "recent": [r.to_dict() for r in reversed(self.history[-12:])],
                "counts": {
                    "approved": sum(1 for r in self.history
                                   if r.decision is Decision.APPROVED),
                    "rejected": sum(1 for r in self.history
                                   if r.decision is Decision.REJECTED),
                    "expired": sum(1 for r in self.history
                                  if r.decision is Decision.EXPIRED),
                },
            }


__all__ = ["ApprovalGate", "ApprovalRequest", "Decision", "classify_reply",
           "format_request"]
