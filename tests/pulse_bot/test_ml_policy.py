# tests/pulse_bot/test_ml_policy.py
"""Unit tests for EntryMLPolicy load, predict, and logging contract."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
import xgboost as xgb

from pulse_bot.ml.features import (
    ENTRY_FEATURE_ORDER,
    FEATURE_SCHEMA_VERSION,
)
from pulse_bot.ml.policy import (
    DEFAULT_ENTRY_MODEL_PATH,
    DEFAULT_ENTRY_THRESHOLD,
    EntryMLPolicy,
    get_active_policy_name,
    load_entry_policy_if_available,
    sha256_file,
)

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")


def _train_toy_model(tmp_path: Path) -> Path:
    """Train a tiny model on synthetic data matching the entry schema."""
    rng = np.random.default_rng(0)
    n = 120
    data = rng.standard_normal((n, len(ENTRY_FEATURE_ORDER)))
    X = pd.DataFrame(data, columns=ENTRY_FEATURE_ORDER)
    # Use surviving features (fast_score + total_score removed 2026-04-22).
    y = (X["unique_buyers"] + X["buy_count"] > 0).astype(int).values
    model = xgb.XGBClassifier(
        n_estimators=15, max_depth=2, random_state=0,
        objective="binary:logistic", eval_metric="auc",
    )
    model.fit(X, y, verbose=False)
    model_path = tmp_path / "entry_model.ubj"
    model.save_model(model_path)
    # Write meta.json that matches feature order
    (model_path.with_suffix(".meta.json")).write_text(json.dumps({
        "features": ENTRY_FEATURE_ORDER,
        "auc": 0.80,
        "base_rate": 0.5,
    }))
    return model_path


def test_sha256_file_is_stable(tmp_path: Path) -> None:
    p = tmp_path / "a.bin"
    p.write_bytes(b"hello")
    h = sha256_file(p)
    assert h == hashlib.sha256(b"hello").hexdigest()
    assert sha256_file(p) == h  # deterministic


def test_policy_loads_and_predicts(tmp_path: Path) -> None:
    mp = _train_toy_model(tmp_path)
    policy = EntryMLPolicy.from_path(mp, threshold=0.5)
    assert len(policy.model_hash) == 64
    assert policy.schema_version == FEATURE_SCHEMA_VERSION
    scoring = {"unique_buyers": 1.0, "buy_count": 1.0}
    p = policy.predict_proba(scoring)
    assert 0.0 <= p <= 1.0
    should_buy, p2 = policy.decide(scoring)
    assert p == p2
    assert isinstance(should_buy, bool)


def test_policy_predicts_higher_for_strong_signal(tmp_path: Path) -> None:
    mp = _train_toy_model(tmp_path)
    policy = EntryMLPolicy.from_path(mp)
    # Model learned y = (fast+total > 0). Strong vs weak should ordinally differ.
    p_strong = policy.predict_proba({"unique_buyers": 5.0, "buy_count": 5.0})
    p_weak = policy.predict_proba({"unique_buyers": -5.0, "buy_count": -5.0})
    assert p_strong > p_weak


def test_policy_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        EntryMLPolicy.from_path(tmp_path / "missing.ubj")


def test_policy_load_with_fallback_returns_none_if_missing(tmp_path: Path) -> None:
    result = load_entry_policy_if_available(tmp_path / "no_such_model.ubj")
    assert result is None


def test_policy_load_with_fallback_returns_policy_if_present(tmp_path: Path) -> None:
    mp = _train_toy_model(tmp_path)
    policy = load_entry_policy_if_available(mp)
    assert policy is not None
    assert isinstance(policy, EntryMLPolicy)


def test_dump_features_json_roundtrips(tmp_path: Path) -> None:
    mp = _train_toy_model(tmp_path)
    policy = EntryMLPolicy.from_path(mp)
    feats_json = policy.dump_features_json(
        {"unique_buyers": 5.0, "buy_count": 3.0},
        holder_snapshot={"top1_30": 20.0, "top5_30": 50.0, "hc_30": 100,
                         "top1_delta": 1.0, "top5_delta": 2.0},
    )
    feats = json.loads(feats_json)
    assert feats["unique_buyers"] == 5.0
    assert feats["top1_30"] == 20.0
    # Every feature in schema must be present
    for name in ENTRY_FEATURE_ORDER:
        assert name in feats


def test_get_active_policy_name_defaults_to_rules(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PULSE_POLICY", raising=False)
    assert get_active_policy_name() == "rules"


def test_get_active_policy_name_reads_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PULSE_POLICY", "ML")
    assert get_active_policy_name() == "ml"
    monkeypatch.setenv("PULSE_POLICY", "hybrid")
    assert get_active_policy_name() == "hybrid"


def test_decide_threshold_respects_threshold(tmp_path: Path) -> None:
    mp = _train_toy_model(tmp_path)
    # Threshold=1.01 → never BUY
    policy_high = EntryMLPolicy.from_path(mp, threshold=1.01)
    should_buy, _ = policy_high.decide({"unique_buyers": 5.0, "buy_count": 5.0})
    assert not should_buy
    # Threshold=0.0 → always BUY
    policy_low = EntryMLPolicy.from_path(mp, threshold=0.0)
    should_buy, _ = policy_low.decide({"unique_buyers": -5.0, "buy_count": -5.0})
    assert should_buy


def test_policy_refuses_stale_schema(tmp_path: Path) -> None:
    """2026-04-23: silently loading a stale-schema model is how the creator
    skew bug masked itself. Refuse to load by default; only allow with
    explicit PULSE_ALLOW_STALE_MODEL=1 override (for forced inspection)."""
    mp = _train_toy_model(tmp_path)
    (mp.with_suffix(".meta.json")).write_text(json.dumps({
        "features": ["completely", "different", "feature", "order"],
        "auc": 0.80,
    }))
    with pytest.raises(RuntimeError, match="does not match current"):
        EntryMLPolicy.from_path(mp)


def test_policy_stale_schema_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog
) -> None:
    """Explicit PULSE_ALLOW_STALE_MODEL=1 lets the model load but logs an
    ERROR — never silent."""
    mp = _train_toy_model(tmp_path)
    (mp.with_suffix(".meta.json")).write_text(json.dumps({
        "features": ["completely", "different", "feature", "order"],
        "auc": 0.80,
    }))
    monkeypatch.setenv("PULSE_ALLOW_STALE_MODEL", "1")
    with caplog.at_level("ERROR"):
        EntryMLPolicy.from_path(mp)
    assert any("STALE-SCHEMA" in rec.message for rec in caplog.records)


def test_decide_with_confidence_three_way(tmp_path: Path) -> None:
    """2026-04-23: confidence-gating returns BUY/SKIP/RULES based on
    val-tuned floor/ceiling (persisted in meta.json)."""
    mp = _train_toy_model(tmp_path)
    # Overwrite meta to inject deterministic thresholds for the test
    meta = json.loads(mp.with_suffix(".meta.json").read_text())
    meta["confidence_thresholds"] = {"floor": 0.30, "ceiling": 0.70}
    meta["calibration"] = {"a": 1.0, "b": 0.0}
    mp.with_suffix(".meta.json").write_text(json.dumps(meta))

    policy = EntryMLPolicy.from_path(mp)
    assert policy.proba_floor == 0.30
    assert policy.proba_ceiling == 0.70

    # Strong positive synthetic input → should land in BUY.
    strong_pos = {c: 5.0 for c in ENTRY_FEATURE_ORDER}
    action, p_raw, p_cal = policy.decide_with_confidence(strong_pos)
    assert action in {"BUY", "RULES"}, (action, p_raw)
    assert 0.0 <= p_cal <= 1.0

    # Strong negative → should land in SKIP.
    strong_neg = {c: -5.0 for c in ENTRY_FEATURE_ORDER}
    action_neg, _, _ = policy.decide_with_confidence(strong_neg)
    assert action_neg in {"SKIP", "RULES"}


def test_decide_with_confidence_fallback_without_meta(tmp_path: Path) -> None:
    """Old models without confidence_thresholds still load — they just
    never fire BUY/SKIP (everything lands in RULES). That's the correct
    graceful-degrade behaviour."""
    mp = _train_toy_model(tmp_path)
    # Default meta has no confidence_thresholds
    policy = EntryMLPolicy.from_path(mp)
    # Floor/ceiling default to ±0.2 around threshold (0.5) — a ~40-pt
    # band that is wide enough to accommodate most tokens but still
    # allows extreme proba to trigger BUY/SKIP.
    assert abs(policy.proba_floor - 0.30) < 1e-9
    assert abs(policy.proba_ceiling - 0.70) < 1e-9
    action, _, _ = policy.decide_with_confidence(
        {c: 0.0 for c in ENTRY_FEATURE_ORDER}
    )
    assert action in {"BUY", "SKIP", "RULES"}


def test_calibration_platt_bounded() -> None:
    """_calibrate must return values in (0, 1) even for extreme inputs."""
    from pulse_bot.ml.policy import EntryMLPolicy
    import numpy as np
    fake = EntryMLPolicy.__new__(EntryMLPolicy)
    fake.calibration = {"a": 100.0, "b": -50.0}  # steep logistic
    assert fake._calibrate(0.0) == pytest.approx(0.0, abs=1e-9)
    assert fake._calibrate(1.0) == pytest.approx(1.0, abs=1e-9)
    assert 0.0 < fake._calibrate(0.5) < 1.0
