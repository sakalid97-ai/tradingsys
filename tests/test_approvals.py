"""Tests for the approval gate.

The properties that matter: silence is never consent, expiry is a rejection,
and the parameters you approved are the parameters that execute.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ts.approvals import (  # noqa: E402
    ApprovalGate,
    ApprovalRequest,
    Decision,
    classify_reply,
    format_request,
)


class FakeNotifier:
    def __init__(self):
        self.sent: list[tuple] = []

    def send(self, title, body="", severity=None, fields=None, kind="alert"):
        self.sent.append((title, body))
        return True

    def warning(self, title, body="", **fields):
        self.sent.append((title, body))
        return True

    def trade(self, title, body="", **fields):
        self.sent.append((title, body))
        return True

    def critical(self, title, body="", **fields):
        self.sent.append((title, body))
        return True


class TestClassifyReply(unittest.TestCase):
    def test_yes_variants(self):
        for word in ("yes", "YES", "y", "approve", "confirm", "go", "do it", "ok",
                     "1", "si", "yes."):
            self.assertEqual(classify_reply(word), Decision.APPROVED, word)

    def test_no_variants(self):
        for word in ("no", "NO", "n", "reject", "deny", "cancel", "nope", "0"):
            self.assertEqual(classify_reply(word), Decision.REJECTED, word)

    def test_non_answers_return_none(self):
        for word in ("", "   ", "maybe", "what?", "hmm", "buy btc"):
            self.assertIsNone(classify_reply(word), word)


class TestRequestLifecycle(unittest.TestCase):
    def setUp(self):
        self.notifier = FakeNotifier()
        self.gate = ApprovalGate(notifier=self.notifier, timeout_seconds=90.0)

    def _request(self, **kwargs):
        kwargs.setdefault("action", "enter")
        kwargs.setdefault("title", "APPROVE: long BTCUSD")
        return self.gate.request(**kwargs)

    def test_request_is_sent_to_telegram(self):
        self._request(body="breakout confirmed", fields={"risk": "$250"})
        self.assertEqual(len(self.notifier.sent), 1)
        title, body = self.notifier.sent[0]
        self.assertIn("APPROVE", title)
        self.assertIn("250", body)

    def test_request_is_pending(self):
        request = self._request()
        self.assertTrue(request.pending)
        self.assertIn(request.request_id, self.gate.pending)

    def test_approve_executes(self):
        request = self._request()
        ok, message = self.gate.answer("yes", chat_id="573029836")
        self.assertTrue(ok)
        self.assertTrue(request.approved)
        self.assertIn("573029836", request.decided_by)
        self.assertIsNotNone(request.wait_seconds)

    def test_reject_blocks_execution(self):
        request = self._request()
        self.gate.answer("no", chat_id="573029836")
        self.assertEqual(request.decision, Decision.REJECTED)
        self.assertFalse(request.approved)

    def test_reply_targets_oldest_request(self):
        first = self._request(title="first")
        second = self._request(title="second")
        ok, _ = self.gate.answer("yes")
        self.assertTrue(first.pending is False)
        self.assertTrue(second.pending, "the reply must answer the oldest first")
        self.assertTrue(first.approved)

    def test_reply_can_name_a_request(self):
        self._request(title="first")
        second = self._request(title="second")
        ok, message = self.gate.answer("yes", request_id=second.request_id)
        self.assertTrue(second.approved)
        self.assertIn(second.request_id, message)

    def test_unknown_request_id_is_rejected(self):
        self._request()
        ok, message = self.gate.answer("yes", request_id="NOPE-9999")
        self.assertFalse(ok)

    def test_non_answer_is_rejected(self):
        self._request()
        ok, message = self.gate.answer("maybe")
        self.assertFalse(ok)
        self.assertTrue(self.gate.pending, "a non-answer must not consume the request")

    def test_answer_with_nothing_pending(self):
        ok, message = self.gate.answer("yes")
        self.assertFalse(ok)


class TestExpiryIsRejection(unittest.TestCase):
    def test_expired_request_does_not_execute(self):
        gate = ApprovalGate(notifier=FakeNotifier(), timeout_seconds=0.05)
        request = gate.request(action="enter", title="x")
        import time

        time.sleep(0.12)
        expired = gate.expire()
        self.assertEqual(len(expired), 1)
        self.assertEqual(request.decision, Decision.EXPIRED)
        self.assertFalse(request.approved,
                         "an unanswered request must NEVER execute")

    def test_expiry_notifies(self):
        notifier = FakeNotifier()
        gate = ApprovalGate(notifier=notifier, timeout_seconds=0.05)
        gate.request(action="enter", title="Will expire")
        import time

        time.sleep(0.12)
        gate.expire()
        self.assertTrue(any("NOT executed" in body for _, body in notifier.sent))

    def test_expiry_removes_from_pending(self):
        gate = ApprovalGate(notifier=FakeNotifier(), timeout_seconds=0.05)
        gate.request(action="k", title="x")
        import time

        time.sleep(0.12)
        gate.expire()
        self.assertEqual(len(gate.pending), 0)


class TestFloodProtection(unittest.TestCase):
    def test_max_pending_blocks_more(self):
        gate = ApprovalGate(notifier=FakeNotifier(), timeout_seconds=90.0,
                            max_pending=3)
        for i in range(3):
            gate.request(action="enter", title=f"r{i}")
        with self.assertRaises(RuntimeError):
            gate.request(action="enter", title="overflow")

    def test_answering_frees_a_slot(self):
        gate = ApprovalGate(notifier=FakeNotifier(), max_pending=2)
        gate.request(action="e", title="a")
        gate.request(action="e", title="b")
        gate.answer("no")
        gate.request(action="e", title="c")   # must not raise
        self.assertEqual(len(gate.pending), 2)


class TestFrozenParameters(unittest.TestCase):
    def test_approved_fields_are_recorded_exactly(self):
        gate = ApprovalGate(notifier=FakeNotifier())
        fields = {"symbol": "BTCUSD", "side": "long", "quantity": 4.03,
                  "price": 83317.44, "stop": 82255.37, "target": 83193.31,
                  "risk_dollars": 250.0}
        request = gate.request(action="enter", title="APPROVE", fields=fields)
        gate.answer("yes")
        self.assertEqual(request.fields, fields)
        for key, value in fields.items():
            self.assertEqual(request.fields[key], value,
                             "approved parameters must be frozen, not recomputed")

    def test_formatted_request_is_readable(self):
        request = ApprovalRequest(
            request_id="ENT-0001", action="enter", title="APPROVE: long BTCUSD",
            body="breakout with volume", fields={"quantity": 4.03, "risk": "$250"})
        text = format_request(request)
        self.assertIn("APPROVE: long BTCUSD", text)
        self.assertIn("quantity: 4.03", text)
        self.assertIn("risk: $250", text)
        self.assertIn("YES", text)
        self.assertIn("no reply means", text)


class TestSnapshot(unittest.TestCase):
    def test_counts_decisions(self):
        gate = ApprovalGate(notifier=FakeNotifier())
        for _ in range(3):
            gate.request(action="e", title="x")
            gate.answer("yes")
        for _ in range(2):
            gate.request(action="e", title="x")
            gate.answer("no")
        snapshot = gate.snapshot()
        self.assertEqual(snapshot["counts"]["approved"], 3)
        self.assertEqual(snapshot["counts"]["rejected"], 2)
        self.assertEqual(len(snapshot["recent"]), 5)

    def test_cancel_all(self):
        gate = ApprovalGate(notifier=FakeNotifier())
        gate.request(action="e", title="a")
        gate.request(action="e", title="b")
        self.assertEqual(gate.cancel_all(reason="kill switch"), 2)
        self.assertEqual(len(gate.pending), 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
