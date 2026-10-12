# Autonomous Trading System

A complete, self-contained trading system: **market data -> analysis -> backtest
-> validation -> execution -> live management -> dashboard**.

Runs entirely on the Python standard library plus `pandas`/`numpy`. No broker
SDK, no RPC service, no framework. The desktop shortcut launches a real web
dashboard with a live equity curve, open positions, trade journal and kill
switch.

```
py run.py backtest          # analyse + validate on history
py run.py dashboard          # web dashboard on http://127.0.0.1:8642
py run.py paper              # simulated live trading
py run.py live               # real money (explicit opt-in required)
py run.py report             # metrics from the state store
```

---

## The strategy

Trend-continuation breakout with a market regime filter. Long-only by default.

| Layer | Rule |
|---|---|
| **Regime gate** | SPY must be above its 200-day average, otherwise no new entries |
| **Entry** | Close makes a new 20-day high, ADX(14) >= 20, volume above the 20-day average |
| **Initial stop** | Entry − 2 × ATR(14) |
| **Trailing stop** | Chandelier: highest close − 3 × ATR, **ratchets upward only** |
| **Trend exit** | Close breaks the 10-day low |
| **Size** | `quantity = (equity × 1%) / (2 × ATR)` — risk-based, not price-based |
| **Caps** | 8 positions, 6% total open risk, −3% daily stop, −15% drawdown halt |

The regime filter exists because trend systems lose steadily in sideways
markets; skipping it is the single largest common cause of failure.

### "1% a trade"

Two different things get called "1% per trade":

| Meaning | Value |
|---|---|
| **Risk per trade** (what this system does) | 1% of equity is *at risk* on every trade — a loss costs you 1%, a winner pays a multiple of it |
| **Average return per trade** (what you asked for) | the *expected* P&L as a fraction of equity, per trade |

This system delivers the first by construction (asserted by tests), and
**0.568R ≈ 0.56% of equity per trade** on the validation set below. That is
about half your 1% target. It is honest: promising a guaranteed 1% average per
trade would be a fabrication, and no trend system produces that.

What 0.56%/trade compounds to, given a 45% win rate and ~21 trades/year:

```
5-year result (2021-10 -> 2026-10, 15 US large-cap ETFs and equities)

  Total return      98.09%
  CAGR              14.67%
  Max drawdown       9.76%
  Sharpe             1.14
  Sortino            1.30
  Calmar             1.50
  Trades            127
  Win rate          44.88%
  Avg win         +2.35R
  Avg loss         -0.88R
  Expectancy        +0.568R / trade  (0.562% of equity)
  Profit factor      2.52
  Max consec. losses    7
```

Costs are **included**: 5 bps slippage per fill in the direction that hurts you,
plus commission. Entries fill at the **next** bar's open, never the signal bar.

---

## Multi-agent system (indices, forex, crypto)

A second, independent system that trades **directional two-sided** markets (you
can be long *or* short) across three asset classes: indices, forex, and crypto.

```
py run.py agents --cycles 1              # one sweep, print the result
py run.py agents --cycle-seconds 60      # run continuously (dry run)
```

### The six agents

| Agent | Responsibility |
|---|---|
| **scanner** | Multi-market monitoring: fetches bars for all 14 instruments, refuses to publish *stale* data (a weekend index close must not become a signal) |
| **analysis** | Regime (fast/slow MAs), ADX-scaled trend strength, R² of close vs time, momentum z-score, Kaufman efficiency ratio, Donchian breakouts |
| **probability** | Bayesian engine: one Beta posterior per symbol over P(up), updated by Bayes factors derived from the analysis |
| **edge_detector** | Expectancy + cost + confidence gates, then delegates *how big* to Kelly |
| **guardian** | Portfolio risk. The only agent that can veto. Deliberately subscribes to no market reasoning so it cannot be talked out of a limit |
| **execution** | Order submission plus live stop/target management, intrabar |

Agents communicate over a message bus (publish/subscribe, 12 typed topics) and
never call each other, so any one can be replaced without breaking the others.
A slow network call in the scanner cannot block the execution of an
already-approved plan.

### Bayesian probability engine

One `Beta(α, β)` posterior per instrument, updated by Bayes factors:

```
posterior_odds = prior_odds × BF₁ × BF₂ × ...
     BF = P(evidence | up) / P(evidence | down)
```

Three design decisions worth calling out, each of which fixed a real bug found
while building it:

1. **Correlated evidence is discounted.** Momentum, trend quality, efficiency and
   breakout status are four noisy views of the *same* trend. Multiplying their
   Bayes factors as if independent manufactures confidence. `update_many`
   discounts the combined log-odds by `source_independence` (0.6), so four
   correlated sources behave like ~2.4 independent ones.
2. **Odds are capped.** A persistent trend yields BF > 1 on *every* cycle, so α
   grows geometrically and the posterior pinned itself at p_up = 0.9999 after a
   dozen cycles despite no genuinely new information. `max_odds` (12:1) caps how
   extreme the posterior may become, preserving max humility.
3. **Confidence is a real probability.** `confidence = P(p > 0.5)` computed from
   the Beta CDF, so `--min-confidence 0.65` means "65% sure the direction is
   right". It starts at exactly 0.5 for an uninformative Beta(2,2).

### Kelly position sizing

For a trade risking 1R to win `payoff` R, with win probability `p`:

```
f* = (p × payoff − (1 − p)) / payoff
```

Then three safeguards, because full Kelly assumes you know `p` exactly:

1. **Fractional Kelly** — `--kelly-fraction 0.25`. Over-betting is punished far
   more than under-betting, so real systems never run full Kelly on an estimate.
2. **Credibility shrinkage** — `p` is pulled toward the breakeven baseline when
   the posterior is not concentrated, so partial conviction gets a partial size.
3. **Hard risk cap** — the risk cap is the real governor: 1% of equity per trade.
   Kelly only decides whether and how aggressively to lean into that budget.

Sizing is **instrument-aware**, because a 1-point move is not the same P&L on
SPX500, EURUSD and BTCUSD:

| Class | 1 lot is worth | Value of a 1-point move |
|---|---|---|
| Index (CFD) | 1 USD/point | \$1 |
| Forex | 100,000 base currency | \$10 per pip on a major |
| Crypto | 1 coin | \$1 per 1 dollar of price |

### Two-sided trading

Long *and* short are first-class. `regime = −1` (price below slow MA, fast below
slow) makes the edge agent flip to `short`, take `p_win = 1 − p_up`, and mirror
the stop and target. Nothing in the risk layer treats shorting as special —
indices, forex, and crypto are all two-sided by configuration.

### Bounded evidence-weight

`edge_detector` requires `expectancy_R ≥ min_expectancy` **and**
`posterior confidence ≥ min_confidence` **and** a positive Kelly fraction. A
trade with non-positive expectancy is graded F no matter how confident the model
is — confidence is a statement about direction, not about whether the payoff
compensates for the risk.

## Plugging in an LLM analyst (FinRobot / CrewAI)

`ts/agents/analyst.py` bridges a language model into the Bayesian engine **without
letting it take over the risk path**. Try it:

```
py tradingsys\scripts\demo_analyst.py --simulate-skill good
py tradingsys\scripts\demo_analyst.py --simulate-skill none   # untested -> zero influence
py tradingsys\scripts\demo_analyst.py --simulate-skill bad    # invert
```

| Analyst track record | BF on a bullish call | Effect on the posterior |
|---|---|---|
| untested (< 10 scored calls) | **1.0000** | does not move at all |
| 50% (a coin flip) | 1.0000 | no influence |
| 80% accurate | 1.497 | leans it upward |
| 20% accurate | **0.668** | **inverts** -- argues for a fall |

Three rules make this safe:

1. **An unproven analyst has zero influence.** Until at least
   ``min_observations`` calls have been scored, ``skill()`` is 0, so the
   log-odds contribution is exactly 0 and the Bayes factor is exactly 1.0.
   Calibration has to be *earned* from a track record, not assumed from a
   prompt.
2. **Influence is bounded.** One call shifts the log-odds by at most
   ``max_log_odds x skill x skill_scale`` nats -- comparable to a single
   price-based feature. It is one opinion among several, never a veto, and even
   a perfect analyst cannot overrule strong price evidence.
3. **A bad analyst inverts.** Negative skill turns a bullish call into bearish
   evidence, which is the correct Bayesian treatment of a poorly-skilled source
   and falls out for free from calibrating on the track record.

Wiring your own model in takes one function -- no dependency on any framework:

```python
from ts.agents.analyst import AnalystAdapter

def call_finrobot(prompt: str) -> str:
    return finrobot.agents.analyse(prompt)      # or a CrewAI crew, or OpenAI

adapter = AnalystAdapter()
evidence = adapter.analyse("AAPL", context, call_finrobot)
if evidence is not None:
    engine.update(evidence)      # your existing BayesianEngine, unchanged
```

The live agent layer accepts it directly, so the multi-agent system uses it
without any other change:

```python
ProbabilityAgent(bus, analyst_adapter=adapter,
                 analyst_caller=call_finrobot,
                 context_provider=lambda symbol, analysis: build_context(symbol, analysis))
```

Every signal is journalled with direction, conviction, measured skill, log-odds
contribution, reasoning, model name and the current calibration table, so the
dashboard can show exactly what the model said and how much it was allowed to
matter.

**Rejection is the default.** Anything failing validation -- the wrong symbol, an
unknown direction, an out-of-range conviction, non-JSON prose -- returns `None`
rather than a fabricated signal. ``snapshot()["rejected"]`` reports the failure
count and the reason for each.

## What makes this not a fantasy

Every one of these is enforced in code and asserted by the test suite
(`py tradingsys/tests/test_suite.py` — 42 tests):

1. **No lookahead.** Signals use `shift(1)` rolling windows, and fills only
   happen on the bar *after* the signal. `test_no_lookahead_appending_future_bars`
   proves a signal cannot change when future bars are appended.
2. **Stops are evaluated intrabar** from the bar's low/high. A gap through your
   stop fills at the gap price — correctly worse than you planned.
3. **Cash is a real state variable.** Tests assert cash never goes negative and
   that `equity == cash + positions_value` on every bar.
4. **Risk is exact.** `test_risk_is_exactly_one_percent` asserts the sizer's
   dollar risk / equity == 1%.
5. **A deterministic stop-out test** verifies a loss lands at ~1R and never
   3R, and a gap-risk test documents why gaps can exceed it.
6. **Circular dependencies avoided**: the engine never imports the dashboard and
   the dashboard never imports the engine.

### Bugs found and fixed while building it

These were real, verified failures — kept here because they are exactly the
kinds of mistakes that make backtests lie:

- **`plus_di + plus_di` instead of `plus_di + minus_di`** in ADX — halved every
  DX value, so the "ADX >= 20" filter was silently behaving like >= 40.
- **Donchian channels shifted twice** — `donchian_upper()` already shifts by 1,
  and the strategy shifted again, delaying every breakout by an extra bar.
- **`DrawdownMonitor` conflated threshold with observed value** — tripped the
  drawdown brake on bar one and produced a system with zero trades.
- **Paper broker ignored entry commissions** — realised P&L disagreed with cash.
- **`Trade.pnl` stored vs `net_pnl` computed** — the two could drift; P&L is now
  derived from fill prices and fees only.

---

## Architecture

```
tradingsys/
├── run.py                     CLI entry point
├── Dashboard.bat              desktop launcher
├── config/universe.txt        symbols, one per line
├── ts/
│   ├── ...
│   ├── agents/
│   │   ├── bus.py          pub/sub message bus + typed topics
│   │   ├── base.py         agent lifecycle
│   │   ├── instruments.py  index/forex/crypto specs
│   │   ├── feed.py         multi-asset market data + staleness
│   │   ├── analysis.py     market analysis agent
│   │   ├── probability.py  Bayesian probability agent
│   │   ├── edge.py         edge detection agent
│   │   ├── kelly.py        Kelly position sizing
│   │   ├── execution.py    execution + trade management agent
│   │   ├── guardian.py     risk controls agent
│   │   └── supervisor.py   the orchestrator
│   ├── broker/
│   │   ├── base.py            Broker interface
│   │   ├── paper.py           simulated fills against the live price feed
│   │   └── alpaca.py          Alpaca REST adapter (paper + live)
│   ├── quant.py            Beta CDF/quantile, no SciPy
│   └── web/                stdlib HTTP server + dashboard assets
├── tests/
│   ├── test_suite.py      42 tests: backtest invariants, risk, broker
│   └── test_agents.py     44 tests: Bayesian, Kelly, instruments, bus
```

The dashboard server reads only from `state/`, so the UI keeps working (showing
last-known state) even when the engine is stopped, and a UI crash can never stop
trading.

---

## Using it

### 1. Analyse and validate

```
py run.py backtest --range 5y
```

Useful flags: `--range max`, `--risk 0.01`, `--entry-lookback 20`, `--adx 20`,
`--stop 2`, `--trail 3`, `--max-positions 8`, `--gate SPY`, `--shorts`.

### 2. Watch it

```
py run.py dashboard
```

Dashboards shows: equity curve and drawdown, live KPIs, open positions with
open R, the full trade journal, per-symbol stats, Monte Carlo outcome ranges,
a live signal scanner, and the event log.

From the desktop, double-click **Trading Dashboard** (created by
`py tradingsys\scripts\make_shortcut.py`).

### 3. Trade it

```
py run.py paper --ticks 20          # simulated fills, no orders leave the box
py run.py paper --dry-run --ticks 5 # signals only, nothing submitted
```

Paper trading is the only mode enabled by default. Real trading needs:

```
ALPACA_KEY_ID=...
ALPACA_SECRET_KEY=...
ALPACA_LIVE_CONFIRMED=yes      # you must assert this out loud
py run.py paper --capital 10000  # same engine, Alpaca paper endpoint
```

The Alpaca adapter refuses to construct on the live endpoint unless
`mode='live'` is passed **and** `ALPACA_LIVE_CONFIRMED=yes` is set.

---

## Safety

Five independent brakes, all live during backtest, paper and live trading:

| Brake | Trigger |
|---|---|
| Kill switch | `state/KILL` exists, or the dashboard button — flattens and halts |
| Daily loss limit | −3% on the session: flatten, no new entries until next session |
| Drawdown halt | −15% from peak equity: freeze all new entries |
| Position cap | 8 simultaneous trades |
| Risk cap | 6% of equity at risk at once |
| Cash guard | an order can never exceed available cash |

---

## Known limitations — read before trusting it

1. **One 5-year, single-market sample.** 127 trades on US large-cap equities
   through 2021–2026 is not a broad validation. Expect the live distribution to
   be worse than the backtest, not better.
2. **No walk-forward is wired into the CLI yet.** Parameters were chosen from
   the literature, not fitted to this sample — which is the point — but that
   means nobody has formally verified they are optimal out-of-sample.
3. **Monte Carlo assumes trade independence.** It shuffles realised trades;
   real losses cluster, so it will **understate** true drawdown risk.
4. **Slippage is a fixed 5 bps.** Real gaps, halts and illiquid names are worse.
   Small-cap names with wide spreads will under-perform materially.
5. **Corporate actions.** Split/dividend adjustments come from the data source;
   the backtester does not model ex-dividend cash separately.
6. **One bar per decision.** Intraday behaviour is approximated by the bar's
   high/low, not tick data. Stops assume worst-case ordering.
7. **You can still turn it off.** The kill switch halts trading but cannot
   return money already lost.

## Extending it

- **New instrument**: add the symbol to `config/universe.txt`.
- **New strategy**: subclass `Strategy` in `ts/strategy.py`; everything
  downstream (risk, backtest, metrics, dashboard, engine) is strategy-agnostic.
- **New broker**: implement `ts/broker/base.py::Broker` — ten methods.
- **Different timeframes**: `--interval 1h` on `dashboard`/`paper`, and the
  metrics layer infers the annualisation factor from the bar spacing.
