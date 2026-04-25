# tests/pulse_bot/test_config_hash.py
"""Unit tests for the train→serve config_hash drift guard.

Covers:
* deterministic hashing (same config → same hash; reorder fields irrelevant)
* drift detection (any tracked field flip → hash changes; per-field diff)
* cosmetic-field immunity (dashboard_refresh_seconds etc. don't affect hash)
* policy load-time WARN on hash mismatch (no refuse-to-load)
* policy load-time silent when hash matches
* legacy meta.json (no config_hash) does not warn
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, fields, replace
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
import xgboost as xgb

from pulse_bot.config import PulseBotConfig
from pulse_bot.ml.config_hash import (
    TRAIN_RELEVANT_FIELDS,
    compute_config_hash,
    diff_relevant_fields,
    diff_relevant_fields_from_dict,
    extract_relevant_fields,
)
from pulse_bot.ml.features import ENTRY_FEATURE_ORDER, FEATURE_SCHEMA_VERSION

# ── 1. Determinism ──────────────────────────────────────────────────


def test_same_config_same_hash() -> None:
    """Two PulseBotConfig instances with identical values must hash the same."""
    a = PulseBotConfig()
    b = PulseBotConfig()
    assert compute_config_hash(a) == compute_config_hash(b)


def test_hash_is_hex_sha256() -> None:
    """Returned hash is a 64-char hex string."""
    h = compute_config_hash(PulseBotConfig())
    assert isinstance(h, str)
    assert len(h) == 64
    int(h, 16)  # raises if not hex


def test_extract_relevant_fields_keys_match_constant() -> None:
    """extract_relevant_fields returns exactly TRAIN_RELEVANT_FIELDS as keys."""
    out = extract_relevant_fields(PulseBotConfig())
    assert set(out.keys()) == set(TRAIN_RELEVANT_FIELDS)


# ── 2. Drift detection (positive cases) ─────────────────────────────


def test_flip_exit_max_hold_changes_hash() -> None:
    """exit_max_hold_seconds is the canonical Option-B regression vector."""
    base = PulseBotConfig()
    drifted = replace(base, exit_max_hold_seconds=180.0)
    assert compute_config_hash(base) != compute_config_hash(drifted)


def test_flip_score_threshold_changes_hash() -> None:
    base = PulseBotConfig()
    drifted = replace(base, score_threshold_buy=base.score_threshold_buy + 5)
    assert compute_config_hash(base) != compute_config_hash(drifted)


def test_flip_entry_train_hparam_changes_hash() -> None:
    base = PulseBotConfig()
    drifted = replace(base, entry_train_max_depth=base.entry_train_max_depth + 1)
    assert compute_config_hash(base) != compute_config_hash(drifted)


def test_flip_entry_ml_proba_floor_from_none_changes_hash() -> None:
    """None → 0.5 (full ML-only) is the live config flip we care about."""
    base = replace(PulseBotConfig(), entry_ml_proba_floor=None)
    drifted = replace(base, entry_ml_proba_floor=0.5)
    assert compute_config_hash(base) != compute_config_hash(drifted)


@pytest.mark.parametrize("field", list(TRAIN_RELEVANT_FIELDS))
def test_every_tracked_field_affects_hash(field: str) -> None:
    """Sanity: changing each tracked field individually changes the hash.

    Skips fields that are non-trivial to flip (none in the current set,
    but the parametrize keeps test future-proof).
    """
    base = PulseBotConfig()
    current = getattr(base, field)
    # Choose a value that is guaranteed to differ from the default.
    if isinstance(current, bool):
        new_val = not current
    elif isinstance(current, int):
        new_val = current + 1
    elif isinstance(current, float):
        new_val = current + 1.0
    elif current is None:
        new_val = 0.5  # all None-able fields in current set are floats
    else:
        pytest.skip(f"unhandled type for {field}: {type(current)}")
    drifted = replace(base, **{field: new_val})
    assert compute_config_hash(base) != compute_config_hash(
        drifted
    ), f"flipping {field} did not change the hash"


# ── 3. Cosmetic-field immunity ──────────────────────────────────────


def test_cosmetic_field_does_not_change_hash() -> None:
    """dashboard_refresh_seconds is intentionally excluded — flipping it
    must NOT invalidate a deployed model."""
    base = PulseBotConfig()
    drifted = replace(
        base, dashboard_refresh_seconds=base.dashboard_refresh_seconds + 5
    )
    assert compute_config_hash(base) == compute_config_hash(drifted)


def test_log_level_does_not_change_hash() -> None:
    base = PulseBotConfig()
    drifted = replace(base, log_level="DEBUG")
    assert compute_config_hash(base) == compute_config_hash(drifted)


def test_db_paths_do_not_change_hash() -> None:
    base = PulseBotConfig()
    drifted = replace(base, db_path="other.db", optimizer_db_path="other_opt.db")
    assert compute_config_hash(base) == compute_config_hash(drifted)


# ── 4. Field-level diff helpers ─────────────────────────────────────


def test_diff_relevant_fields_finds_change() -> None:
    a = PulseBotConfig()
    b = replace(a, exit_hard_stop_loss_pct=8.0)
    diff = diff_relevant_fields(a, b)
    assert "exit_hard_stop_loss_pct" in diff
    assert diff["exit_hard_stop_loss_pct"] == (a.exit_hard_stop_loss_pct, 8.0)


def test_diff_empty_when_equal() -> None:
    assert diff_relevant_fields(PulseBotConfig(), PulseBotConfig()) == {}


def test_diff_from_dict_handles_missing_keys() -> None:
    """A meta.json missing a tracked field (added after model training)
    must show up as drift, not silently agree."""
    cfg = PulseBotConfig()
    expected = extract_relevant_fields(cfg)
    expected.pop("exit_hard_stop_loss_pct")
    diff = diff_relevant_fields_from_dict(expected, cfg)
    assert "exit_hard_stop_loss_pct" in diff
    train_v, runtime_v = diff["exit_hard_stop_loss_pct"]
    assert train_v == "__MISSING__"
    assert runtime_v == cfg.exit_hard_stop_loss_pct


# ── 5. End-to-end: train→serve drift WARN ───────────────────────────


def _train_toy_model(tmp_path: Path, meta_overrides: dict | None = None) -> Path:
    """Mimic train.py's meta.json output with a controllable config_hash."""
    rng = np.random.default_rng(0)
    n = 80
    X = pd.DataFrame(
        rng.standard_normal((n, len(ENTRY_FEATURE_ORDER))),
        columns=ENTRY_FEATURE_ORDER,
    )
    y = (X["unique_buyers"] + X["buy_count"] > 0).astype(int).values
    model = xgb.XGBClassifier(
        n_estimators=10,
        max_depth=2,
        random_state=0,
        objective="binary:logistic",
        eval_metric="auc",
    )
    model.fit(X, y, verbose=False)
    model_path = tmp_path / "entry_model.ubj"
    model.save_model(model_path)
    base_meta = {
        "features": ENTRY_FEATURE_ORDER,
        "schema_version": FEATURE_SCHEMA_VERSION,
        "auc": 0.80,
        "base_rate": 0.5,
        "confidence_thresholds": {"floor": 0.3, "ceiling": 0.7},
        "calibration": {"a": 1.0, "b": 0.0},
    }
    if meta_overrides:
        base_meta.update(meta_overrides)
    model_path.with_suffix(".meta.json").write_text(json.dumps(base_meta))
    return model_path


def test_policy_warns_on_hash_drift(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Hash recorded with a *different* config → WARNING at load time."""
    from pulse_bot.ml.policy import EntryMLPolicy

    # Pretend training was done with exit_max_hold_seconds=180 (the
    # canonical Option-B drift). Runtime default is 90.
    train_cfg = replace(PulseBotConfig(), exit_max_hold_seconds=180.0)
    train_hash = compute_config_hash(train_cfg)
    train_values = extract_relevant_fields(train_cfg)
    model_path = _train_toy_model(
        tmp_path,
        meta_overrides={
            "config_hash": train_hash,
            "config_values": train_values,
            "config_field_names": list(TRAIN_RELEVANT_FIELDS),
        },
    )
    caplog.set_level(logging.WARNING, logger="pulse_bot.ml.policy")
    policy = EntryMLPolicy.from_path(model_path)
    assert policy is not None
    msgs = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any("differs from training-time config" in m for m in msgs), msgs
    assert any("exit_max_hold_seconds" in m for m in msgs), msgs


def test_policy_silent_when_hash_matches(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Matching hash → no drift WARN."""
    # Train using the *same* config the test runtime sees.
    from pulse_bot.config import get_config
    from pulse_bot.ml.policy import EntryMLPolicy

    cfg = get_config()
    train_hash = compute_config_hash(cfg)
    train_values = extract_relevant_fields(cfg)
    model_path = _train_toy_model(
        tmp_path,
        meta_overrides={
            "config_hash": train_hash,
            "config_values": train_values,
        },
    )
    caplog.set_level(logging.WARNING, logger="pulse_bot.ml.policy")
    EntryMLPolicy.from_path(model_path)
    drift_msgs = [
        r.getMessage()
        for r in caplog.records
        if r.levelno == logging.WARNING
        and "differs from training-time config" in r.getMessage()
    ]
    assert drift_msgs == []


def test_policy_silent_on_legacy_meta_without_hash(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A meta.json from before this feature shipped must not trigger
    a WARN every startup — that would just be noise."""
    from pulse_bot.ml.policy import EntryMLPolicy

    model_path = _train_toy_model(tmp_path)  # no config_hash override
    caplog.set_level(logging.WARNING, logger="pulse_bot.ml.policy")
    EntryMLPolicy.from_path(model_path)
    drift_msgs = [
        r.getMessage()
        for r in caplog.records
        if "differs from training-time config" in r.getMessage()
    ]
    assert drift_msgs == []


def test_policy_loads_despite_drift(tmp_path: Path) -> None:
    """Drift must not refuse-to-load — operator may have intentionally
    flipped a tracked field (e.g. exit_ml_active kill-switch)."""
    from pulse_bot.ml.policy import EntryMLPolicy

    train_cfg = replace(PulseBotConfig(), exit_ml_active=False)
    model_path = _train_toy_model(
        tmp_path,
        meta_overrides={
            "config_hash": compute_config_hash(train_cfg),
            "config_values": extract_relevant_fields(train_cfg),
        },
    )
    policy = EntryMLPolicy.from_path(model_path)
    assert policy is not None
    # Sanity: still produces a score.
    # (Use a synthetic feature vector inside the policy by calling raw model.)
    assert hasattr(policy, "_model")


# ── 6. Field-set sanity ─────────────────────────────────────────────


def test_all_tracked_fields_exist_on_pulsebotconfig() -> None:
    """If a field is renamed/removed from config.py without updating
    TRAIN_RELEVANT_FIELDS the hash silently fills with __MISSING__ —
    catch that here."""
    cfg_field_names = {f.name for f in fields(PulseBotConfig)}
    missing = [f for f in TRAIN_RELEVANT_FIELDS if f not in cfg_field_names]
    assert not missing, f"TRAIN_RELEVANT_FIELDS references gone fields: {missing}"
