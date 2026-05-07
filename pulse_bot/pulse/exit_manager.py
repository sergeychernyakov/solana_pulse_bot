# pulse_bot/pulse/exit_manager.py
"""ExitManager — decides when and how much to sell.

Architecture (2026-04-23 v2, codex-reviewed):

  1. HARD RULES (immutable, always checked first):
       creator_dump, pulse_dead, trend_dying, sell_pressure,
       buy_rate_drop, no_new_blood, whale_exit, near_graduation,
       hard_stop, take_profit, trailing_stop, timeout.
     ML can never override these — that would risk holding through a rug.

  2. ML GATED DECISION (ExitMLPolicy.decide_with_confidence, 4-way):
       * SELL_ALL     — force sell_all (escalates rule hold)
       * SELL_PARTIAL — force sell_partial at a confidence-sized ladder
       * HOLD_HARD    — block ONLY ``weak_pulse_profit`` partial exits
                        (explicit allowlist, not fall-through)
       * RULES        — defer to rule logic

  3. SOFT RULES (partial exits):
       strong_profit, weak_pulse_profit. ML's HOLD_HARD can block
       weak_pulse_profit when PnL is still positive and entry was
       high-confidence.

Observability (codex E6):
  * ``ml_override_count`` — ML forced sell_all (rules said hold)
  * ``ml_partial_count`` — ML forced partial (rules said hold)
  * ``ml_hold_hard_count`` — ML blocked a soft rule exit
  * Each counter exposed via ``ExitManager.ml_counters`` for
    aggregate logging at bot shutdown / daily reports.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pulse_bot.config import PulseBotConfig
    from pulse_bot.ml.policy import ExitMLPolicy, ExitQuantilePolicy
    from pulse_bot.pulse.monitor import PulseSnapshot


# E5 confidence-sized partial ladder. Config-backed since 2026-04-24 so
# optimizer can sweep these. Defaults preserve old hardcoded values
# (0.55→30%, 0.65→50%, 0.75→70%). `proba ≥ exit_ml_sell_threshold`
# (0.80) is SELL_ALL — not on this ladder.


def _sizing_ladder_from_config(
    cfg: "PulseBotConfig",
) -> tuple[tuple[float, float], ...]:
    """Build the 3-step ladder from config fields. Order-enforced by
    sorting on proba threshold so a misconfigured `probe_3 < probe_1`
    still yields a monotone ladder."""
    raw = (
        (cfg.ml_sizing_proba_1, cfg.ml_sizing_frac_1),
        (cfg.ml_sizing_proba_2, cfg.ml_sizing_frac_2),
        (cfg.ml_sizing_proba_3, cfg.ml_sizing_frac_3),
    )
    return tuple(sorted(raw, key=lambda x: x[0]))


def _sizing_from_proba(p: float, cfg: "PulseBotConfig | None" = None) -> float:
    """Pick the partial-exit fraction for a given proba using the ladder.

    ``cfg`` optional for call-site back-compat (tests); when None we fall
    back to the historical hardcoded ladder.
    """
    if cfg is not None:
        ladder = _sizing_ladder_from_config(cfg)
    else:
        ladder = ((0.55, 0.30), (0.65, 0.50), (0.75, 0.70))
    frac = ladder[0][1] if ladder else 0.30
    for threshold, f in ladder:
        if p >= threshold:
            frac = f
    return frac


@dataclass
class ExitSignal:
    """Decision from ExitManager."""

    action: str  # "hold" | "sell_partial" | "sell_all"
    reason: str
    sell_pct: float  # 0.0 for hold, 0.3 for partial, 1.0 for sell_all
    # Advisory / active: exit ML's P(should_sell_now). None when no model
    # loaded or model predict failed.
    ml_exit_proba: float | None = None
    # 4-way action from ExitMLPolicy.decide_with_confidence, for audit.
    ml_action: str | None = None  # "SELL_ALL" | "SELL_PARTIAL" | "RULES" | "HOLD_HARD"


class ExitManager:
    """Configurable exit rules. All thresholds from config for backtesting."""

    def __init__(
        self,
        config: PulseBotConfig,
        ml_advisor: "ExitMLPolicy | None" = None,
        quantile_sl_policy: "ExitQuantilePolicy | None" = None,
        quantile_tp_policy: "ExitQuantilePolicy | None" = None,
        quantile_max_hold_policy: "ExitQuantilePolicy | None" = None,
        *,
        mint: str | None = None,
        scored_at: float | None = None,
    ) -> None:
        self._cfg = config
        self._remaining_pct: float = 1.0
        self._partial_count: int = 0
        self._has_taken_profit: bool = False
        self._peak_pnl_pct: float = 0.0  # for trailing stop
        self._ml_advisor = ml_advisor
        # E3: SL-tightening quantile advisor. Activated only when
        # ``exit_regression_active=True`` and binary ML is directionally
        # confident — see _should_sl_tighten for the full gate.
        self._quantile_sl = quantile_sl_policy
        # Shadow-only TP quantile (q=0.75). Not gated to live decisions
        # yet — predictions are logged for production validation.
        self._quantile_tp = quantile_tp_policy
        # 2026-04-30: dynamic max_hold quantile (q=0.75). Activated only
        # when ``PULSE_EXIT_MAX_HOLD_DYNAMIC=1``. Predicted ONCE on the
        # first decide() call, then used as the timeout boundary for the
        # rest of the position's life. Cached on the instance.
        self._quantile_max_hold = quantile_max_hold_policy
        self._dynamic_max_hold_cached: float | None = None
        # Context for shadow logging (mint identifies token, scored_at is
        # the entry timestamp used as scored_at in shadow_predictions).
        self._mint = mint
        self._scored_at = scored_at
        self.ml_counters: dict[str, int] = {
            "ml_override_count": 0,
            "ml_partial_count": 0,
            "ml_hold_hard_count": 0,
            "ml_sl_tightened_count": 0,
        }
        # Loud-once visibility: makes "no ML stack at all" obvious in
        # logs. Without this, an accidentally-removed ``exit_model.ubj``
        # silently downgrades the bot to rules-only exits while logs
        # still reference the ML decision tree (codex review 2026-04-26
        # finding #5).
        if (
            ml_advisor is None
            and quantile_sl_policy is None
            and quantile_tp_policy is None
        ):
            ExitManager._warn_no_ml_advisors_once()

    _no_ml_warning_emitted: bool = False

    @classmethod
    def _warn_no_ml_advisors_once(cls) -> None:
        if cls._no_ml_warning_emitted:
            return
        cls._no_ml_warning_emitted = True
        import logging as _logging

        _logging.getLogger(__name__).warning(
            "ExitManager initialised without any ML advisor "
            "(ml_advisor / quantile_sl / quantile_tp all None). "
            "Bot will exit on rules ONLY — confirm this is intentional. "
            "Common cause: missing data/ml/exit_model.ubj or "
            "exit_regression_active=False."
        )

    @property
    def remaining_pct(self) -> float:
        return self._remaining_pct

    def decide(
        self, pulse: PulseSnapshot, pnl_pct: float, elapsed_sec: float
    ) -> ExitSignal:
        """Evaluate pulse and return exit decision."""

        ml_proba: float | None = None
        ml_action: str | None = None
        cfg = self._cfg
        if self._ml_advisor is not None:
            try:
                state = self._build_state_for_ml(pulse, pnl_pct, elapsed_sec)
                ml_action, ml_proba = self._ml_advisor.decide_with_confidence(
                    state,
                    pulse,
                    current_pnl_pct=pnl_pct,
                )
            except Exception:
                ml_proba = None
                ml_action = None

        # ── SHADOW: log quantile q25/q75 if requested (no decision impact) ──
        # Predict on every tick where we have the policy(s) loaded and the
        # caller wired mint/scored_at context. Missing context → noop.
        if self._mint is not None and self._scored_at is not None:
            from pulse_bot.ml import shadow as _shadow

            if _shadow.quantile_shadow_enabled() and (
                self._quantile_sl is not None or self._quantile_tp is not None
            ):
                try:
                    shadow_state = self._build_state_for_ml(
                        pulse, pnl_pct, elapsed_sec
                    )
                    q25 = (
                        self._quantile_sl.predict(shadow_state, pulse)
                        if self._quantile_sl is not None
                        else 0.0
                    )
                    q75 = (
                        self._quantile_tp.predict(shadow_state, pulse)
                        if self._quantile_tp is not None
                        else 0.0
                    )
                    _shadow.record_quantile_shadow(
                        mint=self._mint,
                        scored_at=self._scored_at,
                        snapshot_t=elapsed_sec,
                        q25_pred=q25,
                        q75_pred=q75,
                        actual_pnl_pct=pnl_pct,
                    )
                except Exception:  # nosec B110
                    # Reason: shadow is best-effort observability; never let
                    # a logging error poison the exit decision path.
                    pass

        # ── Hard exits — IMMUTABLE, always checked first ──────────

        if pulse.creator_selling and cfg.exit_on_creator_dump:
            return self._sell_all("creator_dump", ml_proba, ml_action)

        if (
            pulse.buy_rate < cfg.pulse_dead_buy_rate
            and pulse.window_events >= cfg.pulse_min_events
        ):
            return self._sell_all("pulse_dead", ml_proba, ml_action)

        if pulse.trend_declining_count >= cfg.exit_trend_dying_count:
            return self._sell_all("trend_dying", ml_proba, ml_action)

        if pulse.sell_rate > pulse.buy_rate * cfg.exit_sell_pressure_ratio:
            return self._sell_all("sell_pressure", ml_proba, ml_action)

        if (
            cfg.exit_peak_buy_rate_drop_ratio > 0.0
            and pulse.peak_buy_rate >= cfg.exit_peak_buy_rate_floor
            and pulse.buy_rate_drop_from_peak <= cfg.exit_peak_buy_rate_drop_ratio
        ):
            return self._sell_all("buy_rate_drop", ml_proba, ml_action)

        if (
            pulse.new_wallet_rate == 0
            and pulse.window_events >= cfg.exit_no_new_wallets_events
        ):
            return self._sell_all("no_new_blood", ml_proba, ml_action)

        if pulse.whale_exit and cfg.exit_on_whale:
            return self._sell_all("whale_exit", ml_proba, ml_action)

        if pulse.curve_progress_pct > cfg.exit_near_graduation_pct:
            return self._sell_all("near_graduation", ml_proba, ml_action)

        # E3: SL-preempt. Only fires when the binary classifier is
        # directionally leaning SELL (SELL_ALL or SELL_PARTIAL action)
        # AND q=0.25 forward-PnL head predicts <-5% AND current PnL is
        # already in the red ([-hard_stop, -5%) range). Still a sell
        # escalation — cannot soften existing hard_stop. Fires BEFORE
        # hard_stop check so it can tighten, not widen, the SL. Logged
        # as ml_sl_tightened for post-hoc tracking.
        if self._should_sl_tighten(ml_action, pnl_pct):
            state = self._build_state_for_ml(pulse, pnl_pct, elapsed_sec)
            try:
                q25 = self._quantile_sl.predict(state, pulse)
            except Exception:
                q25 = 0.0
            # 2026-05-06 — sanity bounds. Training ρ ≈ 0.08 makes the
            # SL head a weak signal; a degenerate model could output
            # values outside the physically-plausible range and trigger
            # spurious SL tightens. q25 is supposed to be the 25th
            # percentile of forward PnL %% — i.e. a (typically) negative
            # number. If it's positive, the model is broken (predicts
            # gain on a SELL-leaning trade); if it's < -100%, equally
            # broken. Reject the prediction silently and skip the gate
            # — natural hard_stop (line below) still protects us.
            if not (-100.0 <= q25 <= 0.0):
                pass
            elif q25 < -5.0:
                self.ml_counters["ml_sl_tightened_count"] += 1
                return self._sell_all("ml_sl_tightened", ml_proba, ml_action)

        if pnl_pct < -cfg.exit_hard_stop_loss_pct:
            return self._sell_all("hard_stop", ml_proba, ml_action)

        if cfg.exit_take_profit_enabled and pnl_pct >= cfg.exit_take_profit_pct:
            # TP override (user directive 2026-04-23): if ML is very-
            # confident hold (proba < strict threshold) AND position is
            # still within safe runway (peak < 300%, current < 500%) —
            # block the TP sell and keep holding. Safety: trailing_stop
            # + timeout + hard_stop all remain immutable; moonbag cap
            # prevents unbounded hold.
            if (
                getattr(cfg, "exit_ml_hold_hard_enabled", True)
                and ml_action == "HOLD_HARD"
                and ml_proba is not None
                and ml_proba < self._ml_advisor.TP_HOLD_HARD_STRICT_THRESHOLD
                and self._peak_pnl_pct < self._ml_advisor.TP_HOLD_HARD_MAX_PEAK_PCT
                and pnl_pct < self._ml_advisor.TP_HOLD_HARD_MAX_CURRENT_PCT
                and "take_profit" in self._ml_advisor.HOLD_HARD_BLOCKABLE_REASONS
            ):
                self.ml_counters["ml_hold_hard_count"] += 1
                return self._hold(
                    ml_proba,
                    ml_action,
                    reason="ml_hold_hard_blocked_take_profit",
                )
            return self._sell_all("take_profit", ml_proba, ml_action)

        if cfg.exit_trailing_stop_enabled:
            self._peak_pnl_pct = max(self._peak_pnl_pct, pnl_pct)
            if self._peak_pnl_pct >= cfg.exit_trailing_stop_activation_pct:
                drawdown_from_peak = self._peak_pnl_pct - pnl_pct
                if drawdown_from_peak >= cfg.exit_trailing_stop_distance_pct:
                    return self._sell_all("trailing_stop", ml_proba, ml_action)

        # 2026-04-30: dynamic max_hold via exit_quantile_max_hold model.
        # Behind PULSE_EXIT_MAX_HOLD_DYNAMIC=1 (default OFF). Predicts on
        # first tick and caches; clamped to [30, cfg.exit_max_hold_seconds]
        # so the model can only EARLY-exit, never extend past the static
        # safety ceiling.
        effective_max_hold = cfg.exit_max_hold_seconds
        if (
            getattr(cfg, "exit_max_hold_dynamic", False)
            and self._quantile_max_hold is not None
        ):
            if self._dynamic_max_hold_cached is None:
                try:
                    state = self._build_state_for_ml(pulse, pnl_pct, elapsed_sec)
                    pred = float(self._quantile_max_hold.predict(state, pulse))
                    # 2026-05-01 (codex M1): static ceiling is the absolute
                    # cap. Floor is min(30s, ceiling) so optimizer sweeps
                    # below 30s don't INVERT the clamp and let the model
                    # extend hold past the safety ceiling.
                    floor = min(30.0, float(cfg.exit_max_hold_seconds))
                    ceiling = float(cfg.exit_max_hold_seconds)
                    self._dynamic_max_hold_cached = max(floor, min(pred, ceiling))
                    # 2026-05-01: log so filter_health observability picks
                    # up firings (was silent dead gate before).
                    import logging as _logging
                    _logging.getLogger(__name__).info(
                        "dynamic max_hold predicted=%.1fs cached=%.1fs (mint=%s)",
                        pred, self._dynamic_max_hold_cached,
                        (self._mint or "?")[:12],
                    )
                except Exception:
                    # Reason: fall back to static ceiling on prediction
                    # error rather than holding indefinitely.
                    self._dynamic_max_hold_cached = float(cfg.exit_max_hold_seconds)
            effective_max_hold = self._dynamic_max_hold_cached

        if elapsed_sec > effective_max_hold:
            return self._sell_all("timeout", ml_proba, ml_action)

        # ── Partial exits (soft rules) ────────────────────────────

        available = self._remaining_pct - cfg.exit_moonbag_pct
        if available <= 0.01:
            return self._maybe_ml_escalate(pnl_pct, elapsed_sec, ml_proba, ml_action)

        # Strong profit → take partial (NOT blockable by HOLD_HARD)
        if pnl_pct > cfg.exit_profit_threshold_pct and not self._has_taken_profit:
            sell_pct = min(cfg.exit_partial_on_profit_pct, available)
            self._has_taken_profit = True
            return self._sell_partial(sell_pct, "strong_profit", ml_proba, ml_action)

        # Weak pulse + profit → partial sell.
        # BLOCKABLE by ML HOLD_HARD when entry was high-confidence and PnL
        # is still salvageable. Explicit allowlist per codex E2 — no
        # fall-through, no doc-based "trust me" safety.
        if (
            pulse.buy_rate < cfg.pulse_weak_buy_rate
            and pnl_pct > cfg.exit_weak_pulse_min_profit_pct
        ):
            reason = "weak_pulse_profit"
            if (
                getattr(cfg, "exit_ml_hold_hard_enabled", True)
                and ml_action == "HOLD_HARD"
                and reason in self._ml_advisor.HOLD_HARD_BLOCKABLE_REASONS
            ):
                self.ml_counters["ml_hold_hard_count"] += 1
                return self._hold(
                    ml_proba, ml_action, reason="ml_hold_hard_blocked_weak_pulse"
                )
            sell_pct = min(cfg.exit_partial_on_weak_pulse_pct, available)
            return self._sell_partial(sell_pct, reason, ml_proba, ml_action)

        return self._maybe_ml_escalate(pnl_pct, elapsed_sec, ml_proba, ml_action)

    def _build_state_for_ml(
        self, pulse: PulseSnapshot, pnl_pct: float, elapsed_sec: float
    ) -> dict:
        drawdown = max(self._peak_pnl_pct - pnl_pct, 0.0)
        return {
            "hold_seconds": elapsed_sec,
            "current_pnl_pct": pnl_pct,
            "peak_pnl_pct": self._peak_pnl_pct,
            "drawdown_from_peak": drawdown,
        }

    def _should_sl_tighten(self, ml_action: str | None, pnl_pct: float) -> bool:
        """Gate for SL tightening — per user directive: only when model is
        confident in the SELL direction AND current position is already
        in loss territory. Paper-safe by construction: can only sell
        earlier than hard_stop would, never later."""
        cfg = self._cfg
        if not getattr(cfg, "exit_regression_active", False):
            return False
        if self._quantile_sl is None:
            return False
        if ml_action not in ("SELL_ALL", "SELL_PARTIAL"):
            return False
        # PnL must already be in the mid-red zone: between hard_stop and
        # the guard floor. Above the floor the ML's "sell soon" view
        # doesn't justify tripping the hard path.
        hard_stop_floor = -getattr(cfg, "exit_hard_stop_loss_pct", 15.0)
        if not (hard_stop_floor < pnl_pct <= -5.0):
            return False
        return True

    def _maybe_ml_escalate(
        self,
        pnl_pct: float,
        elapsed_sec: float,
        ml_proba: float | None,
        ml_action: str | None,
    ) -> ExitSignal:
        """Apply E2/E5 ML escalation when rules land on ``hold``.

        * ``SELL_ALL`` → force full exit.
        * ``SELL_PARTIAL`` → force sized partial (ladder by proba).
        * ``HOLD_HARD``, ``RULES``, None → hold.

        Protected by ``exit_ml_min_hold_seconds`` so MEV bursts on the
        very first tick can't flip us out prematurely. Never overrides
        the rule cascade above (those already returned).
        """
        cfg = self._cfg
        if not getattr(cfg, "exit_ml_active", False):
            return self._hold(ml_proba, ml_action)
        if elapsed_sec < getattr(cfg, "exit_ml_min_hold_seconds", 15.0):
            return self._hold(ml_proba, ml_action)
        if ml_action == "SELL_ALL" and ml_proba is not None:
            self.ml_counters["ml_override_count"] += 1
            return self._sell_all("ml_exit_trigger", ml_proba, ml_action)
        if ml_action == "SELL_PARTIAL" and ml_proba is not None:
            available = self._remaining_pct - cfg.exit_moonbag_pct
            if available > 0.01:
                frac = _sizing_from_proba(ml_proba, cfg)
                sell_pct = min(frac, available)
                self.ml_counters["ml_partial_count"] += 1
                return self._sell_partial(
                    sell_pct, "ml_partial_trigger", ml_proba, ml_action
                )
        return self._hold(ml_proba, ml_action)

    def _sell_all(
        self,
        reason: str,
        ml_proba: float | None = None,
        ml_action: str | None = None,
    ) -> ExitSignal:
        pct = self._remaining_pct
        self._remaining_pct = 0
        return ExitSignal(
            action="sell_all",
            reason=reason,
            sell_pct=pct,
            ml_exit_proba=ml_proba,
            ml_action=ml_action,
        )

    def _sell_partial(
        self,
        pct: float,
        reason: str,
        ml_proba: float | None = None,
        ml_action: str | None = None,
    ) -> ExitSignal:
        self._remaining_pct -= pct
        self._partial_count += 1
        return ExitSignal(
            action="sell_partial",
            reason=reason,
            sell_pct=pct,
            ml_exit_proba=ml_proba,
            ml_action=ml_action,
        )

    def _hold(
        self,
        ml_proba: float | None = None,
        ml_action: str | None = None,
        reason: str = "pulse_ok",
    ) -> ExitSignal:
        return ExitSignal(
            action="hold",
            reason=reason,
            sell_pct=0.0,
            ml_exit_proba=ml_proba,
            ml_action=ml_action,
        )
