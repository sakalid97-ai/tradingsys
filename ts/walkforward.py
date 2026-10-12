"""Walk-forward validation.

The only honest way to test a parameterised strategy. A single backtest fits
parameters to the whole sample and then reports performance on that same
sample -- that measures the optimiser, not the edge.

Walk-forward splits history into consecutive folds::

    |<--- in-sample --->|gap|<--- out-of-sample --->|...
                        ^ purged                 ^ params frozen here

* On each fold a grid search picks the best parameters using **in-sample** data
  only.
* Those frozen parameters are then run on the **next** window, which the
  optimiser never saw.
* Each fold's out-of-sample result compounds into one continuous equity curve.

The final number is what the strategy would actually have earned trading it
this way, re-optimising every fold and learning nothing from the future.

Two details decide whether the result is trustworthy:

1. **The purge gap.** Indicators leak. The 200-day moving average on the last
   in-sample bar is computed from data overlapping the out-of-sample window, and
   a 20-day Donchian breakout needs 20 untouched bars to be meaningful. A gap of
   ``purge_bars`` stops that information bleeding across the boundary.
2. **Rolling, not anchored, windows.** An anchored in-sample window keeps
   growing, so old regimes dominate and parameters stop adapting. Rolling
   windows re-optimise on recent data, which is what a live system does.

What to look for in the output:

* **Out-of-sample expectancy close to in-sample expectancy** -- the edge survives.
* **Parameters that stay roughly stable across folds** -- the grid is finding
  signal, not noise. Wildly different parameters each fold means the optimiser
  is fitting noise, however good the out-of-sample numbers look.
* **Out-of-sample win rate and profit factor** -- should not collapse.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

import pandas as pd

from .backtest import BacktestConfig, Backtester
from .metrics import compute_metrics
from .risk import RiskConfig
from .strategy import StrategyParams, optimise_grid

log = logging.getLogger(__name__)

# Walk-forward needs enough out-of-sample data that each fold holds real trades.
# Below these limits a grid search always finds *something*, and reporting that
# as out-of-sample performance is the most common way a backtest lies.
MIN_BARS_TO_VALIDATE = 1200
MIN_DAYS_TO_VALIDATE = 60


@dataclass
class WalkForwardConfig:
    """Fold layout and optimisation settings."""

    n_folds: int = 5
    in_sample_ratio: float = 0.7     # fraction of each fold used for IS
    purge_bars: int = 25             # gap between IS end and OOS start
    grid: dict = field(default_factory=lambda: {
        "entry_lookback": [15, 20, 30, 40, 55],
        "adx_min": [15.0, 20.0, 25.0, 30.0],
        "atr_stop_mult": [1.5, 2.0, 2.5, 3.0],
        "atr_trail_mult": [2.0, 3.0, 4.0],
    })
    objective: str = "sharpe"        # "sharpe" | "expectancy" | "cagr"
    min_trades_is: int = 8           # reject params producing fewer trades
    rolling: bool = True             # False = anchored (in-sample grows)
    commission_per_share: float = 0.005
    commission_min: float = 1.0
    slippage_bps: float = 5.0

    def clamp(self) -> "WalkForwardConfig":
        if self.n_folds < 1:
            raise ValueError("n_folds must be >= 1")
        if not 0.1 <= self.in_sample_ratio < 1.0:
            raise ValueError("in_sample_ratio must be in [0.1, 1)")
        if self.purge_bars < 0:
            raise ValueError("purge_bars must be >= 0")
        if self.objective not in ("sharpe", "expectancy", "cagr"):
            raise ValueError(f"unknown objective {self.objective!r}")
        return self


@dataclass
class FoldResult:
    """One in-sample / out-of-sample cycle."""

    fold: int
    in_sample_start: str
    in_sample_end: str
    out_of_sample_start: str
    out_of_sample_end: str
    best_params: dict
    is_metrics: dict
    oos_metrics: dict
    combos_tested: int = 0
    note: str = ""

    def to_dict(self) -> dict:
        return {
            "fold": self.fold,
            "in_sample": [self.in_sample_start, self.in_sample_end],
            "out_of_sample": [self.out_of_sample_start, self.out_of_sample_end],
            "params": self.best_params,
            "is": self.is_metrics,
            "oos": self.oos_metrics,
            "combos_tested": self.combos_tested,
            "note": self.note,
        }


@dataclass
class WalkForwardResult:
    """Everything the walk-forward produced."""

    folds: list[FoldResult]
    oos_equity: pd.DataFrame
    oos_trades: list
    metrics: dict
    config: dict
    warnings: list[str] = field(default_factory=list)

    def summary(self) -> str:
        m = self.metrics
        lines = [
            "=" * 68,
            "WALK-FORWARD VALIDATION (out-of-sample only)",
            "=" * 68,
            f"  Folds                : {len(self.folds)}",
            f"  Out-of-sample period : {m.get('start')} -> {m.get('end')} "
            f"({m.get('years')} yrs)",
            f"  Final equity         : {m.get('final_equity', 0):,.0f}",
            f"  Total return         : {m.get('total_return', 0):.2%}",
            f"  CAGR                 : {m.get('cagr', 0):.2%}",
            f"  Max drawdown         : {m.get('max_drawdown', 0):.2%}",
            f"  Sharpe (ann.)        : {m.get('sharpe', 0):.2f}",
            f"  Trades               : {m.get('n_trades', 0)}",
            f"  Win rate             : {m.get('win_rate', 0):.2%}",
            f"  Expectancy / trade   : {m.get('expectancy_R', 0):.3f}R "
            f"({m.get('expectancy_pct', 0):.3%} of equity)",
            f"  Profit factor        : {m.get('profit_factor', 0):.2f}",
            "-" * 68,
            "  DEGRADATION  (in-sample expectancy -> out-of-sample expectancy)",
        ]
        for fold in self.folds:
            is_e = fold.is_metrics.get("expectancy_R")
            oos_e = fold.oos_metrics.get("expectancy_R")
            if is_e is None or oos_e is None:
                continue
            decay = ""
            if abs(is_e) > 1e-9:
                decay = f"   ({oos_e / is_e:+.0%} of IS)"
            lines.append(
                f"    fold {fold.fold}: IS {is_e:+.3f}R -> OOS {oos_e:+.3f}R{decay}"
            )
        lines.append("=" * 68)
        return "\n".join(lines)

    def parameter_stability(self) -> dict:
        """How much the optimised parameters move between folds.

        High spread means the grid is fitting noise: the parameters that "work"
        change completely each time, so the next fold's parameters are
        effectively random. Report this alongside the returns.
        """
        if not self.folds:
            return {}
        keys = self.folds[0].best_params.keys()
        stability: dict[str, dict] = {}
        for key in keys:
            values = [f.best_params.get(key) for f in self.folds
                      if f.best_params.get(key) is not None]
            if not values or not all(isinstance(v, (int, float)) for v in values):
                stability[key] = {"values": values, "numeric": False}
                continue
            mean = sum(values) / len(values)
            variance = sum((v - mean) ** 2 for v in values) / len(values)
            stability[key] = {
                "values": values,
                "mean": round(mean, 3),
                "spread": round(max(values) - min(values), 3),
                "std": round(variance ** 0.5, 3),
                "numeric": True,
            }
        return stability


class WalkForward:
    """Rolling in-sample optimise, out-of-sample trade, repeat."""

    def __init__(
        self,
        data: dict[str, pd.DataFrame],
        config: WalkForwardConfig | None = None,
        risk_config: RiskConfig | None = None,
        base_params: StrategyParams | None = None,
        market_gate: pd.DataFrame | None = None,
    ) -> None:
        self.data = {s: f for s, f in data.items() if f is not None and not f.empty}
        self.cfg = (config or WalkForwardConfig()).clamp()
        self.risk = (risk_config or RiskConfig()).clamp()
        self.base = (base_params or StrategyParams()).clamp()
        self.market_gate = market_gate
        self.warnings: list[str] = []

        if not self.data:
            raise ValueError("walk-forward needs at least one symbol with data")

    # ------------------------------------------------------------------ layout

    def _calendar(self) -> pd.DatetimeIndex:
        idx: pd.DatetimeIndex | None = None
        for frame in self.data.values():
            idx = frame.index if idx is None else idx.union(frame.index)
        if idx is None:
            raise ValueError("no bars in any symbol")
        calendar = idx.sort_values()
        self._check_validatable(calendar)
        return calendar

    def _check_validatable(self, calendar: pd.DatetimeIndex) -> None:
        """Refuse to validate a history too short to validate.

        1-minute data is capped at 7 days by the data source, which is enough to
        TRADE but nowhere near enough to VALIDATE. This refuses outright rather
        than emitting a warning nobody reads.
        """
        bars = len(calendar)
        days = (calendar[-1] - calendar[0]).days if len(calendar) > 1 else 0
        self.data_bars = bars
        self.data_days = days
        if bars < MIN_BARS_TO_VALIDATE or days < MIN_DAYS_TO_VALIDATE:
            raise ValueError(
                f"refusing to validate on {bars:,} bars / {days} days of history. "
                f"Walk-forward needs at least {MIN_BARS_TO_VALIDATE:,} bars spanning "
                f"{MIN_DAYS_TO_VALIDATE} days so every fold holds real trades. "
                "1-minute bars are capped at 7 days by the data source, so they can be "
                "TRADED but not VALIDATED. Use 'py run.py walkforward --interval 1d "
                "--range 2y' on daily bars, or paper-trade forward and score it "
                "yourself.")

    def _folds(self, calendar: pd.DatetimeIndex) -> list[tuple]:
        """Return (is_start, is_end, oos_start, oos_end) per fold.

        Each fold covers ``is_len + purge + oos_len`` bars; the in-sample share
        of the non-purged span is fixed by ``in_sample_ratio``.
        """
        n = len(calendar)
        if n < 60:
            raise ValueError(f"need at least 60 bars to walk forward, got {n}")

        purge_total = self.cfg.purge_bars * max(self.cfg.n_folds - 1, 0)
        span = n - purge_total
        if span <= 0:
            raise ValueError(
                f"{self.cfg.n_folds} folds with {self.cfg.purge_bars} purge bars "
                f"need more than {n} bars")

        per_fold = max(1, span // self.cfg.n_folds)
        is_len = max(30, int(per_fold * self.cfg.in_sample_ratio))
        oos_len = max(10, per_fold - is_len)

        if self.cfg.n_folds * (is_len + oos_len) + purge_total > n:
            self.warnings.append(
                f"fold layout (is={is_len}, oos={oos_len}, folds="
                f"{self.cfg.n_folds}) exceeds the {n} bars available; later folds "
                "will be truncated")

        folds: list[tuple] = []
        cursor = 0
        for _ in range(self.cfg.n_folds):
            # rolling: the in-sample window slides forward each fold.
            # anchored: in-sample always starts at the beginning of history.
            is_start_pos = 0 if not self.cfg.rolling else cursor
            is_end_pos = min(is_start_pos + is_len, n - 1)
            if is_end_pos <= is_start_pos:
                break

            oos_start_pos = min(is_end_pos + self.cfg.purge_bars, n - 1)
            oos_end_pos = min(oos_start_pos + oos_len - 1, n - 1)
            if oos_end_pos < oos_start_pos:
                break

            folds.append((
                calendar[is_start_pos],
                calendar[is_end_pos],
                calendar[oos_start_pos],
                calendar[oos_end_pos],
            ))
            cursor = is_end_pos + self.cfg.purge_bars + oos_len
            if cursor >= n:
                break

        if not folds:
            raise ValueError(
                f"could not lay out {self.cfg.n_folds} folds over {n} bars; "
                "reduce n_folds or widen the date range")
        return folds

    # ------------------------------------------------------------- evaluation

    def _run(self, params, trade_from, trade_until, initial_capital):
        backtester = Backtester(
            strategy_params=params,
            risk_config=self.risk,
            config=BacktestConfig(
                initial_capital=initial_capital,
                slippage_bps=self.cfg.slippage_bps,
                commission_per_share=self.cfg.commission_per_share,
                commission_min=self.cfg.commission_min,
                trade_from=trade_from,
                trade_until=trade_until,
            ),
            market_gate=self.market_gate,
        )
        return backtester.run(self.data)

    def _score(self, metrics: dict) -> float:
        key = {"sharpe": "sharpe", "expectancy": "expectancy_R",
               "cagr": "cagr"}[self.cfg.objective]
        value = metrics.get(key)
        return float(value) if value is not None else float("-inf")

    def _optimise(self, is_start, is_end) -> tuple[StrategyParams, dict, int]:
        """Grid search using in-sample data only."""
        combos = optimise_grid(self.base, self.cfg.grid)
        best_params = None
        best_metrics: dict = {}
        best_score = float("-inf")
        tested = 0

        for params in combos:
            try:
                result = self._run(params, is_start, is_end, 100_000.0)
            except (ValueError, KeyError, IndexError) as exc:
                log.debug("combination failed: %s", exc)
                continue
            tested += 1
            metrics = result.metrics
            if metrics.get("n_trades", 0) < self.cfg.min_trades_is:
                continue
            score = self._score(metrics)
            if score > best_score:
                best_score, best_params, best_metrics = score, params, metrics

        if best_params is None:
            self.warnings.append(
                f"no combination produced >= {self.cfg.min_trades_is} trades on "
                f"{is_start.date()}..{is_end.date()}; using default parameters")
            return self.base, {}, tested
        return best_params, best_metrics, tested

    # --------------------------------------------------------------------- run

    def run(self, initial_capital: float = 100_000.0) -> WalkForwardResult:
        calendar = self._calendar()
        layout = self._folds(calendar)

        folds: list[FoldResult] = []
        equity = initial_capital
        oos_frames: list[pd.DataFrame] = []
        oos_trades: list = []

        for fold_number, (is_start, is_end, oos_start, oos_end) in enumerate(
                layout, start=1):
            params, is_metrics, tested = self._optimise(is_start, is_end)
            oos_result = self._run(params, oos_start, oos_end, equity)
            oos_metrics = oos_result.metrics

            # Compound: carry realised equity into the next fold.
            equity = float(oos_metrics.get("final_equity") or equity)

            frame = oos_result.equity_curve.copy()
            frame["fold"] = fold_number
            oos_frames.append(frame)
            oos_trades.extend(oos_result.trades)

            folds.append(FoldResult(
                fold=fold_number,
                in_sample_start=str(is_start.date()),
                in_sample_end=str(is_end.date()),
                out_of_sample_start=str(oos_start.date()),
                out_of_sample_end=str(oos_end.date()),
                best_params=params.to_dict(),
                is_metrics=_trim(is_metrics),
                oos_metrics=_trim(oos_metrics),
                combos_tested=tested,
            ))

            print(f"  fold {fold_number}: IS {is_start.date()}..{is_end.date()} -> "
                  f"OOS {oos_start.date()}..{oos_end.date()}   "
                  f"equity={equity:,.0f}   trades={oos_metrics.get('n_trades', 0)}")

        if not oos_frames:
            raise ValueError("walk-forward produced no out-of-sample equity")

        equity_curve = pd.concat(oos_frames).sort_index()
        metrics = compute_metrics(equity_curve, oos_trades,
                                 risk_per_trade=self.risk.risk_per_trade)
        metrics["n_folds"] = len(folds)
        metrics["total_fees"] = round(sum(t.fees for t in oos_trades), 2)

        return WalkForwardResult(
            folds=folds,
            oos_equity=equity_curve,
            oos_trades=oos_trades,
            metrics=metrics,
            config={
                "n_folds": self.cfg.n_folds,
                "in_sample_ratio": self.cfg.in_sample_ratio,
                "purge_bars": self.cfg.purge_bars,
                "objective": self.cfg.objective,
                "rolling": self.cfg.rolling,
                "grid": self.cfg.grid,
                "initial_capital": initial_capital,
                "risk_per_trade": self.risk.risk_per_trade,
            },
            warnings=list(dict.fromkeys(self.warnings)),
        )


def _trim(metrics: dict) -> dict:
    """Keep headline numbers so fold records stay readable."""
    keys = ("total_return", "cagr", "max_drawdown", "sharpe", "n_trades",
            "win_rate", "expectancy_R", "profit_factor")
    return {k: metrics[k] for k in keys if k in metrics}


__all__ = ["WalkForward", "WalkForwardConfig", "WalkForwardResult", "FoldResult"]
