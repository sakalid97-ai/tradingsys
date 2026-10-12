"""Turn free-form Telegram messages into the commands the system already runs.

The model's only job is translation: prose in, a narrow JSON contract out. It
never executes, never names a target price, and never chooses a quantity.

Every interpretation is fed through the *same* :func:`ts.commands.parse`
validation the typed commands use, and through the same confirmation gate for
raising a risk limit. That is the whole point: an LLM that says "yes, set risk
to 50%" produces exactly the same structured command as someone typing
`/set risk 0.5`, and it is rejected by the same bounds check for the same
reason.

If the model is unavailable, the deterministic parser handles the message as
before. Natural language is an addition, never a dependency.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field

from ..llm import LLMClient, LLMConfig, LLMError
from ..commands import parse as parse_typed_command

log = logging.getLogger(__name__)

SYSTEM_PROMPT = """You translate a trader's message into one command for a trading system.

Reply with EXACTLY one JSON object and nothing else:

{"command": "<name>", "args": {<arguments>}}

Commands and their arguments:
- status            {}                        equity, positions, risk
- positions         {}                        open positions
- forward           {}                        predicted vs realised performance
- risk              {}                        current limits
- kill              {"reason": "<why>"}        emergency stop, flatten everything
- resume            {}                        clear the kill switch
- pause             {}                        stop opening trades
- start             {}                        resume opening trades
- set               {"key": "<risk|kelly|max_risk|daily|dd|expectancy|confidence>", "value": <number>}
- run               {"command": "<backtest|walkforward|agents|paper|report>", "args": {<flags>}}

Rules:
- percent: "half a percent" -> 0.005, "one percent" -> 0.01
- "risk" means money risked per trade; "kelly" means the Kelly fraction
- if the trader is only asking a question or greeting, use {"command": "chat", "args": {"reply": "<answer in one or two sentences>"}}
- if you cannot tell what they want, use {"command": "unknown", "args": {}}
- NEVER invent a symbol, price or quantity. You do not place orders."""

COMMAND_ALIASES = {
    "status": "status", "state": "status", "how": "status", "howis": "status",
    "positions": "positions", "pos": "positions", "trades": "positions",
    "forward": "forward", "drift": "forward", "performance": "forward",
    "risk": "risk", "limits": "risk", "settings": "risk",
    "kill": "kill", "stop": "kill", "flatten": "kill", "panic": "kill",
    "resume": "resume", "unhalt": "resume", "continue": "resume",
    "pause": "pause", "hold": "pause",
    "start": "start", "go": "start",
    "set": "set", "change": "set", "adjust": "set",
    "chat": "chat", "unknown": "unknown",
}
PERCENT_WORDS = {
    "half a percent": 0.005, "half percent": 0.005, "quarter percent": 0.0025,
    "one percent": 0.01, "two percent": 0.02, "three percent": 0.03,
    "five percent": 0.05, "ten percent": 0.10,
    "fifty basis points": 0.005, "twenty basis points": 0.002,
}


@dataclass
class Interpretation:
    """What the model made of a message."""

    raw: str
    command: str
    args: dict
    source: str = "llm"          # llm | deterministic | chat
    reply: str = ""
    error: str = ""

    @property
    def handled(self) -> bool:
        return not self.error


def _extract_json(text: str) -> str | None:
    """Find the JSON object in a model reply, tolerating prose and fences."""
    stripped = text.strip()
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


def _normalise(value: float | str) -> float | None:
    """Accept 0.005, '0.005', '0.5%', 'half a percent'."""
    if isinstance(value, (int, float)):
        return float(value)
    if not isinstance(value, str):
        return None
    text = value.strip().lower()
    if text in PERCENT_WORDS:
        return PERCENT_WORDS[text]
    if text.endswith("%"):
        try:
            return float(text[:-1].strip()) / 100.0
        except ValueError:
            return None
    try:
        return float(text)
    except ValueError:
        return None


class Interpreter:
    """Translates free text into a validated command."""

    def __init__(self, client: LLMClient | None = None,
                 config: LLMConfig | None = None) -> None:
        self.client = client or (LLMClient(config) if (config or LLMConfig()).configured
                                 else None)
        self.history: list[dict] = []
        self.llm_calls = 0
        self.fallbacks = 0

    @property
    def available(self) -> bool:
        return self.client is not None and self.client.cfg.configured

    # -------------------------------------------------------------- interpret

    def interpret(self, text: str, context=None) -> Interpretation:
        """Turn one message into a command.

        Tries the deterministic parser first -- it is fast, free, and already
        validated -- and only falls back to the model when the message is not a
        command at all. That ordering keeps the common case instant and keeps
        the LLM on the cold path.
        """
        raw = text.strip()
        if not raw:
            return Interpretation(raw, "unknown", {}, error="empty message")

        # a typed command is always handled by the deterministic parser
        if raw.startswith("/"):
            try:
                command, args = parse_typed_command(raw, context)
                return Interpretation(raw, command, args, source="deterministic")
            except Exception as exc:  # noqa: BLE001
                return Interpretation(raw, "unknown", {}, error=str(exc))

        if not self.available:
            self.fallbacks += 1
            try:
                command, args = parse_typed_command(raw, context)
                return Interpretation(raw, command, args, source="deterministic")
            except Exception:
                return Interpretation(
                    raw, "unknown", {},
                    error="I did not understand that. Send /help for the commands I know.")

        # free text: ask the model
        try:
            completion = self.client.complete(SYSTEM_PROMPT, raw)
            self.llm_calls += 1
        except LLMError as exc:
            log.warning("interpreter fell back to the deterministic parser: %s", exc)
            self.fallbacks += 1
            try:
                command, args = parse_typed_command(raw, context)
                return Interpretation(raw, command, args, source="deterministic")
            except Exception:
                return Interpretation(raw, "unknown", {}, error=str(exc))

        return self._from_completion(raw, completion)

    def _from_completion(self, raw: str, completion: str) -> Interpretation:
        candidate = _extract_json(completion)
        if candidate is None:
            return Interpretation(raw, "unknown", {}, error="the model did not return JSON")

        try:
            payload = json.loads(candidate)
        except json.JSONDecodeError as exc:
            return Interpretation(raw, "unknown", {},
                                  error=f"the model returned invalid JSON: {exc.msg}")
        if not isinstance(payload, dict):
            return Interpretation(raw, "unknown", {},
                                  error="the model did not return a JSON object")

        command = str(payload.get("command", "")).strip().lower()
        command = COMMAND_ALIASES.get(command, command)
        args = payload.get("args")
        if not isinstance(args, dict):
            args = {}

        if command == "chat":
            reply = str(args.get("reply", "")).strip()[:600]
            return Interpretation(raw, "chat", {}, source="chat", reply=reply)
        if command == "unknown":
            return Interpretation(raw, "unknown", {},
                                  error="I did not understand that. Send /help for the "
                                        "commands I know.")

        # normalise numbers the model wrote as words or percentages
        if "value" in args:
            value = _normalise(args["value"])
            if value is None:
                return Interpretation(raw, "unknown", {},
                                      error="could not read that number")
            args["value"] = value
        if command == "run":
            args.setdefault("args", {})
            if not isinstance(args["args"], dict):
                args["args"] = {}

        self.history.append({"at": datetime_now(), "raw": raw[:200],
                             "command": command, "args": args,
                             "source": "llm"})
        return Interpretation(raw, command, args, source="llm")

    # ---------------------------------------------------------------- reporting

    def snapshot(self) -> dict:
        return {
            "available": self.available,
            "model": (self.client.cfg.model if self.client else None),
            "base_url": (self.client.cfg.base_url if self.client else None),
            "llm_calls": self.llm_calls,
            "fallbacks": self.fallbacks,
            "recent": list(reversed(self.history[-15:])),
        }


def datetime_now() -> str:
    from datetime import UTC, datetime

    return datetime.now(UTC).isoformat()


__all__ = ["Interpreter", "Interpretation", "SYSTEM_PROMPT", "COMMAND_ALIASES"]
