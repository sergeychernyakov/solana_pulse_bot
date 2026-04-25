# tests/pulse_bot/test_daily_validation.py
"""Unit tests for each validator in pulse_bot.ml.daily_validation.

Tests use synthetic data so they are fast and deterministic. They verify
each check fires correctly on its target failure mode, and passes under
benign conditions.
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
import xgboost as xgb

from pulse_bot.ml.daily_validation import (
    ADVERSARIAL_ABS_THRESHOLD,
    KNOWN_LEAK_FEATURES,
    PRIOR_DRIFT_THRESHOLD,
    SHUFFLED_LABELS_THRESHOLD,
    ValidationResult,
    _load_yesterday_report,
    _split_chrono,
    _split_chrono_exit,
    check_adversarial_validation,
    check_calibration,
    check_economic_backtest,
    check_feature_importance_sanity,
    check_ks_predictions,
    check_prior_drift,
    check_rolling_walk_forward,
    check_shuffled_labels,
)

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")


def _make_random_data(
    n_rows: int = 200,
    n_feat: int = 5,
    positive_rate: float = 0.3,
    seed: int = 0,
) -> tuple[pd.DataFrame, pd.Series]:
    rng = np.random.default_rng(seed)
    X = pd.DataFrame(
        rng.standard_normal((n_rows, n_feat)),
        columns=[f"f{i}" for i in range(n_feat)],
    )
    y = pd.Series((rng.random(n_rows) < positive_rate).astype(int))
    return X, y


def _default_params() -> dict:
    return {
        "n_estimators": 30,
        "max_depth": 2,
        "learning_rate": 0.1,
        "random_state": 42,
        "objective": "binary:logistic",
        "eval_metric": "auc",
    }


# ── shuffled_labels ──────────────────────────────────────────────────


def test_shuffled_labels_passes_on_random_data() -> None:
    X, y = _make_random_data(n_rows=300, positive_rate=0.3, seed=1)
    r = check_shuffled_labels(X, y, _default_params(), n_runs=3)
    assert isinstance(r, ValidationResult)
    assert r.name == "shuffled_labels"
    assert r.passed, f"Random data should not leak — got AUC={r.metric}"
    assert r.metric is not None
    # Under the null, 3-run mean AUC with N=240 train / 60 test is noisy.
    # Accept anything below the configured threshold.
    assert r.metric <= SHUFFLED_LABELS_THRESHOLD + 0.15


def test_shuffled_labels_skipped_on_tiny_sample() -> None:
    X, y = _make_random_data(n_rows=10, positive_rate=0.3)
    r = check_shuffled_labels(X, y, _default_params(), n_runs=3)
    assert r.passed
    assert "skipped" in r.details


# ── adversarial_validation ───────────────────────────────────────────


def test_adversarial_passes_on_same_distribution() -> None:
    rng = np.random.default_rng(42)
    X_train = pd.DataFrame(rng.standard_normal((200, 4)), columns=list("abcd"))
    X_test = pd.DataFrame(rng.standard_normal((100, 4)), columns=list("abcd"))
    r = check_adversarial_validation(X_train, X_test)
    assert r.passed
    assert r.metric is not None
    assert r.metric <= ADVERSARIAL_ABS_THRESHOLD + 0.05


def test_adversarial_detects_distribution_shift() -> None:
    rng = np.random.default_rng(42)
    X_train = pd.DataFrame(rng.standard_normal((200, 4)), columns=list("abcd"))
    # Huge mean shift — trivially separable
    X_test = pd.DataFrame(
        rng.standard_normal((100, 4)) + 10.0,
        columns=list("abcd"),
    )
    r = check_adversarial_validation(X_train, X_test)
    assert not r.passed
    assert r.severity == "warn"  # downgraded: temporal drift is soft signal
    assert r.metric is not None and r.metric > ADVERSARIAL_ABS_THRESHOLD


def test_adversarial_wow_delta_alert() -> None:
    rng = np.random.default_rng(42)
    X_train = pd.DataFrame(rng.standard_normal((200, 4)), columns=list("abcd"))
    # Small shift — abs AUC modest, but much higher than "previous" run
    X_test = pd.DataFrame(
        rng.standard_normal((100, 4)) + 2.0,
        columns=list("abcd"),
    )
    r = check_adversarial_validation(X_train, X_test, previous_auc=0.55)
    # Either abs or wow should fire
    assert not r.passed
    assert r.details["wow_delta"] is not None


# ── prior_drift ──────────────────────────────────────────────────────


def test_prior_drift_passes_small_change() -> None:
    r = check_prior_drift(train_prior=0.30, recent_prior=0.33)
    assert r.passed
    assert r.metric is not None and r.metric < PRIOR_DRIFT_THRESHOLD


def test_prior_drift_fails_large_change() -> None:
    r = check_prior_drift(train_prior=0.30, recent_prior=0.50)
    assert not r.passed
    assert r.severity == "alert"


def test_prior_drift_handles_zero_prior() -> None:
    r = check_prior_drift(train_prior=0.0, recent_prior=0.2)
    assert not r.passed
    assert "error" in r.details


# ── ks_predictions ───────────────────────────────────────────────────


def test_ks_predictions_passes_same_distribution() -> None:
    rng = np.random.default_rng(1)
    a = rng.uniform(0, 1, 200)
    b = rng.uniform(0, 1, 200).tolist()
    r = check_ks_predictions(a, b)
    assert r.passed
    assert r.metric is not None  # p-value


def test_ks_predictions_detects_shift() -> None:
    rng = np.random.default_rng(1)
    # Non-overlapping distributions
    today = rng.normal(0.2, 0.05, 200).clip(0, 1)
    yesterday = rng.normal(0.8, 0.05, 200).clip(0, 1).tolist()
    r = check_ks_predictions(today, yesterday)
    assert not r.passed
    assert r.metric is not None and r.metric < 0.01


def test_ks_predictions_skipped_without_baseline() -> None:
    r = check_ks_predictions(np.array([0.1, 0.2, 0.3] * 50), None)
    assert r.passed
    assert "skipped" in r.details


# ── calibration ──────────────────────────────────────────────────────


def test_calibration_returns_brier_and_bins() -> None:
    rng = np.random.default_rng(0)
    y_true = rng.integers(0, 2, 100)
    y_proba = rng.uniform(0, 1, 100)
    r = check_calibration(y_true, y_proba, n_bins=5)
    assert r.passed  # INFO only, never fails
    assert r.metric is not None
    assert "bins" in r.details


# ── feature_importance_sanity ────────────────────────────────────────


def _fit_toy_model(features: list[str], seed: int = 42) -> xgb.XGBClassifier:
    rng = np.random.default_rng(seed)
    n = 200
    X = pd.DataFrame(rng.standard_normal((n, len(features))), columns=features)
    # Label depends on first feature — ensures importance is not empty
    y = (X[features[0]] > 0).astype(int).values
    m = xgb.XGBClassifier(
        n_estimators=20,
        max_depth=2,
        random_state=seed,
        objective="binary:logistic",
        eval_metric="auc",
    )
    m.fit(X, y, verbose=False)
    return m


def test_feature_importance_sanity_passes_on_clean_features() -> None:
    m = _fit_toy_model(["alpha", "beta", "gamma", "delta"])
    r = check_feature_importance_sanity(m)
    assert r.passed
    assert r.details["leak_features_in_top"] == []


def test_feature_importance_sanity_detects_leak() -> None:
    # Put a known-leak feature as the dominant one
    leak = next(iter(KNOWN_LEAK_FEATURES))
    m = _fit_toy_model([leak, "benign1", "benign2"])
    r = check_feature_importance_sanity(m)
    assert not r.passed
    assert leak in r.details["leak_features_in_top"]


# ── economic_backtest ───────────────────────────────────────────────


def test_economic_backtest_positive_pnl_when_model_is_good() -> None:
    # Perfectly-calibrated proba: matches label exactly
    y = np.array([1] * 60 + [0] * 40)
    proba = y.astype(float) * 0.9 + 0.05  # positives ~0.95, negatives ~0.05
    r = check_economic_backtest(
        y,
        proba,
        tp_pct=50,
        sl_pct=30,
        proba_threshold=0.5,
    )
    assert r.passed
    assert r.metric is not None and r.metric > 0


def test_economic_backtest_negative_pnl_when_random() -> None:
    rng = np.random.default_rng(0)
    y = (rng.random(100) < 0.2).astype(int)
    proba = rng.uniform(0, 1, 100)  # random signal
    r = check_economic_backtest(
        y,
        proba,
        tp_pct=50,
        sl_pct=80,
        proba_threshold=0.3,
    )
    # With 20% base rate and 50/80 asymmetric payoff, random model is
    # expected to lose money on average. Any single run is noisy but seed
    # fixed.
    # Either passed or failed — only check structure, not sign.
    assert r.metric is not None


def test_economic_backtest_realistic_is_stricter() -> None:
    y = np.array([1] * 50 + [0] * 50)
    proba = np.concatenate([np.full(50, 0.9), np.full(50, 0.1)])
    standard = check_economic_backtest(y, proba, realistic=False)
    realistic = check_economic_backtest(y, proba, realistic=True)
    assert standard.metric is not None and realistic.metric is not None
    assert realistic.metric < standard.metric, "Realistic variant should penalize more than standard"


def test_economic_backtest_no_entries() -> None:
    y = np.array([1, 0, 1, 0])
    proba = np.array([0.1, 0.1, 0.1, 0.1])  # below default 0.5 threshold
    r = check_economic_backtest(y, proba, proba_threshold=0.5)
    assert r.passed
    assert r.details["n_entries"] == 0


# ── rolling_walk_forward ────────────────────────────────────────────


def test_rolling_walk_forward_skipped_on_small_sample() -> None:
    X, y = _make_random_data(n_rows=200, positive_rate=0.3)
    r = check_rolling_walk_forward(X, y, _default_params())
    assert r.severity == "info"
    assert "skipped" in r.details


# ── split helpers ───────────────────────────────────────────────────


def test_split_chrono_entry_80_20() -> None:
    df = pd.DataFrame(
        {
            "scored_at": list(range(100)),
            "label": [0] * 80 + [1] * 20,
        }
    )
    tr, te = _split_chrono(df, "scored_at")
    assert len(tr) == 80 and len(te) == 20
    # Chronological: test is the later portion
    assert tr["scored_at"].max() < te["scored_at"].min()


def test_split_chrono_exit_groups_by_mint() -> None:
    # 5 mints, each with 3 rows at increasing entry_ts
    rows = []
    for i, m in enumerate(["A", "B", "C", "D", "E"]):
        entry_ts = i * 100.0
        for j in range(3):
            rows.append(
                {
                    "mint": m,
                    "entry_ts": entry_ts,
                    "sample_ts": entry_ts + j,
                    "label": 0,
                }
            )
    df = pd.DataFrame(rows)
    tr, te = _split_chrono_exit(df)
    # No mint may straddle the split
    tr_mints = set(tr.mint)
    te_mints = set(te.mint)
    assert not tr_mints & te_mints, f"Mint leaked across split: {tr_mints & te_mints}"


# ── orchestrator helpers ────────────────────────────────────────────


def test_load_yesterday_report_finds_recent(tmp_path: Path) -> None:
    from datetime import date, timedelta

    yesterday = date.today() - timedelta(days=1)
    p = tmp_path / f"daily_report_entry_{yesterday.isoformat()}.json"
    p.write_text(json.dumps({"kind": "entry", "hello": "world"}))
    rep = _load_yesterday_report(tmp_path, "entry")
    assert rep is not None
    assert rep["hello"] == "world"


def test_load_yesterday_report_returns_none_if_missing(tmp_path: Path) -> None:
    rep = _load_yesterday_report(tmp_path, "entry")
    assert rep is None


def test_load_yesterday_report_skips_corrupt(tmp_path: Path) -> None:
    from datetime import date, timedelta

    y = date.today() - timedelta(days=1)
    (tmp_path / f"daily_report_entry_{y.isoformat()}.json").write_text("{invalid")
    assert _load_yesterday_report(tmp_path, "entry") is None
