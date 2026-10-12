"""Guard against validating on data too short to validate.

7 days of 1-minute bars is enough to trade but nowhere near enough to validate:
a grid search over ~2,700 bars will always find parameters that look brilliant
and mean nothing. Walk-forward now refuses rather than reporting that curve.
"""

from __future__ import annotations

from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
server = ROOT / "ts" / "web" / "context.py"

text = server.read_text(encoding="utf-8")

old = "            agent_feed = MarketFeed(feed=feed, interval=interval, range_=range_)"
new = '''            agent_feed = MarketFeed(feed=feed, interval=interval, range_=range_)
            # 1m data is capped at 7 days; anything shorter than that on a
            # session instrument cannot support the strategy's warm-up.
            if interval == "1m" and range_ in ("1d", "5d"):
                range_ = "7d"'''
if old not in text:
    print("[FAIL] context.py: MarketFeed anchor not found")
    sys.exit(1)
text = text.replace(old, new, 1)
server.write_text(text, encoding="utf-8")
print("[ok]   context.py: 1m range floored at 7d")

# ------------------------------------------------------------- walkforward guard
wf = ROOT / "ts" / "walkforward.py"
text = wf.read_text(encoding="utf-8")

if "MIN_BARS_TO_VALIDATE" in text:
    print("[skip] walkforward.py: validation guard already present")
    sys.exit(0)

old_log = 'log = logging.getLogger(__name__)'
new_log = '''log = logging.getLogger(__name__)

# Walk-forward needs enough out-of-sample bars that each fold contains real
# trades. Anything below this cannot produce a trustworthy number, and a grid
# search run on too little data manufactures an edge that does not exist.
MIN_BARS_TO_VALIDATE = 1200
MIN_DAYS_TO_VALIDATE = 60'''
if old_log not in text:
    print("[FAIL] walkforward.py: log anchor not found")
    sys.exit(1)
text = text.replace(old_log, new_log, 1)

old_cal = '''    def _calendar(self) -> pd.DatetimeIndex:
        idx: pd.DatetimeIndex | None = None
        for frame in self.data.values():
            idx = frame.index if idx is None else idx.union(frame.index)
        if idx is None:
            raise ValueError("no bars in any symbol")
        return idx.sort_values()'''
new_cal = '''    def _calendar(self) -> pd.DatetimeIndex:
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

        A grid search over a small sample always finds *something*. Reporting
        that as out-of-sample performance is the single most common way a
        backtest lies, so it is refused outright rather than warned about.
        """
        bars = len(calendar)
        days = (calendar[-1] - calendar[0]).days if len(calendar) > 1 else 0
        self.data_days = days
        self.data_bars = bars

        if bars < MIN_BARS_TO_VALIDATE or days < MIN_DAYS_TO_VALIDATE:
            raise ValueError(
                f"refusing to validate on {bars:,} bars / {days} days of history. "
                f"Walk-forward needs at least {MIN_BARS_TO_VALIDATE:,} bars spanning "
                f"{MIN_DAYS_TO_VALIDATE} days so each fold holds real trades. "
                "1-minute data is capped at 7 days by the data source, so it can be "
                "TRADED but not VALIDATED -- run 'py run.py walkforward --interval 1d "
                "--range 2y' on daily bars instead, or paper-trade forward and score "
                "the results yourself.")'''
if old_cal not in text:
    print("[FAIL] walkforward.py: _calendar anchor not found")
    sys.exit(1)
text = text.replace(old_cal, new_cal, 1)
wf.write_text(text, encoding="utf-8")
print("[ok]   walkforward.py: refuses to validate on short data")

sys.path.insert(0, str(ROOT))
try:
    from ts.walkforward import WalkForward, MIN_BARS_TO_VALIDATE, MIN_DAYS_TO_VALIDATE
    print(f"[ok] imports clean (min {MIN_BARS_TO_VALIDATE:,} bars / "
          f"{MIN_DAYS_TO_VALIDATE} days)")
except Exception as exc:  # noqa: BLE001
    print(f"[FAIL] import error: {exc}")
sys.exit(0)
