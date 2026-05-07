# tests/pulse_bot/test_creator_blacklist_filter.py
"""Tests for DecisionService.filter_creator_blacklist (2026-05-01).

Why this matters: the legacy ``filters/creator.py`` was dead code for weeks
(never imported into the pipeline), and the rule scorer only ever saw a
leak-free as-of view that hardcoded ``blacklisted=False``. So flipping the
``creators.blacklisted`` flag had ZERO production effect until this gate
was added. These tests pin the gate's behaviour so it doesn't silently
regress again.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from pulse_bot.decision_service import DecisionService, EntryDecision


class _FakeDB:
    """Stub returning a canned single-row response for SELECT blacklisted."""

    def __init__(self, blacklisted: int | None):
        self._blacklisted = blacklisted

    def _sync_query(self, sql, params=(), one=False):
        if self._blacklisted is None:
            return None  # no row
        return {"blacklisted": self._blacklisted}


def _decision(should_enter=True):
    return EntryDecision(
        should_enter=should_enter,
        entry_type="full",
        entry_score=30,
        entry_buyer_num=5,
    )


def _token(creator: str | None = "scammer123") -> SimpleNamespace:
    return SimpleNamespace(creator=creator, mint="MINT", created_at=100.0)


@pytest.mark.asyncio
async def test_creator_blacklisted_forces_skip():
    """When DB reports blacklisted=1, decision should flip to SKIP."""
    ds = DecisionService(db=_FakeDB(blacklisted=1))
    out = await ds.filter_creator_blacklist(_token(), _decision(), "MINT")
    assert out.should_enter is False
    assert ds.creator_blacklist_skips == 1


@pytest.mark.asyncio
async def test_creator_not_blacklisted_passes():
    """When DB reports blacklisted=0, decision unchanged."""
    ds = DecisionService(db=_FakeDB(blacklisted=0))
    out = await ds.filter_creator_blacklist(_token(), _decision(), "MINT")
    assert out.should_enter is True
    assert ds.creator_blacklist_skips == 0


@pytest.mark.asyncio
async def test_creator_unknown_passes():
    """Unknown creator (not in `creators` table yet) → no skip."""
    ds = DecisionService(db=_FakeDB(blacklisted=None))
    out = await ds.filter_creator_blacklist(_token(), _decision(), "MINT")
    assert out.should_enter is True
    assert ds.creator_blacklist_skips == 0


@pytest.mark.asyncio
async def test_already_skip_decision_is_noop():
    """If decision is already SKIP, don't double-count."""
    ds = DecisionService(db=_FakeDB(blacklisted=1))
    out = await ds.filter_creator_blacklist(
        _token(), _decision(should_enter=False), "MINT"
    )
    assert out.should_enter is False
    assert ds.creator_blacklist_skips == 0  # didn't fire — was already SKIP


@pytest.mark.asyncio
async def test_missing_creator_field_passes_through():
    """Token without a creator field → unchanged decision (defensive)."""
    ds = DecisionService(db=_FakeDB(blacklisted=1))
    out = await ds.filter_creator_blacklist(
        _token(creator=None), _decision(), "MINT"
    )
    assert out.should_enter is True
    assert ds.creator_blacklist_skips == 0


@pytest.mark.asyncio
async def test_db_failure_passes_through():
    """DB error must not crash decision — passes through unchanged."""
    class _BoomDB:
        def _sync_query(self, sql, params=(), one=False):
            raise RuntimeError("db boom")

    ds = DecisionService(db=_BoomDB())
    out = await ds.filter_creator_blacklist(_token(), _decision(), "MINT")
    assert out.should_enter is True
    assert ds.creator_blacklist_skips == 0
