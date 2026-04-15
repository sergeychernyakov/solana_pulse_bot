# pulse_bot/clock.py
"""Clock abstraction — RealClock for live, SimulatedClock for backtest."""

from __future__ import annotations

import asyncio
import time
from abc import ABC, abstractmethod


class Clock(ABC):
    """Abstract clock. All modules use clock.now() and clock.sleep() instead of time/asyncio directly."""

    @abstractmethod
    def now(self) -> float:
        """Current time as unix timestamp."""
        ...

    @abstractmethod
    async def sleep(self, seconds: float) -> None:
        """Wait. Instant in backtest, real in live."""
        ...


class RealClock(Clock):
    """Real time for live and paper trading."""

    def now(self) -> float:
        return time.time()

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(seconds)


class SimulatedClock(Clock):
    """Simulated time for backtesting. Sleep is instant, time advances from data."""

    def __init__(self, start_ts: float = 0.0) -> None:
        self._ts = start_ts

    def now(self) -> float:
        return self._ts

    async def sleep(self, seconds: float) -> None:
        self._ts += seconds

    def advance_to(self, ts: float) -> None:
        """Jump to a specific timestamp (used when replaying events)."""
        self._ts = max(self._ts, ts)

    def set(self, ts: float) -> None:
        """Force set time."""
        self._ts = ts
