# tests/pulse_bot/test_optimizer_correctness.py
"""Correctness proof for the grid-search optimizer.

Each test isolates one invariant and supplies deterministic inputs so the
expected output is derivable by hand. Together they cover:

  1. ``PaperTradeRunner.process_trade`` processes every trade until an exit
     fires — no silent early-exit.
  2. ``PaperTradeRunner._calc_pnl`` applies Pump.fun fees + priority fees.
  3. ``_simulate_trade_from`` exit pricing, hard-stop, inactivity/dead_token,
     timeout-with-empty-monitor behaviour.
  4. ``_make_trade_result`` PnL / hold arithmetic.
  5. ``_simulate_combo_event_driven`` portfolio_max_positions enforcement —
     including the ``<=`` heap-pop boundary.
  6. ``_build_result`` win/loss/PF/ROI metrics.
  7. ``_preload_dataset`` drops tokens without trades, splits full/monitor
     correctly, and picks entry_price from the last valid full-window buy.
  8. ``_worker_run_combo`` (parallel Phase 1) emits the **same** candidate
     list as the serial ``_stream_and_evaluate`` path — provability of the
     parallel = serial parity guarantee.
  9. End-to-end: ``optimizer.run(workers=1)`` and ``optimizer.run(workers=2)``
     produce identical top-N results on a shared mock database.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import psycopg2
import pytest

from pulse_bot.config import PUMPFUN_FEE_PCT, PUMPFUN_PRIORITY_FEE, PulseBotConfig
from pulse_bot.core import PaperTradeRunner, decide_entry
from pulse_bot.db import Database
from pulse_bot.filters.fast import FastResult
from pulse_bot.models import ScoringResult, Token, Trade
from pulse_bot.optimizer import CachedToken, Optimizer, _worker_init, _worker_run_combo

# ---------------------------------------------------------------------------
# Fixtures & helpers
# ---------------------------------------------------------------------------


def _mk_trade(
    ts: float,
    price: float,
    *,
    mint: str = "M",
    tx_type: str = "buy",
    sol: float = 0.1,
    wallet: str | None = None,
    is_creator: bool = False,
    v_sol: float = 30.0,
) -> Trade:
    """Build a Trade at ``ts`` whose price = sol/token = ``price``."""
    token_amount = sol / price if price > 0 else 1.0
    return Trade(
        mint=mint,
        wallet=wallet or f"W{int(ts)}",
        tx_type=tx_type,
        sol_amount=sol,
        token_amount=token_amount,
        new_token_balance=0.0,
        bonding_curve_key="",
        v_sol_in_bonding_curve=v_sol,
        v_tokens_in_bonding_curve=0.0,
        market_cap_sol=v_sol,
        timestamp=ts,
        is_creator=is_creator,
    )


def _cfg(**overrides: object) -> PulseBotConfig:
    """Config with deterministic exits — turn everything OFF by default."""
    cfg = PulseBotConfig()
    cfg.exit_take_profit_pct = 10_000.0  # unreachable
    cfg.exit_hard_stop_loss_pct = 99.0  # very wide — only hit deliberately
    cfg.exit_trailing_stop_enabled = False
    cfg.exit_on_creator_dump = False
    cfg.exit_on_whale = False
    cfg.exit_inactivity_seconds = 0.0
    cfg.exit_min_hold_seconds = 0.0
    cfg.pulse_min_events = 9_999  # never emit snapshot → no pulse exit
    cfg.pulse_dead_buy_rate = -1.0
    cfg.exit_no_new_wallets_events = 9_999
    cfg.exit_trend_dying_count = 9_999
    cfg.exit_sell_pressure_ratio = 9_999.0
    cfg.portfolio_max_positions = 3
    cfg.buy_amount_sol = 0.1
    cfg.portfolio_initial_sol = 1.0
    # Disable execution slippage so hand-derived PnL formulas match runner.
    cfg.execution_base_slippage = 0.0
    for k, v in overrides.items():
        setattr(cfg, k, v)
    return cfg


def _token(mint: str = "M", created_at: float = 1_000.0, creator: str = "C") -> Token:
    return Token(
        mint=mint,
        name="",
        symbol=mint,
        creator=creator,
        created_at=created_at,
        uri="",
        launchpad="pumpfun",
    )


# ---------------------------------------------------------------------------
# 1. PaperTradeRunner.process_trade — no silent early exit
# ---------------------------------------------------------------------------


class TestPaperTradeRunnerProcessesAllTrades:
    """Addresses suspected bug: "does process_trade silently exit after 1 trade?"."""

    def test_processes_every_trade_when_no_exit_fires(self) -> None:
        cfg = _cfg()
        runner = PaperTradeRunner(cfg, entry_price=0.001)
        trades = [_mk_trade(10.0 + i, 0.001) for i in range(20)]

        exits = 0
        for t in trades:
            if runner.process_trade(t, entry_time=0.0) is not None:
                exits += 1

        assert exits == 0, "no exit condition is configured → must never exit early"
        assert runner.total_buys == 20
        assert runner.total_sells == 0

    def test_exits_exactly_once_on_hard_stop(self) -> None:
        """Verifies the loop stops when an exit fires, not before."""
        cfg = _cfg(exit_hard_stop_loss_pct=20.0)
        runner = PaperTradeRunner(cfg, entry_price=0.001)

        # First 5 trades at entry price (no exit), 6th drops price by 50% (exit).
        pre = [_mk_trade(10.0 + i, 0.001) for i in range(5)]
        trigger = _mk_trade(20.0, 0.0005)  # −50% vs entry
        post = [_mk_trade(30.0 + i, 0.0005) for i in range(5)]

        first_exit_at = None
        for i, t in enumerate([*pre, trigger, *post]):
            result = runner.process_trade(t, entry_time=0.0)
            if result is not None:
                first_exit_at = i
                assert result.exit_reason == "hard_stop"
                break

        assert first_exit_at == 5, "exit must fire on the drop trade, not before"


# ---------------------------------------------------------------------------
# 2. PnL arithmetic with fees
# ---------------------------------------------------------------------------


class TestPaperTradeRunnerPnlMath:
    def test_flat_price_returns_negative_pnl_from_fees(self) -> None:
        """At current==entry, PnL must equal -2*fee% - priority_cost%."""
        cfg = _cfg(buy_amount_sol=0.1)
        runner = PaperTradeRunner(cfg, entry_price=0.001)
        # current_price left at entry (no trade processed)
        pnl = runner._calc_pnl()

        # Derivation: effective_exit/effective_entry = (1-fee)/(1+fee)
        expected_raw = ((1 - PUMPFUN_FEE_PCT) / (1 + PUMPFUN_FEE_PCT) - 1) * 100
        priority_cost_pct = (2 * PUMPFUN_PRIORITY_FEE / 0.1) * 100
        assert pnl == pytest.approx(expected_raw - priority_cost_pct, rel=1e-9)

    def test_zero_entry_price_returns_zero(self) -> None:
        cfg = _cfg()
        runner = PaperTradeRunner(cfg, entry_price=0.0)
        assert runner._calc_pnl() == 0.0


# ---------------------------------------------------------------------------
# 3. _simulate_trade_from — exit paths
# ---------------------------------------------------------------------------


class TestSimulateTradeFrom:
    def test_empty_monitor_with_inactivity_emits_dead_token(self) -> None:
        cfg = _cfg(exit_inactivity_seconds=120.0)
        cached = CachedToken(
            token=_token(),
            monitor_trades=[],
            creator_snapshot=None,
            creator_tokens_today=0,
            entry_price=0.001,
        )
        result = Optimizer._simulate_trade_from(
            cached,
            "full",
            25,
            0.001,
            entry_time=500.0,
            cfg=cfg,
        )
        assert result["exit_reason"] == "dead_token"
        assert result["exit_time"] == pytest.approx(500.0 + 120.0)
        assert result["hold_seconds"] == pytest.approx(120.0)
        # No price movement → PnL is the fee-only baseline, i.e. negative but finite.
        assert result["exit_price"] == pytest.approx(0.001)

    def test_empty_monitor_no_inactivity_emits_timeout(self) -> None:
        cfg = _cfg(exit_inactivity_seconds=0.0)
        cached = CachedToken(
            token=_token(),
            monitor_trades=[],
            creator_snapshot=None,
            creator_tokens_today=0,
            entry_price=0.001,
        )
        result = Optimizer._simulate_trade_from(
            cached,
            "full",
            25,
            0.001,
            entry_time=500.0,
            cfg=cfg,
        )
        assert result["exit_reason"] == "timeout"
        # exit_time == entry_time → hold=0, slot freed immediately in Phase 2.
        assert result["exit_time"] == pytest.approx(500.0)
        assert result["hold_seconds"] == pytest.approx(0.0)

    def test_inactivity_gap_mid_stream_triggers_dead_token(self) -> None:
        """A big gap between monitor trades must fire dead_token at the boundary."""
        cfg = _cfg(exit_inactivity_seconds=60.0)
        # Entry at 500. First monitor at 520 (gap 20s from entry). Next at 700 (gap 180s).
        monitor = [_mk_trade(520.0, 0.001), _mk_trade(700.0, 0.001)]
        cached = CachedToken(
            token=_token(),
            monitor_trades=monitor,
            creator_snapshot=None,
            creator_tokens_today=0,
            entry_price=0.001,
        )
        result = Optimizer._simulate_trade_from(
            cached,
            "full",
            25,
            0.001,
            entry_time=500.0,
            cfg=cfg,
        )
        assert result["exit_reason"] == "dead_token"
        # Boundary is ``last_trade_ts + inactivity`` == 520 + 60 = 580.
        assert result["exit_time"] == pytest.approx(520.0 + 60.0)

    def test_hard_stop_uses_current_price_not_synthetic_stop(self) -> None:
        """PaperTradeRunner returns the actual trigger trade's price for
        hard_stop (NOT a synthetic ``entry × (1 − SL/100)``).

        2026-05-12 semantic change (codex review): ``exit_hard_stop_loss_pct``
        is a floor on the after-fee leg PnL. The trigger fires on the
        first trade whose price pushes leg PnL below that floor. Using
        a synthetic stop_price + re-running ``_weighted_pnl`` over it
        was double-counting fees and inflating reported losses by ~6-7pp.
        Honest behaviour: report the price at which the trigger actually
        fired so ``pnl_pct`` matches the trigger condition exactly.
        """
        from pulse_bot.core import calc_pnl_pct

        cfg = _cfg(exit_hard_stop_loss_pct=25.0)
        trigger = _mk_trade(510.0, 0.0005)  # −50% price move triggers SL=25%
        cached = CachedToken(
            token=_token(),
            monitor_trades=[trigger],
            creator_snapshot=None,
            creator_tokens_today=0,
            entry_price=0.001,
        )
        result = Optimizer._simulate_trade_from(
            cached,
            "full",
            25,
            0.001,
            entry_time=500.0,
            cfg=cfg,
        )
        expected_pnl = calc_pnl_pct(0.001, 0.0005, cfg.buy_amount_sol)
        assert result["exit_reason"] == "hard_stop"
        assert result["exit_price"] == pytest.approx(0.0005)
        assert result["pnl_pct"] == pytest.approx(expected_pnl)

    def test_stream_ends_without_exit_matches_live_dead_token(self) -> None:
        """Regression for BUG #1 (codex review 2026-04-17).

        When ``monitor_trades`` is **non-empty** and neither an exit signal
        nor an inactivity gap fires, the optimizer used to fall through to
        ``exit_reason="timeout"`` with ``exit_time=last_trade_ts`` — freeing
        portfolio slots ~6s after entry. Live ``pipeline.py::_paper_trade``
        (lines ~490-501) closes the position as ``dead_token`` at
        ``last_event_ts + exit_inactivity_seconds`` when inactivity tracking
        is enabled.
        """
        cfg = _cfg(exit_inactivity_seconds=300.0)
        # Two quiet monitor trades, close together (no gap triggers dead_token
        # mid-stream). Stream ends at ts=510 → live would hold until 510+300.
        monitor = [_mk_trade(505.0, 0.001), _mk_trade(510.0, 0.001)]
        cached = CachedToken(
            token=_token(),
            monitor_trades=monitor,
            creator_snapshot=None,
            creator_tokens_today=0,
            entry_price=0.001,
        )
        result = Optimizer._simulate_trade_from(
            cached,
            "full",
            25,
            0.001,
            entry_time=500.0,
            cfg=cfg,
        )
        assert (
            result["exit_reason"] == "dead_token"
        ), "stream-ended with inactivity>0 must mirror live dead_token semantics"
        assert result["exit_time"] == pytest.approx(510.0 + 300.0)
        assert result["hold_seconds"] == pytest.approx(310.0)

    def test_stream_ends_without_exit_no_inactivity_is_timeout(self) -> None:
        """Mirror of the fix above but with inactivity=0 — keep legacy timeout."""
        cfg = _cfg(exit_inactivity_seconds=0.0)
        monitor = [_mk_trade(505.0, 0.001), _mk_trade(510.0, 0.001)]
        cached = CachedToken(
            token=_token(),
            monitor_trades=monitor,
            creator_snapshot=None,
            creator_tokens_today=0,
            entry_price=0.001,
        )
        result = Optimizer._simulate_trade_from(
            cached,
            "full",
            25,
            0.001,
            entry_time=500.0,
            cfg=cfg,
        )
        assert result["exit_reason"] == "timeout"
        assert result["exit_time"] == pytest.approx(510.0)  # last trade ts


# ---------------------------------------------------------------------------
# 3b. decide_entry — entry_type must respect entry_mode
# ---------------------------------------------------------------------------


def _fast_result(decision: str, buy_count: int, score: int = 20) -> FastResult:
    return FastResult(
        decision=decision,
        score=score,
        reasons="",
        buy_count=buy_count,
        sell_count=0,
        unique_buyers=buy_count,
        volume_sol=1.0,
        buy_rate=1.0,
        sell_ratio=0.0,
        curve_pct=10.0,
        elapsed=5.0,
    )


def _full_result(
    decision: str, buy_count: int, total_score: int = 30, exit_price: float = 0.001
) -> ScoringResult:
    return ScoringResult(
        decision=decision,
        total_score=total_score,
        buy_count=buy_count,
        exit_price=exit_price,
    )


class TestDecideEntryRespectsMode:
    """Regression for BUG #2 (codex review 2026-04-17).

    ``decide_entry`` used to set ``entry_type = "fast" if is_fast else "full"``
    without consulting ``config.entry_mode``. In full-only runs with an
    incidental FAST_BUY, that caused:
      1. The buyer-number gate to use ``fast_result.buy_count`` instead of
         ``full_result.buy_count``.
      2. Run metadata to record fast_buys inside full-only sessions
         (99 460 / 99 824 = 99.6% of full-mode runs in the tainted session).
    """

    def test_full_mode_with_incidental_fast_buy_uses_full_window(self) -> None:
        cfg = PulseBotConfig()
        cfg.entry_mode = "full"
        cfg.min_entry_buyer_number = 1
        cfg.max_entry_buyer_number = 999

        # Fast window says BUY (3 buys); full window says BUY (10 buys).
        fast = _fast_result("FAST_BUY", buy_count=3)
        full = _full_result("BUY", buy_count=10)

        should_enter, entry_type, _, _ = decide_entry(fast, full, cfg)
        assert should_enter is True
        assert (
            entry_type == "full"
        ), "in entry_mode=full, entry_type must reflect the mode, not fast_result"

    def test_full_mode_buyer_gate_uses_full_buy_count(self) -> None:
        """Given fast_count<min and full_count>=min, full-mode must still enter."""
        cfg = PulseBotConfig()
        cfg.entry_mode = "full"
        cfg.min_entry_buyer_number = 8
        cfg.max_entry_buyer_number = 30

        # Fast window has only 3 buys → fast gate (3+1=4) would reject.
        # Full window has 10 buys → full gate (10+1=11) accepts.
        fast = _fast_result("FAST_BUY", buy_count=3)
        full = _full_result("BUY", buy_count=10)

        should_enter, entry_type, _, _ = decide_entry(fast, full, cfg)
        assert (
            should_enter is True
        ), "full-mode must apply the buyer gate against full window, not fast"
        assert entry_type == "full"

    def test_fast_mode_keeps_fast_entry_type(self) -> None:
        """Sanity: entry_mode=fast still picks entry_type=fast."""
        cfg = PulseBotConfig()
        cfg.entry_mode = "fast"
        cfg.min_entry_buyer_number = 1
        cfg.max_entry_buyer_number = 999

        fast = _fast_result("FAST_BUY", buy_count=5)
        full = _full_result("SKIP", buy_count=2, total_score=5)

        should_enter, entry_type, _, _ = decide_entry(fast, full, cfg)
        assert should_enter is True
        assert entry_type == "fast"

    def test_both_mode_prefers_actual_trigger(self) -> None:
        """In entry_mode=both, entry_type reflects which side fired."""
        cfg = PulseBotConfig()
        cfg.entry_mode = "both"
        cfg.min_entry_buyer_number = 1
        cfg.max_entry_buyer_number = 999

        # Fast WAIT, full BUY → entry_type must be "full"
        fast = _fast_result("WAIT", buy_count=2)
        full = _full_result("BUY", buy_count=10)
        _, t1, _, _ = decide_entry(fast, full, cfg)
        assert t1 == "full"

        # Fast BUY, full SKIP → entry_type must be "fast"
        fast = _fast_result("FAST_BUY", buy_count=5)
        full = _full_result("SKIP", buy_count=2, total_score=5)
        _, t2, _, _ = decide_entry(fast, full, cfg)
        assert t2 == "fast"


# ---------------------------------------------------------------------------
# 4. _make_trade_result arithmetic
# ---------------------------------------------------------------------------


class TestMakeTradeResult:
    def test_pnl_sol_matches_buy_amount_times_pct(self) -> None:
        cfg = _cfg(buy_amount_sol=0.2)
        cached = CachedToken(
            token=_token(),
            monitor_trades=[],
            creator_snapshot=None,
            creator_tokens_today=0,
            entry_price=0.001,
        )
        out = Optimizer._make_trade_result(
            cached,
            "full",
            25,
            exit_price=0.0015,
            entry_price=0.001,
            exit_reason="take_profit",
            exit_ts=1_100.0,
            entry_time=1_000.0,
            pnl_pct=50.0,
            cfg=cfg,
        )
        assert out["pnl_sol"] == pytest.approx(0.2 * 0.5)  # 0.1 SOL
        assert out["sol_received"] == pytest.approx(0.2 + 0.1)  # 0.3 SOL
        assert out["hold_seconds"] == pytest.approx(100.0)
        assert out["mint"] == "M"
        assert out["exit_reason"] == "take_profit"

    def test_hold_seconds_clamped_nonnegative(self) -> None:
        cfg = _cfg()
        cached = CachedToken(
            token=_token(),
            monitor_trades=[],
            creator_snapshot=None,
            creator_tokens_today=0,
            entry_price=0.001,
        )
        out = Optimizer._make_trade_result(
            cached,
            "full",
            25,
            exit_price=0.001,
            entry_price=0.001,
            exit_reason="timeout",
            exit_ts=100.0,
            entry_time=200.0,
            pnl_pct=0.0,
            cfg=cfg,  # exit before entry
        )
        assert out["hold_seconds"] == 0.0


# ---------------------------------------------------------------------------
# 5. Portfolio max_positions enforcement
# ---------------------------------------------------------------------------


class TestMaxPositionsEnforcement:
    """The event-driven sim pops freed slots before each new entry.

    Boundary semantics depend on exit_reason (mirrors BacktestEngine):
    * inactivity-like (``dead_token``/``timeout``/``backtest_end``) free the
      slot at ``exit_time <= entry_time`` — BT closes them in
      ``_close_expired_positions`` BEFORE admitting same-timestamp entries.
    * ExitManager-driven (``take_profit``/``hard_stop``/…) require
      ``exit_time < entry_time`` — BT runs them in ``_handle_position_trade``
      AFTER same-timestamp entries are admitted.
    """

    def _run(self, cfg: PulseBotConfig, candidates: list[tuple]) -> list[dict]:
        # Zero-monitor cached tokens → deterministic exit_time = entry_time + inactivity.
        kept = {
            mint: CachedToken(
                token=_token(mint=mint),
                monitor_trades=[],
                creator_snapshot=None,
                creator_tokens_today=0,
                entry_price=0.001,
            )
            for _, mint, *_ in candidates
        }
        opt = Optimizer.__new__(Optimizer)
        return opt._simulate_combo_event_driven(
            {"cfg": cfg, "candidates": candidates},
            kept,
        )

    def test_third_candidate_skipped_when_cap_is_two(self) -> None:
        cfg = _cfg(portfolio_max_positions=2, exit_inactivity_seconds=100.0)
        # All three enter inside the 100s inactivity window → cap should cut #3.
        candidates = [
            (1000.0, "A", "full", 25, 0.001),
            (1020.0, "B", "full", 25, 0.001),
            (1050.0, "C", "full", 25, 0.001),
        ]
        closed = self._run(cfg, candidates)
        mints = [t["mint"] for t in closed]
        assert mints == ["A", "B"], "C must be dropped: slots held by A and B"

    def test_boundary_equal_exit_frees_slot(self) -> None:
        """Entry at t = exit_time of a ``dead_token`` exit frees the slot.

        ``dead_token`` is inactivity-like → slot is freed at the boundary.
        See class docstring for the full exit-reason → boundary mapping.
        """
        cfg = _cfg(portfolio_max_positions=1, exit_inactivity_seconds=50.0)
        # A exits at 1050.0 (1000+50) as dead_token. B enters at exactly 1050.0.
        candidates = [
            (1000.0, "A", "full", 25, 0.001),
            (1050.0, "B", "full", 25, 0.001),
        ]
        closed = self._run(cfg, candidates)
        assert [t["mint"] for t in closed] == ["A", "B"]
        assert closed[0]["exit_reason"] == "dead_token"

    def test_boundary_overlap_drops_candidate(self) -> None:
        """Entry strictly before previous exit is blocked."""
        cfg = _cfg(portfolio_max_positions=1, exit_inactivity_seconds=50.0)
        candidates = [
            (1000.0, "A", "full", 25, 0.001),
            (1049.999, "B", "full", 25, 0.001),  # just before A's exit
        ]
        closed = self._run(cfg, candidates)
        assert [t["mint"] for t in closed] == ["A"]

    def test_exitmanager_exit_does_not_free_slot_at_boundary(self) -> None:
        """ExitManager-driven exits (take_profit/hard_stop/…) do NOT release
        the slot at the same-timestamp boundary.

        Mirrors BacktestEngine, which closes ExitManager exits inside
        ``_handle_position_trade`` AFTER same-timestamp entries are admitted.
        Only inactivity-like exits (``dead_token``/``timeout``) fire in
        ``_close_expired_positions`` BEFORE entries and therefore free the
        slot at the boundary.
        """
        cfg = _cfg(
            portfolio_max_positions=1,
            exit_inactivity_seconds=0.0,  # disable inactivity → no dead_token
            exit_hard_stop_loss_pct=30.0,  # tight SL — fires on every trade, no pulse needed
        )
        # A's monitor has a -70% trade at 1050 → hard_stop fires at exactly 1050.
        # ``hard_stop`` is ExitManager-class for slot purposes: BT fires it
        # inside ``_handle_position_trade`` (post-admission), so it MUST NOT
        # free the slot at the boundary.
        take_profit_trade = _mk_trade(1050.0, 0.0003)
        kept = {
            "A": CachedToken(
                token=_token(mint="A"),
                monitor_trades=[take_profit_trade],
                creator_snapshot=None,
                creator_tokens_today=0,
                entry_price=0.001,
            ),
            "B": CachedToken(
                token=_token(mint="B"),
                monitor_trades=[],
                creator_snapshot=None,
                creator_tokens_today=0,
                entry_price=0.001,
            ),
        }
        # B wants to enter at exactly A's take_profit timestamp (1050).
        candidates = [
            (1000.0, "A", "full", 25, 0.001),
            (1050.0, "B", "full", 25, 0.001),
        ]
        opt = Optimizer.__new__(Optimizer)
        closed = opt._simulate_combo_event_driven(
            {"cfg": cfg, "candidates": candidates},
            kept,
        )
        # A's hard_stop exit does NOT free slot at boundary → B blocked.
        assert [t["mint"] for t in closed] == ["A"], (
            "hard_stop at t=1050 must NOT free slot for same-ts entry; "
            "only dead_token/timeout do."
        )
        assert closed[0]["exit_reason"] == "hard_stop"


# ---------------------------------------------------------------------------
# 5b. Combo grid canonicalisation (no-op dimension collapse)
# ---------------------------------------------------------------------------


class TestComboGridCanonicalisation:
    """``entry_mode='full'`` ignores fast-window params → those dimensions
    must be collapsed to a single canonical value so semantically identical
    combos don't pollute the grid and top-N results (codex SMELL #1).
    """

    def _opt_with_grid(self, grid: dict) -> Optimizer:
        opt = Optimizer.__new__(Optimizer)
        opt._base_cfg = _cfg()
        opt._grid = grid
        return opt

    def test_full_mode_collapses_fast_noop_dimensions(self) -> None:
        """All combos with ``entry_mode='full'`` have the SAME fast params."""
        opt = self._opt_with_grid(
            {
                "entry_mode": ["full"],
                "fast_observe_seconds": [3, 5, 8],
                "fast_score_threshold": [10, 15, 25],
                "score_threshold_buy": [20, 30],
            }
        )
        combos = list(opt._iter_combos())
        # 3 x 3 fast combinations collapse to 1 → total = 1 * 1 * 1 * 2 = 2.
        assert len(combos) == 2
        for c in combos:
            assert c["fast_observe_seconds"] == 3  # first value
            assert c["fast_score_threshold"] == 10
        # score_threshold_buy still varies.
        assert {c["score_threshold_buy"] for c in combos} == {20, 30}

    def test_fast_mode_retains_all_dimensions(self) -> None:
        """``entry_mode='fast'`` must NOT collapse fast params (they matter)."""
        opt = self._opt_with_grid(
            {
                "entry_mode": ["fast"],
                "fast_observe_seconds": [3, 5, 8],
                "fast_score_threshold": [10, 15, 25],
            }
        )
        combos = list(opt._iter_combos())
        # No collapsing → 3 * 3 = 9.
        assert len(combos) == 9
        unique_fast = {
            (c["fast_observe_seconds"], c["fast_score_threshold"]) for c in combos
        }
        assert len(unique_fast) == 9

    def test_mixed_modes_collapse_only_full(self) -> None:
        """When the grid spans multiple modes, only ``full`` rows collapse."""
        opt = self._opt_with_grid(
            {
                "entry_mode": ["fast", "full", "both"],
                "fast_observe_seconds": [3, 5],
                "fast_score_threshold": [10, 15],
            }
        )
        combos = list(opt._iter_combos())
        by_mode: dict[str, list[dict]] = {"fast": [], "full": [], "both": []}
        for c in combos:
            by_mode[c["entry_mode"]].append(c)
        assert len(by_mode["fast"]) == 4  # 2 x 2 retained
        assert len(by_mode["both"]) == 4  # 2 x 2 retained
        assert len(by_mode["full"]) == 1  # collapsed to (3, 10)
        assert by_mode["full"][0]["fast_observe_seconds"] == 3
        assert by_mode["full"][0]["fast_score_threshold"] == 10


# ---------------------------------------------------------------------------
# 6. _build_result metrics
# ---------------------------------------------------------------------------


class TestBuildResultMetrics:
    def test_winrate_pf_roi_math(self) -> None:
        opt = Optimizer.__new__(Optimizer)
        opt._session_id = "TEST"

        closed = [
            {
                "pnl_sol": 0.20,
                "pnl_pct": 40.0,
                "exit_reason": "take_profit",
                "entry_type": "full",
                "hold_seconds": 60.0,
            },
            {
                "pnl_sol": -0.05,
                "pnl_pct": -10.0,
                "exit_reason": "hard_stop",
                "entry_type": "full",
                "hold_seconds": 30.0,
            },
            {
                "pnl_sol": 0.10,
                "pnl_pct": 20.0,
                "exit_reason": "take_profit",
                "entry_type": "fast",
                "hold_seconds": 40.0,
            },
        ]
        cfg = _cfg(portfolio_initial_sol=1.0)
        r = opt._build_result("rid", {"k": 1}, cfg, closed)

        assert r["total_trades"] == 3
        assert r["wins"] == 2
        assert r["losses"] == 1
        assert r["win_rate"] == pytest.approx(200 / 3)
        assert r["total_pnl_sol"] == pytest.approx(0.25)
        assert r["gross_profit_sol"] == pytest.approx(0.30)
        assert r["gross_loss_sol"] == pytest.approx(0.05)
        assert r["profit_factor"] == pytest.approx(0.30 / 0.05)
        assert r["roi_pct"] == pytest.approx(25.0)
        assert r["avg_hold_seconds"] == pytest.approx((60 + 30 + 40) / 3)
        assert r["fast_buys"] == 1
        assert r["full_buys"] == 2
        assert json.loads(r["exit_reasons"]) == {"take_profit": 2, "hard_stop": 1}


# ---------------------------------------------------------------------------
# 7. _preload_dataset: integration against a tiny in-memory-ish SQLite snapshot
# ---------------------------------------------------------------------------


def _populate_snapshot(dsn: str, tokens: list[dict], trades: list[dict]) -> None:
    """Populate a PostgreSQL test database with minimal token/trade data."""
    conn = psycopg2.connect(dsn)
    conn.autocommit = True
    try:
        seen_creators: set[str] = set()
        with conn.cursor() as cur:
            for t in tokens:
                creator = t.get("creator", "")
                cur.execute(
                    "INSERT INTO tokens (mint, name, symbol, creator, created_at, uri, launchpad)"
                    " VALUES (%s,%s,%s,%s,%s,%s,%s)",
                    (
                        t["mint"],
                        t.get("name", ""),
                        t.get("symbol", ""),
                        creator,
                        t["created_at"],
                        "",
                        "pumpfun",
                    ),
                )
                if creator and creator not in seen_creators:
                    seen_creators.add(creator)
                    cur.execute(
                        "INSERT INTO creators (wallet, total_tokens_created, times_seen,"
                        " tokens_where_creator_sold_early, first_seen_at, last_seen_at, blacklisted)"
                        " VALUES (%s,%s,%s,%s,%s,%s,%s)",
                        (creator, 1, 1, 0, t["created_at"], t["created_at"], 0),
                    )
            for tr in trades:
                cur.execute(
                    "INSERT INTO trades (mint, wallet, tx_type, sol_amount, token_amount,"
                    " v_sol_in_bonding_curve, market_cap_sol, timestamp, is_creator)"
                    " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                    (
                        tr["mint"],
                        tr.get("wallet", "W"),
                        tr["tx_type"],
                        tr["sol_amount"],
                        tr["token_amount"],
                        tr.get("v_sol", 30.0),
                        tr.get("v_sol", 30.0),
                        tr["timestamp"],
                        int(tr.get("is_creator", 0)),
                    ),
                )
    finally:
        conn.close()


class TestPreloadDataset:
    def test_drops_tokens_with_no_trades(self, tmp_path: Path, pg_test_db: str) -> None:
        _populate_snapshot(
            pg_test_db,
            tokens=[
                {"mint": "WITH", "created_at": 1000.0, "creator": "A"},
                {"mint": "EMPTY", "created_at": 1000.0, "creator": "B"},
            ],
            trades=[
                {
                    "mint": "WITH",
                    "tx_type": "buy",
                    "sol_amount": 0.1,
                    "token_amount": 100.0,
                    "timestamp": 1001.0,
                },
            ],
        )
        cfg = _cfg()
        cfg.db_path = pg_test_db
        cfg.observe_seconds = 45
        opt = Optimizer(cfg, Database(pg_test_db))
        opt._snapshot_path = pg_test_db  # pretend the snapshot is already built

        records = opt._preload_dataset()
        mints = [r.token.mint for r in records]
        assert mints == ["WITH"], "zero-trade tokens must be dropped"

    def test_entry_price_uses_last_buy_in_full_window(
        self, tmp_path: Path, pg_test_db: str
    ) -> None:
        """Entry price = sol/token of the LAST valid buy inside the full window."""
        _populate_snapshot(
            pg_test_db,
            tokens=[{"mint": "M", "created_at": 1000.0, "creator": "A"}],
            trades=[
                # Inside full window (≤ 1045): two buys with different prices
                {
                    "mint": "M",
                    "tx_type": "buy",
                    "sol_amount": 0.1,
                    "token_amount": 100.0,
                    "timestamp": 1010.0,
                },  # price 0.001
                {
                    "mint": "M",
                    "tx_type": "sell",
                    "sol_amount": 0.05,
                    "token_amount": 25.0,
                    "timestamp": 1020.0,
                },  # ignored (sell)
                {
                    "mint": "M",
                    "tx_type": "buy",
                    "sol_amount": 0.2,
                    "token_amount": 100.0,
                    "timestamp": 1030.0,
                },  # price 0.002 → pick this
                # Post-window monitor trade (> 1045)
                {
                    "mint": "M",
                    "tx_type": "buy",
                    "sol_amount": 0.5,
                    "token_amount": 100.0,
                    "timestamp": 1060.0,
                },
            ],
        )
        cfg = _cfg()
        cfg.db_path = pg_test_db
        cfg.observe_seconds = 45
        opt = Optimizer(cfg, Database(pg_test_db))
        opt._snapshot_path = pg_test_db

        [rec] = opt._preload_dataset()
        assert rec.entry_price == pytest.approx(0.002)
        assert len(rec.full_trades) == 3
        assert len(rec.monitor_trades) == 1
        assert rec.monitor_trades[0].timestamp == 1060.0


# ---------------------------------------------------------------------------
# 8. Worker Phase-1 parity: _worker_run_combo emits same candidates as serial
# ---------------------------------------------------------------------------


class TestWorkerSerialParity:
    """Same base config + same dataset ⇒ same candidates in both paths."""

    def test_worker_matches_serial_stream(
        self, tmp_path: Path, pg_test_db: str
    ) -> None:
        # Two tokens with enough buys to trigger entry in default-permissive cfg.
        tokens = [
            {"mint": f"T{i}", "created_at": 1000.0 + i * 100, "creator": f"C{i}"}
            for i in range(3)
        ]
        trades = []
        for i, tok in enumerate(tokens):
            # 10 buys from distinct wallets inside the full window (≤ +45s)
            for k in range(10):
                trades.append(
                    {
                        "mint": tok["mint"],
                        "wallet": f"W{i}_{k}",
                        "tx_type": "buy",
                        "sol_amount": 0.1,
                        "token_amount": 100.0,
                        "timestamp": tok["created_at"] + 1 + k,
                        "v_sol": 10.0,
                    }
                )
        _populate_snapshot(pg_test_db, tokens, trades)

        cfg = _cfg()
        cfg.db_path = pg_test_db
        cfg.observe_seconds = 45
        cfg.min_entry_buyer_number = 1
        cfg.max_entry_buyer_number = 999
        cfg.score_threshold_buy = -999  # accept any score
        cfg.fast_score_threshold = -999
        cfg.min_market_cap_sol = 0.0

        opt = Optimizer(cfg, Database(pg_test_db))
        opt._snapshot_path = pg_test_db

        # Serial Phase-1
        combo_ctx = {
            "idx": 0,
            "params": {},
            "cfg": cfg,
            "fast_filter": __import__(
                "pulse_bot.filters.fast", fromlist=["FastFilter"]
            ).FastFilter(cfg),
            "scorer": __import__(
                "pulse_bot.filters.scorer", fromlist=["Scorer"]
            ).Scorer(cfg, opt._db),
            "candidates": [],
            "run_id": "r",
        }
        opt._stream_and_evaluate([combo_ctx])
        serial_candidates = sorted(combo_ctx["candidates"])

        # Parallel Phase-1 via the worker helper (in-process, fork not needed).
        dataset = opt._preload_dataset()
        _worker_init(dataset, cfg)
        try:
            _, _, closed = _worker_run_combo(("r", {}))
        finally:
            # Clean up module-global state so other tests see a fresh worker ctx.
            from pulse_bot import optimizer as _opt_mod

            _opt_mod._WORKER_DATA.clear()

        # Reconstruct candidates list from closed trades (entry_time + mint).
        worker_entries = sorted((t["entry_time"], t["mint"]) for t in closed)
        serial_entries = sorted((c[0], c[1]) for c in serial_candidates)
        assert (
            worker_entries == serial_entries
        ), "parallel worker must choose the same entries as the serial path"


# ---------------------------------------------------------------------------
# 9. End-to-end parity: run(workers=1) == run(workers=2)
# ---------------------------------------------------------------------------


class TestSerialVsParallelEndToEnd:
    """The parallel path is the riskiest change — this test proves it is an
    exact re-arrangement of the serial work, not a different algorithm.
    """

    def _build_db(self, path: str) -> None:
        tokens = [
            {"mint": f"M{i}", "created_at": 1000.0 + i * 1000, "creator": f"C{i}"}
            for i in range(4)
        ]
        trades = []
        for i, tok in enumerate(tokens):
            # full-window buys (entries will be attempted)
            for k in range(10):
                trades.append(
                    {
                        "mint": tok["mint"],
                        "wallet": f"W{i}_{k}",
                        "tx_type": "buy",
                        "sol_amount": 0.05,
                        "token_amount": 50.0,
                        "timestamp": tok["created_at"] + 1 + k,
                        "v_sol": 10.0,
                    }
                )
            # monitor-window trades: two tokens pump (price doubles), two dump.
            post_price = 0.002 if i % 2 == 0 else 0.0001
            for k in range(6):
                trades.append(
                    {
                        "mint": tok["mint"],
                        "wallet": f"P{i}_{k}",
                        "tx_type": "buy",
                        "sol_amount": 0.05,
                        "token_amount": 0.05 / post_price,
                        "timestamp": tok["created_at"] + 50 + k,
                        "v_sol": 20.0,
                    }
                )
        _populate_snapshot(path, tokens, trades)

    def _run(self, snap: str, out_db: str, workers: int) -> list[dict]:
        cfg = _cfg()
        cfg.db_path = snap
        cfg.observe_seconds = 45
        cfg.optimizer_db_path = out_db
        cfg.min_entry_buyer_number = 1
        cfg.max_entry_buyer_number = 999
        cfg.score_threshold_buy = -999
        cfg.fast_score_threshold = -999

        opt_db = Database(out_db)
        opt_db.init_schema()
        opt = Optimizer(cfg, opt_db)
        # Tiny 3-combo grid so the test is fast and deterministic.
        opt.set_grid(
            {
                "entry_mode": ["full"],
                "exit_hard_stop_loss_pct": [20.0, 50.0, 99.0],
            }
        )
        return opt.run(max_combos=0, workers=workers)

    def test_parallel_matches_serial(self, tmp_path: Path, pg_test_db: str) -> None:
        self._build_db(pg_test_db)

        serial = self._run(pg_test_db, pg_test_db, workers=1)
        parallel = self._run(pg_test_db, pg_test_db, workers=2)

        def _fingerprint(results: list[dict]) -> list[tuple]:
            return sorted(
                (
                    json.dumps(json.loads(r["params"]), sort_keys=True),
                    r["total_trades"],
                    round(r["total_pnl_sol"], 9),
                    round(r["win_rate"], 6),
                )
                for r in results
            )

        assert _fingerprint(serial) == _fingerprint(
            parallel
        ), "serial and parallel paths must produce identical top-N results"
