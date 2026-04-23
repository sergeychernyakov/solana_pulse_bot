# tests/pulse_bot/test_creator_snapshot_scoring.py
"""Coverage for creator-snapshot gates in Scorer (#58)."""

from __future__ import annotations

from unittest.mock import Mock

from pulse_bot.config import PulseBotConfig
from pulse_bot.filters.scorer import Scorer
from pulse_bot.models import CreatorStats, Token


def _token() -> Token:
    return Token(
        mint="m1",
        creator="c1",
        symbol="X",
        name="X",
        uri="",
        created_at=1_000_000.0,
    )


def _stats(**overrides) -> CreatorStats:
    base = dict(
        wallet="c1",
        total_tokens_created=5,
        snapshot_prior_tokens=5,
        rug_rate=0.0,
        graduation_rate=0.0,
        creator_age_days=0.0,
        inter_token_interval_sec=0.0,
    )
    base.update(overrides)
    return CreatorStats(**base)


def _scorer(**cfg_overrides) -> Scorer:
    base = dict(
        entry_min_curve_velocity=0.0,
        entry_min_curve_acceleration=-1e9,
        entry_max_curve_acceleration=1e9,
        entry_max_top3_buyer_pct=100.0,
        creator_max_tokens_today=10_000,
        min_entry_buyer_number=1,
        # Disable combo4680/strict-combo gates that hard-reject synthetic test data.
        entry_min_sol_volume_hard=0.0,
        entry_min_unique_buyers_hard=0,
        fast_hard_min_volume_sol=0.0,
        fast_hard_min_unique_buyers=0,
        entry_max_fast_score=10_000,
        creator_min_graduation_rate=0.0,
        creator_min_age_days=0.0,
    )
    base.update(cfg_overrides)
    cfg = PulseBotConfig(**base)
    return Scorer(cfg, Mock())


def test_high_rug_rate_rejects() -> None:
    s = _scorer(creator_rug_rate_reject=0.5)
    result = s.score(_token(), [], creator_snapshot=_stats(rug_rate=0.8))
    assert result.decision == "SKIP"
    assert "creator_rug_rate" in result.reasons_summary


def test_low_graduation_rate_rejects() -> None:
    s = _scorer(creator_min_graduation_rate=0.10)
    result = s.score(
        _token(), [], creator_snapshot=_stats(graduation_rate=0.02)
    )
    assert result.decision == "SKIP"
    assert "creator_grad_rate_low" in result.reasons_summary


def test_new_creator_rejected_when_age_gate_on() -> None:
    s = _scorer(creator_min_age_days=7.0)
    result = s.score(
        _token(), [], creator_snapshot=_stats(creator_age_days=1.5)
    )
    assert result.decision == "SKIP"
    assert "creator_too_new" in result.reasons_summary


def test_snapshot_priors_floor_protects_brand_new() -> None:
    """Creator with only 1 prior token must NOT be rejected even with
    extreme rug_rate — too few priors to judge."""
    s = _scorer(creator_rug_rate_reject=0.5, creator_snapshot_min_priors=2)
    result = s.score(
        _token(),
        [],
        creator_snapshot=_stats(snapshot_prior_tokens=1, rug_rate=1.0),
    )
    assert "creator_rug_rate" not in result.reasons_summary


def test_defaults_leave_scoring_unchanged() -> None:
    """Neutral config must not fire any snapshot gates."""
    s = _scorer()  # all defaults
    result = s.score(
        _token(),
        [],
        creator_snapshot=_stats(rug_rate=0.9, graduation_rate=0.0),
    )
    # No snapshot-gate rejection reason present.
    reasons = result.reasons_summary
    assert "creator_rug_rate" not in reasons
    assert "creator_grad_rate_low" not in reasons
    assert "creator_too_new" not in reasons


def test_graduation_bonus_applied() -> None:
    s = _scorer()
    result = s.score(
        _token(),
        [],
        creator_snapshot=_stats(graduation_rate=0.4, snapshot_prior_tokens=10),
    )
    assert "creator_graduated" in result.reasons_summary
