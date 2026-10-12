"""Automated edge detection agent.

Combines a probability estimate with a payoff to decide whether a trade is worth
taking, then delegates *how big* to Kelly. Three gates, in order:

1. **Expectancy.** ``E = p * payoff - (1 - p) * 1`` must clear ``min_expectancy``
   in R units. This is the only number that matters for profitability and it is
   deliberately separate from probability: a 55% win rate at 1:1 is worth
   nothing; a 40% win rate at 3:1 is excellent.
2. **Cost coverage.** Every trade pays a fee and a stop; the payoff must clear
   them or the edge exists only on paper.
3. **Confidence.** The Bayesian engine must be reasonably sure of ``p``, or the
   trade is skipped. Sizing on a coin flip is how accounts die.
"""

from __future__ import annotations

from dataclasses import dataclass

from .base import Agent
from .bus import Topic
from .kelly import KellyConfig, KellySizer, breakeven_probability


@dataclass
class EdgeOpportunity:
    """A validated, sized opportunity ready for the execution agent."""

    symbol: str
    asset_class: str
    side: str
    price: float
    stop_distance: float
    target_distance: float
    payoff: float
    p_win: float
    expectancy_r: float
    breakeven_p: float
    confidence: float
    edge_grade: str
    kelly_risk: float
    quantity: float
    atr: float
    stale: bool
    reason: str = ""

    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "asset_class": self.asset_class,
            "side": self.side,
            "price": round(self.price, 6),
            "stop_distance": round(self.stop_distance, 6),
            "target_distance": round(self.target_distance, 6),
            "payoff": round(self.payoff, 3),
            "p_win": round(self.p_win, 5),
            "expectancy_r": round(self.expectancy_r, 4),
            "breakeven_p": round(self.breakeven_p, 5),
            "confidence": round(self.confidence, 3),
            "edge_grade": self.edge_grade,
            "kelly_risk": round(self.kelly_risk, 2),
            "quantity": round(self.quantity, 8),
            "atr": round(self.atr, 6),
            "stale": self.stale,
            "reason": self.reason,
        }


def expectancy_r(p_win: float, payoff: float) -> float:
    """Expected R for a trade risking 1R to win ``payoff`` R."""
    return p_win * payoff - (1.0 - p_win)


def grade_edge(expectancy: float, confidence: float) -> str:
    """Letter grade for a trade, used by the dashboard.

    A trade with non-positive expectancy is an **F** no matter how confident the
    model is -- confidence can never rescue a losing edge, because confidence is
    a statement about direction, not about whether the payoff compensates.

    Otherwise ``score`` blends expectancy (in R) with posterior confidence, and
    the thresholds were picked against the range the system actually produces:
    expectancy roughly 0.2-1.8R and confidence 0.6-1.0.
    """
    if expectancy <= 0.0:
        return "F"
    score = expectancy * 2.0 + (confidence - 0.5) * 4.0
    if score >= 2.5:
        return "A"
    if score >= 1.6:
        return "B"
    if score >= 0.8:
        return "C"
    if score > 0.0:
        return "D"
    return "F"


class EdgeDetectionAgent(Agent):
    """Finds and sizes opportunities; publishes TRADE_PLAN messages."""

    name = "edge_detector"
    role = "automated edge detection"
    subscribes = (Topic.MARKET_REGIME.value, Topic.PROBABILITY.value)

    def __init__(
        self,
        bus,
        sizer: KellySizer | None = None,
        kelly_config: KellyConfig | None = None,
        min_expectancy: float = 0.20,
        min_confidence: float = 0.65,
        atr_stop_mult: float = 2.0,
        atr_target_mult: float = 4.0,
        min_payoff: float = 1.0,
        max_payoff: float = 6.0,
    ) -> None:
        super().__init__(bus)
        self.sizer = sizer or KellySizer(kelly_config or KellyConfig())
        self.min_expectancy = min_expectancy
        self.min_confidence = min_confidence
        self.atr_stop_mult = atr_stop_mult
        self.atr_target_mult = atr_target_mult
        self.min_payoff = min_payoff
        self.max_payoff = max_payoff
        self.analysis: dict[str, dict] = {}
        self.probabilities: dict[str, dict] = {}
        self.plans: list[dict] = []
        self.subscribe_all()

    # ------------------------------------------------------------------ filtering

    def _build_opportunity(self, symbol: str, analysis: dict, prob: dict,
                           instrument) -> EdgeOpportunity | None:
        if analysis.get("stale"):
            return None
        if prob.get("note"):
            return None

        regime = int(analysis.get("regime", 0) or 0)
        if regime == 0:
            return None
        if not instrument.is_two_sided:
            return None

        atr = float(analysis.get("atr", 0.0) or 0.0)
        price = float(analysis.get("price", 0.0) or 0.0)
        if price <= 0 or atr <= 0:
            return None

        side = "long" if regime > 0 else "short"
        p_up = float(prob.get("p_up", 0.5) or 0.5)
        p_win = p_up if side == "long" else (1.0 - p_up)
        confidence = float(prob.get("confidence", 0.0) or 0.0)

        stop_distance = atr * self.atr_stop_mult
        target_distance = atr * self.atr_target_mult
        payoff = target_distance / stop_distance if stop_distance > 0 else 0.0
        payoff = max(self.min_payoff, min(self.max_payoff, payoff))

        exp = expectancy_r(p_win, payoff)
        be = breakeven_probability(payoff)
        grade = grade_edge(exp, confidence)

        expensive = (
            exp < self.min_expectancy
            or confidence < self.min_confidence
            or payoff <= 0
        )
        reason = ("expectancy and confidence clear thresholds"
                  if not expensive else
                  f"rejected: exp {exp:.3f}R (min {self.min_expectancy:.3f}), "
                  f"confidence {confidence:.2f} (min {self.min_confidence:.2f})")

        if expensive:
            return EdgeOpportunity(
                symbol=symbol, asset_class=instrument.asset_class.value, side=side,
                price=price, stop_distance=stop_distance, target_distance=target_distance,
                payoff=payoff, p_win=p_win, expectancy_r=exp, breakeven_p=be,
                confidence=confidence, edge_grade=grade, kelly_risk=0.0, quantity=0.0,
                atr=atr, stale=False, reason=reason,
            )

        kelly = self.sizer.size_trade(
            symbol=symbol, side=side, payoff=payoff, probability=p_win,
            confidence=float(prob.get("evidence_strength", confidence)),
            stop_distance=stop_distance,
            available_risk=self.sizer.config.bankroll * self.sizer.config.max_risk_per_trade,
        )
        if not kelly.approved:
            return EdgeOpportunity(
                symbol=symbol, asset_class=instrument.asset_class.value, side=side,
                price=price, stop_distance=stop_distance, target_distance=target_distance,
                payoff=payoff, p_win=p_win, expectancy_r=exp, breakeven_p=be,
                confidence=confidence, edge_grade=grade, kelly_risk=0.0, quantity=0.0,
                atr=atr, stale=False, reason=f"rejected by sizer: {kelly.reason}",
            )

        quantity = self.sizer.quantity_from_risk(kelly, instrument)
        return EdgeOpportunity(
            symbol=symbol, asset_class=instrument.asset_class.value, side=side,
            price=price, stop_distance=stop_distance, target_distance=target_distance,
            payoff=payoff, p_win=p_win, expectancy_r=exp, breakeven_p=be,
            confidence=confidence, edge_grade=grade,
            kelly_risk=kelly.risk_dollars, quantity=quantity, atr=atr, stale=False,
            reason=f"Kelly sized: risk {kelly.risk_dollars:.2f} ({kelly.kelly_fraction:.4f} "
                   f"of bankroll), {kelly.reason}",
        )

    # ------------------------------------------------------------------ handlers

    def handle(self, message) -> None:
        topic = str(message.topic)
        payload = message.payload or {}
        symbol = payload.get("symbol")
        if not symbol:
            return
        if topic == Topic.MARKET_REGIME.value:
            self.analysis[symbol] = payload
        elif topic == Topic.PROBABILITY.value:
            self.probabilities[symbol] = payload

    def run(self) -> None:
        from .instruments import resolve

        plans: list[dict] = []
        for symbol, analysis in self.analysis.items():
            prob = self.probabilities.get(symbol)
            if not prob:
                continue
            try:
                instrument = resolve(symbol)
            except Exception:  # noqa: BLE001
                continue
            opportunity = self._build_opportunity(symbol, analysis, prob, instrument)
            if opportunity is None:
                continue
            record = opportunity.to_dict()
            record["instrument"] = instrument.to_dict()
            plans.append(record)
            self.publish(Topic.TRADE_PLAN, record)

        self.plans = plans
        self.publish(Topic.AGENT_STATUS, {
            "agent": self.name,
            "opportunities": len(plans),
            "approved": sum(1 for p in plans if p["quantity"] > 0),
            "by_asset_class": _count(plans, "asset_class"),
        })


def _count(plans, key) -> dict[str, int]:
    out: dict[str, int] = {}
    for plan in plans:
        out[plan.get(key, "?")] = out.get(plan.get(key, "?"), 0) + 1
    return out


__all__ = ["EdgeDetectionAgent", "EdgeOpportunity", "expectancy_r", "grade_edge"]
