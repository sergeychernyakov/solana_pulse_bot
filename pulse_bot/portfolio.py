# pulse_bot/portfolio.py
"""Portfolio — tracks balance, positions, and P&L."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pulse_bot.config import PulseBotConfig
    from pulse_bot.execution import FillResult

logger = logging.getLogger(__name__)


@dataclass
class Position:
    """An open position in a token."""

    mint: str
    symbol: str
    entry_price: float
    entry_time: float
    tokens_held: float
    sol_invested: float
    remaining_pct: float = 1.0  # 1.0 → 0.1 after partial sells
    total_sol_received: float = 0.0
    partial_sell_count: int = 0
    entry_type: str = "full"


@dataclass
class ClosedTrade:
    """A completed trade for the report."""

    mint: str
    symbol: str
    entry_price: float
    exit_price: float
    entry_time: float
    exit_time: float
    sol_invested: float
    sol_received: float
    pnl_sol: float
    pnl_pct: float
    hold_seconds: float
    exit_reason: str
    partial_sells: int
    entry_type: str  # "fast" | "full"


class Portfolio:
    """Manages balance, positions, and trade history.

    All configurable: buy amount, max positions, fees.
    """

    def __init__(self, config: PulseBotConfig) -> None:
        self._cfg = config
        self.balance: float = config.portfolio_initial_sol
        self.positions: dict[str, Position] = {}
        self.closed_trades: list[ClosedTrade] = []
        self._peak_balance: float = config.portfolio_initial_sol

    @property
    def open_count(self) -> int:
        return len(self.positions)

    @property
    def can_buy(self) -> bool:
        return (
            self.open_count < self._cfg.portfolio_max_positions
            and self.balance >= self._cfg.buy_amount_sol
        )

    @property
    def total_value(self) -> float:
        """Balance + estimated value of open positions (at entry price)."""
        open_val = sum(
            p.sol_invested * p.remaining_pct for p in self.positions.values()
        )
        return self.balance + open_val

    @property
    def max_drawdown_pct(self) -> float:
        """Maximum drawdown from peak balance."""
        if self._peak_balance <= 0:
            return 0.0
        return ((self._peak_balance - self.total_value) / self._peak_balance) * 100.0

    def open_position(
        self,
        mint: str,
        symbol: str,
        fill: FillResult,
        entry_time: float,
        entry_type: str = "fast",
    ) -> bool:
        """Open a new position after a simulated buy."""
        if not fill.success or mint in self.positions:
            return False
        if not self.can_buy:
            return False

        self.balance -= fill.sol_spent
        self.positions[mint] = Position(
            mint=mint,
            symbol=symbol,
            entry_price=fill.price_per_token,
            entry_time=entry_time,
            tokens_held=fill.tokens_received,
            sol_invested=fill.sol_spent,
            entry_type=entry_type,
        )
        logger.debug(
            "Opened %s: %.4f SOL → %.0f tokens @ %e",
            symbol,
            fill.sol_spent,
            fill.tokens_received,
            fill.price_per_token,
        )
        return True

    def partial_sell(
        self,
        mint: str,
        sell_pct: float,
        fill: FillResult,
        exit_time: float,
        reason: str,
    ) -> None:
        """Execute a partial sell of a position."""
        pos = self.positions.get(mint)
        if not pos or not fill.success:
            return

        self.balance += fill.sol_spent  # sol_spent = net SOL received for sells
        pos.total_sol_received += fill.sol_spent
        pos.remaining_pct -= sell_pct
        pos.partial_sell_count += 1
        self._update_peak()

        logger.debug(
            "Partial sell %s: %.0f%% for %.4f SOL (%s)",
            pos.symbol,
            sell_pct * 100,
            fill.sol_spent,
            reason,
        )

    def close_position(
        self, mint: str, fill: FillResult, exit_time: float, reason: str
    ) -> ClosedTrade | None:
        """Close a position completely."""
        pos = self.positions.get(mint)
        if not pos:
            return None

        if fill.success:
            self.balance += fill.sol_spent
            pos.total_sol_received += fill.sol_spent

        total_received = pos.total_sol_received
        pnl_sol = total_received - pos.sol_invested
        pnl_pct = (pnl_sol / pos.sol_invested) * 100 if pos.sol_invested > 0 else 0

        trade = ClosedTrade(
            mint=mint,
            symbol=pos.symbol,
            entry_price=pos.entry_price,
            exit_price=fill.price_per_token if fill.success else pos.entry_price,
            entry_time=pos.entry_time,
            exit_time=exit_time,
            sol_invested=pos.sol_invested,
            sol_received=total_received,
            pnl_sol=pnl_sol,
            pnl_pct=pnl_pct,
            hold_seconds=exit_time - pos.entry_time,
            exit_reason=reason,
            partial_sells=pos.partial_sell_count,
            entry_type=pos.entry_type,
        )
        self.closed_trades.append(trade)
        del self.positions[mint]
        self._update_peak()

        logger.debug(
            "Closed %s: %+.4f SOL (%+.1f%%) after %.0fs (%s)",
            trade.symbol,
            pnl_sol,
            pnl_pct,
            trade.hold_seconds,
            reason,
        )
        return trade

    def _update_peak(self) -> None:
        tv = self.total_value
        if tv > self._peak_balance:
            self._peak_balance = tv
