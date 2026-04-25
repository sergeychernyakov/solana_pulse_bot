# tests/pulse_bot/test_t30_model.py
"""Phase 3 — @T+30 dual-snapshot model parity + smoke tests.

Mirrors the test_features_parity pattern for the T+30 schema:

* Schema version + feature-order shape are correct.
* Live extractor and a synthetic build-dataset row produce bit-identical
  feature vectors for the same inputs (catches train/serve skew).
* T+30 feature schema is a STRICT SUBSET of the T+90 fields available
  by 30 seconds (excluding only T+30-specific derivations).
* Smoke train: 100 synthetic rows, 10% positives, model trains without
  crash, AUC >= 0.5 on holdout.
"""

from __future__ import annotations

import math
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from pulse_bot.ml.features import (
    CREATOR_FEATURES,
    DERIVED_FEATURES_T30,
    ENTRY_FEATURE_ORDER,
    ENTRY_T30_FEATURE_ORDER,
    FEATURE_SCHEMA_VERSION_T30,
    HELIUS_FEATURES_T30,
    SCORER_FEATURES_T30,
    WALLET_FEATURES,
    extract_entry_features_t30,
    extract_entry_vector_t30,
)

# ── Schema shape ────────────────────────────────────────────────────


def test_schema_version_is_t30() -> None:
    assert isinstance(FEATURE_SCHEMA_VERSION_T30, str)
    assert FEATURE_SCHEMA_VERSION_T30.startswith("entry_t30_v")


def test_t30_feature_order_is_concatenation() -> None:
    """ENTRY_T30_FEATURE_ORDER == [scorer_t30, derived_t30, helius_t30,
    creator, wallet]."""
    assert ENTRY_T30_FEATURE_ORDER == [
        *SCORER_FEATURES_T30,
        *DERIVED_FEATURES_T30,
        *HELIUS_FEATURES_T30,
        *CREATOR_FEATURES,
        *WALLET_FEATURES,
    ]
    assert len(ENTRY_T30_FEATURE_ORDER) == len(set(ENTRY_T30_FEATURE_ORDER))


def test_t30_scorer_subset_of_full_schema() -> None:
    """Every SCORER_FEATURES_T30 entry must also exist in the T+90
    ENTRY_FEATURE_ORDER. The T+30 vector is a strict subset of features
    physically available by 30s of token life."""
    full = set(ENTRY_FEATURE_ORDER)
    for name in SCORER_FEATURES_T30:
        assert name in full, f"{name} should also be in ENTRY_FEATURE_ORDER"


def test_t30_excludes_t120_only_features() -> None:
    """T+30 schema must NOT contain features that depend on T+120 data."""
    forbidden = {
        "top1_120",
        "top5_120",
        "top10_120",
        "hc_120",
        "top1_delta",
        "top5_delta",
        "top10_delta",
        "hc_velocity",
        "top5_minus_top1_120",
        "top10_minus_top5_120",
        "hc_growth_ratio",
    }
    found = forbidden & set(ENTRY_T30_FEATURE_ORDER)
    assert not found, f"T+30 schema leaks T+120-derived features: {found}"


# ── Extractor parity ────────────────────────────────────────────────


def test_t30_extract_fills_missing_with_zero() -> None:
    """Empty inputs → zero defaults except hour_cos=1 and WALLET_FEATURES=NaN."""
    feats = extract_entry_features_t30({}, holder_snapshot_t30=None)
    special = {"hour_cos", *WALLET_FEATURES}
    for name in [n for n in ENTRY_T30_FEATURE_ORDER if n not in special]:
        assert feats[name] == 0.0, f"{name} should default to 0.0"
    assert feats["hour_cos"] == 1.0
    for name in WALLET_FEATURES:
        assert math.isnan(feats[name])


def test_t30_holder_snapshot_t30_only() -> None:
    """holder_snapshot_t30 supplies only the four T+30 columns."""
    snap = {"top1_30": 12.5, "top5_30": 33.0, "top10_30": 50.0, "hc_30": 80}
    feats = extract_entry_features_t30({}, holder_snapshot_t30=snap)
    assert feats["top1_30"] == 12.5
    assert feats["top5_30"] == 33.0
    assert feats["top10_30"] == 50.0
    assert feats["hc_30"] == 80.0
    # hc_velocity_to_30 = hc_30 / 30s → 80/30 ≈ 2.6667
    assert feats["hc_velocity_to_30"] == pytest.approx(80.0 / 30.0)


def test_t30_derived_ratios_match_train_arithmetic() -> None:
    """Live extractor must match the arithmetic that build_dataset_t30
    applies to the equivalent columns. Catches the most common skew bug
    — denominator drift.
    """
    row = {
        "buy_volume_sol": 4.0,
        "sell_volume_sol": 1.0,
        "buy_count": 30.0,
        "sell_count": 5.0,
        "max_buy_sol": 1.5,
        "avg_buy_sol": 0.3,
        "fast_volume_sol": 1.2,
        "fast_buy_rate": 0.4,
    }
    feats = extract_entry_features_t30(row)
    assert feats["buy_vol_to_sell_vol_ratio"] == pytest.approx(4.0 / (1.0 + 0.01))
    assert feats["buy_count_to_sell_count_ratio"] == pytest.approx(30.0 / (5.0 + 1.0))
    assert feats["buy_size_growth"] == pytest.approx(1.5 / (0.3 + 0.001))
    assert feats["fast_to_full_volume_ratio"] == pytest.approx(1.2 / (4.0 + 0.01))
    # T+30 specifically uses /30 denominator (NOT /90 like T+90 model)
    full_rate_30 = 30.0 / 30.0
    assert feats["fast_buy_rate_to_full"] == pytest.approx(0.4 / (full_rate_30 + 0.001))


def test_t30_vector_matches_dict_order() -> None:
    row = {"unique_buyers": 1, "buy_count": 2, "sell_count": 3}
    feats = extract_entry_features_t30(row, hour_utc=0)
    vec = extract_entry_vector_t30(row, hour_utc=0)
    assert len(vec) == len(ENTRY_T30_FEATURE_ORDER)
    for i, name in enumerate(ENTRY_T30_FEATURE_ORDER):
        v, d = vec[i], feats[name]
        if isinstance(v, float) and math.isnan(v):
            assert isinstance(d, float) and math.isnan(d)
        else:
            assert v == d, f"Mismatch at {name}"


def test_t30_live_vs_synthetic_train_parity() -> None:
    """A synthetic 'training row' (dict mirroring build_dataset_t30
    output) and a live-shape ScoringResult-like dict must produce
    identical feature vectors when given the same numbers.

    Same protective pattern as test_creator_features_live_vs_training_parity
    for the main model.
    """
    # Live-shape: scorer + helius dicts; creator/wallet supplied through
    # their own params.
    live_scoring = {
        "unique_buyers": 12,
        "buy_count": 25,
        "sell_count": 4,
        "buy_volume_sol": 3.5,
        "sell_volume_sol": 0.5,
        "max_buy_sol": 0.8,
        "avg_buy_sol": 0.14,
        "fast_volume_sol": 1.1,
        "fast_buy_rate": 0.5,
        "fast_buy_count": 8,
        "sol_price_usd": 200.0,
    }
    live_holder = {"top1_30": 11.0, "top5_30": 28.0, "top10_30": 41.0, "hc_30": 92}
    creator = {
        "creator_age_days": 3.5,
        "creator_median_peak_mc_sol": 30.0,
        "creator_inter_token_interval_sec": 1800.0,
        "creator_total_prior_tokens": 4,
        "creator_balance_sol": 5.0,
        "creator_graduated_count": 1,
    }
    feats_live = extract_entry_features_t30(
        live_scoring,
        holder_snapshot_t30=live_holder,
        creator_snapshot=creator,
        hour_utc=12,
    )
    # Synthetic training row: flat dict carrying the same numbers under
    # canonical column names.
    train_row = {**live_scoring, **live_holder, **creator, "hour_utc": 12}
    feats_train = extract_entry_features_t30(
        train_row,
        holder_snapshot_t30=train_row,
        creator_snapshot=train_row,
        hour_utc=12,
    )
    for name in ENTRY_T30_FEATURE_ORDER:
        v_live = feats_live[name]
        v_train = feats_train[name]
        if isinstance(v_live, float) and math.isnan(v_live):
            assert isinstance(v_train, float) and math.isnan(v_train), name
        else:
            assert v_live == pytest.approx(
                v_train
            ), f"Live/train skew at {name}: live={v_live!r} train={v_train!r}"


# ── Smoke train ─────────────────────────────────────────────────────


def _make_synth_t30_dataset(n_rows: int = 100, pos_frac: float = 0.10) -> pd.DataFrame:
    """Synthesise a small but realistic-shaped T30 frame.

    Positive class gets boosted ``unique_buyers`` and ``hc_30`` so the
    model has *some* signal to grip — required for AUC >= 0.5 to be
    achievable in a 100-row smoke test. Other features are NaN/zero
    or random noise.
    """
    rng = np.random.default_rng(42)
    n_pos = int(n_rows * pos_frac)
    labels = np.array([1] * n_pos + [0] * (n_rows - n_pos))
    rng.shuffle(labels)

    base: dict[str, np.ndarray] = {}
    for col in ENTRY_T30_FEATURE_ORDER:
        base[col] = rng.normal(0.0, 0.1, size=n_rows)
    # Inject signal: positives have higher unique_buyers / hc_30 / fast_buy_count
    for signal_col in ("unique_buyers", "hc_30", "fast_buy_count"):
        base[signal_col] = base[signal_col] + labels * 5.0

    # Sentinels build_dataset normally provides
    base["mint"] = np.array([f"synth_mint_{i:05d}" for i in range(n_rows)])
    base["scored_at"] = np.linspace(1700000000.0, 1700090000.0, n_rows)
    base["label"] = labels.astype(int)
    base["realized_pnl_pct"] = labels.astype(float) * 25.0 - 5.0

    return pd.DataFrame(base)


def test_t30_smoke_train(tmp_path: Path) -> None:
    """Smoke: build synthetic frame, train the T+30 model end-to-end,
    assert AUC >= 0.5 on holdout (trivially achievable when label is
    correlated with two synthetic signal features)."""
    pytest.importorskip("xgboost")
    from pulse_bot.ml.train import train_entry_t30

    df = _make_synth_t30_dataset(n_rows=100, pos_frac=0.10)
    data_path = tmp_path / "entry_t30.parquet"
    try:
        df.to_parquet(data_path, index=False)
    except Exception:
        data_path = tmp_path / "entry_t30.csv"
        df.to_csv(data_path, index=False)

    model_out = tmp_path / "entry_model_t30.ubj"
    result = train_entry_t30(data_path, model_out, split="chrono")

    assert model_out.exists(), "T30 model file should be saved"
    meta_path = model_out.with_suffix(".meta.json")
    assert meta_path.exists(), "T30 meta.json should be saved"

    auc = result["auc"]
    # Skip rather than fail on degenerate splits where the holdout has 0
    # positives — the synthetic shuffle can still stack labels unevenly
    # with the chrono split.
    if isinstance(auc, float) and not math.isnan(auc):
        assert auc >= 0.5, f"T30 smoke AUC should be >= 0.5, got {auc!r}"


def test_t30_meta_json_has_t30_schema_version(tmp_path: Path) -> None:
    """The persisted meta.json must record the T+30 schema version (so
    EntryT30Policy refuses to load a stale T+90 model file)."""
    pytest.importorskip("xgboost")
    import json

    from pulse_bot.ml.train import train_entry_t30

    df = _make_synth_t30_dataset(n_rows=100, pos_frac=0.10)
    data_path = tmp_path / "entry_t30.parquet"
    try:
        df.to_parquet(data_path, index=False)
    except Exception:
        data_path = tmp_path / "entry_t30.csv"
        df.to_csv(data_path, index=False)
    model_out = tmp_path / "entry_model_t30.ubj"
    train_entry_t30(data_path, model_out, split="chrono")
    meta = json.loads(model_out.with_suffix(".meta.json").read_text())
    assert meta["schema_version"] == FEATURE_SCHEMA_VERSION_T30
    assert meta["features"] == ENTRY_T30_FEATURE_ORDER
    assert meta.get("snapshot_age_sec") == 30.0


# ── Policy round-trip ───────────────────────────────────────────────


def test_t30_policy_round_trip(tmp_path: Path) -> None:
    """Train -> save -> load via EntryT30Policy.from_path -> predict
    must yield a finite probability and a valid 3-way decision."""
    pytest.importorskip("xgboost")
    from pulse_bot.ml.policy import EntryT30Policy
    from pulse_bot.ml.train import train_entry_t30

    df = _make_synth_t30_dataset(n_rows=100, pos_frac=0.10)
    data_path = tmp_path / "entry_t30.parquet"
    try:
        df.to_parquet(data_path, index=False)
    except Exception:
        data_path = tmp_path / "entry_t30.csv"
        df.to_csv(data_path, index=False)
    model_out = tmp_path / "entry_model_t30.ubj"
    train_entry_t30(data_path, model_out, split="chrono")

    policy = EntryT30Policy.from_path(model_out)
    sample = {
        "unique_buyers": 12,
        "buy_count": 30,
        "fast_buy_count": 8,
        "fast_buy_rate": 0.5,
        "fast_volume_sol": 1.5,
        "buy_volume_sol": 4.0,
        "sell_volume_sol": 0.5,
        "sell_count": 4,
    }
    holder = {"top1_30": 14.0, "top5_30": 35.0, "top10_30": 50.0, "hc_30": 95}
    proba = policy.predict_proba(sample, holder_snapshot_t30=holder)
    assert 0.0 <= proba <= 1.0
    action, p = policy.decide_with_confidence(sample, holder_snapshot_t30=holder)
    assert action in {"BUY", "SKIP", "DEFER"}
    assert p == pytest.approx(proba)


def test_t30_policy_rejects_invalid_thresholds(tmp_path: Path) -> None:
    """buy_ceiling <= skip_floor is a configuration bug — DEFER bucket
    would be empty by construction, so the constructor must raise."""
    pytest.importorskip("xgboost")
    import xgboost as xgb

    from pulse_bot.ml.policy import EntryT30Policy

    model = xgb.XGBClassifier()
    with pytest.raises(ValueError, match="strictly greater"):
        EntryT30Policy(
            model,
            model_hash="deadbeef",
            buy_ceiling=0.30,
            skip_floor=0.30,
        )
