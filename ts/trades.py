"""Shared data structures: the trade record and the backtest result envelope."""

from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd


@dataclass
class Trade:
    """One completed round trip, with everything needed to grade it."""

    symbol: str
    side: str                    # "long" | "short"
    quantity: int
    entry_time: pd.Timestamp
    entry_price: float
    exit_time: pd.Timestamp
    exit_price: float
    stop_price: float           # initial protective stop
    initial_risk: float         # dollars risked at entry (quantity x stop distance)
    fees: float = 0.0
    slippage: float = 0.0
    exit_reason: str = ""       # "stop" | "trail" | "channel" | "signal" | "eod"
    bars_held: int = 0
    equity_before: float = 0.0
    equity_after: float = 0.0
    mae: float = 0.0            # max adverse excursion, price units (against us)
    mfe: float = 0.0            # max favourable excursion, price units (for us)

    @property
    def gross_pnl(self) -> float:
        direction = 1.0 if self.side == "long" else -1.0
        return direction * (self.exit_price - self.entry_price) * self.quantity

    @property
    def net_pnl(self) -> float:
        """Realised P&L after fees. Derived, never stored, so it cannot drift
        from the prices a trade actually filled at."""
        return self.gross_pnl - self.fees

    @property
    def return_pct(self) -> float:
        """Trade P&L as a fraction of equity at entry."""
        if self.equity_before <= 0:
            return 0.0
        return self.net_pnl / self.equity_before

    @property
    def r_multiple(self) -> float:
        """P&L in units of the risk taken. The single most useful trade stat."""
        if self.initial_risk <= 0:
            return 0.0
        return self.net_pnl / self.initial_risk

    @property
    def is_winner(self) -> bool:
        return self.net_pnl > 0

    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "side": self.side,
            "quantity": self.quantity,
            "entry_time": self.entry_time,
            "entry_price": round(self.entry_price, 4),
            "exit_time": self.exit_time,
            "exit_price": round(self.exit_price, 4),
            "stop_price": round(self.stop_price, 4),
            "initial_risk": round(self.initial_risk, 2),
            "fees": round(self.fees, 2),
            "slippage": round(self.slippage, 4),
            "gross_pnl": round(self.gross_pnl, 2),
            "net_pnl": round(self.net_pnl, 2),
            "return_pct": round(self.return_pct, 5),
            "r_multiple": round(self.r_multiple, 3),
            "bars_held": self.bars_held,
            "exit_reason": self.exit_reason,
            "mae": round(self.mae, 4),
            "mfe": round(self.mfe, 4),
        }


@dataclass
class BacktestResult:
    """Everything a run produced, ready to serialise or plot."""

    equity_curve: pd.DataFrame
    trades: list[Trade]
    metrics: dict
    params: object = None
    risk_config: object = None
    signals: dict[str, pd.DataFrame] = field(default_factory=dict)
    config: dict = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    @property
    def final_equity(self) -> float:
        if self.equity_curve.empty:
            return 0.0
        return float(self.equity_curve["equity"].iloc[-1])

    def trades_frame(self) -> pd.DataFrame:
        if not self.trades:
            return pd.DataFrame()
        return pd.DataFrame([t.to_dict() for t in self.trades])

    def summary(self) -> str:
        m = self.metrics
        lines = [
            "=" * 62,
            "BACKTEST RESULT",
            "=" * 62,
            f"  Period              : {m.get('start')} -> {m.get('end')}  ({m.get('years')} yrs)",
            f"  Final equity        : {m.get('final_equity', 0):,.0f}",
            f"  Total return        : {m.get('total_return', 0):.2%}",
            f"  CAGR                : {m.get('cagr', 0):.2%}",
            f"  Max drawdown        : {m.get('max_drawdown', 0):.2%}",
            f"  Sharpe (ann.)       : {m.get('sharpe', 0):.2f}",
            f"  Sortino (ann.)      : {m.get('sortino', 0):.2f}",
            f"  Calmar              : {m.get('calmar', 0):.2f}",
            "-" * 62,
            f"  Trades              : {m.get('n_trades', 0)}",
            f"  Win rate            : {m.get('win_rate', 0):.2%}",
            f"  Avg win / avg loss  : {m.get('avg_win_R', 0):.2f}R / {m.get('avg_loss_R', 0):.2f}R",
            f"  Expectancy / trade  : {m.get('expectancy_R', 0):.3f}R  ({m.get('expectancy_pct', 0):.3%} of equity)",
            f"  Profit factor       : {m.get('profit_factor', 0):.2f}",
            f"  Max consec. losses  : {m.get('max_consecutive_losses', 0)}",
            f"  Avg bars held       : {m.get('avg_bars_held', 0):.1f}",
            f"  Exposure            : {m.get('exposure', 0):.1%} of bars",
            "=" * 62,
        ]
        return "\n".join(lines)
