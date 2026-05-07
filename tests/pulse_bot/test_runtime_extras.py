# tests/pulse_bot/test_runtime_extras.py
"""Tests for the smaller runtime-context / feature-flags helpers added
in the architecture cleanup tail of 2026-04-28."""

from __future__ import annotations

import os
from dataclasses import replace

import pytest

from pulse_bot.feature_flags import FeatureFlags
from pulse_bot.runtime_context import ObservationContext


# ───────────────────────── ObservationContext ──────────────────────────


class _FakeToken:
    def __init__(self, mint="MintXyz12345abc", creator="C"):
        self.mint = mint
        self.creator = creator
        self.created_at = 1_000_000.0


def test_observation_context_default_population_is_safe():
    ctx = ObservationContext(token=_FakeToken())
    assert ctx.all_trades == []
    assert ctx.creator_snapshot is None
    assert ctx.holder_snapshot is None
    assert ctx.top_n_wallets == []
    assert ctx.wallet_prior_stats == {}
    assert ctx.checkpoint_verdict is None
    assert ctx.scored_at == 0.0


def test_observation_context_mint_short_truncates():
    ctx = ObservationContext(token=_FakeToken(mint="ABCDEFGHIJ123456"))
    assert ctx.mint_short == "ABCDEFGHIJ12"
    assert len(ctx.mint_short) == 12


def test_observation_context_mutable_population():
    """Pipeline builds the ctx in stages — Intake → Hydration → Decision.
    Each stage mutates relevant fields. Test the contract."""
    ctx = ObservationContext(token=_FakeToken())
    # Intake stage
    ctx.all_trades = [object(), object()]
    ctx.scored_at = 1_000_090.0
    # Hydration stage
    ctx.creator_snapshot = {"creator_age_days": 12.0}
    ctx.top_n_wallets = ["w1", "w2"]
    ctx.n_buyers_first_5s = 3.0
    # Checkpoint stage
    ctx.checkpoint_verdict = "BUY_EARLY"
    ctx.checkpoint_proba = 0.85

    assert len(ctx.all_trades) == 2
    assert ctx.creator_snapshot["creator_age_days"] == 12.0
    assert ctx.top_n_wallets == ["w1", "w2"]
    assert ctx.checkpoint_verdict == "BUY_EARLY"


# ───────────────────────── FeatureFlags ────────────────────────────────


def test_feature_flags_defaults_when_env_unset(monkeypatch):
    """When no PULSE_* env is set, defaults match the documented values."""
    for k in (
        "PULSE_ENTRY_GREY_TO_SKIP", "PULSE_ENTRY_T30_ACTIVE",
        "PULSE_ENTRY_T30_SKIP_ACTIVE", "PULSE_SURVIVAL_ACTIVE",
        "PULSE_TIMING_ACTIVE", "PULSE_ALLOW_DEGENERATE_MODEL",
        "PULSE_T30_SKIP_TAIL", "PULSE_T30_BUY_TAIL",
        "PULSE_TIMING_CONFIDENCE_GATE",
        "PULSE_BOT_CLUSTER_HARD_SKIP", "PULSE_CHECKPOINT_LAG_BUFFER",
        "PULSE_MUX_CREATE_QUEUE_MAX", "PULSE_MUX_TRADE_QUEUE_MAX",
        "PULSE_METRICS_PORT",
    ):
        monkeypatch.delenv(k, raising=False)
    f = FeatureFlags()
    assert f.entry_grey_to_skip is False
    assert f.entry_t30_active is False
    assert f.allow_degenerate_model is False
    assert f.t30_skip_tail == pytest.approx(0.05)
    assert f.t30_buy_tail == pytest.approx(0.85)
    assert f.timing_confidence_gate == pytest.approx(0.85)
    assert f.bot_cluster_hard_skip == 3
    assert f.checkpoint_lag_buffer_sec == pytest.approx(0.5)
    assert f.mux_create_queue_max == 5000
    assert f.mux_trade_queue_max == 200
    assert f.metrics_port == 9100


def test_feature_flags_env_overrides(monkeypatch):
    monkeypatch.setenv("PULSE_ENTRY_GREY_TO_SKIP", "1")
    monkeypatch.setenv("PULSE_ALLOW_DEGENERATE_MODEL", "1")
    monkeypatch.setenv("PULSE_T30_SKIP_TAIL", "0.10")
    monkeypatch.setenv("PULSE_BOT_CLUSTER_HARD_SKIP", "5")
    monkeypatch.setenv("PULSE_METRICS_PORT", "0")
    f = FeatureFlags()
    assert f.entry_grey_to_skip is True
    assert f.allow_degenerate_model is True
    assert f.t30_skip_tail == pytest.approx(0.10)
    assert f.bot_cluster_hard_skip == 5
    assert f.metrics_port == 0


def test_feature_flags_invalid_int_falls_back(monkeypatch, caplog):
    import logging
    caplog.set_level(logging.WARNING)
    monkeypatch.setenv("PULSE_BOT_CLUSTER_HARD_SKIP", "not_a_number")
    f = FeatureFlags()
    assert f.bot_cluster_hard_skip == 3  # default
    assert any("not an int" in rec.getMessage() for rec in caplog.records)


def test_feature_flags_immutable():
    """Frozen dataclass — runtime mutation must raise."""
    f = FeatureFlags()
    with pytest.raises(Exception):
        f.entry_grey_to_skip = True  # type: ignore[misc]


def test_feature_flags_log_summary_emits_one_line_per_field(monkeypatch, caplog):
    import logging
    caplog.set_level(logging.INFO)
    f = FeatureFlags()
    f.log_summary()
    msgs = [rec.getMessage() for rec in caplog.records]
    # Header / footer + one per field
    assert any("FeatureFlags" in m for m in msgs)
    for fname in ("entry_grey_to_skip", "t30_skip_tail", "metrics_port"):
        assert any(fname in m for m in msgs)
