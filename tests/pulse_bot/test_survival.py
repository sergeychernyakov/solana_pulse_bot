# tests/pulse_bot/test_survival.py
"""Unit tests for the discrete-time hazard / survival module.

Covers :class:`SurvivalLabelBuilder` (label expansion, censoring rules)
and :func:`predict_remaining_life` (cumulative-survival projection on a
hand-built fake model). No PG, no XGBoost training in tests — those are
covered by the dedicated training script invoked via the harness.
"""

from __future__ import annotations

import math
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from pulse_bot.ml.survival import (
    DEATH_EXIT_REASONS,
    DEFAULT_BUCKET_SECONDS,
    DEFAULT_MAX_HORIZON_SECONDS,
    SurvivalLabelBuilder,
    SurvivalPrediction,
    predict_remaining_life,
    to_dict,
    train_survival_model,
)

# ── Fixtures ────────────────────────────────────────────────────────


def _make_record(
    *,
    mint: str,
    entry_time: float,
    exit_time: float | None,
    exit_reason: str | None,
    status: str = "closed",
) -> dict:
    return {
        "mint": mint,
        "status": status,
        "entry_time": entry_time,
        "exit_time": exit_time if exit_time is not None else 0.0,
        "exit_reason": exit_reason or "",
        "entry_score": 50,
        "entry_buyer_number": 5,
    }


# ── Test 1: row-count invariant ─────────────────────────────────────


def test_build_row_count_equals_sum_of_buckets() -> None:
    """Total emitted rows == sum over tokens of buckets_until_death/censor."""
    builder = SurvivalLabelBuilder(bucket_seconds=5.0, max_horizon_seconds=180.0)
    records = [
        # Dies in 30s = 6 buckets.
        _make_record(
            mint="A",
            entry_time=1000.0,
            exit_time=1030.0,
            exit_reason="pulse_dead",
        ),
        # Censored at 60s = 12 buckets.
        _make_record(
            mint="B",
            entry_time=1000.0,
            exit_time=1060.0,
            exit_reason="take_profit",
        ),
        # Dies at 12.4s — ceil(12.4/5) = 3 buckets.
        _make_record(
            mint="C",
            entry_time=1000.0,
            exit_time=1012.4,
            exit_reason="no_new_blood",
        ),
    ]
    df = builder.build_from_records(records, now_ts=2000.0)
    assert len(df) == 6 + 12 + 3
    counts = df.groupby("mint").size().to_dict()
    assert counts == {"A": 6, "B": 12, "C": 3}


# ── Test 2: synthetic death lands in correct bucket ────────────────


def test_death_label_in_correct_bucket() -> None:
    """Token dying at t=30s → first hazard=1 lands in bucket [25..30s)."""
    builder = SurvivalLabelBuilder(bucket_seconds=5.0, max_horizon_seconds=180.0)
    rec = _make_record(
        mint="X", entry_time=0.0, exit_time=30.0, exit_reason="pulse_dead"
    )
    df = builder.build_from_records([rec], now_ts=10_000.0)
    # 30s / 5s = 6 buckets, last one (index 5, elapsed 25..30) carries 1.
    assert len(df) == 6
    deaths = df[df["died_in_bucket"] == 1]
    assert len(deaths) == 1
    only = deaths.iloc[0]
    assert only["bucket_index"] == 5
    assert only["elapsed_seconds"] == 25.0
    assert only["bucket_end_seconds"] == 30.0
    # Verify the same is true for sell_pressure + no_new_blood.
    for reason in DEATH_EXIT_REASONS:
        rec = _make_record(mint="X", entry_time=0.0, exit_time=30.0, exit_reason=reason)
        df = builder.build_from_records([rec])
        assert int(df["died_in_bucket"].sum()) == 1


# ── Test 3: censored token has no hazards ──────────────────────────


def test_censored_token_has_zero_hazards() -> None:
    """Closed-but-not-dead and open tokens emit all-zero hazard labels."""
    builder = SurvivalLabelBuilder(bucket_seconds=5.0, max_horizon_seconds=180.0)
    records = [
        _make_record(
            mint="WIN",
            entry_time=0.0,
            exit_time=45.0,
            exit_reason="take_profit",
        ),
        _make_record(
            mint="OPEN",
            entry_time=9_950.0,
            exit_time=None,
            exit_reason=None,
            status="open",
        ),
    ]
    df = builder.build_from_records(records, now_ts=10_000.0)
    # Both tokens contribute rows, but every died_in_bucket == 0.
    assert len(df) > 0
    assert int(df["died_in_bucket"].sum()) == 0
    # OPEN token censored at min(now-entry, horizon) = min(50, 180) = 50s.
    open_rows = df[df["mint"] == "OPEN"]
    assert len(open_rows) == 10  # 50 / 5
    # WIN token: 45s → 9 buckets.
    win_rows = df[df["mint"] == "WIN"]
    assert len(win_rows) == 9


def test_horizon_clamps_long_lived_tokens() -> None:
    """A token alive 600s with horizon=180s emits 36 buckets, no death."""
    builder = SurvivalLabelBuilder(bucket_seconds=5.0, max_horizon_seconds=180.0)
    rec = _make_record(
        mint="LONG", entry_time=0.0, exit_time=600.0, exit_reason="pulse_dead"
    )
    df = builder.build_from_records([rec])
    assert len(df) == 36  # 180 / 5
    # Death sits beyond the horizon — censored within the window.
    assert int(df["died_in_bucket"].sum()) == 0


# ── Test 4: predict_remaining_life on a high-hazard fake model ──────


class _StubHazardModel:
    """Minimal predict_proba stand-in for testing.

    Returns a constant hazard ``h`` for every input row. With h=0.2 and
    survival threshold 0.5, cumulative survival drops below 0.5 after
    ceil(log(0.5)/log(0.8)) = 4 buckets — at 5s each that is 20s.
    """

    def __init__(self, hazard: float) -> None:
        self.hazard = hazard

    def predict_proba(self, x):  # noqa: ANN001 — mirrors xgboost API
        n = len(x)
        return np.array([[1 - self.hazard, self.hazard] for _ in range(n)])


def test_high_hazard_predicts_short_life() -> None:
    """Constant 0.2 hazard → S(t) crosses 0.5 by t=20s (well under 60s)."""
    model = _StubHazardModel(hazard=0.2)
    pred = predict_remaining_life(
        model,
        features_at_now={"unique_buyers": 3, "buy_rate": 0.1},
        feature_order=["unique_buyers", "buy_rate", "elapsed_seconds"],
        bucket_seconds=5.0,
        max_horizon_seconds=180.0,
        now_elapsed_seconds=0.0,
    )
    assert isinstance(pred, SurvivalPrediction)
    assert pred.remaining_life_seconds < 60.0
    # ceil(log(0.5)/log(0.8)) = 4 buckets * 5s each = 20s.
    assert math.isclose(pred.remaining_life_seconds, 20.0, abs_tol=1e-6)
    # Hazard curve was filled across the full forward horizon.
    assert len(pred.hazard_curve) == int(180.0 / 5.0)
    assert all(math.isclose(h, 0.2, abs_tol=1e-6) for h in pred.hazard_curve)
    # Confidence: 0.2 is 0.3 away from 0.5 → 0.6.
    assert math.isclose(pred.confidence, 0.6, abs_tol=1e-6)


def test_low_hazard_returns_infinite_life() -> None:
    """0.001 hazard never crosses 0.5 → remaining_life = inf."""
    model = _StubHazardModel(hazard=0.001)
    pred = predict_remaining_life(
        model,
        features_at_now={},
        feature_order=["elapsed_seconds"],
        bucket_seconds=5.0,
        max_horizon_seconds=60.0,
    )
    assert math.isinf(pred.remaining_life_seconds)
    assert to_dict(pred)["remaining_life_seconds"] is None


def test_now_elapsed_advances_features() -> None:
    """now_elapsed_seconds offsets the projected elapsed_seconds vector."""

    class _RecordingModel:
        def __init__(self) -> None:
            self.seen_rows: list[list[float]] = []

        def predict_proba(self, x):  # noqa: ANN001
            self.seen_rows = list(x)
            return np.array([[1.0, 0.0] for _ in x])

    model = _RecordingModel()
    predict_remaining_life(
        model,
        features_at_now={"buy_rate": 0.5},
        feature_order=["buy_rate", "elapsed_seconds"],
        bucket_seconds=5.0,
        max_horizon_seconds=30.0,
        now_elapsed_seconds=10.0,
    )
    # Horizon left = 20s → 4 forward buckets at offsets 10, 15, 20, 25.
    elapsed = [row[1] for row in model.seen_rows]
    assert elapsed == [10.0, 15.0, 20.0, 25.0]


# ── Training smoke test (no XGBoost dependency on labels) ──────────


def test_train_survival_model_smoke(tmp_path: Path) -> None:
    """End-to-end fit on a tiny synthetic frame writes .ubj + meta.json."""
    pytest.importorskip("xgboost")
    rng = np.random.default_rng(42)
    n = 400
    df = pd.DataFrame(
        {
            "elapsed_seconds": rng.uniform(0, 90, n),
            "buy_rate": rng.uniform(0, 2, n),
            "sell_rate": rng.uniform(0, 2, n),
            "bucket_index": rng.integers(0, 18, n),
            # Force a small but non-zero positive rate.
            "died_in_bucket": rng.choice([0, 1], n, p=[0.92, 0.08]),
        }
    )
    out = tmp_path / "survival.ubj"
    meta = train_survival_model(
        df,
        out,
        n_estimators=20,
        max_depth=3,
        bucket_seconds=DEFAULT_BUCKET_SECONDS,
        max_horizon_seconds=DEFAULT_MAX_HORIZON_SECONDS,
    )
    assert out.exists()
    assert (tmp_path / "survival.meta.json").exists()
    assert "features" in meta
    assert "elapsed_seconds" in meta["features"]
    assert meta["positives"] > 0
