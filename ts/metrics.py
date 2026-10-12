"""Performance metrics.

Deliberately excludes the statistics that flatter a strategy (total P&L in
isolation, best month, "accuracy").  What matters for a 1%-per-trade system:

* **Expectancy in R** -- mean P&L divided by mean risk.  Positive expectancy in
  R is the *only* thing that makes a system profitable long-run.
* **Max consecutive losses** -- the emotional/account test.  A model with 45% win
  rate routinely strings 8 losses together; you must fund that.
* **Payoff** -- avg win / avg loss.  Trend systems live by payoff, not accuracy.
* **Exposure** -- how much of the time capital is actually at risk.
"""

from __future__ import annotations

import math

import numpy as np
import pandas as pd

from .trades import Trade


def infer_periods_per_year(index: pd.DatetimeIndex, default: int = 252) -> int:
    """Guess the annualisation factor from the bar spacing."""
    if index is None or len(index) < 3:
        return default
    deltas = pd.Series(index).diff().dropna()
    if deltas.empty:
        return default
    step = deltas.median()
    if pd.isna(step) or step <= pd.Timedelta(0):
        return default
    minutes = step.total_seconds() / 60.0
    minutes_per_year = 365.25 * 24 * 60
    if minutes <= 0:
        return default
    if minutes >= 60 * 20:          # ~daily or slower
        return 252
    if minutes >= 60 * 4:           # multi-hour bars
        return int(round(minutes_per_year / minutes))
    return int(round((252 * 390) / minutes))  # intraday: 390-min equity session


def max_drawdown(equity: pd.Series) -> float:
    if equity.empty:
        return 0.0
    running_max = equity.cummax()
    dd = (equity - running_max) / running_max.replace(0.0, np.nan)
    return float(abs(dd.min())) if dd.notna().any() else 0.0


def drawdown_series(equity: pd.Series) -> pd.Series:
    running_max = equity.cummax()
    return (equity - running_max) / running_max.replace(0.0, np.nan)


def max_consecutive(flags: list[bool]) -> int:
    best = run = 0
    for flag in flags:
        run = run + 1 if flag else 0
        best = max(best, run)
    return best


def compute_metrics(
    equity_curve: pd.DataFrame,
    trades: list[Trade],
    risk_per_trade: float = 0.01,
    periods_per_year: int | None = None,
    risk_free: float = 0.0,
) -> dict:
    """Full metric set. ``equity_curve`` must contain an ``equity`` column."""
    if equity_curve is None or equity_curve.empty:
        return {}

    ppy = periods_per_year or infer_periods_per_year(equity_curve.index)
    equity = equity_curve["equity"].astype(float)
    rets = equity.pct_change().fillna(0.0)

    final_equity = float(equity.iloc[-1])
    start_equity = float(equity.iloc[0])
    total_return = (final_equity / start_equity - 1.0) if start_equity > 0 else 0.0

    span_days = (equity.index[-1] - equity.index[0]).days
    years = span_days / 365.25
    if years > 0 and final_equity > 0 and start_equity > 0:
        cagr = (final_equity / start_equity) ** (1.0 / years) - 1.0
    else:
        cagr = 0.0

    vol = float(rets.std(ddof=0))
    ann_vol = vol * math.sqrt(ppy)
    excess = rets - risk_free / ppy
    sharpe = float(excess.mean() / rets.std(ddof=0) * math.sqrt(ppy)) if rets.std(ddof=0) > 1e-12 else 0.0

    downside = rets[rets < 0]
    downside_std = float(downside.std(ddof=0)) if len(downside) > 1 else 0.0
    sortino = (
        float(excess.mean() / downside_std * math.sqrt(ppy)) if downside_std > 1e-12 else 0.0
    )

    mdd = max_drawdown(equity)
    calmar = (cagr / mdd) if mdd > 1e-9 else 0.0

    # ---- trade statistics -------------------------------------------------
    wins = [t for t in trades if t.is_winner]
    losses = [t for t in trades if not t.is_winner]
    n_trades = len(trades)
    win_rate = len(wins) / n_trades if n_trades else 0.0

    r_multiples = [t.r_multiple for t in trades if abs(t.r_multiple) > 1e-9]
    win_r = [t.r_multiple for t in trades if t.is_winner and abs(t.r_multiple) > 1e-9]
    loss_r = [t.r_multiple for t in losses if abs(t.r_multiple) > 1e-9]

    avg_win_R = float(np.mean(win_r)) if win_r else 0.0
    avg_loss_R = float(np.mean(loss_r)) if loss_r else 0.0
    expectancy_R = float(np.mean(r_multiples)) if r_multiples else 0.0

    gross_profit = float(sum(t.net_pnl for t in wins))
    gross_loss = float(-sum(t.net_pnl for t in losses))
    profit_factor = (gross_profit / gross_loss) if gross_loss > 1e-9 else float("inf")
    payoff = (avg_win_R / abs(avg_loss_R)) if avg_loss_R < -1e-9 else float("inf")

    # Mean P&L per trade as a fraction of equity at trade entry.
    expectancy_pct = float(np.mean([t.return_pct for t in trades])) if n_trades else 0.0
    expectancy_dollars = float(np.mean([t.net_pnl for t in trades])) if n_trades else 0.0

    max_consec_losses = max_consecutive([not t.is_winner for t in trades])
    max_consec_wins = max_consecutive([t.is_winner for t in trades])

    exit_counts: dict[str, int] = {}
    for t in trades:
        exit_counts[t.exit_reason] = exit_counts.get(t.exit_reason, 0) + 1

    exposure = 0.0
    if "in_market" in equity_curve.columns:
        exposure = float(equity_curve["in_market"].mean())

    avg_bars = float(np.mean([t.bars_held for t in trades])) if trades else 0.0

    per_symbol = _per_symbol(trades)

    return {
        "start": str(equity.index[0].date()),
        "end": str(equity.index[-1].date()),
        "years": round(years, 2),
        "periods_per_year": ppy,
        "start_equity": round(start_equity, 2),
        "final_equity": round(final_equity, 2),
        "total_return": round(total_return, 4),
        "cagr": round(cagr, 4),
        "ann_vol": round(ann_vol, 4),
        "max_drawdown": round(mdd, 4),
        "sharpe": round(sharpe, 3),
        "sortino": round(sortino, 3),
        "calmar": round(calmar, 3),
        "n_trades": n_trades,
        "n_wins": len(wins),
        "n_losses": len(losses),
        "win_rate": round(win_rate, 4),
        "avg_win_R": round(avg_win_R, 3),
        "avg_loss_R": round(avg_loss_R, 3),
        "expectancy_R": round(expectancy_R, 4),
        "expectancy_pct": round(expectancy_pct, 5),
        "expectancy_dollars": round(expectancy_dollars, 2),
        "profit_factor": round(profit_factor, 3),
        "payoff": round(payoff, 3),
        "max_consecutive_losses": max_consec_losses,
        "max_consecutive_wins": max_consec_wins,
        "avg_bars_held": round(avg_bars, 1),
        "exposure": round(exposure, 4),
        "exit_reasons": exit_counts,
        "per_symbol": per_symbol,
        "total_fees": round(sum(t.fees for t in trades), 2),
        "total_slippage": round(sum(t.slippage for t in trades), 2),
    }


def _per_symbol(trades: list[Trade]) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for t in trades:
        bucket = out.setdefault(t.symbol, {"trades": 0, "wins": 0, "pnl": 0.0, "r": 0.0})
        bucket["trades"] += 1
        bucket["wins"] += int(t.is_winner)
        bucket["pnl"] += t.net_pnl
        bucket["r"] += t.r_multiple
    return {
        sym: {
            "trades": b["trades"],
            "win_rate": round(b["wins"] / b["trades"], 3),
            "pnl": round(b["pnl"], 2),
            "total_R": round(b["r"], 2),
        }
        for sym, b in out.items()
    }


def monte_carlo_trades(
    trades: list[Trade],
    n_runs: int = 2000,
    seed: int = 7,
    risk_per_trade: float = 0.01,
) -> dict:
    """Bootstrap trade sequences to see the distribution of outcomes.

    Randomising the *order* of the realised trades (IID bootstrap) answers the
    question that a single equity curve cannot: how likely is a ruinous drawdown
    even when the edge is real?  It assumes trade returns are roughly
    independent -- a stated approximation, not a fact.
    """
    if not trades:
        return {}
    rng = np.random.default_rng(seed)
    rs = np.array([t.r_multiple for t in trades], dtype=float)
    if rs.size == 0:
        return {}

    risk_dollars = risk_per_trade
    ending: list[float] = []
    dd: list[float] = []
    consec: list[int] = []
    for _ in range(n_runs):
        sample = rng.choice(rs, size=rs.size, replace=True)
        # Compound each trade's R-multiple against the fixed fractional risk.
        equity = np.cumprod(1.0 + sample * risk_per_trade)
        ending.append(float(equity[-1]))
        peak = np.maximum.accumulate(equity)
        dd.append(float(((equity - peak) / np.maximum(peak, 1e-12)).min() * -1))
        consec.append(max_consecutive([bool(x < 0) for x in sample]))

    ending_arr = np.array(ending)
    dd_arr = np.array(dd)
    return {
        "n_runs": n_runs,
        "n_trades_per_run": int(rs.size),
        "p5_ending": round(float(np.percentile(ending_arr, 5)), 3),
        "p50_ending": round(float(np.percentile(ending_arr, 50)), 3),
        "p95_ending": round(float(np.percentile(ending_arr, 95)), 3),
        "p5_max_dd": round(float(np.percentile(dd_arr, 5)), 3),
        "p50_max_dd": round(float(np.percentile(dd_arr, 50)), 3),
        "p95_max_dd": round(float(np.percentile(dd_arr, 95)), 3),
        "p95_max_consec_losses": int(np.percentile(consec, 95)),
        "worst_ending": round(float(ending_arr.min()), 3),
        "best_ending": round(float(ending_arr.max()), 3),
        "prob_finish_positive": round(float((ending_arr > 1.0).mean()), 4),
    }


__all__ = [
    "compute_metrics",
    "max_drawdown",
    "drawdown_series",
    "infer_periods_per_year",
    "monte_carlo_trades",
]
