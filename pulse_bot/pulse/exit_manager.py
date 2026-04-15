# pulse_bot/pulse/exit_manager.py
"""ExitManager — decides when and how much to sell based on pulse snapshots."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pulse_bot.config import PulseBotConfig
    from pulse_bot.pulse.monitor import PulseSnapshot


@dataclass
class ExitSignal:
    """Decision from ExitManager."""

    action: str  # "hold" | "sell_partial" | "sell_all"
    reason: str
    sell_pct: float  # 0.0 for hold, 0.3 for partial, 1.0 for sell_all


class ExitManager:
    """Configurable exit rules. All thresholds from config for backtesting."""

    def __init__(self, config: PulseBotConfig) -> None:
        self._cfg = config
        self._remaining_pct: float = 1.0
        self._partial_count: int = 0
        self._has_taken_profit: bool = False

    @property
    def remaining_pct(self) -> float:
        return self._remaining_pct

    def decide(
        self, pulse: PulseSnapshot, pnl_pct: float, elapsed_sec: float
    ) -> ExitSignal:
        """Evaluate pulse and return exit decision."""

        # ── Hard exits — sell 100% ─────────────────────────

        if pulse.creator_selling and self._cfg.exit_on_creator_dump:
            return self._sell_all("creator_dump")

        if (
            pulse.buy_rate < self._cfg.pulse_dead_buy_rate
            and pulse.window_events >= self._cfg.pulse_min_events
        ):
            return self._sell_all("pulse_dead")

        if pulse.trend_declining_count >= self._cfg.exit_trend_dying_count:
            return self._sell_all("trend_dying")

        if pulse.sell_rate > pulse.buy_rate * self._cfg.exit_sell_pressure_ratio:
            return self._sell_all("sell_pressure")

        if (
            pulse.new_wallet_rate == 0
            and pulse.window_events >= self._cfg.exit_no_new_wallets_events
        ):
            return self._sell_all("no_new_blood")

        if pulse.whale_exit and self._cfg.exit_on_whale:
            return self._sell_all("whale_exit")

        if pulse.curve_progress_pct > self._cfg.exit_near_graduation_pct:
            return self._sell_all("near_graduation")

        if pnl_pct < -self._cfg.exit_hard_stop_loss_pct:
            return self._sell_all("hard_stop")

        if elapsed_sec > self._cfg.exit_max_hold_seconds:
            return self._sell_all("timeout")

        # ── Partial exits ──────────────────────────────────

        available = self._remaining_pct - self._cfg.exit_moonbag_pct
        if available <= 0.01:
            return self._hold()

        # Strong profit → take partial
        if pnl_pct > self._cfg.exit_profit_threshold_pct and not self._has_taken_profit:
            sell_pct = min(self._cfg.exit_partial_on_profit_pct, available)
            self._has_taken_profit = True
            return self._sell_partial(sell_pct, "strong_profit")

        # Weak pulse + profit → partial sell
        if (
            pulse.buy_rate < self._cfg.pulse_weak_buy_rate
            and pnl_pct > self._cfg.exit_weak_pulse_min_profit_pct
        ):
            sell_pct = min(self._cfg.exit_partial_on_weak_pulse_pct, available)
            return self._sell_partial(sell_pct, "weak_pulse_profit")

        return self._hold()

    def _sell_all(self, reason: str) -> ExitSignal:
        pct = self._remaining_pct
        self._remaining_pct = 0
        return ExitSignal(action="sell_all", reason=reason, sell_pct=pct)

    def _sell_partial(self, pct: float, reason: str) -> ExitSignal:
        self._remaining_pct -= pct
        self._partial_count += 1
        return ExitSignal(action="sell_partial", reason=reason, sell_pct=pct)

    def _hold(self) -> ExitSignal:
        return ExitSignal(action="hold", reason="pulse_ok", sell_pct=0.0)
