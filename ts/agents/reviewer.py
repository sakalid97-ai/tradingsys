"""A pre-trade reviewer that can only escalate.

This is the shape of LLM involvement that is actually safe on a money path. The
deterministic system has already decided to trade; before it executes, the model
is shown the plan and its context and asked one question: is there an obvious
problem here?

It can answer "concern" or "clear". It cannot:

* **execute** -- it returns text, nothing else;
* **block** -- a concern does not stop a trade, it *escalates it to a human*;
* **invent parameters** -- the contract has no field for a price or size.

So the worst case is a needless question, and the best case is catching a trade
that was about to walk into a rate decision. That asymmetry is the whole reason
this is worth having.

If the model is unreachable the review is skipped entirely and the trade
proceeds on deterministic evidence alone. Natural language is an addition, never
a dependency.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

from ..llm import LLMClient, LLMConfig, LLMError

SYSTEM_PROMPT = """You review one proposed trade for a discretionary trader.

You are given the plan, the market context, and the system's own numbers. Your
only job is to flag an OBVIOUS problem the numbers would miss.

Respond with EXACTLY one JSON object:
{"verdict": "clear" | "concern", "reason": "<at most 160 characters>"}

Flag a concern ONLY for something concrete and specific, for example:
- a scheduled high-impact event inside the holding window
- the instrument is halted, suspended, or wildly illiquid
- the plan contradicts the stated context
- two correlated positions would open at once

Do NOT flag a concern for:
- a view that merely differs from the strategy's
- low expectancy or low confidence (those are already handled)
- anything you would have to guess at

When unsure, answer "clear". You are a sanity check, not a second opinion."""


@dataclass
class Review:
    verdict: str = "clear"        # clear | concern | unavailable
    reason: str = ""
    model: str = ""
    available: bool = False

    @property
    def objects(self) -> bool:
        return self.verdict == "concern"

    def to_dict(self) -> dict:
        return {"verdict": self.verdict, "reason": self.reason,
                "model": self.model, "available": self.available}


class TradeReviewer:
    """Asks an LLM whether a trade has an obvious problem."""

    def __init__(self, client: LLMClient | None = None,
                 config: LLMConfig | None = None) -> None:
        self.client = client or (LLMClient(config) if (config or LLMConfig()).configured
                                 else None)
        self.reviews = 0
        self.concerns = 0
        self.failures = 0

    @property
    def enabled(self) -> bool:
        return self.client is not None and self.client.cfg.configured

    def review(self, symbol: str, side: str, expectancy_r: float, confidence: float,
               context: str, extra: dict | None = None) -> Review:
        if not self.enabled:
            return Review(verdict="unavailable", available=False,
                          reason="no model configured")

        prompt = self._build_prompt(symbol, side, expectancy_r, confidence,
                                    context, extra or {})
        try:
            completion = self.client.complete(SYSTEM_PROMPT, prompt)
        except LLMError as exc:
            self.failures += 1
            return Review(verdict="unavailable", available=False,
                          reason=f"model unavailable: {exc}",
                          model=self.client.cfg.model)

        self.reviews += 1
        verdict, reason = self._parse(completion)
        if verdict == "concern":
            self.concerns += 1
        return Review(verdict=verdict, reason=reason,
                      model=self.client.cfg.model, available=True)

    # ------------------------------------------------------------------ prompt

    @staticmethod
    def _build_prompt(symbol, side, expectancy_r, confidence, context,
                      extra) -> str:
        lines = [
            f"Proposed trade: {side} {symbol}",
            f"Expectancy: {expectancy_r:+.2f}R",
            f"Confidence: {confidence:.0%}",
        ]
        for key, value in extra.items():
            lines.append(f"{key}: {value}")
        lines += ["", "Context:", context or "(none supplied)"]
        return "\n".join(lines)

    @staticmethod
    def _parse(completion: str) -> tuple[str, str]:
        candidate = _extract_json(completion)
        if candidate is None:
            return "clear", ""
        try:
            payload = json.loads(candidate)
        except json.JSONDecodeError:
            return "clear", ""
        if not isinstance(payload, dict):
            return "clear", ""
        verdict = str(payload.get("verdict", "clear")).strip().lower()
        if verdict not in ("clear", "concern"):
            verdict = "clear"
        reason = str(payload.get("reason", "") or "").strip()[:160]
        return verdict, reason

    # ---------------------------------------------------------------- reporting

    def stats(self) -> dict:
        return {"enabled": self.enabled, "model": (self.client.cfg.model
                                                   if self.client else None),
                "reviews": self.reviews, "concerns": self.concerns,
                "failures": self.failures}


def _extract_json(text: str) -> str | None:
    stripped = (text or "").strip()
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", stripped, re.DOTALL)
    if fence:
        return fence.group(1)
    start = stripped.find("{")
    if start == -1:
        return None
    depth, in_string, escape = 0, False, False
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
    return None


__all__ = ["TradeReviewer", "Review", "SYSTEM_PROMPT"]
