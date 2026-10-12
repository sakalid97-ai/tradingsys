"""Investigate whether stops ever fire in the synthetic backtest."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tests"))
from test_suite import make_frame, make_universe  # noqa: E402

from ts.backtest import BacktestConfig, Backtester  # noqa: E402
from ts.risk import RiskConfig  # noqa: E402
from ts.strategy import StrategyParams  # noqa: E402

data = make_universe()
bt = Backtester(
    strategy_params=StrategyParams(min_history=150),
    risk_config=RiskConfig(risk_per_trade=0.01, max_positions=6),
    config=BacktestConfig(initial_capital=100_000.0),
)
res = bt.run(data)
tf = res.trades_frame()
print("trades:", len(tf))
print("exit reasons:", tf["exit_reason"].value_counts().to_dict() if not tf.empty else {})
print("winners:", int((tf["net_pnl"] > 0).sum()) if not tf.empty else 0)
print("losers :", int((tf["net_pnl"] <= 0).sum()) if not tf.empty else 0)
if not tf.empty:
    print(tf[["symbol", "entry_price", "exit_price", "stop_price", "net_pnl",
              "r_multiple", "bars_held", "exit_reason"]].to_string(index=False))
