# tests/pulse_bot/test_decision_service.py
"""Tests for DecisionService — the entry-override chain extracted from
pipeline.py (architecture phase B step 1, codex review 2026-04-28).

Goal: prove each override step in isolation, plus the chain composition.
This is the area where every codex Issue #4 / ML override bug lived,
so dense coverage matters here.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from pulse_bot.decision_service import DecisionService, EntryDecision


@dataclass
class FakeTrade:
    wallet: str
    tx_type: str
    sol_amount: float = 1.0
    timestamp: float = 0.0


@dataclass
class FakeToken:
    mint: str = "Mint1"
    created_at: float = 1_000_000.0


@dataclass
class FakeScoringResult:
    buy_count: int = 5
    total_score: int = 50


# ───────────────────────── EntryDecision dataclass ─────────────────────


def test_entry_decision_is_immutable():
    """frozen=True — accidentally mutating in place must raise."""
    d = EntryDecision(should_enter=True, entry_type="full", entry_score=50, entry_buyer_num=3)
    with pytest.raises(Exception):  # FrozenInstanceError
        d.should_enter = False  # type: ignore[misc]


def test_entry_decision_with_replaces_field():
    d = EntryDecision(should_enter=True, entry_type="full", entry_score=50, entry_buyer_num=3)
    d2 = d.with_(should_enter=False)
    assert d.should_enter is True  # original unchanged
    assert d2.should_enter is False
    assert d2.entry_type == "full"  # other fields preserved


# ───────────────────────── ML override ─────────────────────────────────


def test_ml_buy_override_flips_skip_to_buy_and_recomputes_metadata():
    svc = DecisionService(db=None, hard_skip_n_env=0)
    rules_decision = EntryDecision(
        should_enter=False, entry_type="", entry_score=0, entry_buyer_num=0
    )
    result = FakeScoringResult(buy_count=12)
    out = svc.apply_ml_override(
        rules_decision,
        ml_action="BUY",
        ml_proba=0.65,
        ml_cal=0.42,
        result=result,
        mint_short="Mint1",
    )
    # Codex Issue #4 assertions — entry metadata MUST be non-zero now.
    assert out.should_enter is True
    assert out.entry_type == "ml_override"
    assert out.entry_buyer_num == 13  # buy_count + 1
    assert out.entry_score == 42  # ml_cal × 100
    assert svc.ml_overrides_buy == 1


def test_ml_skip_override_flips_buy_to_skip():
    svc = DecisionService(db=None, hard_skip_n_env=0)
    rules_decision = EntryDecision(
        should_enter=True, entry_type="full", entry_score=60, entry_buyer_num=8
    )
    out = svc.apply_ml_override(
        rules_decision, "SKIP", 0.05, 0.001,
        FakeScoringResult(), mint_short="Mint1"
    )
    assert out.should_enter is False
    # Other fields unchanged — caller may still want them for logging.
    assert out.entry_type == "full"
    assert svc.ml_overrides_skip == 1


def test_ml_rules_action_is_noop():
    """RULES action means grey zone — no change."""
    svc = DecisionService(db=None, hard_skip_n_env=0)
    d = EntryDecision(should_enter=True, entry_type="full", entry_score=42, entry_buyer_num=5)
    out = svc.apply_ml_override(d, "RULES", 0.3, 0.05, FakeScoringResult(), "M")
    assert out is d or (out == d)  # equal (frozen + same fields)
    assert svc.ml_overrides_buy == 0
    assert svc.ml_overrides_skip == 0


def test_ml_buy_when_rules_already_buy_is_noop_no_metadata_clobber():
    """ML=BUY + rules=BUY: don't override, don't clobber metadata.
    This was a subtle bug source — early code rewrote entry_type
    even when rules+ML agreed."""
    svc = DecisionService(db=None, hard_skip_n_env=0)
    d = EntryDecision(should_enter=True, entry_type="full", entry_score=80, entry_buyer_num=10)
    out = svc.apply_ml_override(d, "BUY", 0.9, 0.7, FakeScoringResult(), "M")
    # rules already BUY → counter does NOT bump, metadata NOT clobbered
    assert out.should_enter is True
    assert out.entry_type == "full"  # NOT "ml_override"
    assert out.entry_score == 80     # NOT 70 (=ml_cal*100)
    assert svc.ml_overrides_buy == 0


# ───────────────────────── Checkpoint override ─────────────────────────


def test_checkpoint_buy_early_flips_skip_to_buy():
    svc = DecisionService(db=None, hard_skip_n_env=0)
    d = EntryDecision(should_enter=False, entry_type="", entry_score=0, entry_buyer_num=0)
    trades = [FakeTrade("a", "buy"), FakeTrade("b", "buy"), FakeTrade("c", "sell")]
    out = svc.apply_checkpoint_override(
        d, cp_verdict="BUY_EARLY", cp_proba=0.85, cp_source="t30",
        all_trades=trades, mint_short="M",
    )
    assert out.should_enter is True
    assert out.entry_type == "t30"
    assert out.entry_buyer_num == 3  # 2 buys + 1
    assert out.entry_score == 85  # 0.85 × 100


def test_checkpoint_buy_early_with_none_proba_uses_sentinel():
    svc = DecisionService(db=None, hard_skip_n_env=0)
    d = EntryDecision(should_enter=False, entry_type="", entry_score=0, entry_buyer_num=0)
    out = svc.apply_checkpoint_override(
        d, cp_verdict="BUY_EARLY", cp_proba=None, cp_source="timing",
        all_trades=[], mint_short="M",
    )
    assert out.should_enter is True
    assert out.entry_buyer_num == 1  # never zero (sentinel)
    assert out.entry_score == 1


def test_checkpoint_buy_early_when_rules_already_buy_records_source():
    """rules=BUY + cp=BUY_EARLY → keep should_enter, but tag entry_type
    so analytics can see which path drove the decision."""
    svc = DecisionService(db=None, hard_skip_n_env=0)
    d = EntryDecision(should_enter=True, entry_type="full", entry_score=60, entry_buyer_num=5)
    out = svc.apply_checkpoint_override(
        d, "BUY_EARLY", 0.9, "t30", [], "M"
    )
    assert out.should_enter is True
    assert out.entry_type == "t30"
    # Other fields preserved — checkpoint only tags source on agreement.
    assert out.entry_buyer_num == 5
    assert out.entry_score == 60


def test_checkpoint_skip_early_flips_buy_to_skip():
    svc = DecisionService(db=None, hard_skip_n_env=0)
    d = EntryDecision(should_enter=True, entry_type="full", entry_score=60, entry_buyer_num=5)
    out = svc.apply_checkpoint_override(
        d, "SKIP_EARLY", 0.001, "t30_skip", [], "M"
    )
    assert out.should_enter is False
    # entry_type intentionally NOT rewritten here — caller logs cp_source.


def test_checkpoint_no_verdict_is_noop():
    svc = DecisionService(db=None, hard_skip_n_env=0)
    d = EntryDecision(should_enter=True, entry_type="full", entry_score=42, entry_buyer_num=5)
    out = svc.apply_checkpoint_override(d, None, None, "x", [], "M")
    assert out == d


# ───────────────────────── Bot-cluster filter ──────────────────────────


@pytest.mark.asyncio
async def test_bot_cluster_skip_when_threshold_exceeded():
    """3+ is_bot=1 wallets in first 30s → flip to SKIP."""
    class FakeDB:
        def _sync_query(self, sql, params):
            assert "is_bot = 1" in sql
            return [{"n": 4}]  # 4 known bots

    svc = DecisionService(db=FakeDB(), hard_skip_n_env=3)
    token = FakeToken(created_at=1000.0)
    trades = [
        FakeTrade("bot1", "buy", timestamp=1001.0),
        FakeTrade("bot2", "buy", timestamp=1005.0),
        FakeTrade("bot3", "buy", timestamp=1010.0),
        FakeTrade("legit", "buy", timestamp=1015.0),
    ]
    decision = EntryDecision(should_enter=True, entry_type="full", entry_score=60, entry_buyer_num=5)
    out = await svc.filter_bot_cluster(token, trades, decision, "Mint1")
    assert out.should_enter is False
    assert svc.bot_cluster_skips == 1


@pytest.mark.asyncio
async def test_bot_cluster_no_skip_below_threshold():
    class FakeDB:
        def _sync_query(self, sql, params):
            return [{"n": 2}]  # only 2 bots

    svc = DecisionService(db=FakeDB(), hard_skip_n_env=3)
    token = FakeToken()
    trades = [FakeTrade("w1", "buy", timestamp=1005.0)]
    decision = EntryDecision(should_enter=True, entry_type="full", entry_score=60, entry_buyer_num=5)
    out = await svc.filter_bot_cluster(token, trades, decision, "Mint1")
    assert out.should_enter is True
    assert svc.bot_cluster_skips == 0


@pytest.mark.asyncio
async def test_bot_cluster_disabled_when_threshold_zero():
    """PULSE_BOT_CLUSTER_HARD_SKIP=0 disables — no DB call."""
    class CountingDB:
        calls = 0
        def _sync_query(self, *args, **kwargs):
            CountingDB.calls += 1
            return [{"n": 999}]

    svc = DecisionService(db=CountingDB(), hard_skip_n_env=0)
    decision = EntryDecision(should_enter=True, entry_type="full", entry_score=60, entry_buyer_num=5)
    out = await svc.filter_bot_cluster(FakeToken(), [], decision, "M")
    assert out.should_enter is True
    assert CountingDB.calls == 0


@pytest.mark.asyncio
async def test_bot_cluster_no_op_when_rules_already_skip():
    """Don't waste DB query if rules already said SKIP."""
    class CountingDB:
        calls = 0
        def _sync_query(self, *a, **k):
            CountingDB.calls += 1
            return [{"n": 0}]

    svc = DecisionService(db=CountingDB(), hard_skip_n_env=3)
    decision = EntryDecision(should_enter=False, entry_type="", entry_score=0, entry_buyer_num=0)
    out = await svc.filter_bot_cluster(FakeToken(), [], decision, "M")
    assert out.should_enter is False
    assert CountingDB.calls == 0


# ───────────────────────── Chain composition ───────────────────────────


def test_chain_ml_buy_then_checkpoint_skip_yields_skip():
    """ML flipped to BUY but checkpoint says SKIP_EARLY → final SKIP.
    This was the exact production behavior we just verified after
    enabling T+30 SKIP_EARLY mode."""
    svc = DecisionService(db=None, hard_skip_n_env=0)
    d0 = EntryDecision(should_enter=False, entry_type="", entry_score=0, entry_buyer_num=0)
    d1 = svc.apply_ml_override(d0, "BUY", 0.5, 0.3, FakeScoringResult(buy_count=4), "M")
    assert d1.should_enter is True
    d2 = svc.apply_checkpoint_override(
        d1, "SKIP_EARLY", 0.005, "t30_skip", [], "M"
    )
    assert d2.should_enter is False


def test_chain_rules_buy_then_ml_skip_yields_skip():
    """Standard ML override path: rules said BUY, ML disagreed."""
    svc = DecisionService(db=None, hard_skip_n_env=0)
    d0 = EntryDecision(should_enter=True, entry_type="full", entry_score=60, entry_buyer_num=5)
    d1 = svc.apply_ml_override(d0, "SKIP", 0.05, 0.001, FakeScoringResult(), "M")
    assert d1.should_enter is False


def test_chain_all_agree_buy_records_full_path():
    """Rules=BUY, ML=BUY (no override needed), no checkpoint — final
    must remain a clean rules-driven entry."""
    svc = DecisionService(db=None, hard_skip_n_env=0)
    d0 = EntryDecision(should_enter=True, entry_type="full", entry_score=80, entry_buyer_num=10)
    d1 = svc.apply_ml_override(d0, "BUY", 0.95, 0.85, FakeScoringResult(buy_count=10), "M")
    d2 = svc.apply_checkpoint_override(d1, None, None, "", [], "M")
    assert d2.should_enter is True
    assert d2.entry_type == "full"  # unchanged
    assert d2.entry_score == 80


# ───────────────────────── Reg-floor + entry_score encoding ────────────
# 2026-04-29 changes — entry_model_reg ranking head wired into
# apply_ml_override. Two paths that need protection:
#   1. reg_pnl_pct < PULSE_ENTRY_REG_FLOOR_PCT → block the BUY override.
#   2. when reg_pnl_pct supplied, entry_score = round(reg*10)+500 (signed
#      PnL %% × 10 with offset 500). Must be invertible by readers.


def test_ml_buy_with_reg_pnl_encodes_entry_score_with_offset(monkeypatch):
    """entry_score = round(reg_pnl × 10) + 500. Decoder must recover
    the signed PnL %% from the score column."""
    monkeypatch.delenv("PULSE_ENTRY_REG_FLOOR_PCT", raising=False)
    svc = DecisionService(db=None, hard_skip_n_env=0)
    d = EntryDecision(should_enter=False, entry_type="", entry_score=0, entry_buyer_num=0)
    out = svc.apply_ml_override(
        d, "BUY", 0.65, 0.42, FakeScoringResult(buy_count=4),
        "M", reg_pnl_pct=12.34,
    )
    # 12.34 × 10 = 123.4 → round 123 + 500 = 623
    assert out.entry_score == 623
    decoded = (out.entry_score - 500) / 10.0
    assert abs(decoded - 12.3) < 0.01  # round-trip within 0.05% precision


def test_ml_buy_negative_reg_pnl_within_floor_still_buys(monkeypatch):
    """reg_pnl negative but within floor (-100 default) → still BUY."""
    monkeypatch.delenv("PULSE_ENTRY_REG_FLOOR_PCT", raising=False)
    svc = DecisionService(db=None, hard_skip_n_env=0)
    d = EntryDecision(should_enter=False, entry_type="", entry_score=0, entry_buyer_num=0)
    out = svc.apply_ml_override(
        d, "BUY", 0.5, 0.2, FakeScoringResult(buy_count=2),
        "M", reg_pnl_pct=-5.0,
    )
    assert out.should_enter is True
    # -5.0 × 10 = -50 + 500 = 450
    assert out.entry_score == 450
    assert svc.ml_overrides_buy == 1


def test_ml_buy_blocked_when_reg_pnl_below_floor(monkeypatch):
    """reg_pnl_pct < PULSE_ENTRY_REG_FLOOR_PCT → refuse override.
    Live config sets floor=-10.0 so trades predicted worse than -10%
    don't fire."""
    monkeypatch.setenv("PULSE_ENTRY_REG_FLOOR_PCT", "-10.0")
    svc = DecisionService(db=None, hard_skip_n_env=0)
    d = EntryDecision(should_enter=False, entry_type="", entry_score=0, entry_buyer_num=0)
    out = svc.apply_ml_override(
        d, "BUY", 0.7, 0.3, FakeScoringResult(buy_count=5),
        "M", reg_pnl_pct=-15.0,  # below floor
    )
    assert out.should_enter is False  # rules SKIP preserved
    assert out.entry_type == ""
    assert svc.ml_overrides_buy == 0  # NOT counted as a BUY
    assert svc.ml_overrides_skip == 1  # counted as a skip-block


def test_ml_buy_at_exact_floor_still_buys(monkeypatch):
    """reg_pnl == floor is the boundary — gate uses ``<`` so the
    predicate at exactly -10.0 should not block."""
    monkeypatch.setenv("PULSE_ENTRY_REG_FLOOR_PCT", "-10.0")
    svc = DecisionService(db=None, hard_skip_n_env=0)
    d = EntryDecision(should_enter=False, entry_type="", entry_score=0, entry_buyer_num=0)
    out = svc.apply_ml_override(
        d, "BUY", 0.6, 0.25, FakeScoringResult(buy_count=3),
        "M", reg_pnl_pct=-10.0,  # exactly at floor
    )
    assert out.should_enter is True
    assert svc.ml_overrides_buy == 1


def test_ml_buy_without_reg_pnl_falls_back_to_legacy_score(monkeypatch):
    """When reg_pnl_pct is None, entry_score uses the legacy
    int(ml_cal × 100) heuristic — backward compat path."""
    monkeypatch.delenv("PULSE_ENTRY_REG_FLOOR_PCT", raising=False)
    svc = DecisionService(db=None, hard_skip_n_env=0)
    d = EntryDecision(should_enter=False, entry_type="", entry_score=0, entry_buyer_num=0)
    out = svc.apply_ml_override(
        d, "BUY", 0.5, 0.42, FakeScoringResult(buy_count=2),
        "M", reg_pnl_pct=None,
    )
    assert out.should_enter is True
    # Legacy: int(0.42 × 100) = 42
    assert out.entry_score == 42


def test_entry_score_encoding_clamps_to_valid_range(monkeypatch):
    """Range [-49.9%, +49.9%] maps to [1, 999]. Out-of-range input
    must be clamped, not produce 0 or >999. Bypasses the new
    PULSE_ENTRY_REG_CEILING_PCT (default 30%) by raising the ceiling
    so the encoder is what's under test."""
    monkeypatch.delenv("PULSE_ENTRY_REG_FLOOR_PCT", raising=False)
    monkeypatch.setenv("PULSE_ENTRY_REG_CEILING_PCT", "1000.0")
    svc = DecisionService(db=None, hard_skip_n_env=0)
    d = EntryDecision(should_enter=False, entry_type="", entry_score=0, entry_buyer_num=0)
    # Extreme high prediction
    out_hi = svc.apply_ml_override(
        d, "BUY", 0.9, 0.5, FakeScoringResult(buy_count=1), "M",
        reg_pnl_pct=200.0,  # +200% → would encode 2500, clamp to 999
    )
    assert out_hi.entry_score == 999
    # Extreme low prediction (still above default floor=-100)
    out_lo = svc.apply_ml_override(
        d, "BUY", 0.4, 0.1, FakeScoringResult(buy_count=1), "M",
        reg_pnl_pct=-80.0,  # -80% → would encode -300, clamp to 1
    )
    assert out_lo.entry_score == 1


def test_reg_ceiling_blocks_overconfident_predictions(monkeypatch):
    """2026-05-06 — symmetric sanity ceiling on entry_reg.

    A reg_pnl prediction far above ceiling (default 30%) is treated
    as noise/calibration error rather than a strong BUY signal —
    block the override and let the rules path SKIP. Mirrors the
    floor block on the destructive end."""
    monkeypatch.delenv("PULSE_ENTRY_REG_FLOOR_PCT", raising=False)
    monkeypatch.delenv("PULSE_ENTRY_REG_CEILING_PCT", raising=False)
    svc = DecisionService(db=None, hard_skip_n_env=0)
    d = EntryDecision(
        should_enter=False, entry_type="", entry_score=0, entry_buyer_num=0
    )
    out = svc.apply_ml_override(
        d, "BUY", 0.9, 0.5, FakeScoringResult(buy_count=1), "M",
        reg_pnl_pct=50.0,  # > default ceiling 30% → BLOCKED
    )
    assert out.should_enter is False
    assert out.entry_type == ""
    assert svc.ml_overrides_skip == 1
