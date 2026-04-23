# tests/pulse_bot/test_exit_ml_advisory.py
"""Tests for ExitMLPolicy + ExitManager advisory wiring.

Ensures (a) exit model loads and predicts, (b) ExitManager attaches
ml_exit_proba to ExitSignal when advisor present, (c) ExitManager
without advisor keeps ml_exit_proba=None, (d) advisor NEVER overrides
rule-based decisions (the whole point of advisory mode)."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest
import xgboost as xgb

from pulse_bot.config import PulseBotConfig
from pulse_bot.ml.features import EXIT_FEATURE_ORDER
from pulse_bot.ml.policy import (
    ExitMLPolicy,
    load_exit_policy_if_available,
)
from pulse_bot.pulse.exit_manager import ExitManager, ExitSignal

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")


def _train_toy_exit_model(tmp_path: Path) -> Path:
    rng = np.random.default_rng(0)
    n = 150
    data = rng.standard_normal((n, len(EXIT_FEATURE_ORDER)))
    X = pd.DataFrame(data, columns=EXIT_FEATURE_ORDER)
    # Label depends on drawdown — strong signal so model actually learns
    y = (X["drawdown_from_peak"] > 0).astype(int).values
    m = xgb.XGBClassifier(
        n_estimators=20, max_depth=2, random_state=0,
        objective="binary:logistic", eval_metric="auc",
    )
    m.fit(X, y, verbose=False)
    p = tmp_path / "exit_model.ubj"
    m.save_model(p)
    (p.with_suffix(".meta.json")).write_text(json.dumps({
        "features": EXIT_FEATURE_ORDER, "auc": 0.80, "base_rate": 0.5,
    }))
    return p


def _pulse(
    buy_rate: float = 0.5, sell_rate: float = 0.0,
    new_wallet_rate: float = 0.5, creator_selling: bool = False,
    whale_exit: bool = False, peak_buy_rate: float = 0.5,
    buy_rate_drop_from_peak: float = 1.0, trend_declining_count: int = 0,
    curve_progress_pct: float = 10.0, window_events: int = 20,
    curve_velocity: float = 0.0,
) -> SimpleNamespace:
    return SimpleNamespace(
        buy_rate=buy_rate, sell_rate=sell_rate, new_wallet_rate=new_wallet_rate,
        creator_selling=creator_selling, whale_exit=whale_exit,
        peak_buy_rate=peak_buy_rate,
        buy_rate_drop_from_peak=buy_rate_drop_from_peak,
        trend_declining_count=trend_declining_count,
        curve_progress_pct=curve_progress_pct,
        window_events=window_events,
        curve_velocity=curve_velocity,
    )


def test_exit_policy_loads(tmp_path: Path) -> None:
    p = _train_toy_exit_model(tmp_path)
    pol = ExitMLPolicy.from_path(p)
    assert len(pol.model_hash) == 64
    proba = pol.predict_proba(
        {"hold_seconds": 10, "current_pnl_pct": -5, "peak_pnl_pct": 0,
         "drawdown_from_peak": 5},
        pulse=_pulse(),
    )
    assert 0.0 <= proba <= 1.0


def test_exit_policy_fallback_returns_none(tmp_path: Path) -> None:
    assert load_exit_policy_if_available(tmp_path / "nope.ubj") is None


def test_exit_manager_without_advisor_keeps_none_proba() -> None:
    cfg = PulseBotConfig()
    mgr = ExitManager(cfg)  # no ml_advisor
    signal = mgr.decide(_pulse(), pnl_pct=5.0, elapsed_sec=10.0)
    assert signal.ml_exit_proba is None


def test_exit_manager_with_advisor_attaches_proba(tmp_path: Path) -> None:
    p = _train_toy_exit_model(tmp_path)
    pol = ExitMLPolicy.from_path(p)
    cfg = PulseBotConfig()
    mgr = ExitManager(cfg, ml_advisor=pol)
    signal = mgr.decide(_pulse(), pnl_pct=5.0, elapsed_sec=10.0)
    assert signal.ml_exit_proba is not None
    assert 0.0 <= signal.ml_exit_proba <= 1.0


def test_advisor_never_overrides_hard_rules(tmp_path: Path) -> None:
    """Even if ML proba is high/low, rules determine action."""
    p = _train_toy_exit_model(tmp_path)
    pol = ExitMLPolicy.from_path(p)
    cfg = PulseBotConfig()
    mgr = ExitManager(cfg, ml_advisor=pol)
    # Creator dump triggers hard sell — ML should not change that
    s = mgr.decide(_pulse(creator_selling=True), pnl_pct=0.0, elapsed_sec=10.0)
    assert s.action == "sell_all"
    assert s.reason == "creator_dump"
    # Proba attached regardless
    assert s.ml_exit_proba is not None


def test_advisor_failure_does_not_crash(tmp_path: Path) -> None:
    """Broken advisor should leave ml_exit_proba=None, not throw."""

    class BrokenAdvisor:
        def predict_proba(self, state, pulse):
            raise RuntimeError("simulated failure")

    cfg = PulseBotConfig()
    mgr = ExitManager(cfg, ml_advisor=BrokenAdvisor())
    signal = mgr.decide(_pulse(), pnl_pct=5.0, elapsed_sec=10.0)
    assert signal.ml_exit_proba is None
    assert signal.action in ("hold", "sell_all", "sell_partial")


def test_exit_signal_default_ml_proba_is_none() -> None:
    s = ExitSignal(action="hold", reason="x", sell_pct=0.0)
    assert s.ml_exit_proba is None


# ── Exit ML activation (codex Q4 Phase B, 2026-04-23) ──

class _FakeHighConfAdvisor:
    """ML advisor that always returns a very high sell proba."""

    def predict_proba(self, state, pulse) -> float:
        return 0.95


class _FakeLowConfAdvisor:
    """ML advisor that always returns a low sell proba."""

    def predict_proba(self, state, pulse) -> float:
        return 0.10


def test_ml_escalates_hold_to_sell_when_active_and_confident() -> None:
    cfg = PulseBotConfig(
        exit_ml_active=True,
        exit_ml_sell_threshold=0.80,
        exit_ml_min_hold_seconds=10.0,
    )
    mgr = ExitManager(cfg, ml_advisor=_FakeHighConfAdvisor())
    signal = mgr.decide(_pulse(), pnl_pct=5.0, elapsed_sec=30.0)
    assert signal.action == "sell_all"
    assert signal.reason == "ml_exit_trigger"
    assert signal.ml_exit_proba == 0.95


def test_ml_does_not_escalate_when_inactive() -> None:
    cfg = PulseBotConfig(exit_ml_active=False)
    mgr = ExitManager(cfg, ml_advisor=_FakeHighConfAdvisor())
    signal = mgr.decide(_pulse(), pnl_pct=5.0, elapsed_sec=30.0)
    assert signal.action == "hold"
    assert signal.ml_exit_proba == 0.95


def test_ml_does_not_escalate_below_threshold() -> None:
    cfg = PulseBotConfig(exit_ml_active=True, exit_ml_sell_threshold=0.80)
    mgr = ExitManager(cfg, ml_advisor=_FakeLowConfAdvisor())
    signal = mgr.decide(_pulse(), pnl_pct=5.0, elapsed_sec=30.0)
    assert signal.action == "hold"
    assert signal.ml_exit_proba == 0.10


def test_ml_respects_min_hold_seconds() -> None:
    cfg = PulseBotConfig(
        exit_ml_active=True,
        exit_ml_sell_threshold=0.80,
        exit_ml_min_hold_seconds=60.0,
    )
    mgr = ExitManager(cfg, ml_advisor=_FakeHighConfAdvisor())
    # elapsed < min_hold → no escalation
    signal = mgr.decide(_pulse(), pnl_pct=5.0, elapsed_sec=5.0)
    assert signal.action == "hold"
    # elapsed >= min_hold → escalation
    signal = mgr.decide(_pulse(), pnl_pct=5.0, elapsed_sec=70.0)
    assert signal.action == "sell_all"
    assert signal.reason == "ml_exit_trigger"


def test_ml_never_overrides_hard_rules_even_when_active() -> None:
    cfg = PulseBotConfig(
        exit_ml_active=True,
        exit_ml_sell_threshold=0.80,
        exit_ml_min_hold_seconds=0.0,
    )
    mgr = ExitManager(cfg, ml_advisor=_FakeLowConfAdvisor())
    # Creator dump fires regardless of ML preferring hold
    s = mgr.decide(_pulse(creator_selling=True), pnl_pct=0.0, elapsed_sec=30.0)
    assert s.action == "sell_all"
    assert s.reason == "creator_dump"
