"""Diagnose why signals are (not) firing."""

import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ts.datafeed import DataFeed, YahooFeed
from ts.strategy import Strategy, StrategyParams, apply_market_gate

logging.basicConfig(level=logging.WARNING)

feed = YahooFeed(cache_dir="tradingsys/data")
data = DataFeed(feed=feed, interval="1d", range_="5y").pull(["SPY", "NVDA", "AAPL"])

strat = Strategy(StrategyParams(min_history=220))
for sym, frame in data.items():
    sig = strat.compute(frame)
    print(f"\n=== {sym} ({len(frame)} bars) ===")
    print("regime==1 bars      :", int((sig['regime'] == 1).sum()))
    print("breakout_long bars  :", int(sig['breakout_long'].sum()))
    print("trend_strength bars :", int(sig['trend_strength'].sum()))
    print("volume_ok bars      :", int(sig['volume_ok'].sum()))
    print("entry_long bars     :", int(sig['entry_long'].sum()))
    print("last close          :", float(sig['close'].iloc[-1]))
    print("last adx            :", float(sig['adx'].iloc[-1]) if 'adx' in sig else 'n/a')
    print("cols                :", list(sig.columns))
    print(sig[['close', 'atr', 'regime', 'breakout_long', 'trend_strength',
               'volume_ok', 'entry_long']].tail(5).to_string())

# check the market gate
market = data["SPY"]
p = StrategyParams()
regime_ma = market["close"].rolling(200).mean()
gate = (market["close"] > regime_ma)
print("\nSPY above 200dma bars:", int(gate.sum()), "of", len(gate))
print("SPY regime on last 20 bars:", gate.tail(20).astype(int).tolist())
