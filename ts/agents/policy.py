"""Conditional approval: ask a human only when the edge is not convincing.

Asking about every trade is as bad as asking about none. It trains you to approve
without reading, which is the failure mode this whole design exists to avoid.

So the system decides for itself when it is unsure, and only then interrupts you:

    strong edge      -> execute immediately, log it
    convincing       -> execute, notify afterwards
    marginal         -> ASK (this is the only case that costs you attention)
    suspicious       -> ASK, and say why

"Marginal" is deliberately measured on both expectancy *and* confidence, because
they fail differently. High expectancy with low confidence means the strategy
thinks it will win big but is not sure this setup is the one; low expectancy with
high confidence means it is sure, and sure it will lose. Both deserve a second
pair of eyes.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class AutoApprovePolicy:
    """Thresholds that decide whether a plan needs a human."""

    # above both of these, the system executes without asking
    min_expectancy: float = 0.35          # R units
    min_confidence: float = 0.75          # posterior probability

    # always ask, regardless of the numbers
    always_ask_short: bool = False
    always_ask_new_symbol: bool = True    # first trade in a new instrument
    max_auto_risk_pct: float = 0.02       # bigger than this needs a human
    ask_on_llm_disagreement: bool = True

    # symbol-specific overrides: {"BTCUSD": 0.6, "EURUSD": 0.8}
    expectancy_overrides: dict[str, float] = field(default_factory=dict)

    def clamp(self) -> "AutoApprovePolicy":
        self.min_expectancy = float(max(self.min_expectancy, 0.0))
        self.min_confidence = float(min(max(self.min_confidence, 0.5), 0.99))
        self.max_auto_risk_pct = float(max(self.max_auto_risk_pct, 0.001))
        return self

    def required_expectancy(self, symbol: str) -> float:
        return float(self.expectancy_overrides.get(symbol.upper(), self.min_expectancy))

    def evaluate(self, expectancy_r: float, confidence: float, *,
                 side: str = "long", symbol: str = "?", risk_pct: float = 0.0,
                 llm_objects: bool = False) -> tuple[bool, str, str]:
        """Decide whether this plan needs a human.

        Returns ``(needs_approval, tier, reason)`` where tier is one of
        ``auto``, ``auto_notify``, ``ask``.
        """
        self.clamp()
        required = self.required_expectancy(symbol)

        # --- hard rules first: these ask regardless of the numbers
        if self.always_ask_short and side == "short":
            return True, "ask", "shorts always require approval (configured)"
        if risk_pct > self.max_auto_risk_pct:
            return True, "ask", (f"risk {risk_pct:.2%} exceeds the "
                                 f"{self.max_auto_risk_pct:.2%} auto-approval ceiling")
        if self.ask_on_llm_disagreement and llm_objects:
            return True, "ask", "the analyst raised a concern about this setup"

        strong = expectancy_r >= required and confidence >= self.min_confidence

        if strong:
            tier = "auto_notify" if expectancy_r >= required * 1.8 else "auto"
            return False, tier, (f"expectancy {expectancy_r:+.2f}R and confidence "
                                 f"{confidence:.0%} clear the "
                                 f"{required:.2f}R / {self.min_confidence:.0%} bar")

        # --- below the bar: explain which half failed
        shortfall = []
        if expectancy_r < required:
            shortfall.append(f"expectancy {expectancy_r:+.2f}R below {required:.2f}R")
        if confidence < self.min_confidence:
            shortfall.append(f"confidence {confidence:.0%} below "
                             f"{self.min_confidence:.0%}")
        return True, "ask", "; ".join(shortfall)

    def to_dict(self) -> dict:
        return {
            "min_expectancy": self.min_expectancy,
            "min_confidence": self.min_confidence,
            "max_auto_risk_pct": self.max_auto_risk_pct,
            "always_ask_short": self.always_ask_short,
            "always_ask_new_symbol": self.always_ask_new_symbol,
            "ask_on_llm_disagreement": self.ask_on_llm_disagreement,
            "overrides": dict(self.expectancy_overrides),
        }


def first_seen_symbols(history: list[dict], symbol: str,
                       policy: AutoApprovePolicy) -> bool:
    """True if this symbol has no prior trade in the history."""
    if not policy.always_ask_new_symbol:
        return False
    return not any(str(record.get("symbol", "")).upper() == symbol.upper()
                   for record in history)


__all__ = ["AutoApprovePolicy", "first_seen_symbols"]
