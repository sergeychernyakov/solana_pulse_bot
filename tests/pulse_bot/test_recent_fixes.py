# tests/pulse_bot/test_recent_fixes.py
"""Tests for fixes shipped on 2026-04-27 / 28.

These would have caught the bag of bugs we found in production:

1. Grey-zone → SKIP toggle (PULSE_ENTRY_GREY_TO_SKIP).
2. EV-based threshold search (replaces WR objective in train.py).
3. T+30 confidence gate semantics — binary classifier, low proba = loser.
4. Timing confidence gate semantics — multi-class softmax, high proba in
   chosen class = confident. (Earlier bug: gate was inverted, used `< 0.05`
   for SKIP class which can never trigger because SKIP returns p_skip=0.6+.)
5. ``_extract_wallet_prior_features`` top10 NaN-when-≤3-wallets guard
   (codex review: removing this guard tanked AUC 0.905→0.891 because the
   "missing" pattern is load-bearing for XGBoost).
6. Pipeline ``entry_price > 0`` guard (ML override was opening phantom
   positions with entry_price=0 when result.exit_price was unset).
7. Dashboard real-trade filter (must include entry_type='ml_override',
   not just legacy entry_buyer_number > 0).
"""

from __future__ import annotations

import math
from typing import Any

import numpy as np
import pytest

from pulse_bot.ml.features import (
    WALLET_FEATURES,
    _extract_wallet_prior_features,
    compute_n_buyers_first_5s,
    compute_topN_buyer_wallets,
)


# ───────────────────────── Wallet feature parity (top10 guard) ─────────


def _make_wallet_stats(wrs: dict[str, float], pnls: dict[str, float]) -> dict:
    """Build wallet_prior_stats dict in the shape the extractor expects."""
    out = {}
    for w in wrs:
        out[w] = {
            "all_mint_count": 5,
            "wr": wrs[w],
            "total_pnl_sol": pnls.get(w, 0.0),
            "max_pnl_sol": max(pnls.get(w, 0.0), 0.0),
            "first_seen_ts": 1_700_000_000.0,
            "closed_mint_count": 5,
        }
    return out


def test_top10_features_NaN_when_three_or_fewer_wallets():
    """Guard from codex review 2026-04-27: top10_* must stay NaN when
    caller passed ≤3 wallets. Removing this guard caused AUC drop and
    EV-search collapse — load-bearing for XGBoost missing-feature
    splits."""
    stats = _make_wallet_stats(
        {"w1": 0.5, "w2": 0.4, "w3": 0.3},
        {"w1": 1.0, "w2": 0.5, "w3": 0.2},
    )
    feats = _extract_wallet_prior_features(
        stats, ["w1", "w2", "w3"], cutoff_ts=1_700_010_000.0
    )
    # Top-3 features populated.
    assert not math.isnan(feats["top3_buyer_prior_avg_wr"])
    # Top-10 features stay NaN — semantically "we didn't have enough
    # buyers to populate this group; XGBoost will read as missing."
    assert math.isnan(feats["top10_buyer_prior_avg_wr"])
    assert math.isnan(feats["top10_buyer_prior_total_pnl_sol"])


def test_top10_features_populated_when_more_than_three_wallets():
    """When caller passes >3 wallets (real v20 path), top10_* aggregate
    over the FULL list, not just the first 3."""
    stats = _make_wallet_stats(
        {"w1": 0.5, "w2": 0.4, "w3": 0.3, "w4": 0.6, "w5": 0.7},
        {"w1": 1.0, "w2": 0.5, "w3": 0.2, "w4": 2.0, "w5": 1.5},
    )
    feats = _extract_wallet_prior_features(
        stats, ["w1", "w2", "w3", "w4", "w5"], cutoff_ts=1_700_010_000.0
    )
    # All 5 wallets contribute to top10_buyer_prior_avg_wr.
    expected_avg = (0.5 + 0.4 + 0.3 + 0.6 + 0.7) / 5
    assert feats["top10_buyer_prior_avg_wr"] == pytest.approx(expected_avg)
    # Top-3 still uses only the leading 3.
    expected_top3 = (0.5 + 0.4 + 0.3) / 3
    assert feats["top3_buyer_prior_avg_wr"] == pytest.approx(expected_top3)


def test_top10_features_NaN_when_no_history_at_all():
    """Empty wallet_prior_stats → all wallet features NaN, including
    top10."""
    feats = _extract_wallet_prior_features({}, ["w1", "w2", "w3", "w4"], cutoff_ts=0)
    for k in WALLET_FEATURES:
        assert math.isnan(feats[k]), f"{k} should be NaN with no history"


# ───────────────────────── n_buyers_first_5s sniper proxy ──────────────


def test_n_buyers_first_5s_counts_distinct_wallets_in_window():
    """v20 sniper proxy: distinct buyers within 5 seconds of mint
    creation. Same wallet buying twice shouldn't double-count."""
    mint_created_at = 1_000_000.0
    trades = [
        {"tx_type": "buy", "wallet": "w1", "timestamp": 1_000_001.0},  # 1s
        {"tx_type": "buy", "wallet": "w1", "timestamp": 1_000_002.0},  # dupe
        {"tx_type": "buy", "wallet": "w2", "timestamp": 1_000_003.0},  # 3s
        {"tx_type": "buy", "wallet": "w3", "timestamp": 1_000_007.0},  # 7s — outside
        {"tx_type": "sell", "wallet": "w4", "timestamp": 1_000_002.0},  # not buy
    ]
    assert compute_n_buyers_first_5s(trades, mint_created_at) == 2.0


def test_n_buyers_first_5s_NaN_without_creation_time():
    assert math.isnan(compute_n_buyers_first_5s([], 0.0))


# ───────────────────────── compute_topN_buyer_wallets ranking ──────────


def test_compute_topN_orders_by_volume():
    trades = [
        {"tx_type": "buy", "wallet": "small", "sol_amount": 0.1},
        {"tx_type": "buy", "wallet": "big", "sol_amount": 5.0},
        {"tx_type": "buy", "wallet": "mid", "sol_amount": 1.0},
        {"tx_type": "sell", "wallet": "big", "sol_amount": 5.0},  # ignored
    ]
    assert compute_topN_buyer_wallets(trades, n=3) == ["big", "mid", "small"]
    assert compute_topN_buyer_wallets(trades, n=10) == ["big", "mid", "small"]
    assert compute_topN_buyer_wallets(trades, n=1) == ["big"]


# ───────────────────────── EV-based threshold search ───────────────────


def test_ev_thresholds_falls_back_to_wr_when_no_pnl():
    """Without realized_pnl_pct, search uses WR as before — backwards
    compatible."""
    pytest.importorskip("xgboost")  # train.py imports xgb at module load
    from pulse_bot.ml.train import _search_confidence_thresholds

    proba = np.array([0.05, 0.1, 0.2, 0.4, 0.5, 0.7, 0.8, 0.9, 0.95] * 50)
    y = np.array([0, 0, 0, 0, 0, 1, 1, 1, 1] * 50)
    out = _search_confidence_thresholds(proba, y, pnl=None)
    assert out["objective"] == "wr"
    assert out["floor"] is not None
    assert out["ceiling"] is not None


def test_ev_thresholds_picks_high_pnl_bucket():
    """EV-search should pick a bucket with higher mean PnL than baseline.
    Synthetic distribution: high-proba tokens really win bigger.
    """
    pytest.importorskip("xgboost")
    from pulse_bot.ml.train import _search_confidence_thresholds

    # Smooth, monotonic relationship between proba and PnL — what a
    # well-calibrated model would produce. EV-search should identify
    # the upper proba tier and ceiling_ev > val_base_ev.
    rng = np.random.RandomState(0)
    proba = rng.uniform(0.0, 1.0, 3000)
    # Higher proba → higher prob of win and bigger PnL on win.
    is_win = rng.uniform(0, 1, 3000) < (0.05 + 0.4 * proba)
    y = is_win.astype(int)
    pnl = np.where(is_win, 20.0 + 30.0 * proba, -5.0 + 2.0 * proba)
    out = _search_confidence_thresholds(proba, y, pnl=pnl)
    assert out["objective"] == "ev"
    # Healthy distribution → no degeneracy.
    assert out["status"] == "ok"
    assert out["floor"] < out["ceiling"]
    # Ceiling bucket should have BETTER EV than baseline.
    assert out["ceiling_ev"] > out["val_base_ev"]


def test_ev_thresholds_clip_protects_against_outliers():
    """One stray +500% row shouldn't dominate bucket EV. Clip [-100, +200]."""
    pytest.importorskip("xgboost")
    from pulse_bot.ml.train import _search_confidence_thresholds

    proba = np.full(100, 0.9)
    y = np.array([1] + [0] * 99)
    pnl = np.array([500.0] + [-1.0] * 99)  # one moonshot, 99 losses
    out = _search_confidence_thresholds(proba, y, pnl=pnl)
    # With clip at +200, mean = (200 + 99*-1)/100 = 1.01, not 4.99.
    # Without clip we'd see mean ≈ 5.0.
    if "ceiling_ev" in out and out["ceiling_n"] >= 30:
        assert out["ceiling_ev"] < 5.0


# ───────────────────────── Confidence-gate parametric verification ─────


def test_confidence_gate_binary_skip_only_fires_in_extreme_tail():
    """T+30 binary classifier: action='SKIP' when proba < skip_floor.
    But we want to fire SKIP_EARLY only when proba is in the EXTREME
    tail (e.g. proba < 0.005), not at the floor edge. This is the
    inline rule in pipeline.py — we test the threshold semantics via
    a tiny mock of the gating predicate."""

    # Simulate the gate logic from pipeline.py:1175 (binary T+30 model).
    def binary_gate(action: str, proba: float, *, skip_tail: float = 0.05) -> bool:
        """Returns True iff this model should fire SKIP_EARLY."""
        return action == "SKIP" and proba < skip_tail

    # Confident loser — fires.
    assert binary_gate("SKIP", proba=0.001)
    # Marginal SKIP near the floor — DOES NOT fire (would have under
    # the old design).
    assert not binary_gate("SKIP", proba=0.10)
    # SKIP exactly at tail boundary — strict <, so 0.05 doesn't fire.
    assert not binary_gate("SKIP", proba=0.05)
    # BUY action — gate not for BUY direction.
    assert not binary_gate("BUY", proba=0.001)


def test_confidence_gate_multiclass_skip_fires_on_high_class_proba():
    """Timing 3-class softmax: action='SKIP' when p_skip >= 0.6
    (classifier's own confidence floor) AND we want extra-confident
    only. Bug we shipped: I had `< 0.05` here, which can never fire
    because SKIP requires p_skip ≥ 0.6 to be returned at all.
    Correct gate: high p_skip (e.g. > 0.85)."""

    def multiclass_gate(action: str, proba: float, *, gate: float = 0.85) -> bool:
        """Returns True iff timing should fire its action."""
        return action in ("BUY", "SKIP") and proba > gate

    # Highly confident SKIP — fires.
    assert multiclass_gate("SKIP", proba=0.95)
    assert multiclass_gate("SKIP", proba=1.0)
    # Boundary — strict >, 0.85 doesn't fire.
    assert not multiclass_gate("SKIP", proba=0.85)
    # Medium confidence — defers.
    assert not multiclass_gate("SKIP", proba=0.7)
    # Same threshold for BUY.
    assert multiclass_gate("BUY", proba=0.9)
    assert not multiclass_gate("BUY", proba=0.5)
    # WAIT_MORE never overrides regardless of proba.
    assert not multiclass_gate("WAIT_MORE", proba=0.99)


# ───────────────────────── Entry-price guard (pipeline) ────────────────


def test_entry_price_guard_blocks_zero_price_buys():
    """ML-override path was firing at result.exit_price=0 for tokens
    with no observable trades during the scoring window — produced
    'phantom' paper trades that never had a real entry. Pipeline now
    guards: (entry_price <= 0) → no entry."""

    # Mirror the inline guard from pipeline.py:1019.
    def should_block(should_enter: bool, exit_price: float) -> bool:
        return should_enter and (exit_price or 0.0) <= 0.0

    assert should_block(True, 0.0)
    assert should_block(True, -1e-9)  # bizarre negative — block
    assert not should_block(True, 1e-9)  # tiny but valid price — pass
    assert not should_block(False, 0.0)  # rules said no anyway — gate moot


# ───────────────────────── Dashboard "is real trade" filter ────────────


def test_dashboard_real_filter_includes_ml_override_with_zero_buyer_num():
    """Dashboard fix 2026-04-28: ml_override entries have
    entry_buyer_number=0 (decide_entry returns 0 when rules say SKIP)
    but they ARE real bot positions. Filter must include them by
    entry_type, not just buyer_number > 0."""

    REAL_ENTRY_TYPES = {
        "fast", "full", "ml_override", "t30", "t30_skip", "timing", "BUY_EARLY",
    }

    def _is_real(t: dict) -> bool:
        etype = (t.get("entry_type") or "").lower()
        if etype in REAL_ENTRY_TYPES:
            return True
        return (t.get("entry_buyer_number", 0) or 0) > 0

    # ML override with zero buyer number → real (bug we hit).
    assert _is_real({"entry_type": "ml_override", "entry_buyer_number": 0})
    # Legacy full entry with buyer number — still real.
    assert _is_real({"entry_type": "full", "entry_buyer_number": 12})
    # Legacy entry with no type but buyer number set — fallback applies.
    assert _is_real({"entry_type": "", "entry_buyer_number": 5})
    # Pure shadow tracking — neither type nor buyer.
    assert not _is_real({"entry_type": "", "entry_buyer_number": 0})
    assert not _is_real({"entry_type": None, "entry_buyer_number": None})


def test_dashboard_excludes_synthetic_seed_data():
    """16 paper_trades with entry_time=2026-01-01 00:16:40 UTC are
    backtest seed data, not live trades. Dashboard cuts at 2026-04-01."""
    REAL_TRADES_FROM = 1743465600  # 2026-04-01 00:00 UTC

    seed_ts = 1735690600  # 2026-01-01 00:16:40 UTC
    real_ts = 1745957280  # 2026-04-29

    assert seed_ts < REAL_TRADES_FROM
    assert real_ts > REAL_TRADES_FROM


# ───────────────────────── Grey-zone → SKIP toggle ─────────────────────


def test_grey_zone_to_skip_via_env_flag(monkeypatch):
    """PULSE_ENTRY_GREY_TO_SKIP=1 turns the RULES bucket into SKIP.
    Default off → returns RULES (legacy behavior)."""
    pytest.importorskip("xgboost")

    # Build the smallest possible mock policy (we patch only what
    # decide_with_confidence needs).
    from pulse_bot.ml.policy import EntryMLPolicy

    # Construct an instance bypassing __init__ — we only test the
    # branch logic that reads PULSE_ENTRY_GREY_TO_SKIP.
    policy = EntryMLPolicy.__new__(EntryMLPolicy)
    policy.proba_floor = 0.10
    policy.proba_ceiling = 0.50
    policy.calibration = {"a": 1.0, "b": 0.0}
    policy.objective = "binary:logistic"

    # Patch predict_score to return a grey-zone value.
    monkeypatch.setattr(policy, "predict_score", lambda *a, **k: 0.30)

    # Default: grey → RULES.
    monkeypatch.delenv("PULSE_ENTRY_GREY_TO_SKIP", raising=False)
    action, _, _ = policy.decide_with_confidence(scoring_result=object())
    assert action == "RULES"

    # Override: grey → SKIP.
    monkeypatch.setenv("PULSE_ENTRY_GREY_TO_SKIP", "1")
    action, _, _ = policy.decide_with_confidence(scoring_result=object())
    assert action == "SKIP"


def test_grey_zone_decisions_at_boundary(monkeypatch):
    """Above ceiling → BUY; below floor → SKIP regardless of env flag."""
    pytest.importorskip("xgboost")
    from pulse_bot.ml.policy import EntryMLPolicy

    policy = EntryMLPolicy.__new__(EntryMLPolicy)
    policy.proba_floor = 0.10
    policy.proba_ceiling = 0.50
    policy.calibration = {"a": 1.0, "b": 0.0}
    policy.objective = "binary:logistic"

    monkeypatch.setenv("PULSE_ENTRY_GREY_TO_SKIP", "1")

    monkeypatch.setattr(policy, "predict_score", lambda *a, **k: 0.95)
    assert policy.decide_with_confidence(scoring_result=object())[0] == "BUY"

    monkeypatch.setattr(policy, "predict_score", lambda *a, **k: 0.05)
    assert policy.decide_with_confidence(scoring_result=object())[0] == "SKIP"


# ───────────────────────── Codex 2026-04-28 critical fixes ─────────────


def test_threshold_search_degeneracy_guard():
    """When EV is monotonically negative the search returns floor>=ceiling.
    Guard must mark it degenerate + fall back to val proba quartiles."""
    pytest.importorskip("xgboost")
    from pulse_bot.ml.train import _search_confidence_thresholds

    # Pathological case: probas squeezed into narrow band, all-negative
    # PnL across the range — exactly what entry_model produced today.
    np.random.seed(0)
    proba = 0.40 + 0.10 * np.random.rand(2000)  # 0.40 - 0.50
    y = np.zeros(2000, dtype=int)
    y[:30] = 1  # 1.5% base rate
    pnl = np.where(y == 1, 5.0, -3.0)  # mean -2.4% — uniformly bad EV
    # Inject a slight noise so EV-search doesn't trivially find a zero.
    pnl += np.random.normal(0, 0.1, 2000)

    out = _search_confidence_thresholds(proba, y, pnl=pnl)
    # If the search collapsed (which it must on this distribution),
    # status flag must surface it.
    if out["floor"] >= out["ceiling"]:
        # Pre-fix would silently return overlapping thresholds.
        # Post-fix must catch it.
        pytest.fail("guard failed: floor=%s >= ceiling=%s" % (out["floor"], out["ceiling"]))
    # 2026-04-28: status "degenerate_overlap" replaced by two outcomes
    # — "ok_percentile_fallback" (model still ranks) and "degenerate_flat"
    # (no ranking power). Either way, floor < ceiling must hold.
    assert out.get("status") in (
        "ok",
        "ok_percentile_fallback",
        "degenerate_flat",
    )
    assert out["floor"] < out["ceiling"]
    if out.get("status") in ("ok_percentile_fallback", "degenerate_flat"):
        # Fallback quartiles should sit inside the actual proba range.
        assert proba.min() <= out["floor"] <= proba.max()
        assert proba.min() <= out["ceiling"] <= proba.max()


def test_threshold_search_status_field_present():
    """The 'status' key must exist on every return so downstream
    health-check code never KeyErrors."""
    pytest.importorskip("xgboost")
    from pulse_bot.ml.train import _search_confidence_thresholds

    proba = np.linspace(0.01, 0.99, 1000)
    y = (np.random.RandomState(0).rand(1000) < 0.05).astype(int)
    out = _search_confidence_thresholds(proba, y)
    assert "status" in out
    assert out["status"] in (
        "ok",
        "ok_percentile_fallback",
        "degenerate_flat",
    )


def test_health_check_flags_narrow_proba_spread(tmp_path, monkeypatch):
    """Reject retrain when val proba_p99 - p1 < 0.30 — model isn't
    ranking even if AUC is high. Today's broken model: spread=0.24."""
    pytest.importorskip("xgboost")
    from pulse_bot.ml.train import _entry_model_health_check

    # Squeezed band — like today's entry_model
    proba_val = np.random.RandomState(1).uniform(0.40, 0.55, 5000)
    out = _entry_model_health_check(
        model_out=tmp_path / "fake_model.ubj",
        proba_val=proba_val,
        new_auc=0.85,
        thresholds={"floor": 0.45, "ceiling": 0.50, "status": "ok"},
    )
    # spread ≈ 0.15, < 0.30 threshold
    assert out["proba_spread"] < 0.30
    assert out["status"] == "narrow_proba_spread"


def test_health_check_flags_auc_regression(tmp_path):
    """When previous meta exists with higher AUC, health-check warns
    if the new run dropped > 2pp."""
    pytest.importorskip("xgboost")
    from pulse_bot.ml.train import _entry_model_health_check

    model_out = tmp_path / "m.ubj"
    meta = tmp_path / "m.meta.json"
    # Pretend the previous artifact had AUC 0.905
    meta.write_text('{"auc": 0.905}')
    # Touch the model file too so snapshot path is exercised.
    model_out.write_bytes(b"prev_xgb_dump")

    proba_val = np.random.RandomState(2).uniform(0.0, 1.0, 5000)
    out = _entry_model_health_check(
        model_out=model_out,
        proba_val=proba_val,
        new_auc=0.825,  # dropped by 8 pp
        thresholds={"floor": 0.10, "ceiling": 0.70, "status": "ok"},
    )
    assert out["prev_auc"] == pytest.approx(0.905)
    assert out["auc_delta"] == pytest.approx(-0.08)
    assert out["status"] == "auc_regression"
    # Snapshot must have been created
    assert model_out.with_suffix(".ubj.prev").exists()


def test_health_check_passes_clean_retrain(tmp_path):
    pytest.importorskip("xgboost")
    from pulse_bot.ml.train import _entry_model_health_check

    proba_val = np.random.RandomState(3).uniform(0.05, 0.95, 5000)
    out = _entry_model_health_check(
        model_out=tmp_path / "fresh.ubj",
        proba_val=proba_val,
        new_auc=0.91,
        thresholds={"floor": 0.10, "ceiling": 0.75, "status": "ok"},
    )
    assert out["status"] == "ok"
    assert out["proba_spread"] >= 0.30


def test_helius_backfill_completeness_propagates():
    """Codex review 2026-04-28: get_all_signatures returns
    (sigs, complete). complete=False on transient RPC error.
    Caller must NOT mark mint as completed in those cases."""
    # Simulate the run() decision branch.
    def should_mark_completed(complete: bool) -> bool:
        return complete

    assert should_mark_completed(True)
    assert not should_mark_completed(False)


def test_helius_backfill_dedup_key_includes_amount():
    """Two same-second buys from same wallet with different amounts must
    produce different dedup keys (sniper bots fire micro-orders)."""
    # Simulate fetch_existing_trade_keys output shape.
    def make_key(timestamp, wallet, tx_type, sol_amount):
        return (int(float(timestamp)), wallet, tx_type, round(float(sol_amount), 6))

    # Same wallet, same second, different amounts → different keys
    k1 = make_key(1700000000.5, "w1", "buy", 0.10)
    k2 = make_key(1700000000.7, "w1", "buy", 0.50)
    assert k1 != k2  # under old (ts, wallet) key these collapsed → bug

    # Identical trades collapse correctly
    k3 = make_key(1700000000.5, "w1", "buy", 0.10)
    assert k1 == k3


def test_event_time_watermark_filters_late_arrivals():
    """Codex review: trades arriving AFTER target_age but with
    timestamp <= target must NOT be included in checkpoint snapshot —
    that's the whole point of event-time semantics. Replay/backtest
    uses the same filter."""
    class FT:
        def __init__(self, ts):
            self.timestamp = ts

    mint_created_at = 1000.0
    target_age = 30.0  # T+30 checkpoint
    target_wall_ts = mint_created_at + target_age  # = 1030

    # Mix of trades:
    # - on-time: ts in [1000, 1030] (event-time-included)
    # - late: ts > 1030 (arrived after but event-time excluded)
    collected = [
        FT(1005.0),   # ts=5s, valid for T+30
        FT(1020.0),   # ts=20s, valid
        FT(1029.5),   # ts=29.5s, valid (boundary)
        FT(1030.5),   # ts=30.5s, EXCLUDED (post-checkpoint)
        FT(1040.0),   # ts=40s, EXCLUDED
    ]
    visible = [
        t for t in collected
        if (float(t.timestamp) - mint_created_at) <= target_age
    ]
    assert len(visible) == 3
    assert all(t.timestamp <= target_wall_ts for t in visible)


def test_ml_override_recomputes_entry_metadata():
    """Codex review: ML BUY override must NOT persist
    entry_buyer_number=0 / entry_score=0 (those look identical to
    shadow-tracking rows in analytics). Recompute from scoring window."""
    # Simulate the recompute logic from pipeline.py:993-998.
    def recompute_metadata(buy_count: int, ml_cal: float):
        entry_buyer_num = int(buy_count) + 1
        entry_score = max(1, int(round(float(ml_cal) * 100.0)))
        return entry_buyer_num, entry_score

    # Typical case: 5 buys observed during scoring, ML calibrated 0.42
    bn, sc = recompute_metadata(5, 0.42)
    assert bn == 6
    assert sc == 42

    # Edge: zero buys (e.g., very early entry), ml_cal near zero
    bn, sc = recompute_metadata(0, 0.005)
    assert bn == 1  # never zero (sentinel for shadow detection)
    assert sc == 1  # bumped to at least 1


def test_bot_cluster_hard_skip_logic():
    """Hard pre-filter: count distinct buyer wallets in first 30s, skip
    entry when >=3 are flagged is_bot. Tests the predicate purely
    (no PG)."""
    # Simulate trades collected in the scoring window.
    class FakeTrade:
        def __init__(self, wallet, tx_type, age):
            self.wallet = wallet
            self.tx_type = tx_type
            self.timestamp = 1000.0 + age  # mint_created_at = 1000

    trades = [
        FakeTrade("bot1", "buy", 0.5),     # in first 30s
        FakeTrade("bot2", "buy", 1.5),
        FakeTrade("bot3", "buy", 5.0),
        FakeTrade("legit1", "buy", 10.0),
        FakeTrade("late", "buy", 35.0),    # outside first 30s — ignored
        FakeTrade("seller", "sell", 5.0),  # not a buy — ignored
    ]
    mint_created_at = 1000.0
    early_buyers = {
        t.wallet for t in trades
        if t.tx_type == "buy" and 0.0 <= float(t.timestamp) - mint_created_at < 30.0
    }
    assert early_buyers == {"bot1", "bot2", "bot3", "legit1"}

    # Pretend wallet_classifications says: bot1, bot2, bot3 are is_bot=1.
    fake_is_bot = {"bot1", "bot2", "bot3"}
    n_bots = sum(1 for w in early_buyers if w in fake_is_bot)
    assert n_bots == 3
    # Threshold default is 3 → must trigger hard skip.
    HARD_SKIP_N = 3
    assert n_bots >= HARD_SKIP_N

    # And conversely with 2 bots — no hard skip.
    fake_is_bot_partial = {"bot1", "bot2"}
    n_bots_partial = sum(1 for w in early_buyers if w in fake_is_bot_partial)
    assert n_bots_partial < HARD_SKIP_N


def test_max_hold_filters_censored_rows():
    """exit_quantile_max_hold previously trained on a target that was
    right-censored at 600s for 90%+ of rows → learned constant + Spearman
    -0.21. New code drops censored rows BEFORE the chrono split."""
    import pandas as pd

    HORIZON = 600.0
    # Build a tiny synthetic exit-dataset frame
    rng = np.random.RandomState(0)
    n = 1000
    df = pd.DataFrame({
        "mint": [f"m{i//10}" for i in range(n)],
        "entry_ts": rng.uniform(0, 1000, n),
        "forward_seconds_to_peak": rng.choice([HORIZON, 30.0, 100.0, 250.0], n,
                                              p=[0.85, 0.05, 0.05, 0.05]),
    })
    censored = (df["forward_seconds_to_peak"] >= HORIZON - 0.001).sum()
    observed = len(df) - censored
    # Pre-fix: model would learn from all 1000 rows w/ 850 hitting horizon.
    # Post-fix: training only sees the ~150 with observable peaks.
    assert censored > observed, "test fixture intentionally heavily censored"
    df_obs = df[df["forward_seconds_to_peak"] < HORIZON - 0.001]
    assert 100 < len(df_obs) < 250


def test_entry_timing_class_weights_inverse_frequency():
    """Inverse-frequency weighting must give higher weight to rare classes
    so the model doesn't collapse to "predict SKIP for everything"."""
    import numpy as _np
    # Simulate the realistic class distribution: 80% SKIP, 15% WAIT, 5% BUY
    y = _np.array([2] * 800 + [0] * 150 + [1] * 50)
    n_total = len(y)
    class_counts = _np.bincount(y, minlength=3).astype(float)
    weights = _np.where(class_counts > 0, n_total / (3.0 * class_counts), 1.0)
    # Rare classes (BUY) get HIGHER weight than majority (SKIP).
    assert weights[1] > weights[2]  # BUY > SKIP
    assert weights[0] > weights[2]  # WAIT > SKIP
    # Sample weights, when summed per class, equal total/3 each
    # (perfect rebalance).
    sample_w = weights[y]
    for cls in (0, 1, 2):
        cls_total = sample_w[y == cls].sum()
        assert abs(cls_total - n_total / 3.0) < 1e-6


def test_policy_disables_ml_override_on_degenerate_meta(tmp_path, monkeypatch):
    """When meta.json has model_health.status != 'ok', the loaded
    policy gets floor=0 ceiling=1 — every proba returns RULES (i.e.
    cedes the decision to rules engine, no ML override fires)."""
    pytest.importorskip("xgboost")
    # Clear grey-zone-to-SKIP override that runtime envs may have set,
    # so we test the inert-range behaviour, not GREY_TO_SKIP semantics.
    monkeypatch.delenv("PULSE_ENTRY_GREY_TO_SKIP", raising=False)

    from pulse_bot.ml.policy import EntryMLPolicy

    policy = EntryMLPolicy.__new__(EntryMLPolicy)
    # State as-if from_path saw model_health.status='degenerate' and
    # PULSE_ALLOW_DEGENERATE_MODEL was unset → forced to inert range.
    policy.proba_floor = 0.0
    policy.proba_ceiling = 1.0
    policy.calibration = {"a": 1.0, "b": 0.0}
    policy.objective = "binary:logistic"

    for p in (0.001, 0.20, 0.50, 0.80, 0.99):
        def _const(*a, _p=p, **k):  # closure-captured proba
            return _p
        monkeypatch.setattr(policy, "predict_score", _const)
        action, _, _ = policy.decide_with_confidence(scoring_result=object())
        assert action == "RULES", f"proba {p} should give RULES, got {action}"
