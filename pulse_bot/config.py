# pulse_bot/config.py
"""Configuration for the Pulse Bot. Every threshold is a tunable knob for backtesting."""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass
class PulseBotConfig:
    """All bot settings with sensible defaults. Every field is a tunable parameter."""

    # ── WebSocket ──────────────────────────────────────────
    ws_url: str = "wss://pumpportal.fun/api/data"

    # ── Concurrency ────────────────────────────────────────
    max_concurrent_observations: int = 20

    # ── FAST PHASE (3-5 sec, early entry) ──────────────────
    fast_observe_seconds: int = 5  # observation window for fast decision
    fast_min_buys: int = 5  # min buy transactions in window
    fast_min_unique_buyers: int = 3  # min unique buyer wallets
    fast_min_volume_sol: float = 0.3  # min total buy volume in SOL
    fast_max_sell_ratio: float = 0.3  # max sells/buys ratio (>0.3 = too much selling)
    fast_min_buy_rate: float = 0.8  # min buys per second (velocity)
    fast_min_diversity: int = 2  # min unique buy amounts (anti-bot)
    fast_score_threshold: int = 15  # score threshold for FAST_BUY
    fast_max_curve_pct: float = 38.0  # max curve progress (don't FAST_BUY late)
    fast_creator_sold_reject: bool = True  # reject if creator sold in fast window

    # ── FAST PHASE scoring weights ─────────────────────────
    fast_w_buyers: int = 15  # weight: enough unique buyers
    fast_w_volume: int = 10  # weight: enough volume
    fast_w_velocity: int = 15  # weight: buys per second
    fast_w_diversity: int = 5  # weight: diverse buy amounts
    fast_w_no_sells: int = 10  # weight: no sell pressure
    fast_w_curve_healthy: int = 5  # weight: curve not too high

    # ── FULL PHASE (45 sec, confirmation) ──────────────────
    observe_seconds: int = 45  # full observation window
    score_threshold_buy: int = 20  # score threshold for BUY
    score_threshold_borderline: int = 10  # score threshold for BORDERLINE

    # ── FULL PHASE observation filter weights ──────────────
    min_unique_buyers: int = 3
    min_buy_volume_sol: float = 0.5
    max_curve_progress_pct: float = 40.0
    min_buy_diversity: int = 3
    whale_dominance_pct: float = 50.0

    # ── Creator filter ─────────────────────────────────────
    creator_serial_threshold: int = (
        50  # relaxed — data shows serial creators can be profitable
    )
    creator_sell_penalty: bool = True
    creator_sold_score: int = -15  # soft penalty for creator selling

    # ── Sell pressure thresholds ───────────────────────────
    sell_ratio_dump: float = 1.0  # ratio >= this → -30 (dump)
    sell_ratio_heavy: float = 0.7  # ratio >= this → -15
    sell_ratio_moderate: float = 0.4  # ratio >= this → -5
    sell_dump_penalty: int = -50  # data: 32% WR, -14% avg
    sell_heavy_penalty: int = -40  # data: 41% WR, -550% total P&L above 0.7
    sell_moderate_penalty: int = -5
    sell_dominant_bonus: int = 10  # data: 94% WR when sell_p < 0.5

    # ── Volume scoring ─────────────────────────────────────
    volume_massive_sol: float = 20.0  # >this → massive bonus
    volume_massive_score: int = 25
    volume_high_sol: float = 5.0  # >this → high bonus
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
    curve_low_pct: float = (
        36.0  # curve below this = dead token (data: 26% WR, -971% P&L)
    )
    curve_low_score: int = -50  # hard penalty — kills almost all losers

    # ── PumpFun bonding curve ──────────────────────────────
    pumpfun_graduation_sol: float = 85.0

    # ── PORTFOLIO / EXECUTION ──────────────────────────────
    portfolio_initial_sol: float = 0.15
    portfolio_max_positions: int = 3
    buy_amount_sol: float = 0.03
    execution_fee_pct: float = 0.01  # 1% Pump.fun fee
    execution_priority_fee: float = 0.0001  # priority fee SOL
    execution_base_slippage: float = 0.02  # 2% base slippage
    execution_slippage_per_volume_pct: float = (
        0.05  # additional slippage per volume ratio
    )
    execution_max_slippage: float = 0.25  # 25% max slippage cap
    execution_sell_slippage_mult: float = (
        1.5  # sell slippage multiplier (thinner liquidity)
    )

    # ── ENTRY STRATEGY ─────────────────────────────────────
    entry_mode: str = "fast"  # "fast" | "full" | "both"
    # "fast" = enter on FAST_BUY only
    # "full" = enter on full BUY only
    # "both" = enter on FAST_BUY, add on BUY if not already in

    # ── PULSE MONITOR ──────────────────────────────────────
    pulse_window_size: int = 20
    pulse_min_events: int = 15  # need enough events for pulse to be meaningful
    pulse_dead_buy_rate: float = 0.10
    pulse_weak_buy_rate: float = 0.30
    pulse_trend_threshold: float = 0.15  # % change for trend detection
    pulse_whale_exit_sol: float = 1.0  # sell > this = whale exit

    # ── EXIT RULES ─────────────────────────────────────────
    exit_min_hold_seconds: float = 10.0  # min hold before pulse can exit (warmup)
    exit_on_creator_dump: bool = True
    exit_on_whale: bool = True
    exit_sell_pressure_ratio: float = 2.0  # sell_rate > buy_rate × this → exit
    exit_no_new_wallets_events: int = 5  # no new wallets for N buys → exit
    exit_near_graduation_pct: float = 70.0
    exit_hard_stop_loss_pct: float = 50.0  # -50% → hard stop
    exit_max_hold_seconds: float = 7200  # 2 hours max
    exit_trend_dying_count: int = 2  # N consecutive declining windows → exit

    # ── TAKE PROFIT ────────────────────────────────────────
    exit_take_profit_pct: float = 100.0  # +100% → sell all (simple TP)
    exit_take_profit_enabled: bool = True

    # ── PARTIAL EXIT RULES ─────────────────────────────────
    exit_partial_on_profit_pct: float = 0.30  # sell 30% on strong profit
    exit_profit_threshold_pct: float = 200.0  # what counts as strong profit (%)
    exit_partial_on_weak_pulse_pct: float = 0.50  # sell 50% on weak pulse + profit
    exit_weak_pulse_min_profit_pct: float = 50.0  # min profit for weak pulse sell
    exit_moonbag_pct: float = 0.10  # always keep 10%

    # ── BACKTEST ───────────────────────────────────────────
    backtest_db_path: str = "pulse_bot.db"  # source data
    backtest_results_db: str = "backtest_results.db"  # results storage

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
