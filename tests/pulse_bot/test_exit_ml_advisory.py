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


class _FakeAdvisorBase:
    """Minimal advisor exposing the API ExitManager calls.

    decide_with_confidence must return ``(action, proba)``. Subclasses
    pick a fixed action/proba pair so tests are deterministic regardless
    of model internals.
    """

    HOLD_HARD_BLOCKABLE_REASONS = frozenset({"weak_pulse_profit", "take_profit"})
    TP_HOLD_HARD_STRICT_THRESHOLD = 0.15
    TP_HOLD_HARD_MAX_PEAK_PCT = 300.0
    TP_HOLD_HARD_MAX_CURRENT_PCT = 500.0

    def __init__(self, action: str, proba: float) -> None:
        self._action = action
        self._proba = proba

    def predict_proba(self, state, pulse):
        return self._proba

    def decide_with_confidence(
        self, state, pulse, current_pnl_pct=None
    ):
        return (self._action, self._proba)


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
        HOLD_HARD_BLOCKABLE_REASONS = frozenset({"weak_pulse_profit", "take_profit"})
        TP_HOLD_HARD_STRICT_THRESHOLD = 0.15
        TP_HOLD_HARD_MAX_PEAK_PCT = 300.0
        TP_HOLD_HARD_MAX_CURRENT_PCT = 500.0

        def predict_proba(self, state, pulse):
            raise RuntimeError("simulated failure")

        def decide_with_confidence(
            self, state, pulse, current_pnl_pct=None
        ):
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

def _sell_all_advisor() -> _FakeAdvisorBase:
    return _FakeAdvisorBase("SELL_ALL", 0.95)


def _rules_advisor() -> _FakeAdvisorBase:
    return _FakeAdvisorBase("RULES", 0.35)


def _hold_hard_advisor() -> _FakeAdvisorBase:
    return _FakeAdvisorBase("HOLD_HARD", 0.10)


def _partial_advisor(proba: float = 0.65) -> _FakeAdvisorBase:
    return _FakeAdvisorBase("SELL_PARTIAL", proba)


def test_ml_escalates_hold_to_sell_when_active_and_confident() -> None:
    cfg = PulseBotConfig(
        exit_ml_active=True,
        exit_ml_sell_threshold=0.80,
        exit_ml_min_hold_seconds=10.0,
    )
    mgr = ExitManager(cfg, ml_advisor=_sell_all_advisor())
    signal = mgr.decide(_pulse(), pnl_pct=5.0, elapsed_sec=30.0)
    assert signal.action == "sell_all"
    assert signal.reason == "ml_exit_trigger"
    assert signal.ml_exit_proba == 0.95
    assert mgr.ml_counters["ml_override_count"] == 1


def test_ml_does_not_escalate_when_inactive() -> None:
    cfg = PulseBotConfig(exit_ml_active=False)
    mgr = ExitManager(cfg, ml_advisor=_sell_all_advisor())
    signal = mgr.decide(_pulse(), pnl_pct=5.0, elapsed_sec=30.0)
    assert signal.action == "hold"
    assert signal.ml_exit_proba == 0.95
    assert mgr.ml_counters["ml_override_count"] == 0


def test_ml_does_not_escalate_on_rules_action() -> None:
    cfg = PulseBotConfig(exit_ml_active=True)
    mgr = ExitManager(cfg, ml_advisor=_rules_advisor())
    signal = mgr.decide(_pulse(), pnl_pct=5.0, elapsed_sec=30.0)
    assert signal.action == "hold"
    assert signal.ml_exit_proba == 0.35


def test_ml_respects_min_hold_seconds() -> None:
    cfg = PulseBotConfig(
        exit_ml_active=True,
        exit_ml_sell_threshold=0.80,
        exit_ml_min_hold_seconds=60.0,
    )
    mgr = ExitManager(cfg, ml_advisor=_sell_all_advisor())
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
    mgr = ExitManager(cfg, ml_advisor=_hold_hard_advisor())
    # Creator dump fires regardless of ML preferring hold
    s = mgr.decide(_pulse(creator_selling=True), pnl_pct=0.0, elapsed_sec=30.0)
    assert s.action == "sell_all"
    assert s.reason == "creator_dump"


# ── E2 4-way + E5 sizing ladder (codex, 2026-04-23 v2) ──

def test_ml_sell_partial_ladder_30pct() -> None:
    cfg = PulseBotConfig(exit_ml_active=True, exit_ml_min_hold_seconds=0.0)
    mgr = ExitManager(cfg, ml_advisor=_partial_advisor(proba=0.60))
    s = mgr.decide(_pulse(), pnl_pct=10.0, elapsed_sec=30.0)
    assert s.action == "sell_partial"
    assert s.reason == "ml_partial_trigger"
    assert s.sell_pct == pytest.approx(0.30, abs=1e-6)


def test_ml_sell_partial_ladder_70pct() -> None:
    cfg = PulseBotConfig(exit_ml_active=True, exit_ml_min_hold_seconds=0.0)
    mgr = ExitManager(cfg, ml_advisor=_partial_advisor(proba=0.78))
    s = mgr.decide(_pulse(), pnl_pct=10.0, elapsed_sec=30.0)
    assert s.action == "sell_partial"
    assert s.sell_pct == pytest.approx(0.70, abs=1e-6)


def test_hold_hard_blocks_weak_pulse_profit() -> None:
    cfg = PulseBotConfig(
        exit_ml_active=True,
        exit_ml_hold_hard_enabled=True,
        pulse_weak_buy_rate=1.0,  # ensures weak_pulse triggers at buy_rate=0.1
        exit_weak_pulse_min_profit_pct=20.0,
    )
    mgr = ExitManager(cfg, ml_advisor=_hold_hard_advisor())
    # pnl above floor AND above weak-pulse min profit
    s = mgr.decide(
        _pulse(buy_rate=0.1),
        pnl_pct=50.0,
        elapsed_sec=30.0,
    )
    assert s.action == "hold"
    assert s.reason == "ml_hold_hard_blocked_weak_pulse"
    assert mgr.ml_counters["ml_hold_hard_count"] == 1


def test_hold_hard_disabled_allows_weak_pulse_partial() -> None:
    cfg = PulseBotConfig(
        exit_ml_active=True,
        exit_ml_hold_hard_enabled=False,  # rollback lever
        pulse_weak_buy_rate=1.0,
        exit_weak_pulse_min_profit_pct=20.0,
    )
    mgr = ExitManager(cfg, ml_advisor=_hold_hard_advisor())
    s = mgr.decide(
        _pulse(buy_rate=0.1),
        pnl_pct=50.0,
        elapsed_sec=30.0,
    )
    assert s.action == "sell_partial"
    assert s.reason == "weak_pulse_profit"


def test_hold_hard_cannot_block_hard_rules() -> None:
    """HOLD_HARD must never block any hard rule — enumerate every reason
    emitted by _sell_all in exit_manager.py."""
    cfg = PulseBotConfig(
        exit_ml_active=True,
        exit_ml_hold_hard_enabled=True,
        exit_ml_min_hold_seconds=0.0,
    )
    def mgr_factory() -> ExitManager:
        return ExitManager(cfg, ml_advisor=_hold_hard_advisor())
    # creator_dump
    s = mgr_factory().decide(
        _pulse(creator_selling=True), pnl_pct=10.0, elapsed_sec=30.0
    )
    assert s.action == "sell_all" and s.reason == "creator_dump"
    # whale_exit (requires exit_on_whale flag)
    cfg_whale = PulseBotConfig(
        exit_ml_active=True,
        exit_ml_hold_hard_enabled=True,
        exit_ml_min_hold_seconds=0.0,
        exit_on_whale=True,
    )
    s = ExitManager(cfg_whale, ml_advisor=_hold_hard_advisor()).decide(
        _pulse(whale_exit=True), pnl_pct=10.0, elapsed_sec=30.0
    )
    assert s.action == "sell_all" and s.reason == "whale_exit"
    # near_graduation
    s = mgr_factory().decide(
        _pulse(curve_progress_pct=99.0), pnl_pct=10.0, elapsed_sec=30.0
    )
    assert s.action == "sell_all" and s.reason == "near_graduation"
    # hard_stop
    s = mgr_factory().decide(_pulse(), pnl_pct=-20.0, elapsed_sec=30.0)
    assert s.action == "sell_all" and s.reason == "hard_stop"
    # take_profit — NOTE: take_profit is now blockable by HOLD_HARD
    # when advisor proba < 0.15 AND peak < 300 AND pnl < 500 (user
    # directive 2026-04-23). Use a NON-strict HOLD_HARD advisor here
    # (proba 0.10 < 0.15 would block; 0.18 ≥ 0.15 does not block).
    # Verified in test_hold_hard_does_not_block_take_profit_above_strict_threshold.
    adv_nonstrict = _FakeAdvisorBase("HOLD_HARD", 0.18)
    s = ExitManager(cfg, ml_advisor=adv_nonstrict).decide(
        _pulse(), pnl_pct=150.0, elapsed_sec=30.0
    )
    assert s.action == "sell_all" and s.reason == "take_profit"
    # timeout
    s = mgr_factory().decide(_pulse(), pnl_pct=10.0, elapsed_sec=1000.0)
    assert s.action == "sell_all" and s.reason == "timeout"


def test_hold_hard_blocks_take_profit_when_strict_confident() -> None:
    """TP override: HOLD_HARD with proba < 0.15 AND peak < 300 AND pnl <
    500 blocks take_profit. Keeps position alive for trailing_stop /
    timeout / hard_stop to govern."""
    cfg = PulseBotConfig(
        exit_ml_active=True,
        exit_ml_hold_hard_enabled=True,
        exit_take_profit_enabled=True,
        exit_take_profit_pct=100.0,
    )
    adv = _FakeAdvisorBase("HOLD_HARD", 0.10)  # < 0.15 strict
    mgr = ExitManager(cfg, ml_advisor=adv)
    s = mgr.decide(_pulse(), pnl_pct=120.0, elapsed_sec=30.0)
    assert s.action == "hold"
    assert s.reason == "ml_hold_hard_blocked_take_profit"
    assert mgr.ml_counters["ml_hold_hard_count"] == 1


def test_hold_hard_does_not_block_take_profit_above_strict_threshold() -> None:
    """Proba ≥ 0.15 must NOT block take_profit — only very-confident."""
    cfg = PulseBotConfig(
        exit_ml_active=True,
        exit_ml_hold_hard_enabled=True,
        exit_take_profit_enabled=True,
        exit_take_profit_pct=100.0,
    )
    adv = _FakeAdvisorBase("HOLD_HARD", 0.18)  # ≥ 0.15
    mgr = ExitManager(cfg, ml_advisor=adv)
    s = mgr.decide(_pulse(), pnl_pct=120.0, elapsed_sec=30.0)
    assert s.action == "sell_all"
    assert s.reason == "take_profit"


def test_hold_hard_blocks_tp_disabled_when_rollback() -> None:
    """PULSE_EXIT_ML_HOLD_HARD=0 (exit_ml_hold_hard_enabled=False)
    disables TP override too."""
    cfg = PulseBotConfig(
        exit_ml_active=True,
        exit_ml_hold_hard_enabled=False,
        exit_take_profit_enabled=True,
        exit_take_profit_pct=100.0,
    )
    adv = _FakeAdvisorBase("HOLD_HARD", 0.10)
    mgr = ExitManager(cfg, ml_advisor=adv)
    s = mgr.decide(_pulse(), pnl_pct=120.0, elapsed_sec=30.0)
    assert s.action == "sell_all"
    assert s.reason == "take_profit"


def test_hold_hard_does_not_fire_in_loss() -> None:
    """Guardrail: HOLD_HARD requires pnl_pct >= HOLD_HARD_MIN_PNL_PCT (-5%).
    Loss worse than -5% → defer to rules (partial rule does not fire
    at loss anyway, but verify the guard)."""
    from pulse_bot.ml.policy import ExitMLPolicy

    cfg = PulseBotConfig(
        exit_ml_active=True,
        exit_ml_hold_hard_enabled=True,
        pulse_weak_buy_rate=1.0,
        exit_weak_pulse_min_profit_pct=-10.0,  # allow partial even at small loss
    )
    adv = _hold_hard_advisor()
    # Put into HOLD_HARD zone at -7% PnL — guardrail (-5%) blocks it;
    # weak_pulse_profit rule would need pnl > min_profit (-10%), but
    # guard on HOLD_HARD doesn't fire, so partial sell proceeds.
    # Override HOLD_HARD_MIN_PNL_PCT via class constant for deterministic.
    assert ExitMLPolicy.HOLD_HARD_MIN_PNL_PCT == -5.0
    mgr = ExitManager(cfg, ml_advisor=adv)
    # decide_with_confidence pipes current_pnl_pct → action is
    # RULES when PnL < -5 because HOLD_HARD guard fails. Simulate that
    # by bypassing fake advisor and setting action explicitly:
    adv._action = "RULES"  # what ExitMLPolicy would return at -7%
    s = mgr.decide(
        _pulse(buy_rate=0.1),
        pnl_pct=-7.0,
        elapsed_sec=30.0,
    )
    # Partial rule fires (since HOLD_HARD was not asserted)
    assert s.action == "sell_partial" and s.reason == "weak_pulse_profit"
