# tests/pulse_bot/test_model_skill_gate.py
"""Universal model skill-gate (task #95).

Principle: a model that has not demonstrated skill must not influence
live buy/sell decisions. ``assess_skill`` is the single judge; every
model loader consults it. These tests pin the per-model-type rules and
the end-to-end consequence for the reg-floor gate.
"""

from __future__ import annotations

import json
import types

import pytest

from pulse_bot.ml.model_registry import ModelRegistry, assess_skill
from pulse_bot.decision_service import DecisionService, EntryDecision


# ───────────────────────── assess_skill rules ──────────────────────────


def test_model_health_block_is_authoritative_ok():
    skilled, status, _ = assess_skill({"auc": 0.93, "model_health": {"status": "ok"}})
    assert skilled is True
    assert status == "ok"


def test_model_health_block_is_authoritative_degenerate():
    skilled, status, _ = assess_skill(
        {"auc": 0.93, "model_health": {"status": "degenerate", "notes": ["x"]}}
    )
    assert skilled is False
    assert status == "degenerate"


def test_regression_head_no_skill_is_disabled():
    """The live entry_model_reg case: rho≈0.06, auc_sign≈0.37."""
    skilled, status, reason = assess_skill(
        {"objective": "reg:squarederror", "spearman_rho": 0.0597, "auc_sign": 0.3725}
    )
    assert skilled is False
    assert status == "degenerate"
    assert "auc_sign" in reason


def test_regression_head_with_skill_is_enabled():
    skilled, status, _ = assess_skill(
        {"objective": "reg:squarederror", "spearman_rho": 0.42, "auc_sign": 0.61}
    )
    assert skilled is True
    assert status == "ok"


def test_regression_head_worse_than_coinflip_sign_is_disabled():
    """rho is fine but auc_sign <= 0.50 → still disabled."""
    skilled, _, _ = assess_skill(
        {"auc_sign": 0.50, "spearman_rho": 0.30}
    )
    assert skilled is False


def test_quantile_calibrated_is_enabled():
    skilled, status, _ = assess_skill(
        {"objective": "reg:quantileerror", "quantile": 0.25, "coverage": 0.31}
    )
    assert skilled is True
    assert status == "ok"


def test_quantile_miscalibrated_is_disabled():
    skilled, status, _ = assess_skill(
        {"objective": "reg:quantileerror", "quantile": 0.75, "coverage": 0.20}
    )
    assert skilled is False
    assert status == "degenerate"


def test_plain_classifier_high_auc_is_enabled():
    skilled, _, _ = assess_skill({"auc": 0.9126})
    assert skilled is True


def test_plain_classifier_low_auc_is_disabled():
    skilled, _, _ = assess_skill({"auc": 0.52})
    assert skilled is False


def test_no_skill_metric_is_unmeasured_but_usable():
    """timing/survival today: only schema_version. Not abruptly disabled."""
    skilled, status, _ = assess_skill({"schema_version": "survival_v1"})
    assert skilled is True
    assert status == "unmeasured"


# ──────────────────── EV advisory (entry classifiers) ──────────────────
# The EV check is ADVISORY, not a gate: ``ceiling_ev`` comes from
# simulate_exit over training labels and can be wrong (simulator bugs,
# train/serve skew, stale exit config). A non-positive value downgrades
# status to ``ev_warning`` for visibility but never sets skilled=False —
# only live realized PnL is ground truth for "does this model earn".


def test_ev_advisory_warns_but_keeps_classifier_skilled_with_model_health():
    """The live entry_model case: AUC 0.93, model_health.status='ok', and
    a non-positive training-label ceiling_ev=-1.38%. The model STAYS
    skilled — the EV metric is advisory — but status becomes ev_warning
    and the reason carries the advisory text."""
    skilled, status, reason = assess_skill(
        {
            "auc": 0.9329,
            "model_health": {"status": "ok"},
            "confidence_thresholds": {
                "objective": "ev",
                "ceiling_ev": -1.384,
                "val_base_ev": -0.405,
            },
        }
    )
    assert skilled is True
    assert status == "ev_warning"
    assert "ceiling_ev" in reason
    assert "ADVISORY" in reason


def test_ev_advisory_warns_on_classifier_without_model_health_block():
    """The live entry_t30 case: no model_health block, AUC 0.91 passes
    the plain-classifier branch — and a negative ceiling_ev downgrades
    status to ev_warning without disabling it."""
    skilled, status, _ = assess_skill(
        {
            "auc": 0.9126,
            "confidence_thresholds": {
                "objective": "ev",
                "ceiling_ev": -1.194,
                "val_base_ev": -0.405,
            },
        }
    )
    assert skilled is True
    assert status == "ev_warning"


def test_ev_advisory_silent_when_ceiling_ev_positive():
    """A classifier whose most-confident bucket is net-positive gets no
    advisory — status stays ok."""
    skilled, status, _ = assess_skill(
        {
            "auc": 0.80,
            "model_health": {"status": "ok"},
            "confidence_thresholds": {
                "objective": "ev",
                "ceiling_ev": 3.2,
                "val_base_ev": -0.40,
            },
        }
    )
    assert skilled is True
    assert status == "ok"


def test_ev_advisory_does_not_rescue_a_genuinely_unskilled_model():
    """A model that fails the real skill check (low AUC) stays disabled —
    the EV advisory only annotates, it never flips skilled True→False or
    False→True. Status stays degenerate, not ev_warning."""
    skilled, status, _ = assess_skill(
        {
            "auc": 0.52,
            "confidence_thresholds": {
                "objective": "ev",
                "ceiling_ev": -1.0,
                "val_base_ev": -0.40,
            },
        }
    )
    assert skilled is False
    assert status == "degenerate"


def test_ev_advisory_zero_ceiling_ev_warns():
    """ceiling_ev must be STRICTLY positive to be silent — exactly
    break-even (0.0) still triggers the advisory (but not a disable)."""
    skilled, status, _ = assess_skill(
        {
            "auc": 0.90,
            "confidence_thresholds": {
                "objective": "ev",
                "ceiling_ev": 0.0,
                "val_base_ev": -0.40,
            },
        }
    )
    assert skilled is True
    assert status == "ev_warning"


def test_ev_advisory_inert_without_confidence_thresholds():
    """A classifier carrying no confidence_thresholds block (older models
    or WR-objective search) gets no advisory — status stays ok."""
    skilled, status, reason = assess_skill({"auc": 0.9126})
    assert skilled is True
    assert status == "ok"
    assert "auc" in reason


def test_ev_advisory_inert_for_wr_objective_thresholds():
    """WR-objective threshold search has no ceiling_ev — the advisory
    must not fire (only EV-objective blocks carry a money metric)."""
    skilled, status, _ = assess_skill(
        {"auc": 0.80, "confidence_thresholds": {"objective": "wr", "status": "ok"}}
    )
    assert skilled is True
    assert status == "ok"


# ───────────────────────── ModelRegistry wiring ────────────────────────


def test_registry_spec_healthy_uses_assess_skill(tmp_path):
    (tmp_path / "entry_model_reg.ubj").write_bytes(b"stub")
    (tmp_path / "entry_model_reg.meta.json").write_text(
        json.dumps({"objective": "reg:squarederror",
                    "spearman_rho": 0.06, "auc_sign": 0.37})
    )
    reg = ModelRegistry(data_dir=tmp_path)
    spec = reg.get("entry_reg")
    assert spec.exists is True
    assert spec.healthy is False
    assert spec.status == "degenerate"
    assert "auc_sign" in spec.skill_reason


def test_registry_spec_missing_is_not_healthy(tmp_path):
    spec = ModelRegistry(data_dir=tmp_path).get("entry_reg")
    assert spec.exists is False
    assert spec.healthy is False
    assert spec.status == "missing"


# ───────────── end-to-end: reg_pnl_pct=None bypasses reg-floor ──────────


def _decision_skip() -> EntryDecision:
    return EntryDecision(
        should_enter=False, entry_type="rules", entry_score=0, entry_buyer_num=0
    )


def _result(buy_count: int = 7):
    return types.SimpleNamespace(buy_count=buy_count)


def test_disabled_reg_model_means_no_reg_floor_block():
    """When a reg model fails the skill gate, pipeline passes
    reg_pnl_pct=None — apply_ml_override must then do a plain BUY
    override with NO reg-floor evaluation, even if a floor is set."""
    svc = DecisionService(db=None, hard_skip_n_env=0, reg_floor_pct=0.0)
    out = svc.apply_ml_override(
        _decision_skip(), ml_action="BUY", ml_proba=0.30, ml_cal=0.02,
        result=_result(), mint_short="MINT", reg_pnl_pct=None,
    )
    assert out.should_enter is True
    assert out.entry_type == "ml_override"
    assert svc.ml_overrides_buy == 1
    assert svc.ml_overrides_skip == 0


def test_enabled_reg_model_still_applies_reg_floor_block():
    """Control: when reg_pnl_pct IS supplied and below the floor, the
    block still fires — the gate is bypassed only by None, not weakened."""
    svc = DecisionService(db=None, hard_skip_n_env=0, reg_floor_pct=0.0)
    out = svc.apply_ml_override(
        _decision_skip(), ml_action="BUY", ml_proba=0.30, ml_cal=0.02,
        result=_result(), mint_short="MINT", reg_pnl_pct=-1.5,
    )
    assert out.should_enter is False
    assert svc.ml_overrides_skip == 1


# ───────────── p_cal floor gate (multi-config A/B knob) ────────────────


def test_p_cal_floor_blocks_buy_below_floor():
    """ml_override BUY is blocked when calibrated proba < p_cal_floor."""
    svc = DecisionService(db=None, hard_skip_n_env=0, p_cal_floor=0.02)
    out = svc.apply_ml_override(
        _decision_skip(), ml_action="BUY", ml_proba=0.20, ml_cal=0.009,
        result=_result(), mint_short="MINT", reg_pnl_pct=None,
    )
    assert out.should_enter is False
    assert svc.ml_overrides_skip == 1
    assert svc.ml_overrides_buy == 0


def test_p_cal_floor_allows_buy_at_or_above_floor():
    svc = DecisionService(db=None, hard_skip_n_env=0, p_cal_floor=0.02)
    out = svc.apply_ml_override(
        _decision_skip(), ml_action="BUY", ml_proba=0.55, ml_cal=0.05,
        result=_result(), mint_short="MINT", reg_pnl_pct=None,
    )
    assert out.should_enter is True
    assert out.entry_type == "ml_override"
    assert svc.ml_overrides_buy == 1


def test_p_cal_floor_zero_is_no_gate():
    """Default p_cal_floor=0.0 (LIVE config) lets any BUY through."""
    svc = DecisionService(db=None, hard_skip_n_env=0)  # p_cal_floor defaults 0.0
    out = svc.apply_ml_override(
        _decision_skip(), ml_action="BUY", ml_proba=0.16, ml_cal=0.001,
        result=_result(), mint_short="MINT", reg_pnl_pct=None,
    )
    assert out.should_enter is True
    assert svc.ml_overrides_buy == 1


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
