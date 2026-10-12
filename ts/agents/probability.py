"""Bayesian probability agent.

Maintains one :class:`BayesianEngine` per symbol over "will price move up into
the target before the stop", fed by evidence derived from the market analysis.

The mapping from analysis to evidence is where honesty matters, so each rule
states the assumption it is making. A Bayes factor built from a feature that is
already implied by the price would be circular reasoning, laundered into a
"posterior" -- so the evidence here uses only relationships that are genuinely
ex-ante: momentum persistence, trend quality, and breakout follow-through.
"""

from __future__ import annotations

from dataclasses import dataclass

from .base import Agent
from .bayesian import BayesianEngine, Evidence
from .bus import Topic


@dataclass
class EvidenceRule:
    """A named evidence source and the likelihood model behind it."""

    source: str
    base_rate: float           # P(evidence | Up) at neutral market conditions
    contrast_rate: float       # P(evidence | Down) at neutral conditions
    description: str = ""

    def probability(self, feature: float) -> tuple[float, float]:
        """Return (p_if_up, p_if_down) given a normalised feature in [-1, 1].

        The feature tilts both likelihoods in opposite directions around their
        base rates, clamped to (0.005, 0.995). This is a logistic-style tilt,
        which is monotone and keeps probabilities legal.
        """
        feature = max(-1.0, min(1.0, feature))
        shift = feature * 0.9
        p_up = _clamp(self.base_rate * (1.0 + shift))
        p_down = _clamp(self.contrast_rate * (1.0 - shift))
        return p_up, p_down


def _clamp(value: float, lo: float = 0.005, hi: float = 0.995) -> float:
    return max(lo, min(hi, value))


# The default belief set. These are priors about *how markets behave*, not
# fitted values: they are conservative and each is individually modest.
DEFAULT_RULES: tuple[EvidenceRule, ...] = (
    EvidenceRule("momentum", 0.52, 0.48,
                 "momentum persistence: short-term drift continues short-term"),
    EvidenceRule("trend_quality", 0.50, 0.44,
                 "a clean trend is more likely to continue than to reverse"),
    EvidenceRule("efficiency", 0.50, 0.46,
                 "directional movement out of total movement"),
    EvidenceRule("breakout", 0.53, 0.47,
                 "breakouts tend to follow through, not fail, in trending regimes"),
)


class ProbabilityAgent(Agent):
    """Keeps a Bayesian posterior per symbol and publishes PROBABILITY updates."""

    name = "probability"
    role = "bayesian inference"
    subscribes = (Topic.MARKET_REGIME.value, Topic.MARKET_DATA.value)

    def __init__(self, bus, rules=DEFAULT_RULES, alpha0: float = 2.0,
                 beta0: float = 2.0, decay: float = 0.995,
                 analyst_adapter=None, analyst_caller=None,
                 context_provider=None) -> None:
        super().__init__(bus)
        self.rules = rules
        self.alpha0 = alpha0
        self.beta0 = beta0
        self.decay = decay
        # --- optional LLM analyst layer; None means "price-only"
        self.analyst = analyst_adapter
        self.analyst_caller = analyst_caller
        self.context_provider = context_provider
        self.engines: dict[str, BayesianEngine] = {}
        self.latest: dict[str, dict] = {}
        self.subscribe_all()

    # ------------------------------------------------------------------ helpers

    def engine_for(self, symbol: str) -> BayesianEngine:
        if symbol not in self.engines:
            self.engines[symbol] = BayesianEngine(
                symbol, alpha0=self.alpha0, beta0=self.beta0, decay=self.decay)
        return self.engines[symbol]

    def _evidence_from_analysis(self, payload: dict) -> list[Evidence]:
        regime = int(payload.get("regime", 0) or 0)
        momentum = float(payload.get("momentum", 0.0) or 0.0)
        trend_quality = float(payload.get("trend_quality", 0.0) or 0.0)
        efficiency = float(payload.get("efficiency", 0.0) or 0.0)
        breakout_up = bool(payload.get("breakout_up", False))
        breakout_down = bool(payload.get("breakout_down", False))

        # Regime is the sign convention: in a downtrend the same absolute
        # strength is evidence for the *down* side, so flip the feature sign.
        #
        # `trend_quality` (R-squared) and `efficiency` (Kaufman ER) are
        # unsigned magnitudes in [0, 1] where higher means *more* trending --
        # they are used directly. `momentum` and `breakout` are already signed.
        sign = 1.0 if regime >= 0 else -1.0
        breakout_signal = 1.0 if (breakout_up and regime >= 0) else (
            -1.0 if (breakout_down and regime < 0) else 0.0)

        out: list[Evidence] = []
        for rule in self.rules:
            feature = {
                "momentum": momentum * sign,
                "trend_quality": trend_quality * sign,
                "efficiency": efficiency * sign,
                "breakout": breakout_signal,
            }.get(rule.source, 0.0)
            p_up, p_down = rule.probability(feature)
            out.append(Evidence(source=rule.source, p_if_up=p_up, p_if_down=p_down,
                                detail={"feature": round(feature, 3),
                                        "assumption": rule.description}))
        return out

    def _analyst_evidence(self, symbol: str, payload: dict) -> list[Evidence]:
        """Ask the optional LLM analyst layer for one more opinion.

        Returns an empty list when no analyst is configured, when the call
        fails, or when the model's response does not parse -- never a
        fabricated signal.
        """
        if self.analyst is None or self.analyst_caller is None:
            return []
        context = ""
        if self.context_provider is not None:
            try:
                context = self.context_provider(symbol, payload) or ""
            except Exception as exc:  # noqa: BLE001
                self.alarm(f"context provider failed for {symbol}: {exc}")
                return []
        evidence = self.analyst.analyse(symbol, context, self.analyst_caller)
        return [evidence] if evidence is not None else []

    # -------------------------------------------------------------------- cycle

    def run(self) -> None:
        for symbol, engine in self.engines.items():
            snapshot = engine.snapshot()
            if self.analyst is not None:
                snapshot["analyst"] = self.analyst.snapshot()
            self.latest[symbol] = snapshot
            self.publish(Topic.PROBABILITY, snapshot)

    def handle(self, message) -> None:
        if str(message.topic) == Topic.MARKET_DATA.value:
            # new data resets the day's engine for that symbol only if the
            # symbol's data was stale and has now refreshed
            return
        payload = message.payload or {}
        symbol = payload.get("symbol")
        if not symbol:
            return
        if payload.get("stale"):
            self.publish(Topic.PROBABILITY, {
                "symbol": symbol, "note": "stale data, posterior unchanged",
                "p_up": 0.5, "confidence": 0.0,
            })
            return

        engine = self.engine_for(symbol)
        evidence = self._evidence_from_analysis(payload)
        analyst_evidence = self._analyst_evidence(symbol, payload)
        evidence.extend(analyst_evidence)
        try:
            engine.update_many(evidence)
        except ValueError as exc:
            self.alarm(f"bad evidence for {symbol}: {exc}")
            return

        snapshot = engine.snapshot()
        snapshot["evidence"] = [e.describe() for e in evidence]
        snapshot["regime"] = payload.get("regime")
        if analyst_evidence:
            snapshot["analyst_log_odds"] = analyst_evidence[0].detail["log_odds"]
            snapshot["analyst_skill"] = analyst_evidence[0].detail["skill"]
        if self.analyst is not None:
            snapshot["analyst"] = self.analyst.snapshot()
        self.latest[symbol] = snapshot
        self.publish(Topic.PROBABILITY, snapshot)


__all__ = ["ProbabilityAgent", "EvidenceRule", "DEFAULT_RULES"]
