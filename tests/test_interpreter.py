"""Tests for the LLM interpreter.

The property that matters most: a command produced by the model goes through the
same validation as a typed one, so "set risk to 50%" behaves identically whether
a human typed it or the model parsed it.
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ts.agents.interpreter import (  # noqa: E402
    Interpreter,
    Interpretation,
    _extract_json,
    _normalise,
)
from ts.llm import LLMClient, LLMConfig, LLMError  # noqa: E402


class FakeClient(LLMClient):
    """Returns a fixed completion without any network call."""

    def __init__(self, completion: str | None = None, raises: Exception | None = None):
        super().__init__(LLMConfig(base_url="http://local", model="test-model"))
        self.completion = completion
        self.raises = raises
        self.last_prompt = None

    def complete(self, system, user, model=None, temperature=None):
        self.last_prompt = (system, user)
        if self.raises is not None:
            raise self.raises
        return self.completion or ""


def interpret(client, text):
    return Interpreter(client).interpret(text)


class TestParsingHelpers(unittest.TestCase):
    def test_extract_json_from_plain(self):
        self.assertEqual(_extract_json('{"command":"status"}'), '{"command":"status"}')

    def test_extract_json_from_fence(self):
        text = 'Sure!\n```json\n{"command":"kill"}\n```'
        self.assertEqual(_extract_json(text), '{"command":"kill"}')

    def test_extract_json_ignores_prose(self):
        text = 'Here you go: {"command":"positions","args":{}} — hope that helps!'
        self.assertEqual(_extract_json(text), '{"command":"positions","args":{}}')

    def test_extract_json_handles_nested_braces(self):
        text = '{"command":"run","args":{"args":{"range":"2y"}}}'
        self.assertEqual(_extract_json(text), text)

    def test_extract_json_returns_none(self):
        self.assertIsNone(_extract_json("no json here"))

    def test_normalise_numbers(self):
        self.assertEqual(_normalise(0.005), 0.005)
        self.assertEqual(_normalise("0.005"), 0.005)
        self.assertEqual(_normalise("0.5%"), 0.005)
        self.assertEqual(_normalise("half a percent"), 0.005)
        self.assertIsNone(_normalise("abc"))
        self.assertIsNone(_normalise(None))


class TestTypedCommandsSkipTheModel(unittest.TestCase):
    def test_slash_commands_never_reach_the_model(self):
        client = FakeClient(json.dumps({"command": "kill"}))
        result = interpret(client, "/status")
        self.assertEqual(result.source, "deterministic")
        self.assertIsNone(client.last_prompt, "a typed command must not call the LLM")

    def test_slash_kill_is_parsed_deterministically(self):
        client = FakeClient(json.dumps({"command": "status"}))
        result = interpret(client, "/kill drawdown")
        self.assertEqual(result.command, "kill")
        self.assertIn("drawdown", result.args["reason"])


class TestFreeText(unittest.TestCase):
    def test_free_text_uses_the_model(self):
        client = FakeClient(json.dumps(
            {"command": "status", "args": {}}))
        result = interpret(client, "how's it going?")
        self.assertEqual(result.source, "llm")
        self.assertEqual(result.command, "status")
        self.assertIsNotNone(client.last_prompt)

    def test_model_alias_is_normalised(self):
        client = FakeClient(json.dumps({"command": "how", "args": {}}))
        self.assertEqual(interpret(client, "how are we").command, "status")

    def test_bare_command_words_work(self):
        client = FakeClient(json.dumps({"command": "positions"}))
        self.assertEqual(interpret(client, "what do I own").command, "positions")

    def test_chat_reply_is_passed_through(self):
        client = FakeClient(json.dumps(
            {"command": "chat", "args": {"reply": "All systems nominal."}}))
        result = interpret(client, "hello")
        self.assertEqual(result.command, "chat")
        self.assertEqual(result.reply, "All systems nominal.")

    def test_unknown_is_an_error_not_a_silent_noop(self):
        client = FakeClient(json.dumps({"command": "unknown"}))
        result = interpret(client, "buy dogecoin")
        self.assertEqual(result.command, "unknown")
        self.assertTrue(result.error)

    def test_invalid_json_is_reported(self):
        client = FakeClient("I think you should check the dashboard.")
        result = interpret(client, "status")
        self.assertEqual(result.command, "unknown")
        self.assertIn("JSON", result.error)

    def test_no_json_object_at_all(self):
        client = FakeClient("sure, one moment")
        result = interpret(client, "status")
        self.assertTrue(result.error)

    def test_percentage_string_is_normalised(self):
        client = FakeClient(json.dumps(
            {"command": "set", "args": {"key": "risk", "value": "half a percent"}}))
        result = interpret(client, "halve my risk")
        self.assertEqual(result.args["value"], 0.005)

    def test_percent_symbol_is_normalised(self):
        client = FakeClient(json.dumps(
            {"command": "set", "args": {"key": "risk", "value": "0.5%"}}))
        self.assertEqual(interpret(client, "x").args["value"], 0.005)

    def test_unparseable_number_is_rejected(self):
        client = FakeClient(json.dumps(
            {"command": "set", "args": {"key": "risk", "value": "lots"}}))
        result = interpret(client, "set risk to lots")
        self.assertTrue(result.error)
        self.assertEqual(result.command, "unknown")

    def test_run_command_shape(self):
        client = FakeClient(json.dumps(
            {"command": "run", "args": {"command": "backtest",
                                        "args": {"range": "2y"}}}))
        result = interpret(client, "run a backtest over two years")
        self.assertEqual(result.command, "run")
        self.assertEqual(result.args["command"], "backtest")
        self.assertEqual(result.args["args"]["range"], "2y")


class TestFallback(unittest.TestCase):
    def test_model_unavailable_falls_back(self):
        client = FakeClient(raises=LLMError("connection refused"))
        interpreter = Interpreter(client)
        result = interpreter.interpret("status")
        self.assertEqual(result.source, "deterministic")
        self.assertEqual(result.command, "status")

    def test_fallback_still_parses_commands(self):
        client = FakeClient(raises=LLMError("timeout"))
        result = Interpreter(client).interpret("positions")
        self.assertEqual(result.command, "positions")

    def test_fallback_reports_an_understandable_error(self):
        client = FakeClient(raises=LLMError("offline"))
        result = Interpreter(client).interpret("please buy everything")
        self.assertTrue(result.error)

    def test_no_client_configured_uses_deterministic_only(self):
        interpreter = Interpreter(LLMClient(LLMConfig(base_url="", model="")))
        self.assertFalse(interpreter.available)
        result = interpreter.interpret("kill")
        self.assertEqual(result.source, "deterministic")

    def test_empty_message(self):
        result = interpret(FakeClient("{}"), "   ")
        self.assertTrue(result.error)


class TestSafetyProperties(unittest.TestCase):
    """The property that makes this safe to put near a live account."""

    def test_model_command_is_validated_by_the_same_parser(self):
        """A model saying 'set risk to 0.5' must be rejected by the same bounds."""
        from ts.commands import RISK_BOUNDS, CommandError, parse

        low, high = RISK_BOUNDS["risk"]
        for bad_value in (0.5, 0.0, 99.0, high + 0.01, low - 0.001):
            with self.assertRaises((CommandError, ValueError)):
                parse(f"/set risk {bad_value}")

    def test_model_cannot_name_a_symbol_or_quantity(self):
        """The contract has no fields for either."""
        from ts.agents.interpreter import SYSTEM_PROMPT

        self.assertIn("NEVER invent a symbol", SYSTEM_PROMPT)
        self.assertIn("You do not place orders", SYSTEM_PROMPT)

    def test_interpretation_carries_no_fill_instruction(self):
        client = FakeClient(json.dumps(
            {"command": "set", "args": {"key": "risk", "value": 0.005}}))
        result = interpret(client, "halve my risk")
        allowed = {"command", "args", "key", "value", "reason", "command_name"}
        self.assertTrue(set(result.args) <= allowed | {"key", "value"},
                        f"unexpected fields: {set(result.args) - allowed}")

    def test_llm_output_is_journalled_for_audit(self):
        client = FakeClient(json.dumps({"command": "risk"}))
        interpreter = Interpreter(client)
        interpreter.interpret("show me the limits")
        snapshot = interpreter.snapshot()
        self.assertEqual(snapshot["llm_calls"], 1)
        self.assertEqual(len(snapshot["recent"]), 1)
        self.assertEqual(snapshot["recent"][0]["command"], "risk")


if __name__ == "__main__":
    unittest.main(verbosity=2)
