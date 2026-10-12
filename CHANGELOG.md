# Changelog

All notable changes to this project are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and
this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Version numbers track the trading system as a whole. A change that alters how a
signal is generated, how a position is sized, or how risk is enforced is a
**minor** bump at least, because it changes the strategy's behaviour and can
invalidate any forward-tracking history recorded before it. When that happens the
changelog says so explicitly.

---

## [1.2.0] — 2026-10-12

### Added

**Multi-agent system** (`ts/agents/`)
- Six agents over a typed pub/sub bus: scanner, analysis, probability,
  edge detection, guardian, execution.
- Bayesian probability engine per symbol, updated by Bayes factors with
  correlated-source discounting and an odds cap.
- Kelly-criterion sizing for two-sided trades, with fractional Kelly,
  credibility shrinkage toward the breakeven baseline, and a hard risk cap.
- Instrument specs for indices, forex and crypto, so a 1-point move is priced
  correctly per asset class.
- Market regime gate: no new entries while the broad market is risk-off.
- Risk guardian that can veto every other agent, with per-trade, total-risk,
  position-count, daily-loss and drawdown limits.

**LLM analyst adapter** (`ts/agents/analyst.py`)
- Turns any language model's output into bounded evidence for the Bayesian
  engine, via a single `callable(prompt) -> str` contract.
- An unproven analyst has **zero** influence until calibrated on a track
  record; a poorly-skilled analyst's calls invert into counter-evidence.
- FinRobot / CrewAI compatible with no dependency on either.

**Prop-firm support** (`ts/propfirm.py`)
- Rules profiles for FTMO, The5ers, MyForexFunds, FundedNext, E8, a generic
  conservative profile, and custom.
- Limits enforced *inside* the firm's own, with a configurable safety buffer for
  gaps and slippage.
- Profit-target, minimum-trading-days and instrument-prohibition tracking.

**Forward-tracking journal** (`ts/forward.py`)
- Records every live/paper trade and compares realised performance against the
  last backtest's predictions.
- Wilson score interval for win rate and a mean-and-standard-error interval for
  expectancy, so a verdict distinguishes noise from real drift.
- Refuses to draw a conclusion below 20 trades.

**Two-way command channel** (`ts/notify.py`, `ts/commands.py`)
- Alerts pushed to Telegram and/or a webhook, with rate limiting and severity
  gating.
- Commands parsed from messages and applied: `/status`, `/positions`, `/kill`,
  `/resume`, `/pause`, `/start`, `/set <key> <value>`, `/run <command>`,
  `/forward`, `/help`.
- Raising a risk limit requires a confirming reply; anything but an explicit
  yes cancels.
- Commands are accepted only from the configured chat id; other senders are
  ignored and counted.

**Settings and broker layer**
- Persistent settings across four panels: broker, strategy, instruments, risk.
- Broker registry and adapters: paper, Binance (spot/futures, HMAC-signed),
  Alpaca, Exness via MetaTrader 5, and a generic webhook/file bridge.
- Credentials resolve from the environment first and are never written to disk
  unless explicitly requested.

**Dashboard**
- Views: Overview, Positions, Trades, Backtest, Agents, Scanner, Forward,
  Control, Setup, Logs.
- Control panel runs every CLI command from cards with switches, selects and
  number inputs — no command line needed.
- Setup panels for broker connection, strategy selection,
  instrument/timeframe selection, and position sizing/risk.
- Live ticker tape, hero equity strip, KPI sparklines, heartbeat dot and a
  persistent status bar.

**Validation**
- 200 tests across seven suites.
- Walk-forward validation with an in-sample optimiser, a purge gap, and rolling
  or anchored windows.
- Walk-forward refuses to run on data too short to validate.

### Fixed

- **ADX computed `plus_di + plus_di` instead of `plus_di + minus_di`**, halving
  every trend-strength reading.
- **Donchian channels were shifted twice**, delaying every breakout by a bar.
- **The drawdown monitor conflated its threshold with its observed value**,
  tripping on the first bar and producing zero trades.
- **`str(Topic.X)` returned `"Topic.X"` rather than `"market.data"`**, so
  publishing with the enum matched no subscription and the entire agent
  pipeline silently did nothing.
- **`rules_for()` returned the shared registry object**, letting one caller
  mutate every other caller's prop-firm rules.
- **Alpaca's paper URL contains the live URL as a substring**
  (`paper-api.alpaca.markets` contains `api.alpaca.markets`), so every paper
  account was classified live and rejected by the live-trading guard. The same
  class of check was hardened in the Binance adapter.
- **A half-written journal record made the whole forward journal unreadable**;
  malformed records are now skipped.
- **A zero-variance sample produced a degenerate confidence interval**, so any
  prediction other than the exact mean was reported as decisive drift.
- **Trading symbols were passed straight to the data source** in the backtest
  path (`SPX500` -> 404); they are now mapped to their tickers.
- **The paper broker ignored entry commissions**, so realised P&L disagreed
  with cash.
- **`Trade.pnl` was stored separately from the computed `net_pnl`**, letting the
  two drift apart; P&L is now derived from fills and fees.
- **A stray backtick inside a template literal** blanked the entire dashboard
  rather than one panel.
- **The dashboard exposed no way to start the engine**, so it always reported
  IDLE / STOPPED.
- **A losing trade could be labelled "approved"** when the edge detector
  declined to size it; the guardian now rejects unsized plans.
- **Concurrent cache writes collided on one temp filename**, raising
  `PermissionError` when the dashboard and a backtest ran together.

### Changed

- **Default timeframe is now 1 minute, with a 7-day range.** Volume gating is
  off by default at this scale because the data source's 1m volume is
  non-zero on only ~19% of bars.
- The default flat \$1 minimum commission is dropped at intraday timescales,
  where it would dominate the edge.
- The console CLI's output is reconfigured to survive a non-UTF-8 terminal.

---

## [1.1.0] — 2026-10-11

### Added

- Event-driven backtester with no-lookahead enforcement and a restricted
  trading window for out-of-sample runs.
- Trend breakout strategy: 20-day Donchian entry, 200-day regime filter, ADX
  trend-strength gate, ATR initial and chandelier trailing stops, 10-day channel
  exit.
- 1%-per-trade risk sizing derived from stop distance, with position, risk,
  position-count, daily-loss and drawdown caps.
- Performance metrics and an IID Monte Carlo over trade sequences.
- Paper broker with slippage and commission; Alpaca REST adapter.
- Web dashboard with equity curve, drawdown chart, trade journal and kill
  switch.
- 86 tests.

---

## [1.0.0] — 2026-10-10

### Added

- Initial system: data feed with CSV caching, indicator library, strategy,
  risk sizing, backtester and metrics.
