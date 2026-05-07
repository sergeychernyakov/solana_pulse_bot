# tests/pulse_bot/test_filter_health.py
"""Tests for observability/filter_health.py — boot-time scan of
filter firing rates from bot.log."""

from __future__ import annotations

from pathlib import Path

import pytest

from pulse_bot.filter_health import (
    FilterCount,
    scan_log_for_firings,
)


SAMPLE_LOG = """
12:00:01 INFO     pulse_bot.pipeline: New token: ABC by xyz
12:00:02 WARNING  pulse_bot.decision_service: BOT-CLUSTER HARD SKIP ABC: 4 known is_bot wallets in first 30s (≥3 threshold) — skipping
12:00:05 WARNING  pulse_bot.decision_service: EARLY OVERRIDE DEF: rules=BUY → t30_skip=SKIP (proba=0.001)
12:00:08 WARNING  pulse_bot.decision_service: WASH-CLUSTER HARD SKIP GHI: 3 buyers from cluster C42 (size=12) in first 30s (≥3 threshold) — skipping
12:00:12 WARNING  pulse_bot.decision_service: CREATOR-BLACKLIST HARD SKIP JKL: creator scammer123 flagged (Tier-2 scammer definition) — skipping
12:00:15 WARNING  pulse_bot.pipeline: Survival exit: predicted_remaining=10s elapsed=85s confidence=0.31 — closing as survival_predict
12:00:20 WARNING  pulse_bot.decision_service: EARLY OVERRIDE MNO: rules=BUY → t30_skip=SKIP (proba=0.002)
12:00:25 WARNING  pulse_bot.decision_service: BOT-CLUSTER HARD SKIP PQR: 5 known is_bot wallets in first 30s (≥3 threshold) — skipping
12:00:30 INFO     pulse_bot.pipeline: New token: STU
"""


def test_scan_log_counts_each_filter(tmp_path: Path):
    log = tmp_path / "bot.log"
    log.write_text(SAMPLE_LOG.strip())

    rows = scan_log_for_firings(log)
    by_name = {r.name: r for r in rows}

    assert by_name["bot_cluster_skip"].n_firings == 2
    assert by_name["wash_cluster_skip"].n_firings == 1
    assert by_name["creator_blacklist_skip"].n_firings == 1
    assert by_name["survival_predict_exit"].n_firings == 1
    assert by_name["t30_skip_early"].n_firings == 2
    # Filters not appearing in the log should report 0.
    assert by_name["ml_sl_tightened"].n_firings == 0
    assert by_name["dynamic_max_hold_used"].n_firings == 0


def test_scan_log_records_last_seen_timestamp(tmp_path: Path):
    log = tmp_path / "bot.log"
    log.write_text(SAMPLE_LOG.strip())

    rows = scan_log_for_firings(log)
    by_name = {r.name: r for r in rows}

    # Last bot_cluster_skip in fixture is at 12:00:25.
    assert by_name["bot_cluster_skip"].last_seen == "12:00:25"
    # Single firing — last_seen = only timestamp.
    assert by_name["wash_cluster_skip"].last_seen == "12:00:08"
    # Filter never fired → last_seen=None.
    assert by_name["ml_sl_tightened"].last_seen is None


def test_scan_log_missing_file_returns_empty(tmp_path: Path):
    """Missing log file shouldn't crash — caller treats empty as 'no data'."""
    rows = scan_log_for_firings(tmp_path / "nope.log")
    assert rows == []


def test_scan_log_empty_file(tmp_path: Path):
    """Empty log file yields all-zero counts but proper structure."""
    log = tmp_path / "bot.log"
    log.write_text("")
    rows = scan_log_for_firings(log)
    assert all(r.n_firings == 0 for r in rows)
    assert all(r.last_seen is None for r in rows)


def test_scan_log_max_lines_caps_memory(tmp_path: Path):
    """max_lines parameter caps how much we read (defensive against huge logs)."""
    log = tmp_path / "bot.log"
    # Write 100 BOT-CLUSTER firings; cap at 50 → should see at most 50.
    lines = ["12:00:01 WARNING pulse_bot.decision_service: BOT-CLUSTER HARD SKIP X\n"] * 100
    log.write_text("".join(lines))
    rows = scan_log_for_firings(log, max_lines=50)
    by_name = {r.name: r for r in rows}
    assert by_name["bot_cluster_skip"].n_firings == 50
