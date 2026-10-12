"""Multi-agent trading system.

Agents (each one responsibility, all communicating over the bus):

===============  ===========================================================
Agent            Responsibility
===============  ===========================================================
``scanner``       multi-market monitoring: fetches bars, flags stale data
``analysis``      regime, volatility, momentum, trend quality, breakouts
``probability``   Bayesian posterior over "price rises", updated by evidence
``edge_detector``  expectancy + cost + confidence gates, then Kelly sizing
``guardian``      portfolio risk; the only agent that can veto
``execution``     order submission and live stop/target management
===============  ===========================================================
"""

from .base import Agent
from .bus import MessageBus, Topic
from .edge import EdgeDetectionAgent, expectancy_r, grade_edge
from .execution import ExecutionAgent, ManagedTrade
from .guardian import RiskGuardianAgent, RiskLimits
from .kelly import KellyConfig, KellySizer, breakeven_probability, kelly_fraction
from .probability import ProbabilityAgent
from .scanner import ScannerAgent
from .supervisor import MultiAgentSupervisor, SupervisorConfig

__all__ = [
    "Agent",
    "MessageBus",
    "Topic",
    "EdgeDetectionAgent",
    "expectancy_r",
    "grade_edge",
    "ExecutionAgent",
    "ManagedTrade",
    "RiskGuardianAgent",
    "RiskLimits",
    "KellyConfig",
    "KellySizer",
    "breakeven_probability",
    "kelly_fraction",
    "ProbabilityAgent",
    "ScannerAgent",
    "MultiAgentSupervisor",
    "SupervisorConfig",
]
