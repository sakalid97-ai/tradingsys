"""Tests for the parallel fetch helper and the parallelised scanner."""

from __future__ import annotations

import sys
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ts.parallel import _safe_call, parallel_map  # noqa: E402


def slow_identity(value, delay=0.05):
    time.sleep(delay)
    return value


class TestParallelMap(unittest.TestCase):
    def test_empty_input(self):
        self.assertEqual(parallel_map(str, [], max_workers=4), [])

    def test_single_item_skips_the_pool(self):
        self.assertEqual(parallel_map(lambda x: x * 2, [21]), [42])

    def test_preserves_input_order(self):
        """Order must match the input regardless of completion order."""
        values = list(range(30))
        result = parallel_map(slow_identity, values, max_workers=8)
        self.assertEqual(result, values)

    def test_actually_parallel(self):
        """8 items at 0.1s each must take well under 0.8s."""
        items = list(range(8))
        started = time.perf_counter()
        parallel_map(lambda v: slow_identity(v, 0.1), items, max_workers=8)
        elapsed = time.perf_counter() - started
        self.assertLess(elapsed, 0.45, "fetches did not actually overlap")

    def test_one_failure_does_not_affect_others(self):
        def boom(value):
            if value == 3:
                raise RuntimeError("bad ticker")
            return value * 10

        result = parallel_map(boom, list(range(6)), max_workers=4)
        self.assertIsNone(result[3])
        self.assertEqual([r for r in result if r is not None], [0, 10, 20, 40, 50])

    def test_all_failures_return_all_none(self):
        result = parallel_map(lambda v: 1 / 0, list(range(5)), max_workers=3)
        self.assertEqual(result, [None] * 5)

    def test_worker_count_is_capped_at_item_count(self):
        result = parallel_map(slow_identity, [1, 2], max_workers=64)
        self.assertEqual(result, [1, 2])

    def test_repeated_runs_are_identical(self):
        """Determinism: the same input must always produce the same output."""
        values = list(range(25))
        first = parallel_map(slow_identity, values, max_workers=8)
        for _ in range(3):
            self.assertEqual(parallel_map(slow_identity, values, max_workers=8),
                             first)

    def test_safe_call_returns_none_on_failure(self):
        self.assertIsNone(_safe_call(lambda: 1 / 0, None))


class TestScannerParallel(unittest.TestCase):
    def setUp(self):
        from ts.agents.bus import MessageBus
        from ts.agents.scanner import ScannerAgent

        self.bus = MessageBus()
        self.seen: list[dict] = []
        self.bus.subscribe("market.data", lambda m: self.seen.append(m.payload))

    def _feed(self, delays=None, failures=()):
        class Feed:
            def __init__(self):
                self.calls = []

            def universe_by_class(self, symbols):
                return {}

            def fetch(self, symbol):
                self.calls.append(symbol)
                if symbol in failures:
                    raise ValueError(f"no data for {symbol}")
                delay = (delays or {}).get(symbol, 0.0)
                if delay:
                    time.sleep(delay)

                class Snap:
                    def __init__(self, symbol):
                        self.symbol = symbol
                        self.frame = None
                        self.instrument = type("I", (), {"asset_class": None})()
                        self.is_stale = False

                    @property
                    def last_bar_time(self):
                        import pandas as pd

                        return pd.Timestamp("2026-01-01", tz="UTC")

                    def summary(self):
                        return {"symbol": self.symbol, "last": 100.0}

                return Snap(symbol)

        return Feed()

    def test_scan_publishes_in_configured_order(self):
        from ts.agents.scanner import ScannerAgent

        symbols = ["AAA", "BBB", "CCC", "DDD"]
        agent = ScannerAgent(self.bus, self._feed(), symbols, max_workers=4)
        agent.run()
        published = [p["symbol"] for p in self.seen]
        self.assertEqual(published, symbols,
                         "the scan must publish in universe order, not completion order")

    def test_failed_symbol_is_isolated(self):
        from ts.agents.scanner import ScannerAgent

        symbols = ["GOOD", "BAD", "FINE"]
        agent = ScannerAgent(self.bus, self._feed(failures={"BAD"}), symbols,
                             max_workers=3)
        agent.run()
        published = [p["symbol"] for p in self.seen]
        self.assertNotIn("BAD", published)
        self.assertIn("GOOD", published)
        self.assertIn("FINE", published)
        self.assertIn("BAD", agent.report.failed)

    def test_slow_fetches_overlap(self):
        from ts.agents.scanner import ScannerAgent

        delays = {s: 0.15 for s in ("S1", "S2", "S3", "S4")}
        agent = ScannerAgent(self.bus, self._feed(delays=delays), list(delays),
                             max_workers=4)
        started = time.perf_counter()
        agent.run()
        elapsed = time.perf_counter() - started
        self.assertLess(elapsed, 0.35, "the scan did not overlap its fetches")

    def test_report_lists_every_symbol(self):
        from ts.agents.scanner import ScannerAgent

        symbols = ["X1", "X2", "X3"]
        agent = ScannerAgent(self.bus, self._feed(), symbols, max_workers=2)
        agent.run()
        self.assertEqual(sorted(agent.report.scanned), symbols)
        self.assertEqual(agent.report.failed, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
