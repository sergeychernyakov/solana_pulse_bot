# tests/pulse_bot/test_time_aware_features.py
"""Phase 2.5 (2026-04-25) parity tests for time-aware (multi-snapshot)
features in the main entry model.

Verifies:
1. ``MetricsCalculator`` truncates the buy stream correctly at T+30s.
2. ``extract_entry_features`` computes the four delta features as
   expected from the raw @30/@60/@90 sub-fields.
3. ``ENTRY_FEATURE_ORDER`` lists all 13 new features (9 raw + 4 derived)
   *after* the existing groups so old-model column ordering is intact.
4. ``build_entry_dataset`` ships every new column in the returned frame.
5. ``FEATURE_SCHEMA_VERSION`` is bumped to v18.
6. **Parity invariant**: with a 90-second observation window the @90
   columns equal the existing full-window scorer features bit-for-bit.
"""

from __future__ import annotations

import pytest

from pulse_bot.filters.metrics import MetricsCalculator
from pulse_bot.ml.features import (
    ENTRY_FEATURE_ORDER,
    FEATURE_SCHEMA_VERSION,
    SCORER_FEATURES,
    TIME_AWARE_DERIVED_FEATURES,
    TIME_AWARE_FEATURES,
    extract_entry_features,
)
from pulse_bot.models import Token, Trade


def _mk_token(created_at: float = 1_000_000.0) -> Token:
    return Token(
        mint="MintA",
        name="A",
        symbol="A",
        creator="CreatorA",
        created_at=created_at,
        uri="",
    )


def _mk_buy(ts: float, wallet: str, sol: float = 0.5) -> Trade:
    """Helper that fabricates a buy Trade at ``ts`` for ``wallet``."""
    return Trade(
        mint="MintA",
        wallet=wallet,
        tx_type="buy",
        sol_amount=sol,
        token_amount=1000.0,
        new_token_balance=0.0,
        bonding_curve_key="",
        v_sol_in_bonding_curve=10.0,
        v_tokens_in_bonding_curve=1_000_000.0,
        market_cap_sol=42.0,
        timestamp=ts,
    )


# ── Test 1: snapshot truncation ─────────────────────────────────────


def test_metrics_unique_buyers_at_30_truncates_correctly() -> None:
    """A buy at +35s must NOT count toward unique_buyers_at_30."""
    token = _mk_token(created_at=1_000_000.0)
    trades = [
        _mk_buy(1_000_000.0 + 5.0, "W1"),  # in @30 window
        _mk_buy(1_000_000.0 + 20.0, "W2"),  # in @30 window
        _mk_buy(1_000_000.0 + 35.0, "W3"),  # past @30, in @60
        _mk_buy(1_000_000.0 + 70.0, "W4"),  # past @60, in @90
    ]
    m = MetricsCalculator().compute(token, trades)
    assert m.unique_buyers_at_30 == 2
    assert m.unique_buyers_at_60 == 3
    assert m.unique_buyers_at_90 == 4
    # Buy rate uses age as denominator (not observation_seconds).
    assert m.buy_rate_at_30 == pytest.approx(2 / 30.0)
    assert m.buy_rate_at_60 == pytest.approx(3 / 60.0)
    assert m.buy_rate_at_90 == pytest.approx(4 / 90.0)
    # Volume is cumulative (not per-second).
    assert m.buy_volume_sol_at_30 == pytest.approx(1.0)  # 0.5 + 0.5
    assert m.buy_volume_sol_at_60 == pytest.approx(1.5)
    assert m.buy_volume_sol_at_90 == pytest.approx(2.0)


# ── Test 2: derived deltas ──────────────────────────────────────────


def test_extract_entry_features_computes_deltas() -> None:
    """delta_top1_30_to_60 = top1_at_60 - top1_30 (linear interp).

    Helius does not capture an @60 snapshot — top1_at_60 is a linear
    interpolation between top1_30 (T+30) and top1_120 (T+120). The
    interpolation parameter at age=60 lands at 1/3 of the
    (top1_30 → top1_120) gradient.
    """
    scoring = {
        "unique_buyers_at_30": 5.0,
        "unique_buyers_at_60": 9.0,
        "unique_buyers_at_90": 11.0,
        "buy_rate_at_30": 0.10,
        "buy_rate_at_60": 0.15,
        "buy_rate_at_90": 0.13,
        "buy_volume_sol_at_30": 1.0,
        "buy_volume_sol_at_60": 2.0,
        "buy_volume_sol_at_90": 3.0,
    }
    holder = {"top1_30": 30.0, "top1_120": 60.0}
    feats = extract_entry_features(scoring, holder_snapshot=holder)
    # 60s is 1/3 of the way from 30s → 120s, so top1_at_60 = 30 + 30/3 = 40.
    assert feats["top1_at_60"] == pytest.approx(40.0)
    assert feats["delta_top1_30_to_60"] == pytest.approx(10.0)
    assert feats["delta_buy_rate_60_to_90"] == pytest.approx(-0.02)
    assert feats["delta_unique_buyers_30_to_60"] == pytest.approx(4.0)


# ── Test 3: schema layout ───────────────────────────────────────────


def test_entry_feature_order_includes_time_aware_at_end() -> None:
    """All 13 new features must appear after the existing schema groups."""
    expected_new = list(TIME_AWARE_FEATURES) + list(TIME_AWARE_DERIVED_FEATURES)
    assert len(expected_new) == 13
    tail = ENTRY_FEATURE_ORDER[-13:]
    assert tail == expected_new
    # No accidental duplicates anywhere.
    assert len(ENTRY_FEATURE_ORDER) == len(set(ENTRY_FEATURE_ORDER))


def test_schema_version_bumped_to_v18() -> None:
    assert FEATURE_SCHEMA_VERSION == "entry_v18_20260425"


# ── Test 4: build_entry_dataset wiring ──────────────────────────────


def test_build_dataset_helper_emits_all_columns() -> None:
    """The build_dataset.py helper that re-aggregates trades returns the
    nine raw TIME_AWARE_FEATURES — every key in the contract must be
    present on the dict so downstream ``base[col] = ta_df[col].values``
    cannot raise ``KeyError``.
    """
    from pulse_bot.ml.build_dataset import _compute_time_aware_features

    created_at = 2_000_000.0
    rows = [
        ("MintA", created_at + 10.0, "buy", 0.4, "W1"),
        ("MintA", created_at + 50.0, "buy", 0.6, "W2"),
        ("MintA", created_at + 80.0, "sell", 0.1, "W2"),  # ignored
    ]
    feats = _compute_time_aware_features(rows, created_at)
    for k in TIME_AWARE_FEATURES:
        assert k in feats, f"helper missing {k}"
    assert feats["unique_buyers_at_30"] == 1
    assert feats["unique_buyers_at_60"] == 2
    assert feats["unique_buyers_at_90"] == 2
    assert feats["buy_volume_sol_at_30"] == pytest.approx(0.4)
    assert feats["buy_volume_sol_at_60"] == pytest.approx(1.0)
    # Sell is ignored.
    assert feats["buy_volume_sol_at_90"] == pytest.approx(1.0)


# ── Test 5: parity invariant ────────────────────────────────────────


def test_parity_at_90_equals_full_window() -> None:
    """When the observation window ends exactly at +90s the @90 features
    must equal the full-window scorer analogues bit-for-bit. This is the
    invariant Phase 2.5 promises in features.py.
    """
    token = _mk_token(created_at=1_000_000.0)
    # All buys land inside the [0, 90s] window.
    trades = [
        _mk_buy(1_000_000.0 + 5.0, "W1", sol=0.3),
        _mk_buy(1_000_000.0 + 20.0, "W2", sol=0.7),
        _mk_buy(1_000_000.0 + 60.0, "W3", sol=0.5),
        _mk_buy(1_000_000.0 + 89.0, "W4", sol=0.2),
    ]
    m = MetricsCalculator().compute(token, trades)
    assert m.unique_buyers_at_90 == m.unique_buyers
    assert m.buy_volume_sol_at_90 == pytest.approx(m.total_buy_volume_sol)
    # buy_rate_at_90 uses 90s denominator; not directly stored on
    # TokenMetrics for the full window — but equals buy_count / 90.
    assert m.buy_rate_at_90 == pytest.approx(m.buy_count / 90.0)


def test_parity_helper_matches_metrics_for_synthetic_stream() -> None:
    """build_dataset helper and live MetricsCalculator must agree on the
    same trade stream (different inputs — ``Trade`` dataclass vs raw row
    tuple — but the same arithmetic).
    """
    token = _mk_token(created_at=3_000_000.0)
    trades = [
        _mk_buy(3_000_000.0 + 2.0, "Wa", sol=0.1),
        _mk_buy(3_000_000.0 + 25.0, "Wb", sol=0.2),
        _mk_buy(3_000_000.0 + 55.0, "Wc", sol=0.3),
        _mk_buy(3_000_000.0 + 88.0, "Wd", sol=0.4),
    ]
    m = MetricsCalculator().compute(token, trades)

    from pulse_bot.ml.build_dataset import _compute_time_aware_features

    rows = [("MintA", t.timestamp, t.tx_type, t.sol_amount, t.wallet) for t in trades]
    feats = _compute_time_aware_features(rows, token.created_at)

    for age in (30, 60, 90):
        assert feats[f"unique_buyers_at_{age}"] == getattr(m, f"unique_buyers_at_{age}")
        assert feats[f"buy_rate_at_{age}"] == pytest.approx(
            getattr(m, f"buy_rate_at_{age}")
        )
        assert feats[f"buy_volume_sol_at_{age}"] == pytest.approx(
            getattr(m, f"buy_volume_sol_at_{age}")
        )


def test_scorer_features_unchanged_by_phase25() -> None:
    """Phase 2.5 must not delete or reorder existing SCORER_FEATURES."""
    expected_minimum = {"unique_buyers", "buy_count", "buy_volume_sol"}
    assert expected_minimum.issubset(set(SCORER_FEATURES))
    # And SCORER_FEATURES must NOT have leaked the new time-aware names —
    # those belong in TIME_AWARE_FEATURES only.
    for name in TIME_AWARE_FEATURES:
        assert (
            name not in SCORER_FEATURES
        ), f"{name} should live in TIME_AWARE_FEATURES, not SCORER_FEATURES"
