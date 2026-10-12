"""Why does the spike scenario not produce an entry?"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tests"))

from test_suite import make_frame  # noqa: E402

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from ts.backtest import BacktestConfig, Backtester  # noqa: E402
from ts.risk import RiskConfig  # noqa: E402
from ts.strategy import Strategy, StrategyParams  # noqa: E402

n_calm, n_crash = 300, 30
index = pd.date_range("2021-01-01", periods=n_calm + n_crash + 2, tz="UTC", freq="B")
closes = [100.0 * (1.0 - 0.20 * i / n_calm) for i in range(n_calm)]
spike = closes[-1] * 1.22
crash = [spike * (1.0 - 0.02 * (j + 1)) for j in range(n_crash + 1)]
series = closes + [spike] + crash
frame = pd.DataFrame(
    {
        "open": series,
        "high": [c * 1.001 for c in series],
        "low": [c * 0.999 for c in series],
        "close": series,
        "volume": 1_000_000.0,
    },
    index=index[: len(series)],
)
print("bars:", len(frame), "last calm close:", closes[-1], "spike:", spike)

params = StrategyParams(min_history=150, require_regime_long=False, adx_min=0,
                         volume_min_ratio=0)
sig = Strategy(params).compute(frame)
print("entry_long count:", int(sig["entry_long"].sum()))
print("breakout_long count:", int(sig["breakout_long"].sum()))
print("trend_strength count:", int(sig["trend_strength"].sum()))
print("volume_ok count:", int(sig["volume_ok"].sum()))
spike_idx = n_calm  # position of the spike bar
print("\naround the spike:")
print(sig[["close", "atr", "breakout_long", "trend_strength", "volume_ok", "entry_long"]]
      .iloc[spike_idx - 3: spike_idx + 4].to_string())

res = Backtester(
    strategy_params=params,
    risk_config=RiskConfig(risk_per_trade=0.01, max_positions=6, max_position_pct=1.0),
    config=BacktestConfig(initial_capital=100_000.0),
).run({"X": frame})
print("\nbacktest trades:", len(res.trades))
for t in res.trades:
    print("  ", t.symbol, t.entry_price, "->", t.exit_price, t.exit_reason,
          round(t.r_multiple, 3))
