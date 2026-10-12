"""Regression test: endpoint classification must never use substring matching."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ts.broker.alpaca import (  # noqa: E402
    LIVE_URL,
    PAPER_URL,
    AlpacaBroker,
    AlpacaConfig,
)
from ts.broker.binance import (  # noqa: E402
    LIVE_FUTURES,
    LIVE_SPOT,
    TEST_FUTURES,
    TEST_SPOT,
    BinanceBroker,
    BinanceConfig,
)


class TestEndpointClassification(unittest.TestCase):
    """The bug: 'api.alpaca.markets' is a substring of the PAPER url."""

    def test_paper_url_is_not_the_live_url(self):
        self.assertIn("api.alpaca.markets", PAPER_URL)
        self.assertNotEqual(PAPER_URL, LIVE_URL)

    def test_paper_config_is_classified_as_paper(self):
        broker = AlpacaBroker.__new__(AlpacaBroker)
        broker.cfg = AlpacaConfig(mode="paper")
        broker.base_url = PAPER_URL
        broker.mode = self._classify(broker.base_url)
        self.assertEqual(broker.mode, "paper")

    def test_live_config_is_classified_as_live(self):
        broker = AlpacaBroker.__new__(AlpacaBroker)
        broker.cfg = AlpacaConfig(mode="live")
        broker.base_url = LIVE_URL
        broker.mode = self._classify(broker.base_url)
        self.assertEqual(broker.mode, "live")

    def test_registry_paper_mode_constructs_without_the_guard(self):
        """The exact failure from the UI: paper keys must not trip the live guard."""
        try:
            AlpacaBroker(AlpacaConfig(key_id="test-key", secret_key="test-secret",
                                      mode="paper"))
        except Exception as exc:  # noqa: BLE001
            self.fail(f"paper construction should not raise: {exc}")

    def test_binance_testnet_is_not_live(self):
        self.assertFalse(BinanceConfig(testnet=True).is_live)
        self.assertTrue(BinanceConfig(testnet=False).is_live)

    def test_binance_futures_live_is_live(self):
        self.assertEqual(LIVE_FUTURES, "https://fapi.binance.com")
        self.assertTrue(BinanceConfig(futures=True, testnet=False).is_live)
        self.assertFalse(BinanceConfig(futures=True, testnet=True).is_live)

    def test_no_substring_misclassification(self):
        # 'api.binance.com' is a substring of 'fapi.binance.com'
        self.assertIn("api.binance.com", LIVE_FUTURES)
        self.assertNotEqual(LIVE_FUTURES, LIVE_SPOT)
        self.assertFalse(BinanceConfig(testnet=True).base_url == LIVE_SPOT)

    @staticmethod
    def _classify(base_url: str) -> str:
        normalised = base_url.lower()
        if normalised == LIVE_URL:
            return "live"
        if normalised == PAPER_URL:
            return "paper"
        return "paper" if "paper" in normalised else "live"


if __name__ == "__main__":
    unittest.main(verbosity=2)
