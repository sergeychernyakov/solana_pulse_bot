# tests/pulse_bot/test_types.py
"""Tests for the typed enum module (architecture phase J,
codex review 2026-04-28)."""

from __future__ import annotations

import pytest

from pulse_bot.types import (
    CheckpointVerdict,
    EntryAction,
    EntryType,
    ExitReason,
    ModelHealth,
    TimingClass,
)


def test_entry_action_string_equivalence():
    """Enum members compare-equal to their string value (str-Enum)."""
    assert EntryAction.BUY == "BUY"
    assert EntryAction.SKIP == "SKIP"
    assert EntryAction.RULES == "RULES"


def test_entry_action_from_string_roundtrip():
    for s in ("BUY", "SKIP", "RULES"):
        assert EntryAction.from_string(s).value == s


def test_entry_action_unknown_raises():
    with pytest.raises(ValueError):
        EntryAction.from_string("DEFER")


def test_entry_type_is_real_entry_recognizes_known():
    """Dashboard filter relies on this — must accept all canonical
    entry_type values from paper_trades."""
    for v in ("fast", "full", "ml_override", "t30", "t30_skip",
              "timing", "BUY_EARLY"):
        assert EntryType.is_real_entry(v)


def test_entry_type_is_real_entry_rejects_shadow():
    assert not EntryType.is_real_entry("")
    assert not EntryType.is_real_entry(None)
    assert not EntryType.is_real_entry("unknown_path")


def test_checkpoint_verdict_only_buy_or_skip():
    assert CheckpointVerdict.BUY_EARLY == "BUY_EARLY"
    assert CheckpointVerdict.SKIP_EARLY == "SKIP_EARLY"


def test_timing_class_three_values():
    assert {c.value for c in TimingClass} == {"WAIT_MORE", "BUY_NOW", "SKIP"}


def test_exit_reason_enum_includes_canonical_set():
    canonical = {"dead_token", "hard_stop", "take_profit", "timeout",
                 "inactivity", "trailing_stop"}
    members = {r.value for r in ExitReason}
    assert canonical.issubset(members)


def test_model_health_ok_legacy_handling():
    """Legacy meta.json without model_health field → status=None →
    treat as healthy (pre-2026-04-28 artifacts)."""
    assert ModelHealth.is_healthy(None)
    assert ModelHealth.is_healthy("ok")
    assert not ModelHealth.is_healthy("degenerate")
    assert not ModelHealth.is_healthy("auc_regression")


def test_str_enum_works_in_dict_keys():
    """str-Enum subclasses must hash like strings — so dicts keyed
    by EntryAction.BUY interchange with 'BUY'."""
    counts: dict[str, int] = {EntryAction.BUY.value: 10}
    assert counts["BUY"] == 10
    counts[EntryAction.SKIP.value] = 5
    assert counts == {"BUY": 10, "SKIP": 5}
