# pulse_bot/pulse/monitor.py
"""PulseMonitor — sliding window over trades with trend detection."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pulse_bot.config import PulseBotConfig
    from pulse_bot.models import Trade


@dataclass
class PulseSnapshot:
    """Current state of the pulse."""

    buy_rate: float  # buys / window_size (0.0 - 1.0)
    sell_rate: float
    new_wallet_rate: float  # new wallets / buys in window
    avg_buy_size_sol: float
    total_sol_in_window: float
    creator_selling: bool
    whale_exit: bool  # sell > whale_exit_sol
    buy_rate_trend: str  # "rising" | "stable" | "declining"
    buy_size_trend: str
    trend_declining_count: int  # consecutive declining windows
    curve_progress_pct: float
    window_events: int  # how many events in window


class PulseMonitor:
    """Sliding event window with trend analysis.

    Not time-based — event-based. On an active token 20 events = 10 seconds,
    on a dead one = 5 minutes. Pulse adapts automatically.
    """

    def __init__(self, config: PulseBotConfig) -> None:
        self._cfg = config
        self._window: deque[Trade] = deque(maxlen=config.pulse_window_size)
        self._seen_wallets: set[str] = set()
        self._prev_buy_rate: float | None = None
        self._prev_avg_buy: float | None = None
        self._trend_declining_count: int = 0

    def update(self, trade: Trade) -> PulseSnapshot | None:
        """Add trade to window. Returns snapshot if enough events, else None."""
        self._window.append(trade)

        if len(self._window) < self._cfg.pulse_min_events:
            return None

        buys = [t for t in self._window if t.tx_type == "buy"]
        sells = [t for t in self._window if t.tx_type == "sell"]

        buy_rate = len(buys) / len(self._window)
        sell_rate = len(sells) / len(self._window)

        # New wallets
        new_wallets = set()
        for t in buys:
            if t.wallet not in self._seen_wallets:
                new_wallets.add(t.wallet)
        self._seen_wallets.update(t.wallet for t in self._window)
        new_wallet_rate = len(new_wallets) / max(len(buys), 1)

        # Buy size
        avg_buy = sum(t.sol_amount for t in buys) / max(len(buys), 1)
        total_sol = sum(t.sol_amount for t in buys)

        # Creator
        creator_selling = any(
            t.is_creator and t.tx_type == "sell" for t in self._window
        )

        # Whale exit
        whale_exit = any(
            t.sol_amount > self._cfg.pulse_whale_exit_sol and t.tx_type == "sell"
            for t in sells
        )

        # Trends
        buy_rate_trend = self._compute_trend(buy_rate, self._prev_buy_rate)
        buy_size_trend = self._compute_trend(avg_buy, self._prev_avg_buy)

        if buy_rate_trend == "declining":
            self._trend_declining_count += 1
        else:
            self._trend_declining_count = 0

        self._prev_buy_rate = buy_rate
        self._prev_avg_buy = avg_buy

        # Curve
        curve_pct = 0.0
        if self._window:
            last = self._window[-1]
            if last.v_sol_in_bonding_curve > 0:
                curve_pct = min(
                    (last.v_sol_in_bonding_curve / self._cfg.pumpfun_graduation_sol)
                    * 100,
                    100,
                )

        return PulseSnapshot(
            buy_rate=buy_rate,
            sell_rate=sell_rate,
            new_wallet_rate=new_wallet_rate,
            avg_buy_size_sol=avg_buy,
            total_sol_in_window=total_sol,
            creator_selling=creator_selling,
            whale_exit=whale_exit,
            buy_rate_trend=buy_rate_trend,
            buy_size_trend=buy_size_trend,
            trend_declining_count=self._trend_declining_count,
            curve_progress_pct=curve_pct,
            window_events=len(self._window),
        )

    def _compute_trend(self, current: float, previous: float | None) -> str:
        if previous is None:
            return "stable"
        if previous < 0.001:
            return "stable"
        diff = (current - previous) / previous
        if diff > self._cfg.pulse_trend_threshold:
            return "rising"
        if diff < -self._cfg.pulse_trend_threshold:
            return "declining"
        return "stable"
