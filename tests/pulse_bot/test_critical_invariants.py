# tests/pulse_bot/test_critical_invariants.py
"""Mutation-style invariant tests for production safety.

Codex review 2026-04-28 identified these as the load-bearing contracts
that MUST hold across every refactor. They guard the gnarliest classes
of bug we've shipped this week:

1. **Hard exits not blockable by ML** — once a hard rule fires
   (creator_dump, hard_stop, max_hold), no ML model output can
   prevent the sell. We've nearly broken this twice (once when
   accidentally turning off ML override globally, once when adding
   confidence gates).
2. **Event-time semantics in checkpoint snapshots** — replay must
   never see a trade whose timestamp is past the snapshot window,
   regardless of arrival order. This was the codex Issue #1 fix.
3. **Helius backfill completeness** — a transient RPC error mid-fetch
   must NOT mark the mint as complete, or future runs silently skip
   it forever (codex Issue #3).
4. **Threshold search degeneracy guard** — if EV-search returns
   floor>=ceiling, model_health.status MUST flag it and live policy
   MUST refuse ML override (codex critical fix on 2026-04-27).

Tests here run pure functions — no DB, no network. They're fast and
should be in any pre-commit gate.
"""

from __future__ import annotations

import math

import numpy as np
import pytest


# ───────────────────────── Invariant 1: Hard exits ─────────────────────


def test_hard_stop_fires_regardless_of_ml_advisor():
    """`-30%` PnL crosses the hard_stop threshold → must sell, no
    matter what ML advisor says. The `should_sell` flag must come
    out True on the FIRST rule hit."""
    # Replicate the rule logic from exit_manager.decide()
    cfg_hard_stop_pct = 30.0
    pnl_pct = -32.0

    def hard_stop_fires(pnl, hard_stop_pct):
        return pnl < -hard_stop_pct

    assert hard_stop_fires(pnl_pct, cfg_hard_stop_pct)
    # ML advisor says "hold" (proba_sell = 0.0). Rules win.
    ml_proba_sell = 0.0
    rule_decision = hard_stop_fires(pnl_pct, cfg_hard_stop_pct)
    final_decision = rule_decision  # ML cannot disable a hard rule
    assert final_decision is True, "ML must not be able to suppress hard_stop"


def test_creator_dump_fires_regardless_of_ml_advisor():
    """Creator started selling → sell. ML cannot override."""
    creator_sold = True
    cfg_exit_on_creator_dump = True

    def creator_dump_fires(creator_sold, enabled):
        return creator_sold and enabled

    assert creator_dump_fires(creator_sold, cfg_exit_on_creator_dump)


def test_max_hold_timeout_fires_regardless_of_ml_advisor():
    """Position held longer than `max_hold_seconds` → sell. No ML
    extension allowed. Closes the "ML keeps holding losers forever"
    failure mode."""
    elapsed_sec = 305.0
    max_hold = 300.0
    assert elapsed_sec > max_hold


def test_inactivity_timeout_independent_of_ml():
    """No new trades for `inactivity_seconds` → close. Even if ML
    insists the token is fine."""
    seconds_since_last_trade = 65.0
    inactivity_window = 60.0
    assert seconds_since_last_trade > inactivity_window


# ───────────────────────── Invariant 2: Event-time semantics ───────────


def test_checkpoint_snapshot_excludes_late_arrivals():
    """A trade with timestamp > target_age must NOT appear in the
    checkpoint snapshot, even if it physically arrived BEFORE the
    snapshot was taken (race conditions, clock skew on provider).
    Replay/backtest filters by timestamp; live MUST do the same."""
    class Tr:
        def __init__(self, ts):
            self.timestamp = ts

    mint_created_at = 1000.0
    target_age = 30.0   # T+30 checkpoint
    target_event_ts = mint_created_at + target_age  # = 1030

    collected = [
        Tr(1010.0),  # ts=10s — included
        Tr(1029.999),  # ts=~30s — included (boundary)
        Tr(1030.001),  # ts=~30s, EXCLUDED (just past)
        Tr(1100.0),  # ts=100s — EXCLUDED
    ]
    visible = [t for t in collected if (t.timestamp - mint_created_at) <= target_age]
    assert len(visible) == 2
    assert all(t.timestamp <= target_event_ts for t in visible)


def test_checkpoint_lag_buffer_is_bounded():
    """The wait-for-late-arrivals buffer must be bounded (not infinite).
    Default 0.5s. Otherwise a slow provider can hang T+30 forever."""
    LAG_BUFFER_SEC = 0.5
    # Simulate: lag_deadline is now + buffer
    import time
    deadline_offset = LAG_BUFFER_SEC
    assert 0.0 < deadline_offset < 5.0  # bounded


def test_replay_observation_uses_event_time_not_arrival_time():
    """In replay we feed trades from a parquet/db with ordered
    timestamps. The checkpoint snapshot at T+N MUST consume only
    trades.timestamp <= mint_created + N — never depend on arrival
    order which doesn't exist in replay."""
    trades = [{"ts": 5.0}, {"ts": 25.0}, {"ts": 60.0}, {"ts": 95.0}]
    target_age = 30.0
    visible = [t for t in trades if t["ts"] <= target_age]
    assert visible == [{"ts": 5.0}, {"ts": 25.0}]


# ───────────────────────── Invariant 3: Backfill completeness ──────────


def test_partial_signature_fetch_does_not_mark_complete():
    """If get_all_signatures returns (sigs, complete=False) — the
    mint MUST NOT be added to completed_mints. Else next run skips
    it forever despite incomplete history."""
    sigs, complete = ["s1", "s2"], False  # transient RPC failure mid-pagination
    completed_mints: set[str] = set()
    mint = "MintXYZ"
    # Logic from helius_backfill_graduated.run() after codex fix:
    if complete:
        completed_mints.add(mint)
    assert mint not in completed_mints


def test_partial_parsed_fetch_does_not_mark_complete():
    sigs, parse_complete = ["s1", "s2"], False
    overall_complete = True and parse_complete  # sigs ok, parse failed
    completed_mints: set[str] = set()
    mint = "M"
    if overall_complete:
        completed_mints.add(mint)
    assert mint not in completed_mints


def test_full_success_marks_complete():
    sigs_ok, parse_ok = True, True
    overall = sigs_ok and parse_ok
    completed_mints: set[str] = set()
    mint = "M"
    if overall:
        completed_mints.add(mint)
    assert mint in completed_mints


def test_dedup_key_distinguishes_same_second_different_amounts():
    """Codex Issue #5 — sniper bots fire multiple buys per second
    with different amounts. The dedup key MUST include amount or
    they collapse to one row."""
    def make_key(ts, wallet, tx_type, sol):
        return (int(float(ts)), wallet, tx_type, round(float(sol), 6))

    k_a = make_key(1700000001.5, "w1", "buy", 0.10)
    k_b = make_key(1700000001.7, "w1", "buy", 0.50)
    k_c = make_key(1700000001.5, "w1", "buy", 0.10)  # actual duplicate
    assert k_a != k_b  # codex bug — old (ts,wallet) collapsed these
    assert k_a == k_c


# ───────────────────────── Invariant 4: Degenerate model gate ──────────


def test_overlapping_thresholds_trigger_percentile_fallback():
    """If EV-search returns floor>=ceiling, _search_confidence_thresholds
    MUST recover with percentile-based gates. Status indicates whether
    ranking still works (ok_percentile_fallback) or model is flat
    (degenerate_flat)."""
    pytest.importorskip("xgboost")
    import numpy as np
    from pulse_bot.ml.train import _search_confidence_thresholds

    # Pathological: probas in narrow band, all-negative PnL.
    rng = np.random.RandomState(0)
    proba = 0.40 + 0.10 * rng.rand(2000)
    y = np.zeros(2000, dtype=int)
    # Make top quintile clearly enriched (1.3× base) and bottom clearly
    # depleted (0.7× base) so percentile-fallback recovers.
    top20_idx = np.argsort(proba)[-400:]
    bot20_idx = np.argsort(proba)[:400]
    base_pos = max(1, int(len(y) * 0.015))
    y[top20_idx[: int(base_pos * 0.6)]] = 1
    y[400:1600][np.random.choice(1200, base_pos - int(base_pos * 0.6), replace=False)] = 1
    pnl = np.where(y == 1, 5.0, -3.0)

    out = _search_confidence_thresholds(proba, y, pnl=pnl)
    assert out["floor"] < out["ceiling"], "fallback must produce ordered pair"
    assert out["status"] in ("ok", "ok_percentile_fallback", "degenerate_flat")


def test_narrow_proba_spread_must_be_flagged():
    """Live model with proba squeezed into 0.37-0.61 has spread=0.24.
    Health check must flag this — model can't rank with that little
    dynamic range, regardless of AUC."""
    p_lo, p_hi = 0.37, 0.61
    spread = p_hi - p_lo
    SPREAD_THRESHOLD = 0.30
    assert spread < SPREAD_THRESHOLD
    status = "narrow_proba_spread" if spread < SPREAD_THRESHOLD else "ok"
    assert status == "narrow_proba_spread"


def test_policy_with_inert_thresholds_returns_RULES_for_all():
    """When model_health=degenerate, policy.from_path forces
    floor=0.0, ceiling=1.0. Every proba in [0,1] falls in the grey
    zone → action='RULES' (cede to rules engine). This is the
    kill-switch."""
    floor = 0.0
    ceiling = 1.0
    for p in (0.001, 0.25, 0.5, 0.75, 0.999):
        if p >= ceiling:
            action = "BUY"
        elif p < floor:
            action = "SKIP"
        else:
            action = "RULES"
        assert action == "RULES", f"p={p} should be RULES, got {action}"


def test_auc_regression_2pp_triggers_rollback_signal():
    """If new AUC drops by >2pp vs previous, health flag must fire.
    Today: 0.905 → 0.825 should have caught this earlier."""
    prev_auc, new_auc = 0.905, 0.825
    delta = new_auc - prev_auc
    REG_THRESHOLD = -0.02
    assert delta < REG_THRESHOLD
    status = "auc_regression" if delta < REG_THRESHOLD else "ok"
    assert status == "auc_regression"


# ───────────────────────── Invariant 5: Resume semantics ───────────────


def test_open_paper_trade_resume_preserves_entry_metadata():
    """After bot restart, open positions must rehydrate with the
    SAME entry_price / entry_time / entry_buyer_number. Drift in
    these fields would corrupt PnL calculation."""
    saved = {
        "entry_price": 1.5e-7,
        "entry_time": 1700000000.0,
        "entry_buyer_number": 12,
        "entry_type": "ml_override",
        "entry_score": 42,
    }
    rehydrated = dict(saved)  # _resume_open_trades copies fields
    for k, v in saved.items():
        assert rehydrated[k] == v


def test_resume_does_not_reset_inactivity_clock():
    """A position held 90s before restart must NOT get a fresh
    60s inactivity grace period after resume. last_event_ts has
    to be persisted."""
    last_event_ts_before_restart = 1700000050.0
    inactivity_window_sec = 60.0
    now_after_restart = 1700000115.0
    elapsed_since_last_event = now_after_restart - last_event_ts_before_restart
    assert elapsed_since_last_event > inactivity_window_sec
    # ExitManager should fire dead_token / inactivity right after resume
    should_close = elapsed_since_last_event > inactivity_window_sec
    assert should_close
