# pulse_bot/config.py
"""Configuration for the Pulse Bot. Every threshold is a tunable knob for backtesting."""

from __future__ import annotations

import os
from dataclasses import dataclass

# ── Constants (not tunable — facts about Pump.fun) ─────────
PUMPFUN_FEE_PCT = 0.01  # 1% Pump.fun fee
PUMPFUN_GRADUATION_SOL = 85.0  # SOL to graduate
PUMPFUN_PRIORITY_FEE = 0.0001  # priority fee SOL

# 2026-05-04 — PumpPortal silently gated `subscribeTokenTrade` and
# `subscribeAccountTrade` behind an API key whose linked Lightning
# wallet holds ≥0.02 SOL. Without the key the WS still accepts the
# subscribe message but server-side keepalive drops at ~60-90s with
# code 1011 — caused 3 days of zero realtime trade ingestion before
# we probed the WS directly and saw the error response.
PUMPPORTAL_API_KEY = os.environ.get("PUMPPORTAL_API_KEY", "")


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
    # 2026-04-29 — creator self-buy gates. Most pump.fun rugs feature the
    # dev sniping their own token in the first ~3 buys to manufacture
    # momentum.  ``fast_creator_self_buy_reject_max_position`` = N rejects
    # tokens where creator appears as buyer #1..N in the fast window
    # (1-indexed; 0 disables, default = 0 / off — collect data first).
    # ``fast_creator_self_buy_score`` adds a soft penalty even when not
    # hard-rejected so the scorer reflects the rug risk.
    fast_creator_self_buy_reject_max_position: int = 0
    fast_creator_self_buy_score: int = -10

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

    # Post-scoring trade collection window for SKIP/RULES tokens.
    # Default lifted 0 → 600s 2026-04-26: zero default silently broke
    # max_hold sweeps and entry_timing label generation (see memory
    # ``project_post_scoring_data_truncation``). 600s = 10 min extra
    # observation per non-DOA SKIP, costs +N WS/DB writes.
    # Override via env ``PULSE_EXTENDED_OBSERVE_SECONDS`` (e.g. =0 to
    # opt out, =1200 for 20 min).
    pulse_extended_observe_seconds: float = 600.0

    # ── LAUNCHPAD SOURCE ──────────────────────────────────
    # Which adapter feeds new tokens / trades into the pipeline.
    #   "pumpportal"        — current default: PumpPortal WebSocket only.
    #   "geyser+pumpportal" — Yellowstone gRPC primary + PumpPortal fallback
    #                         (deduplicated, auto-failover via multiplexer).
    #   "geyser"            — gRPC only (no fallback, for testing).
    pulse_launchpad: str = "pumpportal"
    # Yellowstone gRPC endpoint of the local validator
    # (must run agave-validator with --geyser-plugin-config).
    pulse_geyser_endpoint: str = "127.0.0.1:10000"
    # Optional auth token for hosted Yellowstone (Triton, Helius gRPC) —
    # leave empty for our own local validator.
    pulse_geyser_x_token: str = ""
    # Multiplexer marks primary "stale" if no event for this many seconds
    # and logs a fallback-active warning. Stream itself never stops; the
    # fallback adapter just naturally carries it.
    pulse_geyser_health_lag_seconds: float = 5.0

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

    # ── EXIT ML ACTIVATION (codex Q4, v2 2026-04-23) ───────
    # Exit model now drives a 4-way gated decision (codex E2):
    #   * proba >= sell_ceiling   → SELL_ALL  (force full exit)
    #   * partial_floor..ceiling  → SELL_PARTIAL (force partial exit)
    #   * HOLD_HARD_THRESHOLD..   → RULES (defer)
    #   * < HOLD_HARD (0.20)      → HOLD_HARD (block weak_pulse_profit
    #                               ONLY; cannot override hard rules)
    # Hard rules (creator_dump, hard_stop, timeout, take_profit,
    # trailing_stop, whale, near_graduation) remain immutable.
    exit_ml_active: bool = True
    exit_ml_sell_threshold: float = 0.80
    exit_ml_partial_floor: float = 0.55  # phase E2
    exit_ml_min_hold_seconds: float = 15.0
    # HOLD_HARD rollback (codex E6 three-layer disable). Set to False
    # via PULSE_EXIT_ML_HOLD_HARD=0 to disable without touching other
    # exit behavior.
    exit_ml_hold_hard_enabled: bool = True
    # Regression head for SL tightening (E3). q=0.25 quantile head
    # forecasts forward-60s PnL; when binary ML is directionally
    # confident SELL AND forecast < -5% AND current PnL in the mid-red
    # zone (-15% < pnl ≤ -5%), escalate to sell_all pre-emptively.
    # TP-loosening head NOT wired — current q=0.75 spearman is -0.07
    # (anti-signal); reconsider once N_exit ≥ 3000.
    # User directive 2026-04-23: activated by default in paper-trading.
    exit_regression_active: bool = True
    # 2026-04-30: dynamic max_hold via exit_quantile_max_hold model. Default
    # OFF — bot uses static cfg.exit_max_hold_seconds. When True, the model
    # is queried once per position to pick max_hold ∈ [30s, exit_max_hold_
    # seconds], so it can only EARLY-exit (the static value remains the
    # safety ceiling). Activate via PULSE_EXIT_MAX_HOLD_DYNAMIC=1 after
    # paired-bootstrap shadow validation.
    exit_max_hold_dynamic: bool = False

    # ── ENTRY ML gating + sizing (moved from hardcoded 2026-04-24) ──
    # Confidence-gating thresholds. None → use val-tuned values baked
    # into the model's meta.json. Set via env to force override at
    # startup (e.g. `PULSE_ENTRY_PROBA_FLOOR=0.5 PULSE_ENTRY_PROBA_CEILING=0.5`
    # for full ML-only mode). Values apply to raw (pre-Platt) proba.
    entry_ml_proba_floor: float | None = None
    entry_ml_proba_ceiling: float | None = None

    # Sizing ladder (E5 confidence-weighted partials). Three (proba,
    # fraction) breakpoints. As proba crosses each threshold, position
    # size steps up. Previously hardcoded in exit_manager.py; moved here
    # so optimizer can sweep. `proba ≥ exit_ml_sell_threshold` (0.80) is
    # SELL_ALL — not on this ladder.
    ml_sizing_proba_1: float = 0.55
    ml_sizing_frac_1: float = 0.30
    ml_sizing_proba_2: float = 0.65
    ml_sizing_frac_2: float = 0.50
    ml_sizing_proba_3: float = 0.75
    ml_sizing_frac_3: float = 0.70

    # ── ENTRY ML training hyperparameters (moved from train.py 2026-04-24) ──
    # XGBoost classifier capacity. Defaults from codex v9: depth=3 +
    # min_child_weight=5 give ~16 leaves across a tree, safe at N_pos≈250.
    # When N_pos grows (Phase 2 N≥500), can widen depth=4 and eventually
    # n_estimators=200+. Optimizer sweep tunes within constraints.
    entry_train_n_estimators: int = 150
    entry_train_max_depth: int = 3
    entry_train_learning_rate: float = 0.05
    entry_train_min_child_weight: int = 5
    entry_train_subsample: float = 0.8
    entry_train_colsample_bytree: float = 0.8

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
        "PULSE_EXIT_ML_PARTIAL_FLOOR": ("exit_ml_partial_floor", float),
        "PULSE_EXIT_ML_MIN_HOLD_SECONDS": ("exit_ml_min_hold_seconds", float),
        "PULSE_EXIT_ML_HOLD_HARD": (
            "exit_ml_hold_hard_enabled",
            lambda v: v.lower() in ("1", "true", "yes"),
        ),
        "PULSE_EXIT_REGRESSION_ACTIVE": (
            "exit_regression_active",
            lambda v: v.lower() in ("1", "true", "yes"),
        ),
        "PULSE_EXIT_MAX_HOLD_DYNAMIC": (
            "exit_max_hold_dynamic",
            lambda v: v.lower() in ("1", "true", "yes"),
        ),
        "PULSE_ENTRY_PROBA_FLOOR": ("entry_ml_proba_floor", float),
        "PULSE_ENTRY_PROBA_CEILING": ("entry_ml_proba_ceiling", float),
        "PULSE_ENTRY_MODE": ("entry_mode", str),
        "PULSE_ML_SIZING_PROBA_1": ("ml_sizing_proba_1", float),
        "PULSE_ML_SIZING_FRAC_1": ("ml_sizing_frac_1", float),
        "PULSE_ML_SIZING_PROBA_2": ("ml_sizing_proba_2", float),
        "PULSE_ML_SIZING_FRAC_2": ("ml_sizing_frac_2", float),
        "PULSE_ML_SIZING_PROBA_3": ("ml_sizing_proba_3", float),
        "PULSE_ML_SIZING_FRAC_3": ("ml_sizing_frac_3", float),
        "PULSE_ENTRY_N_ESTIMATORS": ("entry_train_n_estimators", int),
        "PULSE_ENTRY_MAX_DEPTH": ("entry_train_max_depth", int),
        "PULSE_ENTRY_LEARNING_RATE": ("entry_train_learning_rate", float),
        "PULSE_ENTRY_MIN_CHILD_WEIGHT": ("entry_train_min_child_weight", int),
        "PULSE_ENTRY_SUBSAMPLE": ("entry_train_subsample", float),
        "PULSE_ENTRY_COLSAMPLE_BYTREE": ("entry_train_colsample_bytree", float),
        # Exit-param overrides for sweep scripts (relabel → retrain experiments)
        "PULSE_EXIT_HARD_STOP_LOSS_PCT": ("exit_hard_stop_loss_pct", float),
        "PULSE_EXIT_TAKE_PROFIT_PCT": ("exit_take_profit_pct", float),
        "PULSE_EXIT_MAX_HOLD_SECONDS": ("exit_max_hold_seconds", float),
        "PULSE_EXIT_INACTIVITY_SECONDS": ("exit_inactivity_seconds", float),
        "PULSE_EXTENDED_OBSERVE_SECONDS": ("pulse_extended_observe_seconds", float),
        "PULSE_FAST_CREATOR_SELF_BUY_REJECT_MAX_POSITION": (
            "fast_creator_self_buy_reject_max_position",
            int,
        ),
        "PULSE_FAST_CREATOR_SELF_BUY_SCORE": (
            "fast_creator_self_buy_score",
            int,
        ),
        "PULSE_LAUNCHPAD": ("pulse_launchpad", str),
        "PULSE_GEYSER_ENDPOINT": ("pulse_geyser_endpoint", str),
        "PULSE_GEYSER_X_TOKEN": ("pulse_geyser_x_token", str),
        "PULSE_GEYSER_HEALTH_LAG_SECONDS": ("pulse_geyser_health_lag_seconds", float),
    }
    for env_key, target in env_map.items():
        val = os.environ.get(env_key)
        if val is not None:
            if isinstance(target, tuple):
                field_name, cast = target
                kwargs[field_name] = cast(val)
            else:
                kwargs[target] = val
    cfg = PulseBotConfig(**kwargs)
    _warn_on_dead_exit_combo(cfg)
    return cfg


def _warn_on_dead_exit_combo(cfg: PulseBotConfig) -> None:
    """Emit a startup WARNING when ``(exit_max_hold_seconds,
    exit_take_profit_pct)`` is the long-known dead pair (TP=100% but
    max_hold=90s makes TP unreachable in practice — see memory
    ``project_exit_take_profit_broken`` and
    ``project_economic_backtest_failing``). Optimizer must re-tune the
    pair; live bots running with these defaults are effectively in a
    "rules + dead TP" regime.

    We log once at config-build time and never raise so existing
    setups keep working through the warning.
    """
    import logging as _logging

    _logger = _logging.getLogger("pulse_bot.config")
    tp = float(cfg.exit_take_profit_pct or 0.0)
    mh = float(cfg.exit_max_hold_seconds or 0.0)
    if tp >= 100.0 and mh <= 120.0:
        _logger.warning(
            "EXIT CONFIG WARNING: take_profit_pct=%.1f%% with "
            "max_hold_seconds=%.0fs is the documented dead pair "
            "(TP almost never fires; see "
            "project_exit_take_profit_broken). Run optimizer sweep "
            "before activating ML exit gates.",
            tp,
            mh,
        )
