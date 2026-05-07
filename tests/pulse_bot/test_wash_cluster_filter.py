# tests/pulse_bot/test_wash_cluster_filter.py
"""Tests for DecisionService.filter_wash_cluster — codex-recommended
wash-trading hard gate (2026-05-01).
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from pulse_bot.decision_service import DecisionService, EntryDecision


class _FakeDB:
    """Minimal stub returning canned cluster rows for COUNT-by-cluster."""

    def __init__(self, rows):
        self._rows = rows

    def _sync_query(self, sql, params=()):
        return self._rows


def _trade(ts, wallet, ttype="buy"):
    return SimpleNamespace(timestamp=ts, wallet=wallet, tx_type=ttype)


def _decision(should_enter=True):
    return EntryDecision(
        should_enter=should_enter,
        entry_type="full",
        entry_score=30,
        entry_buyer_num=5,
    )


@pytest.mark.asyncio
async def test_wash_cluster_disabled_by_default():
    """skip_n=0 → never fires, filter is a passthrough."""
    db = _FakeDB(rows=[{"cluster_id": "C1", "n_in_mint": 10, "sz": 20}])
    ds = DecisionService(db=db, wash_cluster_skip_n=0)
    token = SimpleNamespace(created_at=100.0)
    trades = [_trade(105, "w1"), _trade(110, "w2"), _trade(115, "w3")]
    out = await ds.filter_wash_cluster(token, trades, _decision(), "MINT")
    assert out.should_enter is True
    assert ds.wash_cluster_skips == 0


@pytest.mark.asyncio
async def test_wash_cluster_skips_when_threshold_hit():
    """≥3 buyers from same cluster (size in band) → SKIP."""
    db = _FakeDB(rows=[{"cluster_id": "C1", "n_in_mint": 4, "sz": 20}])
    ds = DecisionService(db=db, wash_cluster_skip_n=3,
                         wash_cluster_size_min=5, wash_cluster_size_max=50)
    token = SimpleNamespace(created_at=100.0)
    trades = [_trade(105, f"w{i}") for i in range(5)]
    out = await ds.filter_wash_cluster(token, trades, _decision(), "MINT")
    assert out.should_enter is False, "should hard-skip on wash cluster"
    assert ds.wash_cluster_skips == 1


@pytest.mark.asyncio
async def test_wash_cluster_passes_below_threshold():
    """Only 2 wallets from cluster, threshold=3 → pass through."""
    db = _FakeDB(rows=[])  # empty result = no cluster ≥ threshold
    ds = DecisionService(db=db, wash_cluster_skip_n=3)
    token = SimpleNamespace(created_at=100.0)
    trades = [_trade(105, "w1"), _trade(110, "w2")]
    out = await ds.filter_wash_cluster(token, trades, _decision(), "MINT")
    assert out.should_enter is True
    assert ds.wash_cluster_skips == 0


@pytest.mark.asyncio
async def test_wash_cluster_skips_already_skip_decision_unchanged():
    """If decision is already SKIP, filter is a noop."""
    db = _FakeDB(rows=[{"cluster_id": "C1", "n_in_mint": 5, "sz": 20}])
    ds = DecisionService(db=db, wash_cluster_skip_n=3)
    token = SimpleNamespace(created_at=100.0)
    trades = [_trade(105, "w1")]
    out = await ds.filter_wash_cluster(token, trades, _decision(should_enter=False), "MINT")
    assert out.should_enter is False
    assert ds.wash_cluster_skips == 0  # didn't double-count


@pytest.mark.asyncio
async def test_wash_cluster_ignores_late_buyers():
    """Buyers AFTER 30s post-creation must not count."""
    db = _FakeDB(rows=[{"cluster_id": "C1", "n_in_mint": 5, "sz": 20}])
    ds = DecisionService(db=db, wash_cluster_skip_n=3)
    token = SimpleNamespace(created_at=100.0)
    trades = [_trade(135, "w1"), _trade(140, "w2"), _trade(150, "w3")]  # all > +30
    out = await ds.filter_wash_cluster(token, trades, _decision(), "MINT")
    # No early_buyers → returns unchanged.
    assert out.should_enter is True
    assert ds.wash_cluster_skips == 0


@pytest.mark.asyncio
async def test_wash_cluster_db_failure_passes_through():
    """Filter must not crash decision on DB error — passes through with debug log."""
    class _BoomDB:
        def _sync_query(self, sql, params=()):
            raise RuntimeError("db boom")

    ds = DecisionService(db=_BoomDB(), wash_cluster_skip_n=3)
    token = SimpleNamespace(created_at=100.0)
    trades = [_trade(105, "w1")]
    out = await ds.filter_wash_cluster(token, trades, _decision(), "MINT")
    assert out.should_enter is True  # decision unchanged on error
