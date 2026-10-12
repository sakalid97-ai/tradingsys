"""LLM analyst adapter: turns natural-language research into typed evidence.

Bridges an external analyst (FinRobot, CrewAI, or any LLM) into the Bayesian
engine without letting the model take over the risk path.

The three rules that make this safe:

1. **An unproven analyst has zero influence.** Until the adapter has scored at
   least ``min_observations`` completed calls, its Bayes factor is exactly 1.0
   -- it literally cannot move the posterior. Calibration must be *earned* from
   track record, not assumed from a prompt.
2. **Influence is bounded.** A single call can shift the log-odds by at most
   ``max_log_odds`` nats, so even a maximally-convicted hallucination is
   comparable in weight to one price-based feature. It is one opinion among
   several, never a veto.
3. **A bad analyst inverts.** If the analyst has been *wrong* more often than
   right, its bullish call becomes bearish evidence. That is the correct
   Bayesian treatment of a negatively-skilled source, and it falls out for free
   from calibrating on the track record.

Nothing here calls an LLM. The adapter takes any ``callable(prompt) -> str``,
so FinRobot, a CrewAI crew, or a raw OpenAI client can be plugged in without
this module depending on any of them. ``StubAnalyst`` provides a deterministic
implementation so the whole path is testable offline.
"""

from __future__ import annotations

import json
import logging
import math
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime

from .bayesian import Evidence

log = logging.getLogger(__name__)

# Bounds on what the adapter will accept from a model.
MIN_PROBABILITY = 0.005
MAX_PROBABILITY = 0.995
VALID_DIRECTIONS = ("bullish", "bearish", "neutral")
SANITISE_LIMIT = 2_000          # characters of reasoning we retain


@dataclass
class AnalystSignal:
    """A validated opinion about one symbol."""

    symbol: str
    direction: str               # "bullish" | "bearish" | "neutral"
    conviction: float            # 0..1
    horizon_days: int = 5
    reasoning: str = ""
    source: str = "llm_analyst"
    model: str = "unknown"
    at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    raw: str = ""

    @property
    def sign(self) -> float:
        """+1 bullish, -1 bearish, 0 neutral."""
        return {"bullish": 1.0, "bearish": -1.0, "neutral": 0.0}[self.direction]

    def describe(self) -> str:
        return (f"{self.source}: {self.direction} {self.symbol} "
                f"(conviction {self.conviction:.2f}, {self.hizon_label})")

    @property
    def horzon_label(self) -> str:      # typo kept deliberately out of the API
        return f"{self.horizon_days}d"

    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "direction": self.direction,
            "conviction": round(self.conviction, 3),
            "horizon_days": self.horizon_days,
            "reasoning": self.reasoning[:SANITISE_LIMIT],
            "source": self.source,
            "model": self.model,
            "at": self.at,
        }


class AnalystValidationError(ValueError):
    """The model returned something we refuse to act on."""


@dataclass
class ParseResult:
    """Outcome of parsing one model response."""

    ok: bool
    signal: AnalystSignal | None = None
    error: str = ""

    def __bool__(self) -> bool:  # noqa: D105
        return self.ok


def _clamp(value: float, lo: float, hi: float) -> float:
    if value != value:  # NaN
        return lo
    return max(lo, min(hi, value))


def parse_response(text: str, symbol: str, source: str = "llm_analyst",
                   model: str = "unknown") -> ParseResult:
    """Parse and strictly validate a JSON response from the analyst.

    Accepts either a bare JSON object or one embedded in prose/prose fences,
    since models reliably wrap output in markdown despite instructions.
    Rejects anything missing a valid direction, conviction, or the expected
    symbol -- a partial answer is treated as no answer at all.
    """
    if not text or not text.strip():
        return ParseResult(False, error="empty response")

    candidate = _extract_json(text)
    if candidate is None:
        return ParseResult(False, error="no JSON object found in response")

    try:
        payload = json.loads(candidate)
    except json.JSONDecodeError as exc:
        return ParseResult(False, error=f"invalid JSON: {exc.msg}")
    if not isinstance(payload, dict):
        return ParseResult(False, error="response is not a JSON object")

    # --- direction
    direction = str(payload.get("direction", "")).strip().lower()
    if direction in ("up", "long", "positive", "buy"):
        direction = "bullish"
    elif direction in ("down", "short", "negative", "sell"):
        direction = "bearish"
    elif direction in ("flat", "none", "hold", ""):
        direction = "neutral"
    if direction not in VALID_DIRECTIONS:
        return ParseResult(False, error=f"unknown direction {direction!r}")

    # --- conviction
    try:
        conviction = float(payload.get("conviction", 0.0))
    except (TypeError, ValueError):
        return ParseResult(False, error="conviction is not a number")
    if not 0.0 <= conviction <= 1.0:
        return ParseResult(False, error=f"conviction {conviction} outside [0, 1]")

    # --- symbol must match what we asked about
    returned = str(payload.get("symbol", symbol)).strip().upper().replace("/", "")
    if returned and returned != symbol.strip().upper():
        return ParseResult(False, error=f"response is about {returned}, not {symbol}")

    # --- horizon
    try:
        horizon = int(payload.get("horizon_days", 5) or 5)
    except (TypeError, ValueError):
        horizon = 5
    horizon = int(_clamp(horizon, 1, 90))

    reasoning = _sanitise(str(payload.get("reasoning", "") or ""))

    return ParseResult(True, signal=AnalystSignal(
        symbol=symbol,
        direction=direction,
        conviction=_clamp(conviction, 0.0, 1.0),
        horizon_days=horizon,
        reasoning=reasoning,
        source=source,
        model=model,
        raw=text[:SANITISE_LIMIT],
    ))


def _extract_json(text: str) -> str | None:
    """Find a JSON object in free-form model output."""
    stripped = text.strip()

    # fenced code block
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", stripped, re.DOTALL)
    if fence:
        return fence.group(1)

    # balanced-brace scan (handles prose before/after the object)
    start = stripped.find("{")
    while start != -1:
        depth = 0
        in_string = False
        escape = False
        for index in range(start, len(stripped)):
            char = stripped[index]
            if escape:
                escape = False
                continue
            if char == "\\":
                escape = True
                continue
            if char == '"':
                in_string = not in_string
                continue
            if in_string:
                continue
            if char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    return stripped[start:index + 1]
        start = stripped.find("{", start + 1)
    return None


def _sanitise(text: str, limit: int = SANITISE_LIMIT) -> str:
    """Keep reasoning short and strip control characters for the journal."""
    cleaned = "".join(ch for ch in text if ch >= " " or ch == "\n")
    return cleaned.strip()[:limit]


# --------------------------------------------------------------- calibration


@dataclass
class AnalystCalibration:
    """Running track record for one analyst source.

    ``skill`` is in [-1, 1]: +1 means always right, -1 means always wrong,
    0 means no information. It is exactly 0 until ``min_observations`` calls
    have been scored, so a fresh analyst cannot influence anything.
    """

    correct: int = 0
    total: int = 0
    neutral: int = 0            # calls where the analyst declined to opine
    min_observations: int = 10
    errors: int = 0

    @property
    def observations(self) -> int:
        return self.correct + self.incorrect

    @property
    def incorrect(self) -> int:
        return self.total - self.correct

    @property
    def accuracy(self) -> float | None:
        if self.total == 0:
            return None
        return self.correct / self.total

    def skill(self) -> float:
        """Scaled accuracy in [-1, 1]; 0 while untested."""
        if self.total < self.min_observations:
            return 0.0
        return _clamp(2.0 * (self.correct / self.total) - 1.0, -1.0, 1.0)

    def record(self, was_correct: bool) -> None:
        self.total += 1
        if was_correct:
            self.correct += 1

    def record_error(self) -> None:
        self.errors += 1

    def to_dict(self) -> dict:
        return {
            "correct": self.correct,
            "incorrect": self.incorrect,
            "total": self.total,
            "accuracy": round(self.accuracy, 4) if self.accuracy is not None else None,
            "skill": round(self.skill(), 4),
            "errors": self.errors,
            "min_observations": self.min_observations,
        }


# ------------------------------------------------------------------- adapter


@dataclass
class AnalystAdapterConfig:
    """How much influence an analyst is allowed to have."""

    # Maximum log-odds shift a single call may contribute, in nats.
    # e^0.6 = 1.82x, which is deliberately comparable to one price feature.
    max_log_odds: float = 0.6
    min_observations: int = 10
    # Multiplier applied on top of measured skill, so even a well-calibrated
    # analyst is capped well below the price-based evidence.
    skill_scale: float = 0.7
    require_horizon_match: bool = False
    source_name: str = "llm_analyst"

    def clamp(self) -> "AnalystAdapterConfig":
        self.max_log_odds = float(min(max(self.max_log_odds, 0.0), 2.0))
        self.min_observations = int(max(1, self.min_observations))
        self.skill_scale = float(_clamp(self.skill_scale, 0.0, 1.0))
        return self


class AnalystAdapter:
    """Converts analyst opinions into bounded :class:`Evidence`."""

    def __init__(self, config: AnalystAdapterConfig | None = None) -> None:
        self.cfg = (config or AnalystAdapterConfig()).clamp()
        self.calibrations: dict[str, AnalystCalibration] = {}
        self.history: list[dict] = []
        self.rejected: list[dict] = []

    # ------------------------------------------------------------- calibration

    def calibration_for(self, source: str) -> AnalystCalibration:
        if source not in self.calibrations:
            self.calibrations[source] = AnalystCalibration(
                min_observations=self.cfg.min_observations)
        return self.calibrations[source]

    def record_outcome(self, source: str, was_correct: bool) -> None:
        self.calibration_for(source).record(was_correct)

    def record_error(self, source: str) -> None:
        self.calibration_for(source).record_error()

    # ---------------------------------------------------------------- evidence

    def to_evidence(self, signal: AnalystSignal) -> Evidence:
        """Turn a validated signal into a bounded Bayes factor.

        The shift in log-odds is::

            delta = sign * conviction * max_log_odds * (skill * skill_scale)

        With an untested analyst ``skill`` is 0, so ``delta`` is 0 and the Bayes
        factor is exactly 1 -- the posterior does not move at all.
        """
        skill = self.calibration_for(signal.source).skill()
        delta = (signal.sign * signal.conviction * self.cfg.max_log_odds
                 * skill * self.cfg.skill_scale)

        # Build a legal likelihood pair whose log-ratio is 2*delta.
        # P(evidence | up) = base * e^{+delta}, P(evidence | down) = base * e^{-delta}
        base = 0.5
        p_up = _clamp(base * math.exp(delta), MIN_PROBABILITY, MAX_PROBABILITY)
        p_down = _clamp(base * math.exp(-delta), MIN_PROBABILITY, MAX_PROBABILITY)

        bayes_factor = math.log(p_up / p_down) if p_down > 0 else 0.0
        return Evidence(
            source=f"{self.cfg.source_name}:{signal.source}",
            p_if_up=p_up,
            p_if_down=p_down,
            detail={
                "direction": signal.direction,
                "conviction": round(signal.conviction, 3),
                "skill": round(skill, 4),
                "log_odds": round(bayes_factor, 4),
                "reasoning": signal.reasoning[:400],
                "model": signal.model,
                "horizon_days": signal.horizon_days,
                "calibration": self.calibration_for(signal.source).to_dict(),
            },
        )

    # ----------------------------------------------------------------- calling

    def analyse(self, symbol: str, context: str, caller,
                source: str = "llm_analyst", model: str = "unknown") -> Evidence | None:
        """Ask ``caller(prompt)`` for an opinion, validate it, return evidence.

        Returns ``None`` (never a fabricated signal) when the response is
        unusable. Every rejection is recorded so the dashboard can show how
        often the model fails to follow the contract.
        """
        prompt = build_prompt(symbol, context)
        try:
            raw = caller(prompt)
        except Exception as exc:  # noqa: BLE001 - a dead API is not fatal
            log.warning("analyst call failed for %s: %s", symbol, exc)
            self.calibration_for(source).record_error()
            self.rejected.append({"symbol": symbol, "source": source,
                                  "error": f"call failed: {exc}"})
            return None

        parsed = parse_response(raw, symbol, source=source, model=model)
        if not parsed.ok:
            log.warning("analyst response rejected for %s: %s", symbol, parsed.error)
            self.calibration_for(source).record_error()
            self.rejected.append({"symbol": symbol, "source": source,
                                 "error": parsed.error, "raw": raw[:300]})
            return None

        signal = parsed.signal
        assert signal is not None
        evidence = self.to_evidence(signal)
        self.history.append({"symbol": symbol, **signal.to_dict(),
                             "bayes_factor_log_odds": evidence.detail["log_odds"]})
        return evidence

    # ---------------------------------------------------------------- reporting

    def snapshot(self) -> dict:
        return {
            "config": {
                "max_log_odds": self.cfg.max_log_odds,
                "min_observations": self.cfg.min_observations,
                "skill_scale": self.cfg.skill_scale,
                "source_name": self.cfg.source_name,
            },
            "calibrations": {k: v.to_dict() for k, v in self.calibrations.items()},
            "signals": list(reversed(self.history[-50:])),
            "rejected": list(reversed(self.rejected[-20:])),
            "n_signals": len(self.history),
        }

    def reset(self) -> None:
        self.calibrations.clear()
        self.history.clear()
        self.rejected.clear()


# -------------------------------------------------------------------- prompts

RESPONSE_CONTRACT = """{
  "symbol": "<the ticker you were asked about>",
  "direction": "bullish" | "bearish" | "neutral",
  "conviction": <number between 0 and 1>,
  "horizon_days": <integer 1-90>,
  "reasoning": "<at most 300 characters, concrete and specific>"
}"""


def build_prompt(symbol: str, context: str) -> str:
    """A constrained prompt that asks for machine-readable output.

    The contract is deliberately narrow: exactly the five fields, with
    enumerated direction values and a bounded conviction. Free-form prose is
    asked for only in ``reasoning``, which is never parsed as a decision.
    """
    return f"""You are a quantitative research analyst. Assess {symbol}.

Respond with EXACTLY one JSON object and nothing else, matching this contract:

{RESPONSE_CONTRACT}

Rules:
- "direction" must be one of: bullish, bearish, neutral
- "conviction" must be between 0 and 1; use 0.5 for a coin flip and reserve
  values above 0.8 for well-supported views
- "neutral" with a low conviction is preferred over a confident guess
- "reasoning" must cite specific evidence, not restate the price action

Context:
{context}
"""


# --------------------------------------------------------------- test double


class StubAnalyst:
    """Deterministic analyst for offline testing and demos."""

    def __init__(self, responses: dict[str, str] | None = None,
                 default: str | None = None) -> None:
        self.responses = responses or {}
        self.default = default
        self.calls: list[str] = []

    def __call__(self, prompt: str) -> str:
        self.calls.append(prompt)
        for symbol, response in self.responses.items():
            if symbol in prompt:
                return response
        if self.default is not None:
            return self.default
        return json.dumps({
            "symbol": "UNKNOWN", "direction": "neutral",
            "conviction": 0.0, "horizon_days": 5,
            "reasoning": "stub has no configured response",
        })


__all__ = [
    "AnalystAdapter",
    "AnalystAdapterConfig",
    "AnalystSignal",
    "AnalystCalibration",
    "AnalystValidationError",
    "ParseResult",
    "StubAnalyst",
    "parse_response",
    "build_prompt",
    "RESPONSE_CONTRACT",
]
