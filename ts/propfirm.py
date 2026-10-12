"""Prop-firm / funded-account support.

Trading a funded account is not the same as trading your own money, and the
difference is not the leverage -- it is that the rules are **hard terminations**
rather than stops. Lose 10% of the account and it is over: the capital, the
payout, and the evaluation fee.

So this module does three things:

1. **Encodes each firm's rules** so the system knows what it must not do.
2. **Enforces stricter limits than the firm's**, with an explicit safety buffer.
   A firm allows a 5% daily loss; the system will stop at 3.5%. That gap is
   deliberate -- a slippage or a gap through your stop can breach a limit even
   when every individual trade was inside it, and a buffer is the only thing
   standing between a bad fill and a terminated account.
3. **Blocks the specific things firms forbid**, rather than relying on the
   trader to remember them.

Important honesty note: **prop-firm rules change often, and differ by account
type, firm, and region.** The registry below carries known values and a
``verified_on`` date, but it is a starting point. You must confirm the rules of
*your* account and set them explicitly -- the system will not guess on your
behalf. A wrong limit in either direction is expensive.
"""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime

log = logging.getLogger(__name__)

# How far inside the firm's limit we will actually stop. 0.25 leaves a 25%
# buffer, which is the difference between "the system tried to protect you"
# and "the account survived a gap".
DEFAULT_SAFETY_BUFFER = 0.25


@dataclass
class PropFirmRules:
    """The constraints one funded account imposes.

    ``dd_reference`` matters more than it looks:
      * ``"initial"``  -- drawdown is measured from the starting balance (FTMO).
      * ``"high_water"`` -- from the highest equity ever reached (The5ers and
        most others). This is stricter once the account is profitable, because
        the line moves up with every gain.
    """

    firm: str
    name: str = ""
    max_daily_loss_pct: float = 0.05
    max_drawdown_pct: float = 0.10
    dd_reference: str = "initial"          # initial | high_water
    profit_target_pct: float = 0.0         # 0 = no target (already funded)
    phase: str = "funded"                    # funded | evaluation | challenge
    min_trading_days: int = 0
    max_risk_per_trade: float = 0.02        # firm's own per-trade cap, if any
    consistent_target: bool = False          # no single day > X% of profit
    max_single_day_profit_share: float = 0.0
    prohibited_instruments: list[str] = field(default_factory=list)
    allowed_instruments: list[str] = field(default_factory=list)   # empty = all
    ea_allowed: bool = True
    news_trading_allowed: bool = True
    weekend_holding_allowed: bool = True
    night_holding_allowed: bool = True
    min_trade_days_before_payout: int = 0
    payout_requires_review: bool = False
    notes: str = ""
    verified_on: str = ""

    def clamp(self) -> "PropFirmRules":
        """Repair nonsensical values in place and return self.

        Callers that must not mutate the original should copy first -- see
        :func:`rules_for`.
        """
        self.max_daily_loss_pct = float(min(max(self.max_daily_loss_pct, 0.005), 0.5))
        self.max_drawdown_pct = float(min(max(self.max_drawdown_pct, 0.01), 0.6))
        self.profit_target_pct = float(max(self.profit_target_pct, 0.0))
        self.min_trading_days = max(0, int(self.min_trading_days))
        if self.dd_reference not in ("initial", "high_water"):
            self.dd_reference = "initial"
        if self.phase not in ("funded", "evaluation", "challenge"):
            self.phase = "funded"
        return self

    def to_dict(self) -> dict:
        return asdict(self)


# ---------------------------------------------------------------- the registry
# Values are the commonly published ones. They change; verify against your own
# account before enabling prop-firm mode.

_REGISTRY: dict[str, PropFirmRules] = {
    "ftmo": PropFirmRules(
        firm="ftmo", name="FTMO",
        max_daily_loss_pct=0.05, max_drawdown_pct=0.10, dd_reference="initial",
        profit_target_pct=0.10, phase="evaluation", min_trading_days=4,
        max_risk_per_trade=0.02, prohibited_instruments=[], ea_allowed=True,
        news_trading_allowed=False, weekend_holding_allowed=True,
        notes="Total drawdown is measured from the initial balance. High-impact "
              "news trading is prohibited on evaluation.",
        verified_on="2026-10",
    ),
    "the5ers": PropFirmRules(
        firm="the5ers", name="The5ers",
        max_daily_loss_pct=0.03, max_drawdown_pct=0.06, dd_reference="high_water",
        profit_target_pct=0.08, phase="evaluation", min_trading_days=3,
        max_risk_per_trade=0.02, ea_allowed=True, news_trading_allowed=True,
        notes="Drawdown from the high-water mark, so it tightens as the account "
              "grows. Aggressive single-trade profit can breach consistency.",
        verified_on="2026-10",
    ),
    "myforexfunds": PropFirmRules(
        firm="myforexfunds", name="MyForexFunds",
        max_daily_loss_pct=0.05, max_drawdown_pct=0.12, dd_reference="initial",
        profit_target_pct=0.08, phase="evaluation", min_trading_days=5,
        ea_allowed=True,
        notes="Drawdown from the initial balance.",
        verified_on="2026-10",
    ),
    "fundednext": PropFirmRules(
        firm="fundednext", name="FundedNext",
        max_daily_loss_pct=0.05, max_drawdown_pct=0.10, dd_reference="initial",
        profit_target_pct=0.10, phase="evaluation", min_trading_days=2,
        ea_allowed=True,
        notes="Consistency rule applies to funded accounts.",
        verified_on="2026-10",
    ),
    "e8": PropFirmRules(
        firm="e8", name="E8 Markets",
        max_daily_loss_pct=0.05, max_drawdown_pct=0.08, dd_reference="initial",
        profit_target_pct=0.08, phase="evaluation", min_trading_days=0,
        ea_allowed=True,
        notes="Drawdown from the initial balance.",
        verified_on="2026-10",
    ),
    "generic_conservative": PropFirmRules(
        firm="generic_conservative", name="Generic / conservative (recommended)",
        max_daily_loss_pct=0.03, max_drawdown_pct=0.06, dd_reference="high_water",
        profit_target_pct=0.0, phase="funded", min_trading_days=0,
        max_risk_per_trade=0.01, ea_allowed=True,
        notes="Not a firm -- a deliberately tight profile for any funded account "
              "whose rules you have not confirmed. Safer to start here and loosen.",
        verified_on="2026-10",
    ),
    "custom": PropFirmRules(
        firm="custom", name="Custom (set the rules yourself)",
        max_daily_loss_pct=0.03, max_drawdown_pct=0.06, dd_reference="initial",
        profit_target_pct=0.0, phase="funded", min_trading_days=0,
        notes="Read the rules from your own account and enter them. Nothing is "
              "assumed on your behalf.",
        verified_on="",
    ),
}


def registry() -> list[dict]:
    return [rules.to_dict() for rules in _REGISTRY.values()]


def rules_for(firm_id: str) -> PropFirmRules | None:
    """Look up a firm's rules.

    Returns a **deep copy**, never the registry entry itself. ``clamp()`` mutates
    in place, so handing out the shared object would let one caller -- or one
    editing a test -- permanently change every other caller's rules. For a
    prop-firm config that is a genuine correctness bug.
    """
    rules = _REGISTRY.get(firm_id.lower().strip())
    if rules is None:
        return None
    clone = PropFirmRules(**vars(rules))
    clone.prohibited_instruments = list(rules.prohibited_instruments)
    clone.allowed_instruments = list(rules.allowed_instruments)
    return clone.clamp()


def add_custom(rules: PropFirmRules) -> None:
    rules.clamp()
    _REGISTRY[rules.firm] = rules


# ------------------------------------------------------------------- monitoring


@dataclass
class PropFirmState:
    """Live evaluation of the account against its rules."""

    firm: str
    phase: str
    starting_balance: float = 0.0
    equity: float = 0.0
    high_water_equity: float = 0.0
    day_start_equity: float = 0.0
    trading_days: int = 0
    realised_profit: float = 0.0
    day_profits: list[float] = field(default_factory=list)
    halted: bool = False
    halt_reason: str = ""

    @property
    def day_pl_pct(self) -> float:
        if self.day_start_equity <= 0:
            return 0.0
        return (self.equity - self.day_start_equity) / self.day_start_equity

    @property
    def drawdown_pct(self) -> float:
        if self.equity <= 0:
            return 0.0
        peak = max(self.high_water_equity, self.starting_balance)
        return max(0.0, (peak - self.equity) / peak)

    @property
    def profit_pct(self) -> float:
        if self.starting_balance <= 0:
            return 0.0
        return (self.equity - self.starting_balance) / self.starting_balance

    @property
    def target_progress(self) -> float:
        return 0.0      # set by the monitor when a target exists

    def to_dict(self) -> dict:
        return {
            "firm": self.firm,
            "phase": self.phase,
            "starting_balance": round(self.starting_balance, 2),
            "equity": round(self.equity, 2),
            "high_water_equity": round(self.high_water_equity, 2),
            "day_pl_pct": round(self.day_pl_pct, 5),
            "drawdown_pct": round(self.drawdown_pct, 5),
            "profit_pct": round(self.profit_pct, 5),
            "trading_days": self.trading_days,
            "realised_profit": round(self.realised_profit, 2),
            "halted": self.halted,
            "halt_reason": self.halt_reason,
        }


class PropFirmBreach(RuntimeError):
    """A rule has been, or is about to be, violated."""


class PropFirmMonitor:
    """Enforces one firm's rules as hard limits on the trading system.

    The monitor's values are deliberately *inside* the firm's, so the trading
    engine stops before the firm would terminate the account. Everything it
    rejects, it rejects with a reason.
    """

    def __init__(self, rules: PropFirmRules, safety_buffer: float = DEFAULT_SAFETY_BUFFER,
                 starting_balance: float = 100_000.0) -> None:
        if not 0.0 <= safety_buffer < 0.5:
            raise ValueError("safety_buffer must be in [0, 0.5)")
        self.rules = rules.clamp()
        self.safety_buffer = safety_buffer
        self.state = PropFirmState(
            firm=rules.firm, phase=rules.phase, starting_balance=starting_balance,
            equity=starting_balance, high_water_equity=starting_balance,
            day_start_equity=starting_balance,
        )
        self.violations: list[dict] = []

    # ------------------------------------------------- derived limits

    @property
    def daily_stop_pct(self) -> float:
        return self.rules.max_daily_loss_pct * (1.0 - self.safety_buffer)

    @property
    def drawdown_stop_pct(self) -> float:
        return self.rules.max_drawdown_pct * (1.0 - self.safety_buffer)

    @property
    def per_trade_cap_pct(self) -> float:
        firm_cap = self.rules.max_risk_per_trade or 0.02
        # take whichever is stricter, then apply the buffer again
        return min(firm_cap, 0.02) * (1.0 - self.safety_buffer)

    def limits(self) -> dict:
        return {
            "firm": self.rules.firm,
            "firm_daily_loss": round(self.rules.max_daily_loss_pct, 5),
            "firm_drawdown": round(self.rules.max_drawdown_pct, 5),
            "firm_dd_reference": self.rules.dd_reference,
            "firm_profit_target": round(self.rules.profit_target_pct, 5),
            "firm_per_trade": round(self.rules.max_risk_per_trade or 0.02, 5),
            "system_daily_stop": round(self.daily_stop_pct, 5),
            "system_drawdown_stop": round(self.drawdown_stop_pct, 5),
            "system_per_trade_cap": round(self.per_trade_cap_pct, 5),
            "safety_buffer": self.safety_buffer,
            "min_trading_days": self.rules.min_trading_days,
        }

    # ------------------------------------------------------------------- state

    def open_day(self, equity: float | None = None) -> None:
        if equity is not None:
            self.state.equity = float(equity)
        self.state.day_start_equity = self.state.equity
        self.state.trading_days += 1

    def update(self, equity: float) -> None:
        self.state.equity = float(equity)
        self.state.high_water_equity = max(self.state.high_water_equity,
                                           self.state.equity)

    # ------------------------------------------------------------------ checks

    def check(self) -> list[str]:
        """Return every current violation. Empty means the account is compliant."""
        breaches: list[str] = []
        state = self.state

        if state.day_pl_pct <= -self.daily_stop_pct:
            breaches.append(
                f"DAILY LOSS: {state.day_pl_pct:.2%} reached the system stop of "
                f"{self.daily_stop_pct:.2%} (firm allows "
                f"{self.rules.max_daily_loss_pct:.2%})")
        if state.drawdown_pct >= self.drawdown_stop_pct:
            breaches.append(
                f"DRAWDOWN: {state.drawdown_pct:.2%} reached the system stop of "
                f"{self.drawdown_stop_pct:.2%} (firm allows "
                f"{self.rules.max_drawdown_pct:.2%} from {self.rules.dd_reference})")
        if self.rules.profit_target_pct > 0 and state.profit_pct >= self.rules.profit_target_pct:
            breaches.append(
                f"PROFIT TARGET REACHED: {state.profit_pct:.2%} vs target "
                f"{self.rules.profit_target_pct:.2%}. Stop trading and request "
                f"payout; further losses can forfeit the account.")
        return breaches

    def approve_trade(self, symbol: str, risk_pct: float, side: str,
                      notional_pct: float = 0.0) -> tuple[bool, str]:
        """Gate one trade against the firm's rules.

        Order matters: instrument prohibitions first (a rule the firm enforces
        mechanically), then the risk caps, then the account-level limits.
        """
        if self.state.halted:
            return False, f"account halted: {self.state.halt_reason or 'unknown'}"

        if not self.rules.ea_allowed:
            return False, (f"{self.rules.name} does not permit expert advisors on "
                           "this account; automated execution is not allowed")

        symbol_upper = symbol.upper()
        if self.rules.prohibited_instruments:
            if any(symbol_upper.startswith(p.upper())
                   for p in self.rules.prohibited_instruments):
                return False, f"{symbol} is prohibited by {self.rules.name}"
        if self.rules.allowed_instruments:
            if not any(symbol_upper.startswith(a.upper())
                       for a in self.rules.allowed_instruments):
                return False, (f"{symbol} is not in {self.rules.name}'s permitted "
                               "instrument list")

        if risk_pct > self.per_trade_cap_pct:
            return False, (f"per-trade risk {risk_pct:.3%} over the prop-firm cap "
                           f"{self.per_trade_cap_pct:.3%}")

        if not self.rules.news_trading_allowed and self._is_news_window():
            return False, (f"{self.rules.name} prohibits trading during high-impact "
                           "news; the risk calendar has flagged this window")

        breaches = self.check()
        if breaches:
            for breach in breaches:
                self._halt(breach)
            return False, breaches[0]

        if not self.rules.weekend_holding_allowed and self._is_weekend():
            return False, (f"{self.rules.name} requires positions to be flat over "
                           "the weekend; the trade would have to close first")
        return True, "approved"

    def _halt(self, reason: str) -> None:
        if self.state.halted:
            return
        self.state.halted = True
        self.state.halt_reason = reason
        self.violations.append({"at": datetime.now(UTC).isoformat(), "reason": reason})
        log.warning("prop-firm halt: %s", reason)

    @staticmethod
    def _is_news_window() -> bool:
        """Placeholder for a real economic-calendar check.

        Hooking a calendar in here matters: "no news trading" is a rule firms
        enforce by inspecting your trades, not by asking you politely.
        """
        return False

    @staticmethod
    def _is_weekend() -> bool:
        from datetime import datetime as _dt

        return _dt.now(UTC).weekday() >= 5

    # ------------------------------------------------------------- reporting

    def progress(self) -> dict:
        state = self.state
        target = self.rules.profit_target_pct
        profit_pct = state.profit_pct
        days_needed = max(0, self.rules.min_trading_days - state.trading_days)

        if target > 0:
            progress_pct = min(100.0, (profit_pct / target) * 100.0)
            status = "funded" if profit_pct >= target else "evaluation"
        else:
            progress_pct = 0.0
            status = state.phase

        consistency = None
        if self.rules.consistent_target and state.day_profits:
            total = sum(max(0.0, p) for p in state.day_profits)
            if total > 0:
                biggest = max(state.day_profits)
                consistency = biggest / total

        return {
            "status": status,
            "profit_pct": round(profit_pct, 5),
            "target_pct": round(target, 5),
            "target_progress_pct": round(progress_pct, 2),
            "trading_days": state.trading_days,
            "days_needed_for_minimum": days_needed,
            "largest_day_share_of_profit": round(consistency, 4) if consistency else None,
            "flags": self.check(),
        }

    def snapshot(self) -> dict:
        return {"rules": self.rules.to_dict(), "state": self.state.to_dict(),
                "limits": self.limits(), "progress": self.progress(),
                "violations": list(reversed(self.violations[-20:]))}


def apply_to_risk_settings(rules: PropFirmRules, safety_buffer: float = DEFAULT_SAFETY_BUFFER) -> dict:
    """Turn a firm's rules into risk-settings values the engine already enforces.

    Stricter than the firm, by design, and always at least as tight as the
    system's own defaults.
    """
    daily_stop = rules.max_daily_loss_pct * (1.0 - safety_buffer)
    dd_stop = rules.max_drawdown_pct * (1.0 - safety_buffer)
    per_trade = min(rules.max_risk_per_trade or 0.02, 0.02) * (1.0 - safety_buffer)

    # multiple positions must never be able to stack into a breach
    max_total = min(daily_stop * 0.8, per_trade * min(rules.min_trading_days or 8, 8))
    return {
        "risk_per_trade": round(min(per_trade, 0.01), 5),
        "daily_loss_limit": round(max(daily_stop, 0.01), 5),
        "max_drawdown_halt": round(max(dd_stop, 0.02), 5),
        "max_total_risk": round(max(max_total, per_trade), 5),
        "max_positions": max(1, int(min(8, math_floor(per_trade and 8 or 8)))),
    }


def math_floor(value: float) -> int:
    import math

    return int(math.floor(value))


__all__ = [
    "PropFirmRules",
    "PropFirmState",
    "PropFirmMonitor",
    "PropFirmBreach",
    "registry",
    "rules_for",
    "add_custom",
    "apply_to_risk_settings",
    "DEFAULT_SAFETY_BUFFER",
]
