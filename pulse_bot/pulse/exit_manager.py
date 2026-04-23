# pulse_bot/pulse/exit_manager.py
"""ExitManager — decides when and how much to sell based on pulse snapshots."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pulse_bot.config import PulseBotConfig
    from pulse_bot.ml.policy import ExitMLPolicy
    from pulse_bot.pulse.monitor import PulseSnapshot


@dataclass
class ExitSignal:
    """Decision from ExitManager."""

    action: str  # "hold" | "sell_partial" | "sell_all"
    reason: str
    sell_pct: float  # 0.0 for hold, 0.3 for partial, 1.0 for sell_all
    # Advisory: exit ML's P(should_sell_now). None when no model loaded
    # or model predict failed. Not consulted for rule logic — logged
    # only, for later analysis of ML-vs-rules agreement.
    ml_exit_proba: float | None = None


class ExitManager:
    """Configurable exit rules. All thresholds from config for backtesting."""

    def __init__(
        self,
        config: PulseBotConfig,
        ml_advisor: "ExitMLPolicy | None" = None,
    ) -> None:
        self._cfg = config
        self._remaining_pct: float = 1.0
        self._partial_count: int = 0
        self._has_taken_profit: bool = False
        self._peak_pnl_pct: float = 0.0  # for trailing stop
        # Optional exit-model advisor. Advisory only — never overrides
        # the rule logic below. Used for shadow logging of ML opinion
        # alongside rule-based decisions.
        self._ml_advisor = ml_advisor

    @property
    def remaining_pct(self) -> float:
        return self._remaining_pct

    def decide(
        self, pulse: PulseSnapshot, pnl_pct: float, elapsed_sec: float
    ) -> ExitSignal:
        """Evaluate pulse and return exit decision."""

        # Shadow-predict exit probability once per call so we can attach
        # it to whatever decision the rules pick. Never used to override.
        ml_proba: float | None = None
        if self._ml_advisor is not None:
            try:
                # Construct a minimal state object matching extractor fields.
                # Peak & drawdown are tracked in this manager's state.
                drawdown = max(self._peak_pnl_pct - pnl_pct, 0.0)
                state = {
                    "hold_seconds": elapsed_sec,
                    "current_pnl_pct": pnl_pct,
                    "peak_pnl_pct": self._peak_pnl_pct,
                    "drawdown_from_peak": drawdown,
                }
                ml_proba = self._ml_advisor.predict_proba(state, pulse)
            except Exception:
                ml_proba = None

        # ── Hard exits — sell 100% ─────────────────────────

        if pulse.creator_selling and self._cfg.exit_on_creator_dump:
            return self._sell_all("creator_dump", ml_proba)

        if (
            pulse.buy_rate < self._cfg.pulse_dead_buy_rate
            and pulse.window_events >= self._cfg.pulse_min_events
        ):
            return self._sell_all("pulse_dead", ml_proba)

        if pulse.trend_declining_count >= self._cfg.exit_trend_dying_count:
            return self._sell_all("trend_dying", ml_proba)

        if pulse.sell_rate > pulse.buy_rate * self._cfg.exit_sell_pressure_ratio:
            return self._sell_all("sell_pressure", ml_proba)

        # Relative buy-rate fade: exit when current buy_rate falls below
        # `drop_ratio` of the session peak. Guarded by floor to avoid firing
        # on noise before momentum has actually built up.
        if (
            self._cfg.exit_peak_buy_rate_drop_ratio > 0.0
            and pulse.peak_buy_rate >= self._cfg.exit_peak_buy_rate_floor
            and pulse.buy_rate_drop_from_peak <= self._cfg.exit_peak_buy_rate_drop_ratio
        ):
            return self._sell_all("buy_rate_drop", ml_proba)

        if (
            pulse.new_wallet_rate == 0
            and pulse.window_events >= self._cfg.exit_no_new_wallets_events
        ):
            return self._sell_all("no_new_blood", ml_proba)

        if pulse.whale_exit and self._cfg.exit_on_whale:
            return self._sell_all("whale_exit", ml_proba)

        if pulse.curve_progress_pct > self._cfg.exit_near_graduation_pct:
            return self._sell_all("near_graduation", ml_proba)

        if pnl_pct < -self._cfg.exit_hard_stop_loss_pct:
            return self._sell_all("hard_stop", ml_proba)

        if (
            self._cfg.exit_take_profit_enabled
            and pnl_pct >= self._cfg.exit_take_profit_pct
        ):
            return self._sell_all("take_profit", ml_proba)

        # ── Trailing stop ─────────────────────────────────
        if self._cfg.exit_trailing_stop_enabled:
            self._peak_pnl_pct = max(self._peak_pnl_pct, pnl_pct)
            if self._peak_pnl_pct >= self._cfg.exit_trailing_stop_activation_pct:
                drawdown_from_peak = self._peak_pnl_pct - pnl_pct
                if drawdown_from_peak >= self._cfg.exit_trailing_stop_distance_pct:
                    return self._sell_all("trailing_stop", ml_proba)

        if elapsed_sec > self._cfg.exit_max_hold_seconds:
            return self._sell_all("timeout", ml_proba)

        # ── Partial exits ──────────────────────────────────

        available = self._remaining_pct - self._cfg.exit_moonbag_pct
        if available <= 0.01:
            return self._hold(ml_proba)

        # Strong profit → take partial
        if pnl_pct > self._cfg.exit_profit_threshold_pct and not self._has_taken_profit:
            sell_pct = min(self._cfg.exit_partial_on_profit_pct, available)
            self._has_taken_profit = True
            return self._sell_partial(sell_pct, "strong_profit", ml_proba)

        # Weak pulse + profit → partial sell
        if (
            pulse.buy_rate < self._cfg.pulse_weak_buy_rate
            and pnl_pct > self._cfg.exit_weak_pulse_min_profit_pct
        ):
            sell_pct = min(self._cfg.exit_partial_on_weak_pulse_pct, available)
            return self._sell_partial(sell_pct, "weak_pulse_profit", ml_proba)

        return self._hold(ml_proba)

    def _sell_all(self, reason: str, ml_proba: float | None = None) -> ExitSignal:
        pct = self._remaining_pct
        self._remaining_pct = 0
        return ExitSignal(
            action="sell_all",
            reason=reason,
            sell_pct=pct,
            ml_exit_proba=ml_proba,
        )

    def _sell_partial(
        self,
        pct: float,
        reason: str,
        ml_proba: float | None = None,
    ) -> ExitSignal:
        self._remaining_pct -= pct
        self._partial_count += 1
        return ExitSignal(
            action="sell_partial",
            reason=reason,
            sell_pct=pct,
            ml_exit_proba=ml_proba,
        )

    def _hold(self, ml_proba: float | None = None) -> ExitSignal:
        return ExitSignal(
            action="hold",
            reason="pulse_ok",
            sell_pct=0.0,
            ml_exit_proba=ml_proba,
        )
