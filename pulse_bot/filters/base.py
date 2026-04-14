# pulse_bot/filters/base.py
"""Abstract base class for token filters."""

from __future__ import annotations

from abc import ABC, abstractmethod

from pulse_bot.models import FilterResult, Token, Trade


class Filter(ABC):
    """A single filter that contributes a score to the overall token evaluation."""

    name: str
    enabled: bool = True

    @abstractmethod
    def check(self, token: Token, trades: list[Trade]) -> FilterResult:
        """Evaluate the token and return a score with reasoning."""
        ...
