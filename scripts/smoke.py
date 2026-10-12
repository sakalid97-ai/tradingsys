"""Smoke test: pull real data, run the backtest, print the report."""

import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ts.backtest import BacktestConfig, Backtester
from ts.datafeed import DataFeed, YahooFeed
from ts.risk import RiskConfig
from ts.strategy import StrategyParams

logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")

UNIVERSE = ["SPY", "QQQ", "AAPL", "MSFT", "NVDA", "AMD", "META", "GOOGL", "AMZN", "TSLA"]


def main() -> int:
    feed = YahooFeed(cache_dir="tradingsys/data")
    print("fetching history ...")
    data = DataFeed(feed=feed, interval="1d", range_="5y").pull(UNIVERSE)
    print("symbols:", sorted(data))
    for sym, frame in data.items():
        print(f"  {sym:<6} {len(frame):>5} bars  {frame.index[0].date()} -> {frame.index[-1].date()}")

    backtester = Backtester(
        strategy_params=StrategyParams(),
        risk_config=RiskConfig(
            risk_per_trade=0.01,
            max_positions=8,
            max_total_risk=0.06,
            daily_loss_limit=0.03,
            max_drawdown_halt=0.15,
        ),
        config=BacktestConfig(
            initial_capital=100_000.0,
            slippage_bps=5.0,
            commission_per_share=0.005,
            commission_min=1.0,
        ),
        market_gate=data.get("SPY"),
    )

    print("\nrunning backtest ...")
    result = backtester.run(data)
    print(result.summary())

    trades = result.trades_frame()
    if not trades.empty:
        print("\nfirst 10 trades:")
        print(trades.head(10).to_string(index=False))
        print("\nexit reasons:", result.metrics["exit_reasons"])
        print("per symbol:", result.metrics["per_symbol"])
        print("\nmonte carlo:", result.metrics["monte_carlo"])

    out = Path("tradingsys/results")
    out.mkdir(exist_ok=True, parents=True)
    trades.to_csv(out / "smoke_trades.csv", index=False)
    result.equity_curve.to_csv(out / "smoke_equity.csv")
    print(f"\nwrote {out / 'smoke_trades.csv'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
