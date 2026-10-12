"""Broker adapters.

The engine talks only to the :class:`Broker` interface below, which keeps paper
and live execution interchangeable and makes the engine testable without ever
touching a real account.
"""

from __future__ import annotations

from .base import Broker, Order, Position  # noqa: F401

__all__ = ["Broker", "Order", "Position"]
