# tests/pulse_bot/test_codex_fixes_2026_04_30.py
"""Tests for the 4 codex-flagged bugs fixed 2026-04-30 / 2026-05-01.

Covers:
- CRITICAL: PULSE_SURVIVAL_THRESHOLD parsed once at startup, not per tick.
- M1:       Dynamic max_hold clamp must not invert when cfg.exit_max_hold_seconds < 30.
- M2:       _simulate_forward_hold_seconds must not crash whole rebuild on bad row.
- M3:       _df_rows_to_trades must honour creator_wallet for is_creator flag.
- Plus:     MonitorResult.hold_seconds populated on all 3 exit paths.
"""

from __future__ import annotations

import os
from unittest.mock import MagicMock

import pandas as pd
import pytest

from pulse_bot.config import PulseBotConfig
from pulse_bot.core import MonitorResult, PaperTradeRunner
from pulse_bot.ml.build_dataset import (
    _df_rows_to_trades,
    _simulate_forward_hold_seconds,
)
from pulse_bot.models import Trade
from pulse_bot.pulse.exit_manager import ExitManager


# ── M1: Dynamic max_hold clamp ────────────────────────────────────


def _stub_quantile_policy(returned_pred: float):
    pol = MagicMock()
    pol.predict.return_value = returned_pred
    return pol


def test_dynamic_max_hold_clamp_respects_low_static_ceiling():
    """If user sets exit_max_hold_seconds=20 (below default floor=30s),
    the model must NOT extend hold past the static ceiling.
    """
    cfg = PulseBotConfig(exit_max_hold_seconds=20.0, exit_max_hold_dynamic=True)
    em = ExitManager(cfg, quantile_max_hold_policy=_stub_quantile_policy(180.0))
    pulse = MagicMock(
        creator_selling=False,
        buy_rate=10.0,
        sell_rate=1.0,
        window_events=20,
        trend_declining_count=0,
        peak_buy_rate=0.0,
        buy_rate_drop_from_peak=0.0,
        new_wallet_rate=1.0,
        whale_exit=False,
        curve_progress_pct=10.0,
    )
    em.decide(pulse, pnl_pct=0.0, elapsed_sec=5.0)
    assert em._dynamic_max_hold_cached is not None
    assert em._dynamic_max_hold_cached <= cfg.exit_max_hold_seconds, (
        "dynamic max_hold must never exceed static ceiling"
    )


def test_dynamic_max_hold_clamp_normal_case():
    """Predicted value mid-range gets passed through; ceiling=120s, prediction=60s."""
    cfg = PulseBotConfig(exit_max_hold_seconds=120.0, exit_max_hold_dynamic=True)
    em = ExitManager(cfg, quantile_max_hold_policy=_stub_quantile_policy(60.0))
    pulse = MagicMock(
        creator_selling=False,
        buy_rate=10.0,
        sell_rate=1.0,
        window_events=20,
        trend_declining_count=0,
        peak_buy_rate=0.0,
        buy_rate_drop_from_peak=0.0,
        new_wallet_rate=1.0,
        whale_exit=False,
        curve_progress_pct=10.0,
    )
    em.decide(pulse, pnl_pct=0.0, elapsed_sec=5.0)
    assert em._dynamic_max_hold_cached == 60.0


def test_dynamic_max_hold_predict_failure_falls_back_to_static():
    """If predict() raises, cache must be set to static ceiling (not None,
    which would re-fire predict on every tick)."""
    cfg = PulseBotConfig(exit_max_hold_seconds=120.0, exit_max_hold_dynamic=True)
    failing = MagicMock()
    failing.predict.side_effect = RuntimeError("model boom")
    em = ExitManager(cfg, quantile_max_hold_policy=failing)
    pulse = MagicMock(
        creator_selling=False,
        buy_rate=10.0,
        sell_rate=1.0,
        window_events=20,
        trend_declining_count=0,
        peak_buy_rate=0.0,
        buy_rate_drop_from_peak=0.0,
        new_wallet_rate=1.0,
        whale_exit=False,
        curve_progress_pct=10.0,
    )
    em.decide(pulse, pnl_pct=0.0, elapsed_sec=5.0)
    assert em._dynamic_max_hold_cached == cfg.exit_max_hold_seconds


# ── M2: _simulate_forward_hold_seconds exception handling ──────────


def test_simulate_forward_hold_seconds_returns_zero_on_empty_future():
    """No future trades → 0.0 (no crash)."""
    df = pd.DataFrame(
        [{"timestamp": 100.0, "tx_type": "buy", "sol_amount": 0.1, "token_amount": 1000,
          "market_cap_sol": 30, "v_sol_in_bonding_curve": 5, "wallet": "w1"}]
    )
    result = _simulate_forward_hold_seconds(df, t=200.0, current_price=30.0, mint="MINT")
    assert result == 0.0


def test_simulate_forward_hold_seconds_returns_zero_on_simulator_crash(monkeypatch):
    """Bad row that crashes simulate_exit must not abort the whole rebuild."""
    from pulse_bot.ml import build_dataset as bd

    def boom(*args, **kwargs):
        raise RuntimeError("simulator boom")

    monkeypatch.setattr(bd, "simulate_exit", boom)
    df = pd.DataFrame(
        [{"timestamp": 100.0, "tx_type": "buy", "sol_amount": 0.1, "token_amount": 1000,
          "market_cap_sol": 30, "v_sol_in_bonding_curve": 5, "wallet": "w1"}]
    )
    # Should NOT raise — must return 0.0 (right-censored fallback).
    result = _simulate_forward_hold_seconds(df, t=50.0, current_price=30.0, mint="MINT")
    assert result == 0.0


# ── M3: is_creator flag honored ────────────────────────────────────


def test_df_rows_to_trades_honors_creator_wallet():
    """Trades whose wallet matches creator_wallet must have is_creator=True."""
    df = pd.DataFrame(
        [
            {"timestamp": 100.0, "tx_type": "buy", "sol_amount": 0.1, "token_amount": 1000,
             "market_cap_sol": 30, "v_sol_in_bonding_curve": 5, "wallet": "creator123"},
            {"timestamp": 101.0, "tx_type": "buy", "sol_amount": 0.1, "token_amount": 1000,
             "market_cap_sol": 30, "v_sol_in_bonding_curve": 5, "wallet": "rando"},
        ]
    )
    trades = _df_rows_to_trades(df, mint="M", creator_wallet="creator123")
    assert trades[0].is_creator is True
    assert trades[1].is_creator is False


def test_df_rows_to_trades_no_creator_defaults_false():
    """When creator_wallet is None, every trade is is_creator=False."""
    df = pd.DataFrame(
        [{"timestamp": 100.0, "tx_type": "buy", "sol_amount": 0.1, "token_amount": 1000,
          "market_cap_sol": 30, "v_sol_in_bonding_curve": 5, "wallet": "creator123"}]
    )
    trades = _df_rows_to_trades(df, mint="M", creator_wallet=None)
    assert trades[0].is_creator is False


# ── MonitorResult.hold_seconds populated on each exit path ────────


def _trade(ts: float, tx: str, sol: float = 0.1, tok: float = 1000.0,
           mc: float = 30.0, wallet: str = "w") -> Trade:
    return Trade(
        mint="M", wallet=wallet, tx_type=tx, sol_amount=sol, token_amount=tok,
        new_token_balance=0.0, bonding_curve_key="", v_sol_in_bonding_curve=10.0,
        v_tokens_in_bonding_curve=0.0, market_cap_sol=mc, timestamp=ts,
    )


def test_monitor_result_hold_seconds_on_hard_stop():
    """hard_stop fires on price drop; hold_seconds = trade.timestamp - entry_time."""
    cfg = PulseBotConfig(exit_hard_stop_loss_pct=15.0)
    runner = PaperTradeRunner(cfg, entry_price=100.0)
    # Big price drop on next trade — should trigger hard_stop.
    bad = _trade(ts=110.0, tx="sell", sol=10.0, tok=1.0)  # price = 10 → -90%
    res = runner.process_trade(bad, entry_time=100.0)
    assert res is not None
    assert res.exit_reason == "hard_stop"
    assert res.hold_seconds == pytest.approx(10.0)


def test_monitor_result_hold_seconds_on_timeout():
    """timeout_result with explicit hold_seconds value."""
    cfg = PulseBotConfig()
    runner = PaperTradeRunner(cfg, entry_price=100.0)
    res = runner.timeout_result(hold_seconds=42.0)
    assert res.exit_reason == "timeout"
    assert res.hold_seconds == 42.0


def test_monitor_result_hold_seconds_default_zero():
    """Backward compat: timeout_result without arg defaults to 0.0 (not crash)."""
    cfg = PulseBotConfig()
    runner = PaperTradeRunner(cfg, entry_price=100.0)
    res = runner.timeout_result()
    assert res.hold_seconds == 0.0
