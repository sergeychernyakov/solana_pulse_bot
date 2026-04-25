# tests/pulse_bot/test_entry_timing.py
"""Phase 5 entry-timing classifier — unit tests.

Covers:

1. Snapshot generation cardinality.
2. Positive-outcome token labelled BUY_NOW at the right checkpoint.
3. DOA token → all SKIP.
4. End-to-end synthetic train + save (200 tokens × 6 snapshots).
5. ``predict_entry_timing`` returns a valid 3-class probability vector.

These are pure CPU tests — no Postgres, no network. ``simulate_exit``
runs synchronously over hand-crafted ``Trade`` lists.
"""

from __future__ import annotations

import math
import random
from pathlib import Path

import pytest

from pulse_bot.config import get_config
from pulse_bot.ml.entry_timing import (
    CLASS_BUY_NOW,
    CLASS_NAMES,
    CLASS_SKIP,
    DEFAULT_SNAPSHOT_TIMES_SEC,
    TIMING_FEATURE_ORDER,
    EntryTimingLabelBuilder,
    TimingPrediction,
    extract_snapshot_features,
    predict_entry_timing,
    train_entry_timing,
)
from pulse_bot.models import Trade

# ── Helpers ─────────────────────────────────────────────────────────


def _trade(
    *,
    mint: str = "M",
    wallet: str = "W",
    tx_type: str = "buy",
    sol_amount: float = 0.5,
    token_amount: float = 1_000_000.0,
    market_cap_sol: float = 30.0,
    timestamp: float = 1.0,
    v_sol: float = 30.0,
    v_tokens: float = 1e9,
    is_creator: bool = False,
) -> Trade:
    """Build a Trade with sensible defaults (price = v_sol/v_tokens)."""
    return Trade(
        mint=mint,
        wallet=wallet,
        tx_type=tx_type,
        sol_amount=sol_amount,
        token_amount=token_amount,
        new_token_balance=0.0,
        bonding_curve_key="bc",
        v_sol_in_bonding_curve=v_sol,
        v_tokens_in_bonding_curve=v_tokens,
        market_cap_sol=market_cap_sol,
        timestamp=timestamp,
        is_creator=is_creator,
    )


def _winner_trades(token_created_at: float, n_buys: int = 60) -> list[Trade]:
    """Strong buyer rush — large MC growth across 90s and beyond.

    Triggers a positive simulated exit (TP / trailing / good current
    price at timeout) at every checkpoint.
    """
    trades: list[Trade] = []
    for i in range(n_buys):
        # Linear MC growth from 30 → 250 across 0..120s post-creation.
        rel = (i + 1) / n_buys * 120.0
        mc = 30.0 + rel * 4.0
        v_sol = 30.0 + rel * 4.0
        trades.append(
            _trade(
                wallet=f"buyer_{i}",
                tx_type="buy",
                sol_amount=0.8,
                market_cap_sol=mc,
                timestamp=token_created_at + rel,
                v_sol=v_sol,
                v_tokens=1e9,
            )
        )
    return trades


def _doa_trades(token_created_at: float) -> list[Trade]:
    """Empty stream — the on-chain "no buyers showed up" pattern."""
    return []


# ── Test 1: snapshot cardinality ────────────────────────────────────


def test_label_builder_emits_one_snapshot_per_checkpoint() -> None:
    """One :class:`TimingSnapshot` per checkpoint, in checkpoint order."""
    cfg = get_config()
    builder = EntryTimingLabelBuilder(config=cfg)
    created_at = 1_000_000.0
    trades = _winner_trades(created_at)

    snaps = builder.build_for_token("MINT_A", trades, created_at)

    assert len(snaps) == len(DEFAULT_SNAPSHOT_TIMES_SEC) == 6
    assert [s.snapshot_t for s in snaps] == list(DEFAULT_SNAPSHOT_TIMES_SEC)
    assert all(s.mint == "MINT_A" for s in snaps)
    # Features keys complete and ordered.
    for s in snaps:
        assert set(s.features.keys()) == set(TIMING_FEATURE_ORDER)


# ── Test 2: positive token gets BUY_NOW at some checkpoint ──────────


def test_winner_token_gets_buy_now_label() -> None:
    """A token whose simulated exit is profitable has at least one
    BUY_NOW (or WAIT_MORE) label and never SKIP at any checkpoint."""
    cfg = get_config()
    builder = EntryTimingLabelBuilder(config=cfg)
    created_at = 1_000_000.0
    trades = _winner_trades(created_at)

    snaps = builder.build_for_token("WINNER", trades, created_at)
    labels = [s.label for s in snaps]

    # No SKIPs on a clearly-profitable token.
    assert CLASS_SKIP not in labels, f"unexpected SKIP labels: {labels}"
    # At least the last snapshot should be BUY_NOW (positive PnL, no
    # next-snapshot to compare against → unconditional BUY).
    assert labels[-1] == CLASS_BUY_NOW
    assert snaps[-1].pnl_pct_at_t > 0.0


# ── Test 3: DOA token → all SKIP ────────────────────────────────────


def test_doa_token_all_skip() -> None:
    """No trades = DOA. Every snapshot must label SKIP.

    The label builder has a positive PnL gate. When ``simulate_exit``
    sees an empty stream the runner returns ``timeout_result()`` with
    ``current_price == entry_price`` ⇒ pnl_pct ≈ 0 — but with fees and
    slippage it ends up below the 0% line. We pin SKIP behavior by
    pushing the negative threshold up so any zero/negative outcome is
    SKIP (this is the conservative live default we want anyway).
    """
    cfg = get_config()
    builder = EntryTimingLabelBuilder(config=cfg)
    created_at = 1_000_000.0

    snaps = builder.build_for_token("DOA", _doa_trades(created_at), created_at)

    assert len(snaps) == 6
    # No visible trades at any checkpoint ⇒ structural SKIP (sentinel).
    assert all(s.label == CLASS_SKIP for s in snaps)
    assert all(s.pnl_pct_at_t < -100.0 for s in snaps)


# ── Test 4: end-to-end synthetic training ───────────────────────────


def _build_synthetic_corpus(
    n_winners: int = 100,
    n_losers: int = 100,
    seed: int = 0,
) -> list[tuple[str, list[Trade], float]]:
    """Return ``(mint, trades, created_at)`` triples. Mix of winners + DOA."""
    rng = random.Random(seed)
    out: list[tuple[str, list[Trade], float]] = []
    for i in range(n_winners):
        created = 1_000_000.0 + i * 1000.0
        # Add per-token noise so trees actually have something to split on.
        n_buys = 50 + rng.randint(0, 20)
        out.append((f"WIN_{i}", _winner_trades(created, n_buys=n_buys), created))
    for i in range(n_losers):
        created = 2_000_000.0 + i * 1000.0
        out.append((f"DOA_{i}", _doa_trades(created), created))
    return out


def test_train_entry_timing_synthetic(tmp_path: Path) -> None:
    """Train on 200 tokens × 6 snapshots, save model, check meta."""
    cfg = get_config()
    builder = EntryTimingLabelBuilder(config=cfg)
    corpus = _build_synthetic_corpus(n_winners=100, n_losers=100, seed=42)

    snaps = builder.build_for_corpus(corpus)
    assert len(snaps) == 200 * 6 == 1200

    model_out = tmp_path / "entry_timing.ubj"
    meta = train_entry_timing(snaps, model_out)

    assert model_out.exists()
    assert model_out.with_suffix(".meta.json").exists()
    assert meta["n_rows"] == 1200
    assert meta["features"] == list(TIMING_FEATURE_ORDER)
    # We expect at least 2 classes present (WAIT_MORE + SKIP at minimum).
    counts = meta["class_counts"]
    nonzero = [c for c in (0, 1, 2) if counts.get(c, 0) > 0]
    assert len(nonzero) >= 2, f"only one class observed: {counts}"


# ── Test 5: predict returns valid 3-class probability vector ────────


def test_predict_returns_valid_probability_vector(tmp_path: Path) -> None:
    """predict_entry_timing returns probas that sum to ~1 and a class name."""
    cfg = get_config()
    builder = EntryTimingLabelBuilder(
        config=cfg,
        neg_pnl_threshold_pct=-0.001,
    )
    corpus = _build_synthetic_corpus(n_winners=50, n_losers=50, seed=7)
    snaps = builder.build_for_corpus(corpus)

    model_out = tmp_path / "entry_timing.ubj"
    train_entry_timing(snaps, model_out)

    # Pick a snapshot from a winner and ask the model.
    winner_snap = next(s for s in snaps if s.mint.startswith("WIN_"))
    pred = predict_entry_timing(winner_snap.features, model_out)

    assert isinstance(pred, TimingPrediction)
    p_wait, p_buy, p_skip = pred.as_vector()
    assert 0.0 <= p_wait <= 1.0
    assert 0.0 <= p_buy <= 1.0
    assert 0.0 <= p_skip <= 1.0
    assert math.isclose(p_wait + p_buy + p_skip, 1.0, abs_tol=1e-3)
    assert pred.decision in CLASS_NAMES


def test_predict_schema_mismatch_raises(tmp_path: Path) -> None:
    """Tampering with meta.schema_version must cause load to fail fast."""
    import json

    cfg = get_config()
    builder = EntryTimingLabelBuilder(config=cfg)
    corpus = _build_synthetic_corpus(n_winners=20, n_losers=20, seed=1)
    snaps = builder.build_for_corpus(corpus)
    model_out = tmp_path / "entry_timing.ubj"
    train_entry_timing(snaps, model_out)

    meta_path = model_out.with_suffix(".meta.json")
    meta = json.loads(meta_path.read_text())
    meta["schema_version"] = "entry_timing_FAKE"
    meta_path.write_text(json.dumps(meta))

    sample_features = {k: 0.0 for k in TIMING_FEATURE_ORDER}
    with pytest.raises(ValueError, match="schema mismatch"):
        predict_entry_timing(sample_features, model_out)


def test_extract_features_shape_and_keys() -> None:
    """``extract_snapshot_features`` produces canonical keys regardless of
    trade visibility (zero-fill on empty)."""
    feats = extract_snapshot_features([], snapshot_t=30.0, token_created_at=0.0)
    assert set(feats.keys()) == set(TIMING_FEATURE_ORDER)
    assert feats["snapshot_t"] == 30.0
    assert feats["unique_buyers"] == 0.0

    # With a single buy 10s in, t=30 sees it; t=5 does not.
    created_at = 100.0
    trades = [
        _trade(
            wallet="alice",
            sol_amount=0.7,
            timestamp=created_at + 10.0,
            market_cap_sol=40.0,
        )
    ]
    f30 = extract_snapshot_features(
        trades, snapshot_t=30.0, token_created_at=created_at
    )
    f5 = extract_snapshot_features(trades, snapshot_t=5.0, token_created_at=created_at)
    assert f30["unique_buyers"] == 1.0
    assert f30["buy_volume_sol"] == pytest.approx(0.7)
    assert f5["unique_buyers"] == 0.0
