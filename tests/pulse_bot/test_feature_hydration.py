# tests/pulse_bot/test_feature_hydration.py
"""Tests for FeatureHydrationService — the extracted hydration layer
(architecture phase F, codex review 2026-04-28).

Goal: prove that hydration logic is testable in isolation from
Pipeline. No DB required — mock ``db`` with stub callables.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import pytest

from pulse_bot.feature_hydration import FeatureHydrationService, HydratedContext


@dataclass
class FakeToken:
    mint: str = "MintAlice"
    creator: str = "Carol"
    created_at: float = 1_000_000.0


@dataclass
class FakeTrade:
    wallet: str
    tx_type: str
    sol_amount: float
    timestamp: float


class FakeDB:
    def __init__(self, creator=None, wallet_stats=None, raise_creator=False,
                 raise_wallet=False):
        self._creator = creator
        self._wallet_stats = wallet_stats or {}
        self._raise_creator = raise_creator
        self._raise_wallet = raise_wallet

    def get_creator_stats_as_of_sync(self, creator, ts):
        if self._raise_creator:
            raise RuntimeError("DB down")
        return self._creator

    def get_wallet_prior_stats_sync(self, wallets, *, exclude_mint, cutoff_ts):
        if self._raise_wallet:
            raise RuntimeError("DB down")
        return {w: self._wallet_stats[w] for w in wallets if w in self._wallet_stats}


def _make_holder_fetcher(snapshot=None, raise_=False):
    def _fetch(mint):
        if raise_:
            raise RuntimeError("Helius timeout")
        return snapshot
    return _fetch


# ───────────────────────── T+90 hydration ──────────────────────────────


def test_hydrate_t90_happy_path_pulls_all_inputs():
    db = FakeDB(
        creator={"creator_age_days": 12.0},
        wallet_stats={
            "alice": {"all_mint_count": 3, "wr": 0.5, "total_pnl_sol": 1.2},
            "bob": {"all_mint_count": 2, "wr": 0.66, "total_pnl_sol": 0.8},
        },
    )
    holder_fetch = _make_holder_fetcher(snapshot={"top1_30": 0.55, "hc_120": 80})
    svc = FeatureHydrationService(db, holder_fetch)

    trades = [
        FakeTrade("alice", "buy", 5.0, 1_000_002.0),  # in first 5s
        FakeTrade("bob", "buy", 3.0, 1_000_007.0),    # outside 5s
        FakeTrade("carol", "buy", 2.0, 1_000_010.0),
    ]
    ctx = svc.hydrate_for_t90(FakeToken(), trades, scored_at=1_000_090.0)

    assert ctx.creator_snapshot == {"creator_age_days": 12.0}
    assert ctx.holder_snapshot == {"top1_30": 0.55, "hc_120": 80}
    assert "alice" in ctx.top_n_wallets and ctx.top_n_wallets[0] == "alice"
    assert "alice" in ctx.wallet_prior_stats
    assert ctx.cutoff_ts == 1_000_090.0
    assert ctx.n_buyers_first_5s == 1.0  # only alice was in [0,5s)


def test_hydrate_t90_creator_failure_does_not_break_others():
    """Codex principle: one DB lookup failure must NOT cascade. The
    other context fields stay valid; failed one is None."""
    db = FakeDB(raise_creator=True, wallet_stats={"alice": {"wr": 0.4}})
    svc = FeatureHydrationService(db, _make_holder_fetcher(snapshot={"x": 1}))

    trades = [FakeTrade("alice", "buy", 1.0, 1_000_010.0)]
    ctx = svc.hydrate_for_t90(FakeToken(), trades, scored_at=1_000_090.0)

    assert ctx.creator_snapshot is None  # failure → None, not raise
    assert ctx.holder_snapshot == {"x": 1}  # this one survived
    assert ctx.top_n_wallets == ["alice"]


def test_hydrate_t90_wallet_failure_returns_empty():
    db = FakeDB(raise_wallet=True)
    svc = FeatureHydrationService(db, _make_holder_fetcher())
    trades = [FakeTrade("alice", "buy", 1.0, 1_000_010.0)]
    ctx = svc.hydrate_for_t90(FakeToken(), trades, scored_at=1_000_090.0)
    assert ctx.top_n_wallets == []
    assert ctx.wallet_prior_stats == {}


def test_hydrate_t90_holder_failure_keeps_holder_none():
    db = FakeDB()
    svc = FeatureHydrationService(db, _make_holder_fetcher(raise_=True))
    ctx = svc.hydrate_for_t90(FakeToken(), [], scored_at=1_000_090.0)
    assert ctx.holder_snapshot is None


def test_hydrate_t90_empty_trades_yields_safe_defaults():
    """No trades → no top-N, no sniper count, but other lookups still fire."""
    db = FakeDB(creator={"x": 1})
    svc = FeatureHydrationService(db, _make_holder_fetcher(snapshot={"y": 2}))
    ctx = svc.hydrate_for_t90(FakeToken(), [], scored_at=1_000_090.0)
    assert ctx.top_n_wallets == []
    assert ctx.wallet_prior_stats == {}
    assert ctx.creator_snapshot == {"x": 1}
    assert ctx.holder_snapshot == {"y": 2}
    assert ctx.n_buyers_first_5s == 0.0  # zero is informative


# ───────────────────────── T+30 hydration ──────────────────────────────


def test_hydrate_t30_uses_visible_trades_only():
    """Top-N + sniper count must come from the EVENT-TIME-CLIPPED list
    passed in, not from a full collection. Caller is responsible for
    clipping; service must not invent extra logic."""
    db = FakeDB(wallet_stats={"alice": {"wr": 0.7}})
    svc = FeatureHydrationService(db, _make_holder_fetcher())

    visible = [
        FakeTrade("alice", "buy", 5.0, 1_000_001.0),  # in first 5s
        FakeTrade("alice", "buy", 5.0, 1_000_002.0),
        FakeTrade("bob", "buy", 1.0, 1_000_020.0),
    ]
    ctx = svc.hydrate_for_t30(FakeToken(), visible, t30_cutoff=1_000_030.0)

    assert ctx.cutoff_ts == 1_000_030.0
    assert "alice" in ctx.top_n_wallets
    assert "bob" in ctx.top_n_wallets
    assert ctx.n_buyers_first_5s == 1.0  # only alice was in [0,5s)


def test_hydrate_t30_creator_intentionally_skipped():
    """T+30 hydration does NOT re-fetch creator (T+90 already has it,
    same value). Must NOT call db.get_creator_stats_as_of_sync at all."""
    calls = {"creator": 0}
    class CountingDB(FakeDB):
        def get_creator_stats_as_of_sync(self, *args, **kwargs):
            calls["creator"] += 1
            return None
    db = CountingDB()
    svc = FeatureHydrationService(db, _make_holder_fetcher())
    svc.hydrate_for_t30(FakeToken(), [], t30_cutoff=1_000_030.0)
    assert calls["creator"] == 0


# ───────────────────────── HydratedContext shape ───────────────────────


def test_hydrated_context_default_safe_for_extractor():
    """A fresh HydratedContext (no overrides) has safe defaults the
    feature extractor can consume without KeyError."""
    ctx = HydratedContext()
    assert ctx.creator_snapshot is None
    assert ctx.holder_snapshot is None
    assert ctx.top_n_wallets == []
    assert ctx.wallet_prior_stats == {}
    assert math.isnan(ctx.n_buyers_first_5s)
    assert ctx.cutoff_ts == 0.0
