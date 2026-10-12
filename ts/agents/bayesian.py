"""Bayesian probability engine.

Maintains a Beta posterior over "the Up side wins" and applies evidence as
**Bayes factors**::

    posterior_odds = prior_odds x BF_1 x BF_2 x ... x BF_n

where ``BF = P(evidence | Up) / P(evidence | Down)``.

Why Bayes factors rather than point updates:

* **Composable and auditable.** Every adjustment is a multiplicative factor that
  can be logged, attributed to a source, and reverted. A dashboard can show
  *why* the model moved.
* **Sequential, which is exactly what a live market feed is.** Each new order
  book print is one more observation, and the posterior is the correct running
  answer -- no retraining, no windowing heuristics.
* **Confidence is a real number.** The Beta's *concentration* (alpha + beta)
  shrinks toward 0.5 as evidence accumulates in conflicting directions, so the
  agent can refuse to size a trade when it is not confident. That is the
  difference between a model and a coin flip.

Updating is the standard sequential Bayes-factor update: each piece of
evidence multiplies the odds, which means multiplying ``alpha`` and leaving
``beta`` alone::

    alpha' = BF x alpha          beta' = beta

This is intentionally *not* mass-preserving. Preserving total concentration
would rotate the mean without adding information, which produces a sharply
skewed Beta that reports ~95% confidence from only a handful of mild signals.
Multiplying alpha instead means confidence genuinely accumulates with evidence,
which is the correct behaviour for a sequential estimator watching a live feed.

``BF`` is clamped to [0.1, 10] so a single anomalous print cannot dominate the
posterior. ``decay`` < 1 applies per-step forgetting so stale evidence fades.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

MIN_CONCENTRATION = 0.05
MIN_PROBABILITY = 1e-6
MAX_PROBABILITY = 1.0 - 1e-6
BF_MIN = 0.1
BF_MAX = 10.0


@dataclass
class Evidence:
    """One observation and its likelihood ratio.

    ``p_if_up`` and ``p_if_down`` are the probabilities of observing this
    evidence in each world. They should come from a calibrated model, not from
    the same feature being measured -- otherwise the engine launders a guess
    into a "posterior".
    """

    source: str
    p_if_up: float
    p_if_down: float
    detail: dict = field(default_factory=dict)

    @property
    def bayes_factor(self) -> float:
        denom = self.p_if_down
        if denom <= 0.0:
            return math.inf if self.p_if_up > 0 else 1.0
        if self.p_if_up <= 0.0:
            return 0.0
        return self.p_if_up / denom

    def is_valid(self) -> bool:
        return (
            0.0 <= self.p_if_up <= 1.0
            and 0.0 <= self.p_if_down <= 1.0
            and (self.p_if_up > 0.0 or self.p_if_down > 0.0)
        )

    def describe(self) -> str:
        bf = self.bayes_factor
        direction = "supports UP" if bf > 1 else "supports DOWN"
        return f"{self.source}: {direction} (BF={bf:.3f})"


class BayesianEngine:
    """Running Beta posterior over P(Up), updated by Bayes factors."""

    def __init__(
        self,
        market_id: str,
        alpha0: float = 2.0,
        beta0: float = 2.0,
        decay: float = 1.0,
        source_independence: float = 0.6,
        max_concentration: float = 60.0,
        max_odds: float = 12.0,
    ) -> None:
        """``decay`` in (0, 1] applies per-step forgetting to prior evidence.

        ``source_independence`` in (0, 1] discounts correlated evidence sources
        (see :meth:`update_many`).
        """
        if alpha0 <= 0 or beta0 <= 0:
            raise ValueError("alpha0 and beta0 must be positive")
        if not (0.0 < decay <= 1.0):
            raise ValueError("decay must be in (0, 1]")
        if not (0.0 < source_independence <= 1.0):
            raise ValueError("source_independence must be in (0, 1]")
        self.market_id = market_id
        self.alpha = float(alpha0)
        self.beta = float(beta0)
        self.decay = float(decay)
        self.source_independence = float(source_independence)
        self.max_concentration = float(max_concentration)
        self.max_odds = float(max_odds)
        self.history: list[Evidence] = []
        self.steps = 0

    # ------------------------------------------------------------- posterior

    @property
    def concentration(self) -> float:
        """Total evidence mass; low means "barely informed"."""
        return self.alpha + self.beta

    @property
    def posterior_mean(self) -> float:
        """E[P(Up)] = alpha / (alpha + beta)."""
        return _clip(self.alpha / self.concentration)

    @property
    def posterior_odds(self) -> float:
        """Odds of Up over Down."""
        return math.inf if self.beta <= 0 else self.alpha / self.beta

    def confidence(self) -> float:
        """Probability the market is more likely Up than Down.

        This is a genuine posterior probability, not a heuristic score::

            confidence = P(p > 0.5)     when the posterior favours Up
                       = P(p < 0.5)     when the posterior favours Down

        computed from the Beta CDF at 0.5. It starts at exactly 0.5 for an
        uninformative Beta(2, 2) and only rises as evidence accumulates, so a
        threshold on it means "this many percent sure the direction is right".
        """
        from ts.quant import beta_cdf

        mean = self.posterior_mean
        if abs(mean - 0.5) < 1e-12:
            return 0.5
        p_gt_half = 1.0 - beta_cdf(0.5, self.alpha, self.beta)
        return p_gt_half if mean > 0.5 else 1.0 - p_gt_half

    def credible_interval(self, mass: float = 0.9) -> tuple[float, float]:
        """Equal-tailed credible interval via the Beta quantile approximation."""
        from ts.quant import beta_quantile

        lo = beta_quantile((1.0 - mass) / 2.0, self.alpha, self.beta)
        hi = beta_quantile(1.0 - (1.0 - mass) / 2.0, self.alpha, self.beta)
        return _clip(lo), _clip(hi)

    # ---------------------------------------------------------------- update

    def update(self, evidence: Evidence) -> float:
        """Fold in one piece of evidence. Returns the new posterior mean."""
        if not evidence.is_valid():
            raise ValueError(f"invalid evidence from {evidence.source!r}: "
                             f"p_if_up={evidence.p_if_up}, p_if_down={evidence.p_if_down}")
        bf = min(max(evidence.bayes_factor, BF_MIN), BF_MAX)

        if self.decay < 1.0:
            # forgetting: shrink both counts toward zero, which widens the
            # posterior without moving its mean
            self.alpha *= self.decay
            self.beta *= self.decay

        self.alpha = _clip(self.alpha * bf, floor=MIN_CONCENTRATION)
        self.beta = _clip(self.beta, floor=MIN_CONCENTRATION)

        self.history.append(evidence)
        self.steps += 1
        return self.posterior_mean

    def update_many(self, evidence) -> float:
        """Fold in several pieces of evidence from one observation.

        The evidence sources are **not independent** -- momentum, trend quality,
        efficiency and breakout status are all noisy views of the same underlying
        trend, so multiplying their Bayes factors as if they were independent
        double-counts and manufactures confidence.

        ``source_independence`` (< 1) damps the combined log-odds toward 1 to
        account for that correlation. With 4 correlated sources at 0.6, they
        behave like ~2.4 genuinely independent observations, which is far closer
        to the truth than treating them as 4.
        """
        items = list(evidence)
        if not items:
            return self.posterior_mean
        for item in items:
            if not item.is_valid():
                raise ValueError(f"invalid evidence from {item.source!r}")

        log_odds = sum(math.log(min(max(e.bayes_factor, 1e-9), math.inf))
                       for e in items)
        log_odds *= self.source_independence
        combined_bf = math.exp(log_odds) if log_odds != math.inf else math.inf
        combined_bf = min(max(combined_bf, BF_MIN), BF_MAX)

        for item in items:
            self.history.append(item)
        self.steps += len(items)

        if self.decay < 1.0:
            self.alpha *= self.decay
            self.beta *= self.decay

        self.alpha = _clip(self.alpha * combined_bf, floor=MIN_CONCENTRATION)
        self.beta = _clip(self.beta, floor=MIN_CONCENTRATION)
        self._cap_odds()
        self._cap_concentration()
        return self.posterior_mean

    def _cap_odds(self) -> None:
        """Bound how extreme the posterior mean may become.

        Independently of how much evidence accumulates, no posterior may claim
        better odds than ``max_odds``. Pinning p_up at 0.999 after a dozen
        cycles in a persistent trend is not a calibrated belief -- it is an
        artefact of a feature that keeps telling the same story. Rescaling alpha
        and beta to the cap preserves total evidence mass while restoring
        humility about the estimate.
        """
        odds = self.posterior_odds
        if not odds or odds == math.inf or odds <= self.max_odds:
            return
        total = self.concentration
        alpha = total * (self.max_odds / (1.0 + self.max_odds))
        beta = total - alpha
        self.alpha = max(alpha, MIN_CONCENTRATION)
        self.beta = max(beta, MIN_CONCENTRATION)

    def _cap_concentration(self) -> None:
        """Bound how confident the engine is allowed to become.

        Rescaling both counts by the same factor leaves the posterior *mean*
        untouched while capping how certain it can be.
        """
        total = self.concentration
        if total <= self.max_concentration:
            return
        scale = self.max_concentration / total
        self.alpha = max(self.alpha * scale, MIN_CONCENTRATION)
        self.beta = max(self.beta * scale, MIN_CONCENTRATION)

    # ------------------------------------------------------------- utilities

    def reset(self, alpha0: float | None = None, beta0: float | None = None) -> None:
        if alpha0 is not None:
            self.alpha = float(alpha0)
        if beta0 is not None:
            self.beta = float(beta0)
        self.history.clear()
        self.steps = 0

    def snapshot(self) -> dict:
        lo, hi = self.credible_interval()
        return {
            "symbol": self.market_id,      # consumers key on `symbol`
            "market_id": self.market_id,
            "p_up": round(self.posterior_mean, 5),
            "odds": round(self.posterior_odds, 4) if self.posterior_odds != math.inf else None,
            "credible_90": [round(lo, 5), round(hi, 5)],
            "concentration": round(self.concentration, 3),
            "confidence": round(self.confidence(), 4),
            # continuous 0..1 measure of how much evidence has accumulated;
            # used for Kelly shrinkage, where the binary `confidence` is useless
            "evidence_strength": round(min(1.0, self.concentration / self.max_concentration), 4),
            "steps": self.steps,
            "alpha": round(self.alpha, 4),
            "beta": round(self.beta, 4),
        }

    def __repr__(self) -> str:  # pragma: no cover
        return (f"BayesianEngine({self.market_id}, p_up={self.posterior_mean:.4f}, "
                f"conf={self.confidence():.3f}, n={self.steps})")


def _clip(value: float, floor: float = 0.0, ceil: float = math.inf) -> float:
    if value != value:  # NaN
        return floor
    return max(floor, min(ceil, value))


def log_odds_from_probability(p: float) -> float:
    """Natural log-odds, with saturation to avoid infinities."""
    p = min(max(p, MIN_PROBABILITY), MAX_PROBABILITY)
    return math.log(p / (1.0 - p))


def probability_from_log_odds(lo: float) -> float:
    return 1.0 / (1.0 + math.exp(-lo))


def combine_posteriors(engines, weights=None) -> tuple[float, float]:
    """Pool several Beta posteriors into one (alpha, beta).

    Weighted mean of the means and the weighted harmonic-mean concentration,
    which correctly pools independent Beta experts without over-stating
    confidence when they disagree.
    """
    engines = list(engines)
    if not engines:
        raise ValueError("no engines to combine")
    weights = weights or [1.0] * len(engines)
    total_w = float(sum(weights))
    if total_w <= 0:
        raise ValueError("weights must sum to a positive number")

    mean = sum(e.posterior_mean * w for e, w in zip(engines, weights)) / total_w
    concentration = sum(e.concentration * w for e, w in zip(engines, weights)) / total_w
    mean = _clip(mean, floor=MIN_PROBABILITY, ceil=MAX_PROBABILITY)
    alpha = mean * concentration
    beta = (1.0 - mean) * concentration
    return alpha, beta


__all__ = [
    "BayesianEngine",
    "Evidence",
    "combine_posteriors",
    "log_odds_from_probability",
    "probability_from_log_odds",
]
