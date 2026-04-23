# pulse_bot/launchpads/base.py
"""Abstract base class for launchpad adapters."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import AsyncIterator

from pulse_bot.models import Token, Trade


class Launchpad(ABC):
    """Abstract launchpad adapter. Each platform implements its own WS protocol."""

    name: str
    ws_url: str

    @abstractmethod
    async def connect(self) -> None:
        """Establish WebSocket connection and start background reader."""
        ...

    @abstractmethod
    async def disconnect(self) -> None:
        """Close WebSocket connection and cleanup."""
        ...

    @abstractmethod
    def stream_new_tokens(self) -> AsyncIterator[Token]:
        """Yield newly created tokens as they arrive from the WebSocket."""
        ...

    @abstractmethod
    async def subscribe_trades(self, mint: str) -> None:
        """Subscribe to trade events for a specific token mint."""
        ...

    @abstractmethod
    async def unsubscribe_trades(self, mint: str) -> None:
        """Unsubscribe from trade events and cleanup the queue for a mint."""
        ...

    @abstractmethod
    def stream_trades(
        self, mint: str, duration_seconds: float, inactivity_timeout: float = 0
    ) -> AsyncIterator[Trade]:
        """Yield trades for a mint during the observation window."""
        ...

    @abstractmethod
    def parse_create_event(self, raw: dict) -> Token:
        """Parse a raw WebSocket message into a Token."""
        ...

    @abstractmethod
    def parse_trade_event(self, raw: dict, creator: str) -> Trade:
        """Parse a raw WebSocket message into a Trade."""
        ...

    @abstractmethod
    def compute_curve_progress(self, v_sol_in_bonding_curve: float) -> float:
        """Compute bonding curve progress as a percentage."""
        ...
