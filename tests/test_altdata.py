"""Tests for the alternative data sources.

Data parsing is where silent bugs live -- a NaN column or a misrouted symbol
looks like a strategy problem, not a data problem. Both happened here.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ts.altdata import (  # noqa: E402
    AlpacaMarketData,
    BinanceKlines,
    DataSourceError,
    RouterFeed,
)

# one real Binance kline, abbreviated to the fields the parser reads
KLINE = [
    1791842940000, "82738.74000000", "82770.01000000", "82738.74000000",
    "82770.00000000", "3.23308000", 1791842999999, "267563.79131940",
    1102, "2.53815000", "210048.07249940", "0",
]


class TestSymbolClassification(unittest.TestCase):
    """BTCUSD is crypto; EURUSD is forex. Both end in USD."""

    def setUp(self):
        self.alpaca = AlpacaMarketData()

    def test_crypto_quotes_are_crypto(self):
        for sym in ("BTCUSD", "ETHUSD", "SOLUSD", "XRPUSD", "BTCUSDT", "DOGEUSDT"):
            self.assertTrue(self.alpaca._is_crypto(sym), sym)

    def test_fx_pairs_are_not_crypto(self):
        for sym in ("EURUSD", "GBPUSD", "USDJPY", "AUDUSD", "USDCAD", "EURGBP"):
            self.assertFalse(self.alpaca._is_crypto(sym), sym)

    def test_equities_are_not_crypto(self):
        for sym in ("AAPL", "MSFT", "SPY", "NVDA"):
            self.assertFalse(self.alpaca._is_crypto(sym), sym)

    def test_slash_forms(self):
        self.assertTrue(self.alpaca._is_crypto("BTC/USD"))
        self.assertFalse(self.alpaca._is_crypto("EUR/USD"))

    def test_router_buckets_a_universe(self):
        router = RouterFeed(alpaca=self.alpaca)
        buckets = router.classify(
            ["BTCUSD", "ETHUSD", "SOLUSD", "XRPUSD", "AAPL", "MSFT", "SPX500", "EURUSD"])
        self.assertEqual(buckets["crypto"], ["BTCUSD", "ETHUSD", "SOLUSD", "XRPUSD"])
        self.assertEqual(buckets["stocks"], [])
        self.assertEqual(buckets["unsupported"], ["AAPL", "MSFT", "SPX500", "EURUSD"])


class TestBinanceParsing(unittest.TestCase):
    """The all-NaN frame that a Series-as-index bug produced."""

    def test_to_frame_produces_real_values(self):
        df = BinanceKlines._to_frame([KLINE])
        self.assertEqual(len(df), 1)
        for col in ("open", "high", "low", "close", "volume"):
            self.assertFalse(df[col].isna().any(), f"{col} came back NaN")
            self.assertTrue((df[col] > 0).all(), f"{col} is not positive")
        self.assertAlmostEqual(df["close"].iloc[0], 82770.0, places=4)
        self.assertAlmostEqual(df["open"].iloc[0], 82738.74, places=4)

    def test_index_is_datetime_and_unique(self):
        df = BinanceKlines._to_frame([KLINE, dict(enumerate(KLINE))])
        self.assertIsInstance(df.index, pd.DatetimeIndex if (pd := __import__("pandas")) else None)
        self.assertTrue(df.index.is_monotonic_increasing)

    def test_symbol_normalisation(self):
        self.assertEqual(BinanceKlines._normalise("BTCUSD"), "BTCUSDT")
        self.assertEqual(BinanceKlines._normalise("BTC/USD"), "BTCUSDT")
        self.assertEqual(BinanceKlines._normalise("BTC-USDT"), "BTCUSDT")
        self.assertEqual(BinanceKlines._normalise("ETH"), "ETHUSDT")

    def test_start_ms_from_range(self):
        self.assertIsNone(BinanceKlines._start_ms("max"))
        self.assertIsNotNone(BinanceKlines._start_ms("7d"))
        self.assertLess(BinanceKlines._start_ms("60d"), BinanceKlines._start_ms("7d"))

    def test_unknown_interval_raises(self):
        with self.assertRaises(DataSourceError):
            BinanceKlines().history("BTCUSD", interval="7m", range_="7d")


class TestRouter(unittest.TestCase):
    def test_unavailable_source_raises_not_attributeerror(self):
        """A None fallback must produce a clear error, not AttributeError."""
        router = RouterFeed(binance=None, alpaca=AlpacaMarketData(), yahoo=None)
        with self.assertRaises(DataSourceError):
            router.history("AAPL", interval="1m", range_="7d")

    def test_crypto_uses_binance_first(self):
        order = RouterFeed()._order("BTCUSD")
        self.assertIsInstance(order[0], BinanceKlines)

    def test_all_sources_unavailable_is_reported(self):
        class Dead:
            name = "dead"

            def history(self, *args, **kwargs):
                raise DataSourceError("no data")

        router = RouterFeed(binance=Dead(), alpaca=Dead(), yahoo=None)
        with self.assertRaises(DataSourceError) as ctx:
            router.history("BTCUSD")
        self.assertIn("every source failed", str(ctx.exception))


if __name__ == "__main__":
    unittest.main(verbosity=2)
