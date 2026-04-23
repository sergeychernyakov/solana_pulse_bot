# tests/pulse_bot/test_exit_peak_drop.py
"""Coverage for the peak buy-rate drop exit rule (#61)."""

from __future__ import annotations

from pulse_bot.config import PulseBotConfig
from pulse_bot.pulse.exit_manager import ExitManager
from pulse_bot.pulse.monitor import PulseSnapshot


def _snapshot(
    *,
    buy_rate: float,
    peak_buy_rate: float,
    sell_rate: float = 0.0,
) -> PulseSnapshot:
    drop = buy_rate / peak_buy_rate if peak_buy_rate > 0 else 1.0
    return PulseSnapshot(
        buy_rate=buy_rate,
        sell_rate=sell_rate,
        new_wallet_rate=0.5,
        avg_buy_size_sol=0.1,
        total_sol_in_window=1.0,
        creator_selling=False,
        whale_exit=False,
        buy_rate_trend="stable",
        buy_size_trend="stable",
        trend_declining_count=0,
        curve_progress_pct=10.0,
        window_events=20,
        peak_buy_rate=peak_buy_rate,
        buy_rate_drop_from_peak=drop,
        max_sell_sol=0.0,
    )


def test_drop_from_peak_fires_when_enabled() -> None:
    cfg = PulseBotConfig(
        exit_peak_buy_rate_drop_ratio=0.3,
        exit_peak_buy_rate_floor=0.3,
        pulse_dead_buy_rate=0.05,
    )
    mgr = ExitManager(cfg)
    snap = _snapshot(buy_rate=0.15, peak_buy_rate=0.7)
    signal = mgr.decide(snap, pnl_pct=0.0, elapsed_sec=10.0)
    assert signal.action == "sell_all"
    assert signal.reason == "buy_rate_drop"


def test_drop_from_peak_respects_floor() -> None:
    """Peak below floor → rule must not fire even on steep relative drop."""
    cfg = PulseBotConfig(
        exit_peak_buy_rate_drop_ratio=0.3,
        exit_peak_buy_rate_floor=0.3,
    )
    mgr = ExitManager(cfg)
    snap = _snapshot(buy_rate=0.01, peak_buy_rate=0.05)  # peak < floor
    signal = mgr.decide(snap, pnl_pct=0.0, elapsed_sec=10.0)
    assert signal.action != "sell_all" or signal.reason != "buy_rate_drop"


def test_drop_from_peak_respects_explicit_disable() -> None:
    """Setting ratio=0.0 always disables regardless of default."""
    cfg = PulseBotConfig()
    cfg.exit_peak_buy_rate_drop_ratio = 0.0
    mgr = ExitManager(cfg)
    snap = _snapshot(buy_rate=0.01, peak_buy_rate=0.7)
    signal = mgr.decide(snap, pnl_pct=0.0, elapsed_sec=10.0)
    assert signal.reason != "buy_rate_drop"


def test_drop_from_peak_enabled_by_default() -> None:
    """Default config turns momentum-fade exit ON at 0.3 (codex #1 flip)."""
    cfg = PulseBotConfig()
    assert cfg.exit_peak_buy_rate_drop_ratio == 0.3
    assert cfg.exit_on_creator_dump is True
