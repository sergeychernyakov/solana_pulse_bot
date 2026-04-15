# pulse_bot/execution.py
"""Simulated execution — models buy/sell on bonding curve with slippage."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pulse_bot.config import PulseBotConfig
    from pulse_bot.models import Trade

logger = logging.getLogger(__name__)


@dataclass
class FillResult:
    """Result of a simulated trade execution."""

    side: str  # "buy" | "sell"
    sol_spent: float  # SOL spent (buy) or received (sell)
    tokens_received: float  # tokens received (buy) or sold (sell)
    price_per_token: float  # effective price
    fee_sol: float  # fee deducted
    slippage_pct: float  # estimated slippage
    success: bool = True


class SimulatedExecution:
    """Simulates buy/sell on Pump.fun bonding curve.

    Uses actual trade data to estimate fill price:
    - Buy: price = last trade price × (1 + slippage)
    - Sell: price = last trade price × (1 - slippage)

    Configurable: buy_amount, slippage model, fee, priority fee.
    """

    def __init__(self, config: PulseBotConfig) -> None:
        self._cfg = config

    def simulate_buy(self, trades: list[Trade], amount_sol: float | None = None) -> FillResult:
        """Simulate buying tokens at current market price.

        Args:
            trades: Recent trades to estimate price from.
            amount_sol: SOL to spend. Defaults to config.buy_amount_sol.
        """
        sol = amount_sol or self._cfg.buy_amount_sol

        if not trades:
            return FillResult(side="buy", sol_spent=sol, tokens_received=0,
                              price_per_token=0, fee_sol=0, slippage_pct=0, success=False)

        # Estimate price from recent buy trades
        buy_trades = [t for t in trades if t.tx_type == "buy" and t.token_amount > 0 and t.sol_amount > 0]
        if not buy_trades:
            return FillResult(side="buy", sol_spent=sol, tokens_received=0,
                              price_per_token=0, fee_sol=0, slippage_pct=0, success=False)

        last_price = buy_trades[-1].sol_amount / buy_trades[-1].token_amount

        # Slippage model
        slippage = self._estimate_slippage(sol, trades)
        fill_price = last_price * (1.0 + slippage)

        # Fee
        fee = sol * self._cfg.execution_fee_pct

        # Calculate tokens received
        net_sol = sol - fee - self._cfg.execution_priority_fee
        tokens = net_sol / fill_price if fill_price > 0 else 0

        return FillResult(
            side="buy", sol_spent=sol, tokens_received=tokens,
            price_per_token=fill_price, fee_sol=fee + self._cfg.execution_priority_fee,
            slippage_pct=slippage * 100,
        )

    def simulate_sell(self, trades: list[Trade], tokens_to_sell: float) -> FillResult:
        """Simulate selling tokens at current market price.

        Args:
            trades: Recent trades to estimate price from.
            tokens_to_sell: Number of tokens to sell.
        """
        if not trades or tokens_to_sell <= 0:
            return FillResult(side="sell", sol_spent=0, tokens_received=0,
                              price_per_token=0, fee_sol=0, slippage_pct=0, success=False)

        # Use last trade price
        priced = [t for t in trades if t.token_amount > 0 and t.sol_amount > 0]
        if not priced:
            return FillResult(side="sell", sol_spent=0, tokens_received=0,
                              price_per_token=0, fee_sol=0, slippage_pct=0, success=False)

        last_price = priced[-1].sol_amount / priced[-1].token_amount

        # Sell slippage is higher (thinner liquidity on the way down)
        slippage = self._estimate_slippage(tokens_to_sell * last_price, trades) * self._cfg.execution_sell_slippage_mult
        fill_price = last_price * (1.0 - slippage)

        gross_sol = tokens_to_sell * fill_price
        fee = gross_sol * self._cfg.execution_fee_pct
        net_sol = gross_sol - fee

        return FillResult(
            side="sell", sol_spent=net_sol, tokens_received=tokens_to_sell,
            price_per_token=fill_price, fee_sol=fee,
            slippage_pct=slippage * 100,
        )

    def _estimate_slippage(self, sol_amount: float, trades: list[Trade]) -> float:
        """Estimate slippage based on order size relative to recent volume.

        Larger orders relative to volume = more slippage.
        Configurable base + volume-adjusted component.
        """
        recent_volume = sum(t.sol_amount for t in trades[-20:])
        if recent_volume <= 0:
            return self._cfg.execution_base_slippage

        size_ratio = sol_amount / recent_volume
        dynamic_slippage = size_ratio * self._cfg.execution_slippage_per_volume_pct

        return min(
            self._cfg.execution_base_slippage + dynamic_slippage,
            self._cfg.execution_max_slippage,
        )
