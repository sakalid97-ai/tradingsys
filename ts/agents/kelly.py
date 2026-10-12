"""Kelly-criterion position sizing for directional, two-sided markets.

For a trade that risks ``a`` dollars to win ``b`` dollars with win probability
``p``, Kelly's optimal fraction of capital is::

    f* = (p * b - (1 - p) * a) / (a * b)

Equivalently, in the units this system actually trades in -- R multiples, where
the loss is 1R and the win is ``payoff`` R::

    f* = (p * payoff - (1 - p)) / payoff

This is the formula the sizer uses, because the strategy layer already thinks in
R and the risk system caps 1R per trade.

Three safeguards make it safe to actually run:

1. **Fractional Kelly.** Full Kelly maximises the expected *logarithm* of
   wealth while assuming p is known exactly. It is catastrophically sensitive to
   over-estimated win rates, and the error from over-betting is far larger than
   from under-betting. ``kelly_fraction`` (typically 0.2-0.35) is what real
   money uses.
2. **Credibility shrinkage.** When the Bayesian engine is uncertain, its
   probability is pulled toward the coin-flip baseline before sizing. Less
   confidence -> smaller K -> smaller size, automatically.
3. **Hard clamps.** Fraction of capital, a single-trade risk cap, a minimum edge,
   and never a negative fraction (shorting here is an explicit signal, not a
   Kelly side-effect).
"""

from __future__ import annotations

import math
from dataclasses import dataclass

MIN_PROBABILITY = 1e-6
MAX_PROBABILITY = 1.0 - 1e-6


@dataclass(frozen=True)
class KellyConfig:
    """Position sizing configuration."""

    kelly_fraction: float = 0.25        # fractional Kelly, in (0, 1]
    max_fraction: float = 0.05           # hard cap: fraction of capital at risk
    max_risk_per_trade: float = 0.01     # 1% of equity risked per trade
    min_edge: float = 0.03              # minimum p - breakeven_p to size up
    min_probability_edge: float = 0.02   # minimum win-rate edge over breakeven
    shrink_toward_coinflip: bool = True
    shrink_strength: float = 0.4        # 0 = raw model, 1 = pure 50/50
    bankroll: float = 10_000.0

    def __post_init__(self) -> None:
        if not (0.0 < self.kelly_fraction <= 1.0):
            raise ValueError("kelly_fraction must be in (0, 1]")
        if not (0.0 < self.max_fraction <= 1.0):
            raise ValueError("max_fraction must be in (0, 1]")
        if not (0.0 < self.max_risk_per_trade <= 0.2):
            raise ValueError("max_risk_per_trade must be in (0, 0.2]")
        if not (0.0 <= self.shrink_strength <= 1.0):
            raise ValueError("shrink_strength must be in [0, 1]")


@dataclass(frozen=True)
class KellyResult:
    """A sizing decision with every intermediate value kept for audit."""

    symbol: str
    side: str                    # "long" | "short" | "none"
    payoff: float                # R-multiple a win pays (0 for the coin-flip case)
    raw_probability: float
    shrunk_probability: float
    breakeven_p: float
    edge: float
    kelly_full: float
    kelly_fraction: float
    risk_dollars: float
    approved: bool
    reason: str = ""
    stop_distance: float = 0.0
    target_distance: float = 0.0

    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "side": self.side,
            "payoff": round(self.payoff, 3),
            "raw_p": round(self.raw_probability, 5),
            "shrunk_p": round(self.shrunk_probability, 5),
            "breakeven_p": round(self.breakeven_p, 5),
            "edge": round(self.edge, 5),
            "kelly_full": round(self.kelly_full, 5),
            "kelly_fraction": round(self.kelly_fraction, 5),
            "risk_dollars": round(self.risk_dollars, 2),
            "approved": self.approved,
            "reason": self.reason,
        }


def breakeven_probability(payoff: float) -> float:
    """Win rate at which a trade has zero expectancy.

    ``a = 1`` (risk 1R), ``b = payoff`` R, so ``p = a / (a + b) = 1/(1+payoff)``.
    """
    if payoff <= 0:
        return 1.0
    return 1.0 / (1.0 + payoff)


def kelly_fraction(p: float, payoff: float) -> float:
    """Full-Kelly fraction for a two-sided trade risking 1R to win ``payoff`` R."""
    if payoff <= 0:
        return 0.0
    p = min(max(p, 0.0), 1.0)
    return max(0.0, (p * payoff - (1.0 - p)) / payoff)


def shrink(p: float, strength: float, baseline: float = 0.5) -> float:
    """Pull a probability estimate toward a baseline when unsure."""
    if not 0.0 <= strength <= 1.0:
        raise ValueError("strength must be in [0, 1]")
    p = min(max(p, 0.0), 1.0)
    return p * (1.0 - strength) + baseline * strength


def confidence_shrink(confidence: float) -> float:
    """Translate P(direction correct) in [0.5, 1] into a Kelly shrinkage strength.

    0.5 (coin flip, no information) -> shrink hard toward the breakeven
    baseline; 1.0 (certain) -> shrink only the humility floor. Continuous and
    monotone, so partial evidence gets partial conviction.
    """
    confidence = min(max(confidence, 0.5), 1.0)
    strength = 0.35 * (1.0 - (confidence - 0.5) * 2.0)
    return 0.05 if strength < 0.05 else strength


class KellySizer:
    """Sizes directional trades from a probability estimate and an R payoff."""

    def __init__(self, config: KellyConfig | None = None) -> None:
        self.config = config or KellyConfig()

    def size_trade(
        self,
        symbol: str,
        side: str,
        payoff: float,
        probability: float,
        confidence: float = 1.0,
        stop_distance: float = 0.0,
        available_risk: float | None = None,
    ) -> KellyResult:
        """Size one directional trade.

        ``payoff`` is the win in R multiples (target distance / stop distance).
        ``probability`` is P(this trade hits its target before its stop).
        ``stop_distance`` is the price distance to the stop, used to translate
        the money risk into a quantity by the caller.
        """
        side = side.lower()
        if side not in ("long", "short"):
            return KellyResult(symbol, "none", payoff, probability, probability, 1.0, 0.0,
                               0.0, 0.0, 0.0, False,
                               f"side must be long or short, got {side!r}",
                               stop_distance, payoff * stop_distance)

        be = breakeven_probability(payoff)
        strength = confidence_shrink(confidence) if self.config.shrink_toward_coinflip else 0.0
        p_adj = shrink(probability, strength, baseline=be)
        edge = p_adj - be

        if edge < self.config.min_probability_edge:
            return KellyResult(
                symbol, "none", payoff, probability, p_adj, be, edge, 0.0, 0.0, 0.0, False,
                f"win-rate edge {edge:.4f} below minimum "
                f"{self.config.min_probability_edge:.4f} "
                f"(p={p_adj:.3f} vs breakeven {be:.3f})",
                stop_distance, payoff * stop_distance,
            )

        full = kelly_fraction(p_adj, payoff)
        if full <= 0.0:
            return KellyResult(
                symbol, "none", payoff, probability, p_adj, be, edge, 0.0, 0.0, 0.0, False,
                "no positive Kelly fraction", stop_distance, payoff * stop_distance,
            )

        fractional = min(full * self.config.kelly_fraction, self.config.max_fraction)
        fractional = max(fractional, 0.0)

        # The risk cap is the real governor: 1% of equity per trade. Kelly only
        # decides whether and how aggressively to lean into that budget.
        risk_cap = self.config.max_risk_per_trade
        risk_fraction = min(fractional, risk_cap)
        risk_dollars = round(risk_fraction * self.config.bankroll, 2)

        if available_risk is not None and risk_dollars > available_risk:
            risk_dollars = round(available_risk, 2)
            note = "capped by remaining portfolio risk"
        else:
            note = "sized by fractional Kelly"

        if risk_dollars < 1.0:
            return KellyResult(
                symbol, "none", payoff, probability, p_adj, be, edge, full, fractional, 0.0,
                False, "risk budget too small to cover one unit of risk",
                stop_distance, payoff * stop_distance,
            )

        return KellyResult(
            symbol=symbol, side=side, payoff=payoff,
            raw_probability=probability, shrunk_probability=p_adj,
            breakeven_p=be, edge=edge, kelly_full=full, kelly_fraction=fractional,
            risk_dollars=risk_dollars, approved=True, reason=note,
            stop_distance=stop_distance, target_distance=payoff * stop_distance,
        )

    def quantity_from_risk(
        self,
        kelly_result: KellyResult,
        instrument,
    ) -> float:
        """Convert a dollar risk budget into legal lots/coins for an instrument."""
        if not kelly_result.approved or kelly_result.stop_distance <= 0:
            return 0.0
        value_per_lot = instrument.contract_size * kelly_result.stop_distance
        if value_per_lot <= 0:
            return 0.0
        raw_lots = kelly_result.risk_dollars / value_per_lot
        return instrument.clamp_quantity(raw_lots)


__all__ = [
    "KellyConfig",
    "KellyResult",
    "KellySizer",
    "kelly_fraction",
    "breakeven_probability",
    "shrink",
    "confidence_shrink",
]
