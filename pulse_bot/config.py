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
    fast_observe_seconds: int = 5           # observation window for fast decision
    fast_min_buys: int = 5                  # min buy transactions in window
    fast_min_unique_buyers: int = 3         # min unique buyer wallets
    fast_min_volume_sol: float = 0.3        # min total buy volume in SOL
    fast_max_sell_ratio: float = 0.3        # max sells/buys ratio (>0.3 = too much selling)
    fast_min_buy_rate: float = 0.8          # min buys per second (velocity)
    fast_min_diversity: int = 2             # min unique buy amounts (anti-bot)
    fast_score_threshold: int = 15          # score threshold for FAST_BUY
    fast_max_curve_pct: float = 38.0        # max curve progress (don't FAST_BUY late)
    fast_creator_sold_reject: bool = True   # reject if creator sold in fast window

    # ── FAST PHASE scoring weights ─────────────────────────
    fast_w_buyers: int = 15                 # weight: enough unique buyers
    fast_w_volume: int = 10                 # weight: enough volume
    fast_w_velocity: int = 15               # weight: buys per second
    fast_w_diversity: int = 5               # weight: diverse buy amounts
    fast_w_no_sells: int = 10               # weight: no sell pressure
    fast_w_curve_healthy: int = 5           # weight: curve not too high

    # ── FULL PHASE (45 sec, confirmation) ──────────────────
    observe_seconds: int = 45               # full observation window
    score_threshold_buy: int = 20           # score threshold for BUY
    score_threshold_borderline: int = 10    # score threshold for BORDERLINE

    # ── FULL PHASE observation filter weights ──────────────
    min_unique_buyers: int = 3
    min_buy_volume_sol: float = 0.5
    max_curve_progress_pct: float = 40.0
    min_buy_diversity: int = 3
    whale_dominance_pct: float = 50.0

    # ── Creator filter ─────────────────────────────────────
    creator_serial_threshold: int = 5
    creator_sell_penalty: bool = True
    creator_sold_score: int = -15           # soft penalty for creator selling

    # ── Sell pressure thresholds ───────────────────────────
    sell_ratio_dump: float = 1.0            # ratio >= this → -30 (dump)
    sell_ratio_heavy: float = 0.7           # ratio >= this → -15
    sell_ratio_moderate: float = 0.4        # ratio >= this → -5
    sell_dump_penalty: int = -30
    sell_heavy_penalty: int = -15
    sell_moderate_penalty: int = -5
    sell_dominant_bonus: int = 5            # ratio < moderate → bonus

    # ── Volume scoring ─────────────────────────────────────
    volume_massive_sol: float = 20.0        # >this → massive bonus
    volume_massive_score: int = 25
    volume_high_sol: float = 5.0            # >this → high bonus
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

    # ── PumpFun bonding curve ──────────────────────────────
    pumpfun_graduation_sol: float = 85.0

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
