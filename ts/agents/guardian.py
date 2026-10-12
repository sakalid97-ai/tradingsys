"""Risk guardian agent.

The only agent that can veto everyone else. Watches realised and imaginary
exposure and publishes a RISK alert that flattens and halts the system when any
limit is breached. It subscribes to no market reasoning at all -- deliberately,
because it must not be swayed by a compelling opportunity.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .base import Agent
from .bus import Topic


@dataclass
class RiskLimits:
    max_positions: int = 6
    max_open_risk_pct: float = 0.05       # 5% of capital at risk at once
    max_single_trade_pct: float = 0.02    # 2% on any one trade
    max_daily_loss_pct: float = 0.04      # -4% on the session -> flatten
    max_drawdown_pct: float = 0.12        # -12% from peak -> freeze entries
    min_confidence: float = 0.30
    max_orders_per_cycle: int = 3

    def to_dict(self) -> dict:
        return {
            "max_positions": self.max_positions,
            "max_open_risk_pct": self.max_open_risk_pct,
            "max_single_trade_pct": self.max_single_trade_pct,
            "max_daily_loss_pct": self.max_daily_loss_pct,
            "max_drawdown_pct": self.max_drawdown_pct,
            "min_confidence": self.min_confidence,
            "max_orders_per_cycle": self.max_orders_per_cycle,
        }


@dataclass
class RiskState:
    equity: float = 0.0
    peak_equity: float = 0.0
    day_start_equity: float = 0.0
    open_risk: float = 0.0
    n_positions: int = 0
    halted: bool = False
    halt_reason: str = ""
    breaches: list[str] = field(default_factory=list)

    @property
    def drawdown(self) -> float:
        if self.peak_equity <= 0:
            return 0.0
        return max(0.0, (self.peak_equity - self.equity) / self.peak_equity)

    @property
    def day_pl(self) -> float:
        base = self.day_start_equity or self.equity
        return (self.equity - base) / base if base else 0.0

    @property
    def open_risk_pct(self) -> float:
        return self.open_risk / self.equity if self.equity else 0.0

    def to_dict(self) -> dict:
        return {
            "equity": round(self.equity, 2),
            "peak_equity": round(self.peak_equity, 2),
            "drawdown": round(self.drawdown, 5),
            "day_pl": round(self.day_pl, 5),
            "open_risk_pct": round(self.open_risk_pct, 5),
            "n_positions": self.n_positions,
            "halted": self.halted,
            "halt_reason": self.halt_reason,
            "breaches": self.breaches[-10:],
        }


class RiskGuardianAgent(Agent):
    """The circuit breaker. Subscribes to position/plan activity only."""

    name = "guardian"
    role = "portfolio risk controls"
    subscribes = (Topic.POSITION.value, Topic.TRADE_PLAN.value, Topic.RISK.value)

    def __init__(self, bus, limits: RiskLimits | None = None,
                 broker=None, kill_switch=None) -> None:
        super().__init__(bus)
        self.limits = limits or RiskLimits()
        self.broker = broker
        self.kill_switch = kill_switch
        self.state = RiskState()
        self.plans: dict[str, dict] = {}
        self.subscribe_all()

    # ----------------------------------------------------------------- handlers

    def handle(self, message) -> None:
        topic = str(message.topic)
        payload = message.payload or {}
        if topic == Topic.TRADE_PLAN.value:
            self.plans[payload.get("symbol", "?")] = payload
            return
        if topic == Topic.POSITION.value and payload.get("closed"):
            self.plans.pop(payload.get("symbol"), None)
            return
        if topic == Topic.RISK.value and payload.get("halt"):
            self._halt(str(payload.get("reason", "external risk signal")))

    # -------------------------------------------------------------------- checks

    def refresh_equity(self) -> None:
        if self.broker is None:
            return
        try:
            account = self.broker.account()
        except Exception:  # noqa: BLE001
            return
        equity = float(account.get("equity", 0.0) or 0.0)
        if equity <= 0:
            return
        if self.state.day_start_equity == 0.0:
            self.state.day_start_equity = equity
        self.state.equity = equity
        self.state.peak_equity = max(self.state.peak_equity, equity)

    def evaluate(self) -> list[str]:
        """Return a list of breaches. Never raises."""
        breaches: list[str] = []
        state = self.state
        limits = self.limits

        if state.n_positions >= limits.max_positions:
            breaches.append(f"position limit ({state.n_positions}/{limits.max_positions})")
        if state.open_risk_pct > limits.max_open_risk_pct:
            breaches.append(f"open risk {state.open_risk_pct:.2%} over "
                            f"{limits.max_open_risk_pct:.2%}")
        if state.day_pl <= -limits.max_daily_loss_pct:
            breaches.append(f"daily loss {state.day_pl:.2%} over "
                            f"{limits.max_daily_loss_pct:.2%}")
        if state.drawdown >= limits.max_drawdown_pct:
            breaches.append(f"drawdown {state.drawdown:.2%} over "
                            f"{limits.max_drawdown_pct:.2%}")

        for symbol, plan in self.plans.items():
            risk = float(plan.get("kelly_risk", 0.0) or 0.0)
            if risk > 0 and state.equity > 0:
                if risk / state.equity > limits.max_single_trade_pct:
                    breaches.append(f"{symbol} single-trade risk over "
                                    f"{limits.max_single_trade_pct:.2%}")
            if float(plan.get("confidence", 0.0) or 0.0) < limits.min_confidence:
                breaches.append(f"{symbol} confidence below {limits.min_confidence:.2f}")
        return breaches

    def _halt(self, reason: str) -> None:
        if self.state.halted:
            return
        self.state.halted = True
        self.state.halt_reason = reason
        self.state.breaches.append(reason)
        self.publish(Topic.RISK, {"halt": True, "reason": reason,
                                  "agent": self.name})
        self.publish(Topic.LOG, {"agent": self.name, "halt": reason})
        if self.kill_switch:
            try:
                self.kill_switch(reason)
            except Exception:  # noqa: BLE001
                self.log.exception("kill switch failed")

    # --------------------------------------------------------------------- run

    def run(self) -> None:
        self.refresh_equity()
        self.state.n_positions = len(self.plans)
        self.state.open_risk = sum(float(p.get("kelly_risk", 0.0) or 0.0)
                                   for p in self.plans.values())
        breach_count = len(self.evaluate())
        self.publish(Topic.AGENT_STATUS, {
            "agent": self.name,
            "risk": self.state.to_dict(),
            "limits": self.limits.to_dict(),
            "breach_count": breach_count,
        })

    def approve_plan(self, plan: dict) -> tuple[bool, str]:
        """Gate a single plan. Used by the supervisor before execution.

        Rejects anything the edge detector already declined to size. Checking
        here as well as at the call site is deliberate: a plan with no position
        size has no trade to approve, and letting it through would report a
        losing idea as "approved".
        """
        state = self.state
        if state.halted:
            return False, f"risk halt active: {state.halt_reason}"
        if not plan.get("quantity"):
            reason = plan.get("reason", "not sized") or "not sized"
            return False, f"edge detector did not size this ({reason[:60]})"
        risk = float(plan.get("kelly_risk", 0.0) or 0.0)
        if state.equity <= 0:
            return True, "equity unknown; trusting the sizer's own caps"
        if risk / state.equity > self.limits.max_single_trade_pct:
            return False, (f"single-trade risk {risk / state.equity:.2%} over "
                           f"{self.limits.max_single_trade_pct:.2%}")
        if state.n_positions >= self.limits.max_positions:
            return False, f"position limit {state.n_positions}/{self.limits.max_positions}"
        if state.open_risk_pct + risk / state.equity > self.limits.max_open_risk_pct:
            return False, (f"open risk would reach "
                           f"{state.open_risk_pct + risk / state.equity:.2%}, over "
                           f"{self.limits.max_open_risk_pct:.2%}")
        return True, "approved"


__all__ = ["RiskGuardianAgent", "RiskLimits", "RiskState"]
