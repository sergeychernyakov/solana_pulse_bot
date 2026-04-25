# tests/pulse_bot/test_optimizer_parity.py
"""Differential trace test — Optimizer vs BacktestEngine on the same dataset.

Why this exists:
    ``test_partial_exits_parity`` and ``test_optimizer_correctness`` prove
    local correctness (math, state machine, serial == parallel). They all
    share ``PaperTradeRunner`` so they can't catch bugs in the optimizer's
    orchestration layer (candidate timeline, slot accounting, exit timing,
    entry-time clock).

What this test does:
    Builds a tiny deterministic SQLite dataset. Runs two structurally
    different orchestrators over it:

      - Reference: ``BacktestEngine`` — own Portfolio/SimulatedClock/
        SimulatedExecution path, partial-sell aware.
      - Under test: ``Optimizer.run(workers=1)`` with a single combo.

    Compares **decision-level traces** (what was entered, when, why it
    exited) rather than raw PnL. The two orchestrators intentionally use
    different execution models (BT applies slippage via SimulatedExecution;
    optimizer uses raw last-trade prices through PaperTradeRunner) so
    exact price/PnL equality is NOT the contract — **entry decisions and
    exit reasons** are.

    Limited to ``entry_mode="full"`` to avoid the known
    ``decide_entry`` vs ``backtest._score_token_for_entry`` divergence in
    ``"both"`` mode (entry-type preference tie-break differs).

Coverage:
    1. Token entry set parity.
    2. Per-position lifecycle parity: entry_type, exit_reason, pnl_sign.
    3. Canonical ordering: traces sorted by ``(entry_ts, mint)``.

Known non-parity:
    BacktestEngine pins ``entry_time = token.created_at + observe_seconds``
    (wall-clock scoring boundary). Live and Optimizer backdate to the last
    full-window trade timestamp so elapsed/inactivity math is in event
    time. Both are intentional, so ``entry_ts`` is used only for canonical
    sorting — not asserted for equality.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import psycopg2
import pytest

from pulse_bot.backtest import BacktestEngine
from pulse_bot.config import PulseBotConfig
from pulse_bot.db import Database
from pulse_bot.optimizer import Optimizer

# ---------------------------------------------------------------------------
# Trace dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PositionTrace:
    """Subset of lifecycle fields that both paths produce from the same
    algorithm, comparable regardless of slippage / execution model."""

    mint: str
    entry_type: str
    entry_ts: float
    exit_ts: float
    exit_reason: str
    pnl_sign: int  # +1 profit, -1 loss, 0 flat


def _pnl_sign(pnl_sol: float) -> int:
    if pnl_sol > 1e-9:
        return 1
    if pnl_sol < -1e-9:
        return -1
    return 0


# ---------------------------------------------------------------------------
# Frozen synthetic dataset
# ---------------------------------------------------------------------------


def _populate_db(dsn: str) -> None:
    """Build a deterministic DB with three tokens designed for unambiguous
    exits within the stream (so neither BT's ``backtest_end`` finale nor
    optimizer's no-monitor-trade fallback can contaminate the comparison):

        M0: pumps +200 % in monitor window → take_profit fires cleanly
        M1: tanks -60 % → hard_stop fires
        M2: one flat trade, then a long inactivity gap with one trade after
            it → dead_token fires via the inactivity rule
    """
    conn = psycopg2.connect(dsn)
    conn.autocommit = True
    try:
        tokens = [
            ("M0", "PUMP0", 1_000.0),
            ("M1", "DUMP1", 2_000.0),
            ("M2", "FLAT2", 3_000.0),
        ]
        with conn.cursor() as cur:
            for mint, sym, created_at in tokens:
                cur.execute(
                    "INSERT INTO tokens (mint, name, symbol, creator, created_at,"
                    " uri, launchpad) VALUES (%s,%s,%s,%s,%s,%s,%s)",
                    (mint, sym, sym, f"C-{mint}", created_at, "", "pumpfun"),
                )
                cur.execute(
                    "INSERT INTO creators (wallet, total_tokens_created, times_seen,"
                    " tokens_where_creator_sold_early, first_seen_at, last_seen_at,"
                    " blacklisted) VALUES (%s,%s,%s,%s,%s,%s,%s)",
                    (f"C-{mint}", 1, 1, 0, created_at, created_at, 0),
                )

            trade_rows: list[tuple] = []

            # Full-window buys for each token — 6 distinct wallets, price 0.001.
            # With observe_seconds=45, these land in the scoring window.
            for mint, _sym, created_at in tokens:
                for k in range(6):
                    ts = created_at + 1.0 + k
                    trade_rows.append(
                        (
                            mint,
                            f"W-{mint}-{k}",
                            "buy",
                            0.1,
                            100.0,  # sol, tokens → price = 0.001
                            30.0 + k,
                            30.0 + k,
                            ts,
                            0,
                        )
                    )

            # Post-entry monitor trades.
            # M0: price 0.003 (3x = +200%) across 5 trades → forces take_profit
            for k in range(5):
                ts = 1_000.0 + 50.0 + k
                trade_rows.append(
                    (
                        "M0",
                        f"P-M0-{k}",
                        "buy",
                        0.3,
                        100.0,  # price = 0.003
                        60.0 + k,
                        60.0 + k,
                        ts,
                        0,
                    )
                )
            # M1: price 0.0004 (-60%) → forces hard_stop
            for k in range(5):
                ts = 2_000.0 + 50.0 + k
                trade_rows.append(
                    (
                        "M1",
                        f"P-M1-{k}",
                        "buy",
                        0.04,
                        100.0,  # price = 0.0004
                        20.0 + k,
                        20.0 + k,
                        ts,
                        0,
                    )
                )
            # M2: one flat monitor trade at t+50 (populates recent_trades in BT
            # and last_trade_ts in optimizer), then a second trade at t+250 with
            # a 200s gap (> inactivity=120s). When the second M2 trade arrives:
            #   - BT's ``_close_expired_positions`` detects inactivity on M2 and
            #     closes it as ``dead_token`` before re-entry checks.
            #   - Optimizer's in-loop gap check in ``_simulate_trade_from`` fires
            #     ``dead_token`` at ``last_trade_ts + inactivity``.
            for k, ts_offset in enumerate((50.0, 250.0)):
                ts = 3_000.0 + ts_offset
                trade_rows.append(
                    (
                        "M2",
                        f"P-M2-{k}",
                        "buy",
                        0.1,
                        100.0,  # price = 0.001 (flat vs entry → no TP/SL)
                        30.0,
                        30.0,
                        ts,
                        0,
                    )
                )

            for row in trade_rows:
                cur.execute(
                    "INSERT INTO trades (mint, wallet, tx_type, sol_amount,"
                    " token_amount, v_sol_in_bonding_curve, market_cap_sol,"
                    " timestamp, is_creator) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                    row,
                )
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


def _cfg(db_path: str) -> PulseBotConfig:
    cfg = PulseBotConfig()
    cfg.db_path = db_path
    cfg.backtest_db_path = db_path
    cfg.entry_mode = "full"  # see module docstring
    cfg.observe_seconds = 45
    cfg.fast_observe_seconds = 5
    cfg.min_entry_buyer_number = 1
    cfg.max_entry_buyer_number = 999
    cfg.score_threshold_buy = -999  # accept every token
    cfg.fast_score_threshold = -999
    cfg.portfolio_max_positions = 10  # no capacity contention in this test
    cfg.portfolio_initial_sol = 10.0
    cfg.buy_amount_sol = 0.1
    cfg.exit_hard_stop_loss_pct = 30.0  # M1 at −60 % triggers this
    cfg.exit_take_profit_pct = 50.0  # M0 at +200 % triggers this
    cfg.exit_take_profit_enabled = True
    cfg.exit_max_hold_seconds = 600
    cfg.exit_inactivity_seconds = 120
    cfg.exit_trailing_stop_enabled = False
    cfg.exit_on_creator_dump = False
    cfg.exit_on_whale = False
    # Emit pulse snapshots on every trade so hard/take-profit exits fire
    # through the ExitManager (BacktestEngine path has no pre-pulse
    # hard-stop shortcut; without a snapshot it never calls decide()).
    # All threshold-based pulse exits are disabled so only TP/SL/timeout
    # can possibly fire.
    cfg.pulse_min_events = 1
    cfg.pulse_dead_buy_rate = -1.0  # buy_rate < -1 → never
    cfg.pulse_weak_buy_rate = -1.0
    cfg.exit_no_new_wallets_events = 9_999
    cfg.exit_trend_dying_count = 9_999
    cfg.exit_sell_pressure_ratio = 9_999.0
    cfg.exit_near_graduation_pct = 200.0
    # Disable data-driven entry gates so synthetic test tokens pass through.
    cfg.entry_min_curve_velocity = 0.0
    cfg.entry_min_curve_acceleration = -1e9
    cfg.entry_max_curve_acceleration = 1e9
    cfg.entry_max_top3_buyer_pct = 100.0
    cfg.entry_max_fast_score = 10_000
    cfg.creator_max_tokens_today = 10_000
    cfg.entry_min_sol_volume_hard = 0.0
    cfg.fast_hard_min_volume_sol = 0.0
    cfg.fast_hard_min_unique_buyers = 0
    cfg.creator_min_graduation_rate = 0.0
    cfg.creator_min_age_days = 0.0
    return cfg


# ---------------------------------------------------------------------------
# Reference and under-test runners
# ---------------------------------------------------------------------------


def _reference_traces(cfg: PulseBotConfig) -> list[PositionTrace]:
    """Run BacktestEngine and project its closed trades to PositionTrace."""
    db = Database(cfg.db_path)
    db.init_schema()
    engine = BacktestEngine(cfg, db)
    result = engine.run()
    traces = [
        PositionTrace(
            mint=t.mint,
            entry_type=t.entry_type,
            entry_ts=t.entry_time,
            exit_ts=t.exit_time,
            exit_reason=t.exit_reason,
            pnl_sign=_pnl_sign(t.pnl_sol),
        )
        for t in result.closed_trades
    ]
    return sorted(traces, key=lambda x: (x.entry_ts, x.mint))


def _optimizer_traces(cfg: PulseBotConfig, out_db: str) -> list[PositionTrace]:
    """Run Optimizer with a single combo and project trades to PositionTrace.

    ``Optimizer.run`` returns only top-N summaries (no per-trade list), so
    the full trade payload is read back from ``optimization_runs.trades_json``
    via a direct SQL read — that's the same blob the caller would consume
    out of the persisted DB in any other analysis.
    """
    cfg.optimizer_db_path = out_db
    opt_db = Database(out_db)
    opt_db.init_schema()
    opt = Optimizer(cfg, opt_db)
    # Single combo = base config → only one run
    opt.set_grid({"entry_mode": ["full"]})
    results = opt.run(max_combos=0, workers=1)
    assert len(results) == 1, "single-combo grid must produce exactly one run"

    conn = psycopg2.connect(out_db)
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT trades_json FROM optimization_runs WHERE run_id = %s",
                (results[0]["run_id"],),
            )
            row = cur.fetchone()
    finally:
        conn.close()
    assert row is not None, "optimizer did not persist the run"
    trades = json.loads(row[0] or "[]")

    traces = [
        PositionTrace(
            mint=t["mint"],
            entry_type=t["entry_type"],
            entry_ts=t["entry_time"],
            exit_ts=t["exit_time"],
            exit_reason=t["exit_reason"],
            pnl_sign=_pnl_sign(t["pnl_sol"]),
        )
        for t in trades
    ]
    return sorted(traces, key=lambda x: (x.entry_ts, x.mint))


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestOptimizerReferenceParity:
    """Differential parity: BacktestEngine vs Optimizer on the same dataset."""

    def test_entry_set_matches(self, tmp_path: Path, pg_test_db: str) -> None:
        """Both orchestrators must enter the same set of tokens."""
        _populate_db(pg_test_db)
        cfg = _cfg(pg_test_db)

        ref = _reference_traces(cfg)
        opt = _optimizer_traces(cfg, pg_test_db)

        assert {t.mint for t in ref} == {t.mint for t in opt}, (
            f"Entry set divergence: ref={sorted(t.mint for t in ref)}, "
            f"opt={sorted(t.mint for t in opt)}"
        )

    def test_per_position_lifecycle_matches(
        self, tmp_path: Path, pg_test_db: str
    ) -> None:
        """For each mint: entry_type, exit_reason, pnl_sign must match.

        ``entry_ts`` is NOT compared — see module docstring for the
        intentional wall-clock-vs-event-time divergence between BT and
        optimizer.
        """
        _populate_db(pg_test_db)
        cfg = _cfg(pg_test_db)

        ref = _reference_traces(cfg)
        opt = _optimizer_traces(cfg, pg_test_db)

        ref_by_mint = {t.mint: t for t in ref}
        opt_by_mint = {t.mint: t for t in opt}

        assert ref_by_mint.keys() == opt_by_mint.keys()
        for mint in sorted(ref_by_mint):
            r = ref_by_mint[mint]
            o = opt_by_mint[mint]
            assert (
                r.entry_type == o.entry_type
            ), f"{mint}: entry_type ref={r.entry_type} opt={o.entry_type}"
            assert (
                r.exit_reason == o.exit_reason
            ), f"{mint}: exit_reason ref={r.exit_reason} opt={o.exit_reason}"
            assert (
                r.pnl_sign == o.pnl_sign
            ), f"{mint}: pnl_sign ref={r.pnl_sign} opt={o.pnl_sign}"

    def test_exit_reasons_are_expected(self, tmp_path: Path, pg_test_db: str) -> None:
        """The synthetic dataset is engineered to fire specific exits. If
        either path produces a different reason the test data has drifted
        or orchestration regressed."""
        _populate_db(pg_test_db)
        cfg = _cfg(pg_test_db)

        ref = {t.mint: t.exit_reason for t in _reference_traces(cfg)}
        opt = {
            t.mint: t.exit_reason
            for t in _optimizer_traces(
                cfg,
                pg_test_db,
            )
        }

        # M0 pumps → take_profit; M1 tanks → hard_stop;
        # M2 is silent → dead_token (inactivity tracking is on).
        expected = {
            "M0": "take_profit",
            "M1": "hard_stop",
            "M2": "dead_token",
        }
        for mint, want in expected.items():
            assert (
                ref.get(mint) == want
            ), f"reference (BacktestEngine) {mint}: {ref.get(mint)} != {want}"
            assert opt.get(mint) == want, f"optimizer {mint}: {opt.get(mint)} != {want}"
