"""Autonomous trend-following trading system.

Layers:
    datafeed   -- cached market data
    indicators -- causal technical indicators
    strategy   -- entry/exit rules
    risk       -- position sizing and portfolio circuit breakers
    backtest   -- event-driven, no-lookahead replay
    metrics    -- performance statistics
    broker     -- paper and live execution adapters
    engine     -- the autonomous loop that trades them
    web        -- dashboard server
"""

__version__ = "1.0.0"

__all__ = [
    "backtest",
    "broker",
    "datafeed",
    "engine",
    "indicators",
    "journal",
    "metrics",
    "quality",
    "risk",
    "strategy",
    "trades",
    "web",
]
