"""Forward-tracking journal: did the edge survive reality?

A backtest says "expect 45% win rate, 0.57R expectancy". Forward trading will
never match that exactly, and small samples drift constantly for no reason at
all. Everything here exists to answer one question honestly:

**Is the difference between what I predicted and what I am seeing noise, or is
the edge gone?**

That needs statistics, not eyeballing:

* **Win rate** is a binomial proportion, so it gets a Wilson score interval --
  the interval that stays honest at extreme proportions, where the normal
  approximation would claim a negative lower bound.
* **Expectancy** is a mean of R-multiples, so it gets a mean-and-standard-error
  interval.

The verdict is then a genuine comparison: if the *predicted* value falls outside
the interval computed from *realised* trades, the difference is larger than
sampling noise can explain. Until then, it is noise, and acting on it would be
worse than doing nothing.

What this deliberately does NOT do: adjust the strategy. A drifting win rate
after 20 trades is almost always noise; tuning to it is how a working system
gets destroyed.
"""

from __future__ import annotations

import json
import logging
import math
import statistics
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path

log = logging.getLogger(__name__)

# N trades below which any comparison is close to meaningless. Reported so the
# dashboard can say "not enough data" instead of implying a verdict.
MIN_SAMPLE = 20
CONFIDENCE = 0.95


# ------------------------------------------------------------------- statistics


def wilson_interval(successes: int, total: int, confidence: float = CONFIDENCE) -> tuple[float, float]:
    """Wilson score interval for a binomial proportion.

    Preferred over the normal approximation because it behaves correctly at
    p=0 and p=1, and it never returns an interval outside [0, 1] -- the normal
    approximation's lower bound goes negative on small samples, which would
    make a 0% realised win rate look far worse than it is.
    """
    if total == 0:
        return 0.0, 0.0
    if successes < 0 or successes > total:
        raise ValueError("successes must be within [0, total]")

    z = {0.90: 1.6449, 0.95: 1.95996, 0.99: 2.5758}.get(round(confidence, 2), 1.95996)
    p = successes / total
    z2 = z * z
    denominator = 1.0 + z2 / total
    centre = p + z2 / (2.0 * total)
    margin = z * math.sqrt((p * (1.0 - p) + z2 / (4.0 * total)) / total)
    low = (centre - margin) / denominator
    high = (centre + margin) / denominator
    return max(0.0, low), min(1.0, high)


def mean_interval(values: list[float], confidence: float = CONFIDENCE) -> tuple[float, float]:
    """Confidence interval for a mean, using the t-like normal multiplier.

    Deliberately approximate: the t distribution's critical values need a table,
    and the normal multiplier is slightly optimistic -- which makes the interval
    slightly too narrow, erring toward "not enough evidence to worry".
    """
    if not values:
        return 0.0, 0.0
    n = len(values)
    if n < 2:
        return float(values[0]), float(values[0])
    mean = statistics.fmean(values)
    std = statistics.stdev(values)
    z = {0.90: 1.6449, 0.95: 1.95996, 0.99: 2.5758}.get(round(confidence, 2), 1.95996)
    margin = z * std / math.sqrt(n)

    # Floor the half-width so a zero-variance sample cannot produce a
    # degenerate interval. Without this, a run of identical R-multiples gives
    # an interval of zero width, and every prediction other than the exact
    # sample mean lands outside it -- reporting noise as decisive drift.
    scale = max(abs(mean), 0.5)
    margin = max(margin, 0.10 * math.sqrt(scale) + 0.05)
    return mean - margin, mean + margin


# ------------------------------------------------------------------- trades


@dataclass
class ForwardTrade:
    """One completed forward trade."""

    symbol: str
    side: str
    quantity: float
    entry_price: float
    exit_price: float
    r_multiple: float
    net_pnl: float
    entry_time: str = ""
    exit_time: str = ""
    exit_reason: str = ""
    strategy: str = "trend_breakout"
    broker: str = "paper"
    notes: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_engine_trade(cls, trade, strategy: str, broker: str) -> "ForwardTrade":
        """Adapt a ts.trades.Trade recorded by the engine."""
        return cls(
            symbol=trade.symbol,
            side=trade.side,
            quantity=float(trade.quantity),
            entry_price=float(trade.entry_price),
            exit_price=float(trade.exit_price),
            r_multiple=float(trade.r_multiple),
            net_pnl=float(trade.net_pnl),
            entry_time=str(trade.entry_time),
            exit_time=str(trade.exit_time),
            exit_reason=str(trade.exit_reason),
            strategy=strategy,
            broker=broker,
        )


@dataclass
class Baseline:
    """What the backtest predicted, for comparison."""

    win_rate: float | None = None
    expectancy_r: float | None = None
    profit_factor: float | None = None
    avg_win_r: float | None = None
    avg_loss_r: float | None = None
    n_trades: int | None = None
    source: str = ""
    captured_at: str = ""

    @classmethod
    def from_metrics(cls, metrics: dict, source: str = "backtest") -> "Baseline":
        return cls(
            win_rate=metrics.get("win_rate"),
            expectancy_r=metrics.get("expectancy_R"),
            profit_factor=metrics.get("profit_factor"),
            avg_win_r=metrics.get("avg_win_R"),
            avg_loss_r=metrics.get("avg_loss_R"),
            n_trades=metrics.get("n_trades"),
            source=source,
            captured_at=datetime.now(UTC).isoformat(),
        )

    def to_dict(self) -> dict:
        return asdict(self)


# ----------------------------------------------------------------- the journal


class ForwardJournal:
    """Append-only record of forward trades with prediction-vs-reality drift."""

    def __init__(self, root: str | Path = "state") -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.trades_path = self.root / "forward_trades.jsonl"
        self.baseline_path = self.root / "forward_baseline.json"

    # ------------------------------------------------------------------ writing

    def record(self, trade: ForwardTrade) -> None:
        line = json.dumps(trade.to_dict(), default=str)
        with self.trades_path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")

    def record_many(self, trades) -> int:
        count = 0
        for trade in trades:
            self.record(trade)
            count += 1
        return count

    def set_baseline(self, baseline: Baseline) -> None:
        self.baseline_path.write_text(json.dumps(baseline.to_dict(), indent=2),
                                      encoding="utf-8")

    def capture_baseline_from_state(self, state_store=None) -> Baseline | None:
        """Pull the most recent backtest metrics in as the baseline."""
        metrics = {}
        if state_store is not None:
            metrics = (state_store.read_backtest() or {}).get("metrics", {})
        elif self.root.joinpath("backtest.json").exists():
            try:
                metrics = json.loads((self.root / "backtest.json").read_text(
                    encoding="utf-8")).get("metrics", {})
            except (OSError, json.JSONDecodeError):
                metrics = {}
        if not metrics:
            return None
        baseline = Baseline.from_metrics(metrics, source="last backtest")
        self.set_baseline(baseline)
        return baseline

    # ------------------------------------------------------------------ reading

    def load(self) -> list[ForwardTrade]:
        if not self.trades_path.exists():
            return []
        out: list[ForwardTrade] = []
        try:
            for line in self.trades_path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    payload = json.loads(line)
                    if not isinstance(payload, dict):
                        raise TypeError("record is not an object")
                except json.JSONDecodeError:
                    continue
                try:
                    out.append(ForwardTrade(**{
                        k: v for k, v in payload.items()
                        if k in ForwardTrade.__dataclass_fields__}))
                except (TypeError, KeyError) as exc:
                    # a truncated or hand-edited record must not take the whole
                    # journal with it
                    log.warning("skipping unreadable forward record: %s", exc)
                    continue
        except OSError as exc:
            log.warning("could not read the forward journal: %s", exc)
        return out

    def load_baseline(self) -> Baseline | None:
        if not self.baseline_path.exists():
            return None
        try:
            payload = json.loads(self.baseline_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        return Baseline(**{k: v for k, v in payload.items()
                           if k in Baseline.__dataclass_fields__})

    # ----------------------------------------------------------------- analysis

    def summary(self) -> dict:
        """Realised performance, with drift judged statistically."""
        trades = self.load()
        baseline = self.load_baseline()

        if not trades:
            return {
                "n_trades": 0,
                "baseline": baseline.to_dict() if baseline else None,
                "verdict": "no data",
                "note": "no forward trades recorded yet",
            }

        r_values = [t.r_multiple for t in trades]
        pnl = [t.net_pnl for t in trades]
        wins = [t for t in trades if t.net_pnl > 0]
        losses = [t for t in trades if t.net_pnl <= 0]
        n = len(trades)

        win_rate = len(wins) / n
        expectancy_r = statistics.fmean(r_values) if r_values else 0.0
        total_pnl = sum(pnl)
        gross_profit = sum(p for p in pnl if p > 0)
        gross_loss = -sum(p for p in pnl if p < 0)
        profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else None

        # equity curve and drawdown over the forward trades
        equity, peak, max_dd = 0.0, 0.0, 0.0
        curve: list[dict] = []
        for trade in trades:
            equity += trade.net_pnl
            peak = max(peak, equity)
            max_dd = max(max_dd, peak - equity)
            curve.append({"t": trade.exit_time, "pnl": round(equity, 2)})

        win_low, win_high = wilson_interval(len(wins), n)
        exp_low, exp_high = mean_interval(r_values)

        payload = {
            "n_trades": n,
            "n_wins": len(wins),
            "n_losses": len(losses),
            "win_rate": round(win_rate, 4),
            "win_rate_ci": [round(win_low, 4), round(win_high, 4)],
            "expectancy_r": round(expectancy_r, 4),
            "expectancy_r_ci": [round(exp_low, 4), round(exp_high, 4)],
            "total_pnl": round(total_pnl, 2),
            "profit_factor": round(profit_factor, 3) if profit_factor else None,
            "max_drawdown": round(max_dd, 2),
            "avg_win_r": round(statistics.fmean([t.r_multiple for t in wins]), 3)
            if wins else None,
            "avg_loss_r": round(statistics.fmean([t.r_multiple for t in losses]), 3)
            if losses else None,
            "by_strategy": _group(trades, "strategy"),
            "by_symbol": _group(trades, "symbol"),
            "exit_reasons": _count(trades, "exit_reason"),
            "equity_curve": curve,
            "baseline": baseline.to_dict() if baseline else None,
        }
        payload["drift"] = self._drift(payload, baseline, n)
        payload["verdict"] = _verdict(payload["drift"], n)
        return payload

    def _drift(self, realised: dict, baseline: Baseline | None,
               n: int) -> dict:
        """Compare each predicted value against the realised interval.

        A metric is only called out when the *prediction* sits outside the
        interval the realised sample can support -- that is the definition of
        "larger than noise".
        """
        if baseline is None:
            return {"status": "no baseline",
                    "note": "capture a baseline from the last backtest to compare"}

        out: dict = {"status": "tracking", "metrics": {}, "n_trades": n}

        def compare(name, predicted, ci, direction="inside", fmt="pct"):
            if predicted is None or not ci:
                return None
            low, high = ci
            outside = predicted < low or predicted > high
            record = {
                "predicted": predicted,
                "ci_low": low,
                "ci_high": high,
                "outside_ci": outside,
            }
            if fmt == "pct":
                record["gap"] = round(realised.get(name, 0.0) - predicted, 4)
            return record

        wr = realised.get("win_rate")
        if baseline.win_rate is not None and wr is not None:
            lo, hi = realised["win_rate_ci"]
            record = compare("win_rate", baseline.win_rate, (lo, hi))
            if record:
                out["metrics"]["win_rate"] = record

        if baseline.expectancy_r is not None:
            lo, hi = realised["expectancy_r_ci"]
            record = compare("expectancy_r", baseline.expectancy_r, (lo, hi))
            if record:
                out["metrics"]["expectancy_r"] = record

        if out["metrics"] and all(m["outside_ci"] for m in out["metrics"].values()):
            out["status"] = "drifting"
        elif any(m["outside_ci"] for m in out["metrics"].values()):
            out["status"] = "partially drifting"
        return out

    def reset(self) -> None:
        for path in (self.trades_path, self.baseline_path):
            try:
                path.unlink()
            except OSError:
                pass


# ------------------------------------------------------------------- helpers


def _group(trades, key: str) -> dict:
    out: dict[str, dict] = {}
    for trade in trades:
        bucket = out.setdefault(str(getattr(trade, key)), {"trades": 0, "wins": 0,
                                                          "pnl": 0.0, "r": 0.0})
        bucket["trades"] += 1
        bucket["wins"] += int(trade.net_pnl > 0)
        bucket["pnl"] += trade.net_pnl
        bucket["r"] += trade.r_multiple
    return {k: {"trades": v["trades"],
                "win_rate": round(v["wins"] / v["trades"], 3),
                "pnl": round(v["pnl"], 2),
                "total_r": round(v["r"], 2)} for k, v in out.items()}


def _count(trades, key: str) -> dict:
    out: dict[str, int] = {}
    for trade in trades:
        value = str(getattr(trade, key) or "unknown")
        out[value] = out.get(value, 0) + 1
    return out


def _verdict(drift: dict, n: int) -> str:
    """Plain-language verdict, with the sample size kept in view."""
    if n < MIN_SAMPLE:
        return (f"too early - {n} of {MIN_SAMPLE} trades. Anything you see now is "
                "as likely to be noise as signal; do not adjust anything.")
    status = drift.get("status")
    if status == "drifting":
        return ("DRIFTING: the backtest's predictions fall outside what this sample "
                "supports. Treat the edge as unproven and stop sizing up.")
    if status == "partially drifting":
        return ("PARTIAL DRIFT: one metric disagrees beyond noise. Watch it; do not "
                "change parameters yet.")
    if drift.get("status") == "no baseline":
        return "no baseline captured yet"
    return ("TRACKING: realised performance is within sampling noise of the "
            "backtest's predictions. Keep going.")


__all__ = [
    "ForwardJournal",
    "ForwardTrade",
    "Baseline",
    "wilson_interval",
    "mean_interval",
    "MIN_SAMPLE",
]
