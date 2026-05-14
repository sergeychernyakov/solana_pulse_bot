# pulse_bot/feature_flags.py
"""Typed feature flags consolidating ``os.environ.get(...)`` calls
that today live scattered across pipeline.py, policy.py, decision_service.py.

Codex review 2026-04-28: the ML-hook activation surface is currently:

    os.environ.get("PULSE_ENTRY_T30_ACTIVE", "0") == "1"
    os.environ.get("PULSE_ENTRY_T30_SKIP_ACTIVE", "0") == "1"
    os.environ.get("PULSE_ENTRY_GREY_TO_SKIP", "0") == "1"
    os.environ.get("PULSE_T30_SKIP_TAIL", "0.05")
    os.environ.get("PULSE_TIMING_CONFIDENCE_GATE", "0.85")
    os.environ.get("PULSE_BOT_CLUSTER_HARD_SKIP", "3")
    os.environ.get("PULSE_ALLOW_DEGENERATE_MODEL", "0")
    os.environ.get("PULSE_METRICS_PORT", "9100")
    os.environ.get("PULSE_CHECKPOINT_LAG_BUFFER", "0.5")
    os.environ.get("PULSE_MUX_CREATE_QUEUE_MAX", "5000")
    os.environ.get("PULSE_MUX_TRADE_QUEUE_MAX", "200")

Each one is a typo waiting to happen. ``FeatureFlags`` reads them
once at startup, casts/validates, exposes a typed API.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


def _bool_env(name: str, default: bool = False) -> bool:
    return os.environ.get(name, "1" if default else "0").lower() in ("1", "true", "yes")


def _float_env(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError:
        logger.warning(
            "FeatureFlags: %s=%r is not a number; using %s", name, raw, default
        )
        return default


def _int_env(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        logger.warning(
            "FeatureFlags: %s=%r is not an int; using %s", name, raw, default
        )
        return default


@dataclass(frozen=True)
class FeatureFlags:
    """Typed snapshot of all PULSE_* env vars, read once at boot.

    Frozen so a runtime mutation can't accidentally flip live trading
    mid-flight (which would be a debugging nightmare).
    """

    # ── Entry decision modes ─────────────────────────────────────
    entry_grey_to_skip: bool = field(
        default_factory=lambda: _bool_env("PULSE_ENTRY_GREY_TO_SKIP")
    )
    entry_t30_active: bool = field(
        default_factory=lambda: _bool_env("PULSE_ENTRY_T30_ACTIVE")
    )
    entry_t30_skip_only_active: bool = field(
        default_factory=lambda: _bool_env("PULSE_ENTRY_T30_SKIP_ACTIVE")
    )
    survival_active: bool = field(
        default_factory=lambda: _bool_env("PULSE_SURVIVAL_ACTIVE")
    )
    timing_active: bool = field(
        default_factory=lambda: _bool_env("PULSE_TIMING_ACTIVE")
    )
    allow_degenerate_model: bool = field(
        default_factory=lambda: _bool_env("PULSE_ALLOW_DEGENERATE_MODEL")
    )

    # ── Confidence gates (codex 2026-04-27) ──────────────────────
    t30_skip_tail: float = field(
        default_factory=lambda: _float_env("PULSE_T30_SKIP_TAIL", 0.05)
    )
    t30_buy_tail: float = field(
        default_factory=lambda: _float_env("PULSE_T30_BUY_TAIL", 0.85)
    )
    timing_confidence_gate: float = field(
        default_factory=lambda: _float_env("PULSE_TIMING_CONFIDENCE_GATE", 0.85)
    )

    # ── Pre-filters ──────────────────────────────────────────────
    bot_cluster_hard_skip: int = field(
        default_factory=lambda: _int_env("PULSE_BOT_CLUSTER_HARD_SKIP", 3)
    )

    # ── Event-time watermark (codex Issue #1, 2026-04-27) ────────
    checkpoint_lag_buffer_sec: float = field(
        default_factory=lambda: _float_env("PULSE_CHECKPOINT_LAG_BUFFER", 0.5)
    )

    # ── Multiplexer queue sizes (codex Phase I, 2026-04-28) ─────
    mux_create_queue_max: int = field(
        default_factory=lambda: _int_env("PULSE_MUX_CREATE_QUEUE_MAX", 5000)
    )
    mux_trade_queue_max: int = field(
        default_factory=lambda: _int_env("PULSE_MUX_TRADE_QUEUE_MAX", 200)
    )

    # ── Observability ────────────────────────────────────────────
    metrics_port: int = field(
        default_factory=lambda: _int_env("PULSE_METRICS_PORT", 9100)
    )

    def log_summary(self) -> None:
        """Emit one INFO line per flag at startup so the bot.log
        records exactly what mode the bot booted in. Matches the
        verbosity codex asked for: 'why model was eligible to run'."""
        from dataclasses import fields

        logger.info("=" * 78)
        logger.info("FeatureFlags — boot snapshot")
        for f in fields(self):
            logger.info("  %-30s = %s", f.name, getattr(self, f.name))
        logger.info("=" * 78)


# Module-level singleton — call sites import directly.
flags = FeatureFlags()
