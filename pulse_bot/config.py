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
    max_concurrent_observations: int = 20

    # ── FAST PHASE (3-5 sec, early entry) ──────────────────
    fast_observe_seconds: int = 5
    fast_min_buys: int = 5
    fast_min_unique_buyers: int = 3
    fast_min_volume_sol: float = 0.3
    fast_max_sell_ratio: float = 0.3
    fast_min_buy_rate: float = 0.8
    fast_min_diversity: int = 2
    fast_score_threshold: int = 15
    fast_max_curve_pct: float = 38.0
    fast_creator_sold_reject: bool = True

    # ── FAST PHASE scoring weights ─────────────────────────
    fast_w_buyers: int = 15
    fast_w_volume: int = 10
    fast_w_velocity: int = 15
    fast_w_diversity: int = 5
    fast_w_no_sells: int = 10
    fast_w_curve_healthy: int = 5

    # ── FULL PHASE (45 sec, confirmation) ──────────────────
    observe_seconds: int = 45
    score_threshold_buy: int = 20
    score_threshold_borderline: int = 10

    # ── HARD ENTRY FILTERS (reject before scoring) ─────────
    min_market_cap_sol: float = 0.0  # min MCap to consider (0 = no filter)
    max_sell_pressure_for_entry: float = (
        999.0  # hard reject if sell_p > this (999 = disabled)
    )
    min_curve_for_entry: float = 0.0  # hard reject if curve < this (0 = disabled)
    fast_entry_enabled: bool = True
    full_entry_enabled: bool = True

    # ── FULL PHASE observation filter weights ──────────────
    min_unique_buyers: int = 3
    min_buy_volume_sol: float = 0.5
    max_curve_progress_pct: float = 40.0
    min_buy_diversity: int = 3
    whale_dominance_pct: float = 50.0

    # ── Creator filter ─────────────────────────────────────
    creator_serial_threshold: int = 50
    creator_sell_penalty: bool = True
    creator_sold_score: int = -15

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
    portfolio_initial_sol: float = 0.05
    portfolio_max_positions: int = 3
    buy_amount_sol: float = 0.001
    execution_base_slippage: float = 0.02
    execution_slippage_per_volume_pct: float = 0.05
    execution_max_slippage: float = 0.25
    execution_sell_slippage_mult: float = 1.5

    # ── ENTRY STRATEGY ─────────────────────────────────────
    entry_mode: str = "fast"  # "fast" | "full" | "both"

    # ── PULSE MONITOR ──────────────────────────────────────
    pulse_window_size: int = 20
    pulse_min_events: int = 15
    pulse_dead_buy_rate: float = 0.10
    pulse_weak_buy_rate: float = 0.30
    pulse_trend_threshold: float = 0.15
    pulse_whale_exit_sol: float = 1.0

    # ── EXIT RULES ─────────────────────────────────────────
    exit_min_hold_seconds: float = 10.0
    exit_on_creator_dump: bool = True
    exit_on_whale: bool = True
    exit_sell_pressure_ratio: float = 2.0
    exit_no_new_wallets_events: int = 5
    exit_near_graduation_pct: float = 70.0
    exit_hard_stop_loss_pct: float = 50.0
    exit_max_hold_seconds: float = 7200
    exit_trend_dying_count: int = 2

    # ── TAKE PROFIT ────────────────────────────────────────
    exit_take_profit_pct: float = 100.0
    exit_take_profit_enabled: bool = True

    # ── TRAILING STOP ──────────────────────────────────────
    exit_trailing_stop_enabled: bool = True
    exit_trailing_stop_activation_pct: float = 50.0  # activate after +50% profit
    exit_trailing_stop_distance_pct: float = 30.0  # sell if drops 30% from peak

    # ── PARTIAL EXIT RULES ─────────────────────────────────
    exit_partial_on_profit_pct: float = 0.30
    exit_profit_threshold_pct: float = 200.0
    exit_partial_on_weak_pulse_pct: float = 0.50
    exit_weak_pulse_min_profit_pct: float = 50.0
    exit_moonbag_pct: float = 0.10

    # ── BACKTEST ───────────────────────────────────────────
    backtest_db_path: str = "pulse_bot.db"
    backtest_results_db: str = "backtest_results.db"

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
        "PULSE_LOG_LEVEL": "log_level",
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
