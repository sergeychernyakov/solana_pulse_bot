# tests/pulse_bot/test_golden_features.py
"""Golden snapshot tests for feature vectors.

Codex review 2026-04-28 (next-level recommendations): "Сделать golden
tests на feature vectors для каждой модели. Сейчас это быстрее ловит
drift, чем обычные поведенческие тесты."

The premise: pin the EXACT feature vector that an extractor produces
for a fixed input (synthetic ScoringResult + holder snapshot etc.).
Any silent change to the extractor — schema bump, helper rename, math
fix — causes a diff. The diff lands as test failure with file:line, so
the next reviewer sees what changed at a glance.

How to update goldens: when a deliberate feature change ships, run
``UPDATE_GOLDENS=1 pytest tests/pulse_bot/test_golden_features.py``
to re-record the expected vectors. CI/pre-commit run without that flag
and fail on drift.
"""

from __future__ import annotations

import math
import os

import pytest


def _approx(actual, expected, rel=1e-6):
    """NaN-aware fuzzy compare — math.nan != math.nan in Python."""
    if isinstance(actual, float) and math.isnan(actual):
        return isinstance(expected, float) and math.isnan(expected)
    if isinstance(expected, float) and math.isnan(expected):
        return False
    return abs(float(actual) - float(expected)) <= max(
        abs(rel * float(expected)), 1e-9
    )


# ───────────────────────── Fixtures ────────────────────────────────────


def _make_minimal_scoring_result():
    """Minimal duck-typed ScoringResult with deterministic values for
    every numeric field the extractor touches."""
    from pulse_bot.models import ScoringResult

    return ScoringResult(
        unique_buyers=10,
        unique_sellers=2,
        buy_count=12,
        sell_count=2,
        buy_volume_sol=5.5,
        sell_volume_sol=0.3,
        buy_diversity=8,
        max_buy_sol=2.0,
        avg_buy_sol=0.46,
        median_buy_sol=0.4,
        sell_pressure=0.05,
        top3_buyer_pct=0.6,
        repeat_buyer_count=2,
        first_buy_sol=0.1,
        buy_velocity_trend=0.2,
        buy_size_trend=0.0,
        time_to_first_buy=3.0,
        buys_per_unique=1.2,
        curve_velocity=0.05,
        curve_acceleration=0.0,
        fast_buy_count=4,
        fast_unique_buyers=4,
        fast_volume_sol=1.5,
        fast_buy_rate=0.8,
        fast_sell_ratio=0.0,
        tokens_last_5min=20,
        concurrent_observations=12,
        pnl_at_fast_entry_pct=0.0,
        fast_trade_count=4,
        full_trade_count=14,
        gap_create_to_first_trade=2.0,
        hour_utc=14,
        market_cap_sol=85.0,
        exit_price=0.000_000_1,
    )


def _make_minimal_holder_snapshot():
    return {
        "top1_30": 0.45,
        "top5_30": 0.65,
        "top10_30": 0.75,
        "hc_30": 50,
        "top1_120": 0.38,
        "top5_120": 0.58,
        "top10_120": 0.70,
        "hc_120": 80,
        "top1_delta": -0.07,
        "top5_delta": -0.07,
    }


# ───────────────────────── Golden test ─────────────────────────────────


GOLDEN_PATH = (
    "tests/pulse_bot/_golden/entry_features_v20.json"
)


def test_extract_entry_features_matches_golden(tmp_path):
    """Pin the v20 schema feature dict for a fixed input. Any drift
    fails this test with a clear diff."""
    pytest.importorskip("xgboost")  # extractor pulls model bits
    from pulse_bot.ml.features import extract_entry_features

    sr = _make_minimal_scoring_result()
    holder = _make_minimal_holder_snapshot()
    feats = extract_entry_features(
        sr,
        holder_snapshot=holder,
        creator_snapshot=None,
        hour_utc=14,
        wallet_prior_stats={},
        top3_buyer_wallets=[],
        cutoff_ts=0.0,
        n_buyers_first_5s=2.0,
    )
    # Coerce values for stable JSON.
    coerced = {
        k: ("nan" if isinstance(v, float) and math.isnan(v) else float(v))
        for k, v in feats.items()
    }

    import json
    from pathlib import Path

    golden_file = Path(GOLDEN_PATH)
    if os.environ.get("UPDATE_GOLDENS") == "1" or not golden_file.exists():
        golden_file.parent.mkdir(parents=True, exist_ok=True)
        golden_file.write_text(json.dumps(coerced, indent=2, sort_keys=True))
        pytest.skip(
            f"Wrote golden to {golden_file}. Re-run without UPDATE_GOLDENS=1 "
            "to verify."
        )

    expected = json.loads(golden_file.read_text())
    diffs: list[str] = []
    extra = set(coerced) - set(expected)
    missing = set(expected) - set(coerced)
    if missing:
        diffs.append(f"Missing keys (regression): {sorted(missing)}")
    if extra:
        diffs.append(f"Extra keys (likely v21 schema bump): {sorted(extra)}")
    for k in sorted(set(coerced) & set(expected)):
        a, e = coerced[k], expected[k]
        a_nan = isinstance(a, str) and a == "nan"
        e_nan = isinstance(e, str) and e == "nan"
        if a_nan and e_nan:
            continue
        if a_nan != e_nan:
            diffs.append(f"  {k}: NaN mismatch (golden={e}, actual={a})")
            continue
        if not _approx(a, e):
            diffs.append(f"  {k}: golden={e}, actual={a}")
    if diffs:
        msg = (
            f"Feature vector drift detected against {golden_file}:\n"
            + "\n".join(diffs)
            + "\n\nIf the change is intentional, re-record with:\n"
            + f"  UPDATE_GOLDENS=1 pytest {__file__}"
        )
        pytest.fail(msg)
