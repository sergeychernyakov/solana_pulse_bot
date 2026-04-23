# pulse_bot/config.py
"""Configuration for the Pulse Bot. Every threshold is a tunable knob for backtesting."""

from __future__ import annotations

import os
from dataclasses import dataclass

# ── Constants (not tunable — facts about Pump.fun) ─────────
PUMPFUN_FEE_PCT = 0.01  # 1% Pump.fun fee
PUMPFUN_GRADUATION_SOL = 85.0  # SOL to graduate
PUMPFUN_PRIORITY_FEE = 0.0001  # priority fee SOL


@dataclass
class PulseBotConfig:
    """All bot settings with sensible defaults. Every field is a tunable parameter."""

    # ── WebSocket ──────────────────────────────────────────
    ws_url: str = "wss://pumpportal.fun/api/data"

    # ── Concurrency ────────────────────────────────────────
    max_concurrent_observations: int = 200

    # ── FAST PHASE (3-5 sec, early entry) ──────────────────
    fast_observe_seconds: int = 5
    fast_min_buys: int = 5
    fast_min_unique_buyers: int = 3
    fast_min_volume_sol: float = 0.3
    # HARD reject gates at fast-phase (April 2026 data-driven): 92% of
    # fast-phase BUY decisions went on to full=SKIP — dead-on-arrival
    # tokens the bot was blindly trading. vol>=2 + buyers>=5 rejects 90%
    # of those while keeping 47% of fast=BUY + full=BUY (quality signal).
    # 0 = disabled for each gate.
    fast_hard_min_volume_sol: float = 1.0
    fast_hard_min_unique_buyers: int = 7
    fast_max_sell_ratio: float = 0.3
    fast_min_buy_rate: float = 0.8
    fast_min_diversity: int = 2
    fast_score_threshold: int = 15
    fast_max_curve_pct: float = 60.0
    fast_creator_sold_reject: bool = True

    # ── FAST PHASE scoring weights ─────────────────────────
    fast_w_buyers: int = 15
    fast_w_volume: int = 10
    fast_w_velocity: int = 15
    fast_w_diversity: int = 5
    fast_w_no_sells: int = 10
    fast_w_curve_healthy: int = 5

    # ── FULL PHASE (45 sec, confirmation) ──────────────────
    observe_seconds: int = 90
    score_threshold_buy: int = 50  # strict defaults from volume-cliff sweep 2026-04-22
    score_threshold_borderline: int = 10

    # ── HARD ENTRY FILTERS (reject before scoring) ─────────
    min_market_cap_sol: float = 0.0  # min MCap to consider (0 = no filter)
    max_sell_pressure_for_entry: float = (
        999.0  # hard reject if sell_p > this (999 = disabled)
    )
    min_curve_for_entry: float = 0.0  # hard reject if curve < this (0 = disabled)
    # Entry buyer-count gates. Neutral defaults (1 / 20):
    #   min=1  — any buyer qualifies (gate effectively disabled)
    #   max=20 — data-driven (commit dc5104f: >20 = 49% WR, -3%)
    # Tune via optimizer after creator enrichment lands; do NOT re-lock to
    # alpha-optimized values derived from leaky in-sample runs.
    min_entry_buyer_number: int = 1
    max_entry_buyer_number: int = 20
    fast_entry_enabled: bool = True
    full_entry_enabled: bool = True
    # Hard entry gates (beat crude buyer_number). Neutral defaults (0);
    # optimizer flips them on to find a rug-filtering sweet spot.
    # - unique_buyers_hard: reject if <N distinct wallets bought in window
    # - sol_volume_hard:    reject if total buy SOL in window below threshold
    # - velocity_accel:     reject if second-half/first-half buy rate < N
    #                       (catches momentum fade before full observation)
    entry_min_unique_buyers_hard: int = 5  # strict defaults (sweep 2026-04-22)
    entry_min_sol_volume_hard: float = 10.0  # sweet-spot from marginal axis
    entry_min_velocity_accel: float = 0.0
    entry_min_curve_velocity: float = 0.05
    entry_min_curve_acceleration: float = -10.0
    entry_max_top3_buyer_pct: float = 50.0
    # Upper cap on acceleration — parabolic pumps reverse into hard_stop.
    # Data (236 tuned-config trades): hard_stop mean +0.25, win mean -0.04.
    # 100.0 = disabled; tune via optimizer.
    entry_max_curve_acceleration: float = 2.0
    # Upper cap on fast_score — paradoxically, very hot fast scores rug more
    # (hard_stop mean 25.5 vs win mean 19.9). 1000 = disabled.
    entry_max_fast_score: int = 30
    # Reject tokens from creators that spam multiple tokens same day.
    # 1000 = disabled; data shows stops have 8.8 vs wins 6.9 tokens/day.
    creator_max_tokens_today: int = 10
    # Helius top-holder concentration gates (Apr 2026). Applied only when
    # token_holders_snapshots has a T+30 snapshot for the mint; absent = no
    # gate. 100.0 = disabled. Preliminary signal: top1 in [20,80) correlates
    # with higher survival; top1 ≥ 80% shows ~61% death rate.
    entry_max_top1_holder_pct: float = (
        100.0  # gate disabled — active gate HURT PnL (sweep 2026-04-22)
    )
    entry_max_top5_holder_pct: float = 100.0
    # Delta signal (codex recommendation): top1 distribution velocity between
    # T+30 and T+120. Positive = concentration growing (bad, dev buying more);
    # negative = distributing (good, organic growth). 100 = disabled.
    entry_max_top1_delta_pct: float = 100.0
    entry_min_top1_delta_pct: float = -100.0

    # ── FULL PHASE observation filter weights ──────────────
    min_unique_buyers: int = 3
    min_buy_volume_sol: float = 0.5
    max_curve_progress_pct: float = 60.0
    min_buy_diversity: int = 3
    whale_dominance_pct: float = 50.0

    # ── Creator filter ─────────────────────────────────────
    creator_serial_threshold: int = 50
    creator_sell_penalty: bool = True
    creator_sold_score: int = -15
    # Creator-snapshot gates (from #48/#49 creator_snapshots table). All
    # disabled by default (0 or 1.0): fire only when optimizer tunes them.
    # rug_rate_reject:     hard reject if creator's historical rug rate ≥ this
    # min_graduation_rate: hard reject if graduation rate < this (rough:
    #                      "creator has never graduated anything" = skip)
    # min_age_days:        hard reject if creator newer than this
    # Only applied when snapshot has ≥ snapshot_min_priors prior tokens,
    # so brand-new creators aren't judged on a single data point.
    creator_rug_rate_reject: float = 1.0  # 1.0 = disabled
    creator_min_graduation_rate: float = 0.05
    creator_min_age_days: float = 1.0
    creator_snapshot_min_priors: int = 2

    # ── Sell pressure scoring ──────────────────────────────
    sell_ratio_dump: float = 1.0
    sell_ratio_heavy: float = 0.7
    sell_ratio_moderate: float = 0.4
    sell_dump_penalty: int = -50
    sell_heavy_penalty: int = -40
    sell_moderate_penalty: int = -5
    sell_dominant_bonus: int = 10

    # ── Volume scoring ─────────────────────────────────────
    volume_massive_sol: float = 20.0
    volume_massive_score: int = 25
    volume_high_sol: float = 5.0
    volume_high_score: int = 15
    volume_ok_score: int = 5
    volume_low_score: int = -5

    # ── Buyer scoring ──────────────────────────────────────
    buyers_30_score: int = 30
    buyers_10_score: int = 20
    buyers_5_score: int = 5
    buyers_low_score: int = -15

    # ── Curve scoring ──────────────────────────────────────
    curve_near_grad_pct: float = 70.0
    curve_near_grad_score: int = -10
    curve_mid_score: int = -5
    curve_healthy_score: int = 5
    curve_low_pct: float = 36.0
    curve_low_score: int = -50

    # ── PORTFOLIO / EXECUTION ──────────────────────────────
    portfolio_initial_sol: float = 2.0
    portfolio_max_positions: int = 30
    # Collector mode: observe + score tokens but DON'T open paper trades.
    # Use to gather signal-quality data without polluting paper_trades table
    # with noisy losing trades while tuning config. Toggle via PULSE_COLLECTOR_ONLY.
    collector_only: bool = False
    buy_amount_sol: float = 0.1
    execution_base_slippage: float = 0.02
    execution_slippage_per_volume_pct: float = 0.05
    execution_max_slippage: float = 0.25
    execution_sell_slippage_mult: float = 1.5

    # ── ENTRY STRATEGY ─────────────────────────────────────
    entry_mode: str = "both"  # "fast" | "full" | "both"

    # ── PULSE MONITOR ──────────────────────────────────────
    pulse_window_size: int = 20
    pulse_min_events: int = 15
    pulse_dead_buy_rate: float = 0.05
    pulse_weak_buy_rate: float = 0.30
    pulse_trend_threshold: float = 0.15
    pulse_whale_exit_sol: float = 1.0

    # ── EXIT RULES ─────────────────────────────────────────
    exit_min_hold_seconds: float = 3.0
    # Creator dump is the cleanest bear signal on pump.fun — dev selling =
    # monetary alignment flipped. Flipped ON 2026-04-22 per codex ranking.
    exit_on_creator_dump: bool = True
    exit_on_whale: bool = False  # codex: fires too often at small-cap size
    exit_sell_pressure_ratio: float = 1.0
    # Momentum-fade exit: sell when buy_rate falls to ratio × peak_buy_rate.
    # 0.3 chosen as mid-range from codex-suggested sweep (0.2/0.3/0.4).
    exit_peak_buy_rate_drop_ratio: float = 0.3
    # Floor on peak buy_rate before drop-from-peak rule can fire. Prevents
    # early noise (e.g. peak=0.1 → drop to 0.03 is not a real signal).
    exit_peak_buy_rate_floor: float = 0.3
    exit_no_new_wallets_events: int = 5
    exit_near_graduation_pct: float = 70.0
    exit_hard_stop_loss_pct: float = 15.0  # tight stop — strict-combo best 2026-04-22
    exit_max_hold_seconds: float = 90.0  # longer hold lets winners mature
    # Inactivity tracking: 0.0 = disabled (neutral default). Non-zero enables
    # the "no trades for N seconds → dead" exit path in pipeline / optimizer.
    exit_inactivity_seconds: float = 120.0
    exit_trend_dying_count: int = 2

    # ── TAKE PROFIT ────────────────────────────────────────
    exit_take_profit_pct: float = 100.0
    exit_take_profit_enabled: bool = True

    # ── TRAILING STOP ──────────────────────────────────────
    exit_trailing_stop_enabled: bool = True
    exit_trailing_stop_activation_pct: float = 50.0  # activate after +50% profit
    exit_trailing_stop_distance_pct: float = 50.0  # sell if drops 50% from peak

    # ── PARTIAL EXIT RULES ─────────────────────────────────
    exit_partial_on_profit_pct: float = 0.30
    exit_profit_threshold_pct: float = 200.0
    exit_partial_on_weak_pulse_pct: float = 0.50
    exit_weak_pulse_min_profit_pct: float = 50.0
    exit_moonbag_pct: float = 0.10

    # ── EXIT ML ACTIVATION (codex Q4 Phase B, 2026-04-23) ──
    # Exit model was shadow-logged via ``ExitManager.ml_exit_proba`` for
    # weeks. Activating it under a conservative single-threshold policy:
    # if enabled AND the advisor returns proba >= threshold, escalate
    # ``hold`` → ``sell_all`` with reason ``ml_exit_trigger``. Never
    # overrides hard rules (creator_dump, hard_stop, timeout, etc.) — those
    # are checked FIRST. Never softens a rule-based sell (ML cannot hold
    # us in a rug). Disabled by default so the bot only changes behavior
    # when ops explicitly opts in via PULSE_EXIT_ML_ACTIVE=1.
    exit_ml_active: bool = False
    # Conservative default 0.80 — calibrated so that only very confident
    # "definitely sell now" signals trigger. Tune after shadow analysis
    # of ml_exit_proba distribution on held positions.
    exit_ml_sell_threshold: float = 0.80
    # Optional minimum hold guard (seconds) to prevent ML from flipping
    # us out of a position the moment we enter — e.g., MEV bot buys
    # triggering premature "drop coming" prediction. Set to 0 to disable.
    exit_ml_min_hold_seconds: float = 15.0

    # ── BACKTEST ───────────────────────────────────────────
    # Source DB for `main.py backtest` replay; defaults to live DB.
    backtest_db_path: str = "pulse_bot.db"
    # Destination for grid-search results (separate file to avoid
    # single-writer contention with the live collector).
    optimizer_db_path: str = "optimizer.db"

    # ── Infrastructure ─────────────────────────────────────
    db_path: str = "pulse_bot.db"
    dashboard_refresh_seconds: int = 3
    log_level: str = "INFO"


def get_config() -> PulseBotConfig:
    """Build config from environment variables with dataclass defaults as fallback."""
    kwargs: dict = {}
    env_map = {
        "PULSE_WS_URL": "ws_url",
        "PULSE_OBSERVE_SECONDS": ("observe_seconds", int),
        "PULSE_FAST_OBSERVE": ("fast_observe_seconds", int),
        "PULSE_MAX_CONCURRENT": ("max_concurrent_observations", int),
        "PULSE_SCORE_BUY": ("score_threshold_buy", int),
        "PULSE_SCORE_BORDERLINE": ("score_threshold_borderline", int),
        "PULSE_FAST_SCORE": ("fast_score_threshold", int),
        "PULSE_DB_PATH": "db_path",
        "PULSE_OPTIMIZER_DB_PATH": "optimizer_db_path",
        "PULSE_LOG_LEVEL": "log_level",
        "PULSE_COLLECTOR_ONLY": (
            "collector_only",
            lambda v: v.lower() in ("1", "true", "yes"),
        ),
        "PULSE_EXIT_ML_ACTIVE": (
            "exit_ml_active",
            lambda v: v.lower() in ("1", "true", "yes"),
        ),
        "PULSE_EXIT_ML_SELL_THRESHOLD": ("exit_ml_sell_threshold", float),
        "PULSE_EXIT_ML_MIN_HOLD_SECONDS": ("exit_ml_min_hold_seconds", float),
    }
    for env_key, target in env_map.items():
        val = os.environ.get(env_key)
        if val is not None:
            if isinstance(target, tuple):
                field_name, cast = target
                kwargs[field_name] = cast(val)
            else:
                kwargs[target] = val
    return PulseBotConfig(**kwargs)
