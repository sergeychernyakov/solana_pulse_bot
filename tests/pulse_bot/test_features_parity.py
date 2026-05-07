# tests/pulse_bot/test_features_parity.py
"""Training-serving parity tests for pulse_bot.ml.features.

These tests verify that the shared extractor produces the exact same
values as what build_dataset.py writes to parquet, and the same values
as what the live Scorer would compute from a ScoringResult.

Failing any of these tests means a production ML prediction could drift
from training — the bug codex flagged as the #1 skew source.
"""

from __future__ import annotations

import math
import sqlite3
from pathlib import Path

import pandas as pd
import pytest

from pulse_bot.ml.features import (
    CREATOR_FEATURES,
    DERIVED_FEATURES,
    ENTRY_FEATURE_ORDER,
    FEATURE_SCHEMA_VERSION,
    HELIUS_FEATURES,
    SCORER_FEATURES,
    TIME_AWARE_DERIVED_FEATURES,
    TIME_AWARE_FEATURES,
    WALLET_FEATURES,
    extract_entry_features,
    extract_entry_vector,
)

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
DB_PATH = REPO_ROOT / "pulse_bot.db"


def test_feature_order_is_stable() -> None:
    """ENTRY_FEATURE_ORDER must concatenate the seven groups in order.

    History:
    * Phase E (2026-04-24): WALLET_FEATURES added as the fifth group.
    * Phase 2.5 (2026-04-25): TIME_AWARE_FEATURES + TIME_AWARE_DERIVED_FEATURES
      appended at the end (groups 6 + 7) so existing column ordering
      stays untouched and old-model bisection diffs stay readable.
    """
    assert ENTRY_FEATURE_ORDER == [
        *SCORER_FEATURES,
        *DERIVED_FEATURES,
        *HELIUS_FEATURES,
        *CREATOR_FEATURES,
        *WALLET_FEATURES,
        *TIME_AWARE_FEATURES,
        *TIME_AWARE_DERIVED_FEATURES,
    ]
    # No duplicates
    assert len(ENTRY_FEATURE_ORDER) == len(set(ENTRY_FEATURE_ORDER))


def test_schema_version_non_empty() -> None:
    assert isinstance(FEATURE_SCHEMA_VERSION, str)
    assert FEATURE_SCHEMA_VERSION


def test_extract_fills_missing_with_zero() -> None:
    """Missing / None scorer + helius fields → 0.0 (NaN policy v1).

    Exceptions:
    * ``hour_cos`` — missing hour_utc=0 → cos=1 (midnight baseline).
    * ``WALLET_FEATURES`` (Phase E 2026-04-24) — NaN when no wallet stats
      were provided, so XGBoost can split on missingness explicitly.
      "No wallet data" is not the same as "zero wallet data".
    * ``CREATOR_FEATURES`` (2026-05-05 NaN-policy v2) — NaN when
      ``creator_snapshot=None`` (no creator data at all). Aligns with
      build_dataset.py merge_asof miss → NaN, fixing the train/serve
      skew on the ~64% of live tokens with brand-new creators.
    """
    feats = extract_entry_features({}, holder_snapshot=None, creator_snapshot=None)
    special = {"hour_cos", *WALLET_FEATURES, *CREATOR_FEATURES}
    zero_defaults = [f for f in ENTRY_FEATURE_ORDER if f not in special]
    for name in zero_defaults:
        assert feats[name] == 0.0, f"{name} should default to 0.0"
    assert feats["hour_cos"] == 1.0  # cos(0) = 1
    for name in WALLET_FEATURES:
        assert math.isnan(
            feats[name]
        ), f"{name} should default to NaN when wallet_prior_stats is None"
    for name in CREATOR_FEATURES:
        assert math.isnan(
            feats[name]
        ), f"{name} should default to NaN when creator_snapshot is None"


def test_extract_reads_scorer_fields() -> None:
    row = {"unique_buyers": 7, "buy_count": 12, "first_buy_sol": 0.5}
    feats = extract_entry_features(row, holder_snapshot=None)
    assert feats["unique_buyers"] == 7.0
    assert feats["buy_count"] == 12.0
    assert feats["first_buy_sol"] == 0.5
    # Everything else zero-filled
    assert feats["sell_count"] == 0.0


def test_cyclical_hour_encoding() -> None:
    # Hour 6 → sin=1, cos=0 (quarter-day)
    feats = extract_entry_features({}, hour_utc=6)
    assert feats["hour_sin"] == pytest.approx(1.0, abs=1e-10)
    assert feats["hour_cos"] == pytest.approx(0.0, abs=1e-10)
    # Hour 0 → sin=0, cos=1
    feats = extract_entry_features({}, hour_utc=0)
    assert feats["hour_sin"] == pytest.approx(0.0, abs=1e-10)
    assert feats["hour_cos"] == pytest.approx(1.0, abs=1e-10)


def test_holder_snapshot_maps_fields() -> None:
    holder = {
        "top1_30": 15.5,
        "top5_30": 45.0,
        "hc_30": 120,
        "top1_delta": -2.3,
        "top5_delta": 5.1,
    }
    feats = extract_entry_features({}, holder_snapshot=holder)
    assert feats["top1_30"] == 15.5
    assert feats["top5_30"] == 45.0
    assert feats["hc_30"] == 120.0
    assert feats["top1_delta"] == -2.3
    assert feats["top5_delta"] == 5.1


def test_vector_matches_dict_order() -> None:
    row = {"unique_buyers": 1, "buy_count": 2, "sell_count": 3}
    feats = extract_entry_features(row, hour_utc=0)
    vec = extract_entry_vector(row, hour_utc=0)
    for i, name in enumerate(ENTRY_FEATURE_ORDER):
        v, d = vec[i], feats[name]
        # NaN != NaN in Python — use explicit identity check for the
        # WALLET_FEATURES NaN sentinels (Phase E NaN-first policy).
        if isinstance(v, float) and math.isnan(v):
            assert isinstance(d, float) and math.isnan(d), f"Mismatch at {name}"
        else:
            assert v == d, f"Mismatch at {name}"


def test_accepts_object_with_attributes() -> None:
    class Fake:
        unique_buyers = 3
        buy_count = 9

    feats = extract_entry_features(Fake(), hour_utc=12)
    assert feats["unique_buyers"] == 3.0
    assert feats["buy_count"] == 9.0


def test_nan_and_none_treated_same() -> None:
    row_with_none = {"unique_buyers": None}
    row_with_nan = {"unique_buyers": float("nan")}
    feats_none = extract_entry_features(row_with_none)
    feats_nan = extract_entry_features(row_with_nan)
    # None → 0.0 (explicit default)
    assert feats_none["unique_buyers"] == 0.0
    # NaN → 0.0 or stays NaN (documented tradeoff; NaN policy zero-fills
    # in build_dataset before training so the skew is resolved there).
    assert feats_nan["unique_buyers"] == 0.0 or math.isnan(feats_nan["unique_buyers"])


# ── Parity with live DB ─────────────────────────────────────────────


@pytest.mark.skipif(not DB_PATH.exists(), reason="pulse_bot.db missing")
def test_parity_with_live_token_scores() -> None:
    """Sample 100 recent token_scores rows, extract via new features.py,
    verify each extracted value equals the original column value.

    This catches (a) column renames, (b) default-value mismatches,
    (c) any silent drift between SCORER_FEATURES and actual DB schema.
    """
    conn = sqlite3.connect(DB_PATH)
    cols_quoted = ", ".join([f'"{c}"' for c in SCORER_FEATURES])
    df = pd.read_sql_query(
        f"""
        SELECT mint, hour_utc, {cols_quoted}
        FROM token_scores
        WHERE source = 'live'
        ORDER BY scored_at DESC
        LIMIT 100
        """,
        conn,
    )
    conn.close()
    if df.empty:
        pytest.skip("No live token_scores rows to verify")
    mismatches: list[str] = []
    for _, row in df.iterrows():
        feats = extract_entry_features(
            row.to_dict(),
            hour_utc=row["hour_utc"],
        )
        for name in SCORER_FEATURES:
            want = row[name]
            got = feats[name]
            if pd.isna(want):
                if got != 0.0:
                    mismatches.append(
                        f"mint={row['mint']} {name}: NaN→{got} (expected 0.0)"
                    )
            elif abs(float(want) - got) > 1e-9:
                mismatches.append(
                    f"mint={row['mint']} {name}: {want} vs extracted {got}"
                )
    assert not mismatches, "Feature parity broken:\n" + "\n".join(mismatches[:20])


def test_creator_features_live_vs_training_parity() -> None:
    """Train path reads ``creator_*`` dict columns; live path passes a
    CreatorStats dataclass whose attribute names are inconsistent (some
    keep the prefix, some drop it). Both paths must yield identical
    feature values for the same underlying numbers.

    This is the 2026-04-23 fix: prior to it, two of four CREATOR_FEATURES
    resolved to 0.0 at live inference because the extractor stripped the
    ``creator_`` prefix before lookup and the CreatorStats attribute was
    stored WITH the prefix (``creator_age_days``) or under a differently
    named alias (``snapshot_prior_tokens``)."""
    from pulse_bot.models import CreatorStats

    # Live-path object — mirrors what db.get_creator_stats_as_of_sync builds
    stats = CreatorStats(
        wallet="abc",
        total_tokens_created=10,
        times_seen=10,
        tokens_where_creator_sold_early=0,
        first_seen_at=0.0,
        last_seen_at=0.0,
        rug_rate=0.0,
        graduation_rate=0.0,
        median_peak_mc_sol=42.5,
        creator_age_days=7.25,
        inter_token_interval_sec=3600.0,
        creator_balance_sol=12.8,
        snapshot_prior_tokens=10,
    )
    # Training-path row — mirrors build_dataset's SELECT aliasing
    train_row = {
        "creator_age_days": 7.25,
        "creator_median_peak_mc_sol": 42.5,
        "creator_inter_token_interval_sec": 3600.0,
        "creator_total_prior_tokens": 10,
        "creator_balance_sol": 12.8,
    }
    feats_live = extract_entry_features({}, creator_snapshot=stats)
    feats_train = extract_entry_features({}, creator_snapshot=train_row)
    for name in CREATOR_FEATURES:
        assert feats_live[name] == feats_train[name], (
            f"Live/train skew at {name}: live={feats_live[name]!r} "
            f"train={feats_train[name]!r}"
        )
    # Spot-check the actual numbers (not just equality)
    assert feats_live["creator_age_days"] == 7.25
    assert feats_live["creator_median_peak_mc_sol"] == 42.5
    assert feats_live["creator_inter_token_interval_sec"] == 3600.0
    assert feats_live["creator_balance_sol"] == 12.8
    # creator_total_prior_tokens / creator_graduated_count removed in
    # v19 cleanup (stable_dead × 2 sequential runs). They're no longer
    # part of CREATOR_FEATURES; data still in CreatorStats for diagnostics.


def test_creator_features_missing_snapshot_is_nan() -> None:
    """``creator_snapshot=None`` → all CREATOR_FEATURES default to NaN
    (2026-05-05 NaN-policy v2). XGBoost handles missingness natively;
    matches build_dataset.py behavior on rows with no creator snapshot."""
    feats = extract_entry_features({}, creator_snapshot=None)
    for name in CREATOR_FEATURES:
        assert math.isnan(feats[name]), f"{name} should be NaN when snapshot=None"


def test_creator_features_solo_creator_partial_nan() -> None:
    """Solo creator (priors < 2): ``creator_median_peak_mc_sol`` and
    ``creator_inter_token_interval_sec`` are mathematically undefined
    (median of empty list, interval of single token) → NaN.
    ``creator_age_days`` and ``creator_balance_sol`` remain real values."""
    snap = {
        "snapshot_prior_tokens": 1,
        "creator_age_days": 5.2,
        "creator_balance_sol": 3.7,
        "median_peak_mc_sol": 0.0,
        "inter_token_interval_sec": 0.0,
    }
    feats = extract_entry_features({}, creator_snapshot=snap)
    assert math.isnan(feats["creator_median_peak_mc_sol"])
    assert math.isnan(feats["creator_inter_token_interval_sec"])
    assert feats["creator_age_days"] == 5.2
    assert feats["creator_balance_sol"] == 3.7


def test_creator_features_veteran_real_values() -> None:
    """Creator with ≥ 2 priors keeps real median/interval values — no
    degenerate-NaN replacement."""
    snap = {
        "snapshot_prior_tokens": 5,
        "creator_age_days": 30.0,
        "creator_balance_sol": 12.0,
        "median_peak_mc_sol": 25.0,
        "inter_token_interval_sec": 3600.0,
    }
    feats = extract_entry_features({}, creator_snapshot=snap)
    assert feats["creator_median_peak_mc_sol"] == 25.0
    assert feats["creator_inter_token_interval_sec"] == 3600.0
    assert feats["creator_age_days"] == 30.0
    assert feats["creator_balance_sol"] == 12.0


def test_creator_balance_sol_in_schema() -> None:
    """Regression: 2026-04-23 added creator_balance_sol after discovering
    9697/24056 snapshots already carried real Helius-captured balances."""
    assert "creator_balance_sol" in CREATOR_FEATURES
    assert "creator_balance_sol" in ENTRY_FEATURE_ORDER


def test_skew_guard_fires_on_regression(caplog) -> None:
    """If someone passes a bogus snapshot (no recognisable keys), the
    extractor must WARN rather than silently return all-zero features.

    This is the fingerprint check for the 2026-04-23 bug: a non-None
    snapshot that resolves every CREATOR_FEATURES to 0.0.
    """
    import logging

    class BogusStats:
        # No attribute matches any CREATOR_FEATURES candidate key —
        # simulates a naming-convention regression.
        something_unrelated = 42

    with caplog.at_level(logging.WARNING, logger="pulse_bot.ml.features"):
        feats = extract_entry_features({}, creator_snapshot=BogusStats())
    # All creator features zero-filled because nothing matched...
    for name in CREATOR_FEATURES:
        assert feats[name] == 0.0
    # ...but we must have screamed about each one.
    warn_msgs = [r.getMessage() for r in caplog.records if r.levelname == "WARNING"]
    # At least the aggregate "all zero" warning fired
    assert any(
        "creator-skew" in m or "CREATOR_FEATURES resolved to 0.0" in m
        for m in warn_msgs
    ), "Skew guard did not fire on bogus snapshot; " "warnings seen: " + repr(warn_msgs)


def test_skew_guard_silent_when_snapshot_is_none(caplog) -> None:
    """Legitimately-missing snapshot (None) must NOT warn — only the
    ambiguous case (non-None but all zero) is a bug signature."""
    import logging

    with caplog.at_level(logging.WARNING, logger="pulse_bot.ml.features"):
        extract_entry_features({}, creator_snapshot=None)
    warn_msgs = [r.getMessage() for r in caplog.records if r.levelname == "WARNING"]
    assert not any(
        "creator" in m.lower() for m in warn_msgs
    ), "Skew guard should not fire on None snapshot; " "warnings seen: " + repr(
        warn_msgs
    )


@pytest.mark.skipif(not DB_PATH.exists(), reason="pulse_bot.db missing")
def test_parity_with_parquet_if_present() -> None:
    """If a trained parquet exists, its feature columns must all be in
    ENTRY_FEATURE_ORDER and vice versa (no drift between training data
    and the shared schema).

    Schema bumps (e.g. Phase 2.5 v17→v18 on 2026-04-25) intentionally
    add new columns and *expect* the parquet to be regenerated before
    the next training run. To avoid this test failing simply because the
    parquet on disk is older than the current schema, we SKIP when the
    set of missing columns is exactly the new TIME_AWARE_* groups — the
    user has not yet rebuilt entry.parquet but the schema is consistent.
    """
    pq = REPO_ROOT / "data" / "ml" / "entry.parquet"
    if not pq.exists():
        pytest.skip("entry.parquet not built")
    df = pd.read_parquet(pq, columns=None)
    df_cols = set(df.columns)
    missing_in_parquet = [c for c in ENTRY_FEATURE_ORDER if c not in df_cols]
    # New columns added across versions — skip when parquet predates each.
    new_in_v18 = set(TIME_AWARE_FEATURES) | set(TIME_AWARE_DERIVED_FEATURES)
    new_in_v20 = {
        "top10_buyer_prior_avg_wr",
        "top10_buyer_prior_total_pnl_sol",
        "n_buyers_first_5s",
    }
    # v21 added wallet_classifications — sniper / smart-money / bot /
    # cluster counts among top-10 buyers. Same forward-compat principle:
    # skip when parquet predates the schema rather than fail.
    new_in_v21 = {
        "n_snipers_in_top10",
        "n_smart_money_in_top10",
        "n_bots_in_top10",
        "n_in_small_cluster_top10",
    }
    forward_compat = new_in_v18 | new_in_v20 | new_in_v21
    if set(missing_in_parquet).issubset(forward_compat) and missing_in_parquet:
        pytest.skip(
            "entry.parquet predates current schema; rebuild via "
            "`python -m pulse_bot.ml.build_dataset --dataset entry`"
        )
    assert (
        not missing_in_parquet
    ), f"Parquet missing features the extractor expects: {missing_in_parquet}"
