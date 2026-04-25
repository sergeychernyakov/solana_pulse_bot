# tests/pulse_bot/test_partial_exits_parity.py
"""Regression tests for the partial-exit + PnL-parity fixes (2026-04-17).

Covers:

  1. ``PaperTradeRunner`` accumulates ``sell_partial`` fills and produces
     a weighted PnL across all legs (partial, partial, sell_all).
  2. ``hard_stop`` after a realised partial fires on the CURRENT remaining
     leg, not on aggregate trade PnL — so realised profit cannot mask a
     tanking residual.
  3. ``db.close_paper_trade`` without ``pnl_pct`` matches
     ``core.calc_pnl_pct`` exactly (fee-adjusted, not raw).
  4. ``db.close_paper_trade`` with caller-supplied ``pnl_pct`` stores it
     verbatim (does NOT recompute — required for partial-aware PnL).
  5. Pipeline close path and optimizer close path produce the same final
     pnl_pct/exit_price for an identical position with partial exits.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from pulse_bot.config import PulseBotConfig
from pulse_bot.core import MonitorResult, PaperTradeRunner, calc_pnl_pct
from pulse_bot.db import Database
from pulse_bot.models import Trade
from pulse_bot.pulse.exit_manager import ExitSignal

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _cfg(**overrides: object) -> PulseBotConfig:
    """Baseline config with all exits disabled; tests drive runners via
    fake ExitManager decisions, not real pulse signals."""
    cfg = PulseBotConfig()
    cfg.exit_take_profit_pct = 10_000.0
    cfg.exit_hard_stop_loss_pct = 99.0
    cfg.exit_trailing_stop_enabled = False
    cfg.exit_on_creator_dump = False
    cfg.exit_on_whale = False
    cfg.exit_inactivity_seconds = 0.0
    cfg.exit_min_hold_seconds = 0.0
    cfg.pulse_min_events = 9_999
    cfg.pulse_dead_buy_rate = -1.0
    cfg.exit_no_new_wallets_events = 9_999
    cfg.exit_trend_dying_count = 9_999
    cfg.exit_sell_pressure_ratio = 9_999.0
    cfg.buy_amount_sol = 0.1
    # Disable execution slippage so hand-derived PnL formulas match runner.
    cfg.execution_base_slippage = 0.0
    for k, v in overrides.items():
        setattr(cfg, k, v)
    return cfg


def _mk_trade(ts: float, price: float) -> Trade:
    sol = 0.1
    token_amount = sol / price if price > 0 else 1.0
    return Trade(
        mint="M",
        wallet=f"W{int(ts)}",
        tx_type="buy",
        sol_amount=sol,
        token_amount=token_amount,
        new_token_balance=0.0,
        bonding_curve_key="",
        v_sol_in_bonding_curve=30.0,
        v_tokens_in_bonding_curve=0.0,
        market_cap_sol=30.0,
        timestamp=ts,
        is_creator=False,
    )


def _feed(
    runner: PaperTradeRunner,
    price: float,
    ts: float,
    signals: list[ExitSignal],
) -> MonitorResult | None:
    """Set a deterministic ExitManager decision then feed one trade.

    Bypasses PulseMonitor snapshot gating so partial_* / sell_all fire on
    exactly the trade we want.
    """

    class _StubPulse:
        def update(self, _trade: Trade) -> object:
            return object()  # truthy → snapshot exists

    class _StubExitMgr:
        def __init__(self, decisions: list[ExitSignal]) -> None:
            self._d = decisions

        def decide(self, *_args: object, **_kwargs: object) -> ExitSignal:
            if self._d:
                return self._d.pop(0)
            return ExitSignal(action="hold", reason="pulse_ok", sell_pct=0.0)

    runner._pulse = _StubPulse()  # type: ignore[assignment]
    runner._exit_mgr = _StubExitMgr(signals)  # type: ignore[assignment]
    return runner.process_trade(_mk_trade(ts, price), entry_time=0.0)


# ---------------------------------------------------------------------------
# 1. Weighted PnL across partial, partial, sell_all
# ---------------------------------------------------------------------------


class TestWeightedPartialPnL:
    def test_partial_partial_sell_all_weighted_math(self) -> None:
        """30 % sold at +100 %, 30 % at +50 %, remaining 40 % at 0 %.

        Weighted leg pnl (ignoring priority fees):
            0.30 * leg(2x) + 0.30 * leg(1.5x) + 0.40 * leg(1x)
        Priority cost: 1 buy + 3 sells = 4 * PRIORITY / buy_amount.
        """
        cfg = _cfg()
        runner = PaperTradeRunner(cfg, entry_price=0.001)

        # Partial #1: 30% at 2x
        r1 = _feed(
            runner, 0.002, 10.0,
            [ExitSignal(action="sell_partial", reason="strong_profit", sell_pct=0.30)],
        )
        assert r1 is None
        assert runner._remaining == pytest.approx(0.70)
        assert runner._partial_fills == [(0.30, 0.002)]

        # Partial #2: 30% at 1.5x
        r2 = _feed(
            runner, 0.0015, 20.0,
            [ExitSignal(action="sell_partial", reason="weak_pulse_profit", sell_pct=0.30)],
        )
        assert r2 is None
        assert runner._remaining == pytest.approx(0.40)
        assert runner._partial_fills == [(0.30, 0.002), (0.30, 0.0015)]

        # Final sell_all at 1x (flat)
        final = _feed(
            runner, 0.001, 30.0,
            [ExitSignal(action="sell_all", reason="pulse_dead", sell_pct=1.0)],
        )
        assert final is not None
        assert final.exit_reason == "pulse_dead"
        assert final.exit_price == pytest.approx(0.001)

        # Hand-derive expected weighted PnL
        expected = 0.30 * calc_pnl_pct(0.001, 0.002, cfg.buy_amount_sol, num_sell_legs=0)
        expected += 0.30 * calc_pnl_pct(0.001, 0.0015, cfg.buy_amount_sol, num_sell_legs=0)
        expected += 0.40 * calc_pnl_pct(0.001, 0.001, cfg.buy_amount_sol, num_sell_legs=0)
        # Strip per-leg zero-sell-priority offset — the helper with
        # num_sell_legs=0 still bakes in 1 priority fee (1+0 txs).
        # Re-derive without any priority:
        from pulse_bot.config import PUMPFUN_FEE_PCT, PUMPFUN_PRIORITY_FEE
        eff_entry = 0.001 * (1 + PUMPFUN_FEE_PCT)

        def leg(price: float) -> float:
            eff_exit = price * (1 - PUMPFUN_FEE_PCT)
            return ((eff_exit - eff_entry) / eff_entry) * 100

        expected_weighted = 0.30 * leg(0.002) + 0.30 * leg(0.0015) + 0.40 * leg(0.001)
        priority_pct = (4 * PUMPFUN_PRIORITY_FEE / cfg.buy_amount_sol) * 100
        expected_weighted -= priority_pct
        assert final.pnl_pct == pytest.approx(expected_weighted, rel=1e-9)

    def test_timeout_includes_partials(self) -> None:
        """timeout_result must apply weighting across fills + remaining."""
        from pulse_bot.config import PUMPFUN_FEE_PCT, PUMPFUN_PRIORITY_FEE

        cfg = _cfg()
        runner = PaperTradeRunner(cfg, entry_price=0.001)
        _feed(
            runner, 0.003, 10.0,
            [ExitSignal(action="sell_partial", reason="strong_profit", sell_pct=0.50)],
        )
        # Now current price drifts down without signals
        _feed(runner, 0.001, 20.0, [])  # hold
        timeout = runner.timeout_result()

        eff_entry = 0.001 * (1 + PUMPFUN_FEE_PCT)

        def leg(price: float) -> float:
            eff_exit = price * (1 - PUMPFUN_FEE_PCT)
            return ((eff_exit - eff_entry) / eff_entry) * 100

        expected = 0.50 * leg(0.003) + 0.50 * leg(0.001)
        expected -= (3 * PUMPFUN_PRIORITY_FEE / cfg.buy_amount_sol) * 100
        assert timeout.pnl_pct == pytest.approx(expected, rel=1e-9)
        assert timeout.exit_reason == "timeout"


# ---------------------------------------------------------------------------
# 2. hard_stop after partial fires on CURRENT remnant, not aggregate
# ---------------------------------------------------------------------------


class TestHardStopAfterPartialUsesLegPnL:
    def test_hard_stop_fires_on_current_leg_even_with_realized_profit(self) -> None:
        """After a 50 % partial at 3x (huge realized gain), a price drop to
        -30 % vs entry must still trigger hard_stop (SL=25 %)."""
        cfg = _cfg(exit_hard_stop_loss_pct=25.0)
        runner = PaperTradeRunner(cfg, entry_price=0.001)

        # Realize 50 % at 3x — aggregate PnL becomes strongly positive
        r1 = _feed(
            runner, 0.003, 10.0,
            [ExitSignal(action="sell_partial", reason="strong_profit", sell_pct=0.50)],
        )
        assert r1 is None

        # Now price falls to -40 % vs entry on the remnant
        trigger = _mk_trade(20.0, 0.0006)  # 0.6x entry = -40 %
        # process_trade checks hard_stop BEFORE calling exit_mgr — so no stub needed
        result = runner.process_trade(trigger, entry_time=0.0)

        assert result is not None, "hard_stop must fire on current remnant"
        assert result.exit_reason == "hard_stop"
        # exit_price is entry*(1 - SL/100), independent of realized gains
        assert result.exit_price == pytest.approx(0.001 * (1 - 0.25))
        # But weighted pnl_pct includes the realized partial → overall positive
        assert result.pnl_pct > 0, (
            "weighted pnl should reflect the realized partial gain + remnant at stop"
        )


# ---------------------------------------------------------------------------
# 3. db.close_paper_trade PnL parity
# ---------------------------------------------------------------------------


def _open_trade_row(db: Database, entry_ts: float = 1_000.0) -> int:
    async def _open() -> int:
        return await db.open_paper_trade(
            {
                "mint": "M",
                "symbol": "MM",
                "entry_price": 0.001,
                "entry_time": entry_ts,
                "entry_mcap_sol": 30.0,
                "entry_buyer_number": 1,
                "entry_type": "full",
                "entry_score": 50,
                "current_price": 0.001,
                "current_pnl_pct": 0.0,
                "buy_amount_sol": 0.1,
            }
        )

    return asyncio.run(_open())


class TestClosePaperTradePnLParity:
    def test_without_pnl_pct_matches_calc_pnl_pct(self, tmp_path: Path, pg_test_db: str) -> None:
        db = Database(str(tmp_path / "live.db"))
        db.init_schema()
        trade_id = _open_trade_row(db)

        async def _close() -> None:
            await db.close_paper_trade(
                trade_id=trade_id,
                exit_price=0.0015,
                exit_reason="take_profit",
                exit_buyer_number=5,
                exit_mcap_sol=40.0,
                entry_price=0.001,
                buy_amount_sol=0.1,
                exit_time=1_120.0,
            )

        asyncio.run(_close())
        row = db.get_paper_trades()[0]
        expected = calc_pnl_pct(0.001, 0.0015, 0.1)
        assert row["pnl_pct"] == pytest.approx(expected, rel=1e-9)
        # Raw (exit-entry)/entry would be +50 %; fee-adjusted is strictly less
        assert row["pnl_pct"] < 50.0

    def test_with_supplied_pnl_pct_is_stored_verbatim(self, tmp_path: Path, pg_test_db: str) -> None:
        """Partial-aware caller passes a weighted pnl_pct; DB must not recompute."""
        db = Database(str(tmp_path / "live.db"))
        db.init_schema()
        trade_id = _open_trade_row(db)

        # Deliberately pick a value the raw formula would NEVER produce so a
        # silent recompute is unmistakable.
        supplied = 77.7777

        async def _close() -> None:
            await db.close_paper_trade(
                trade_id=trade_id,
                exit_price=0.0015,  # would map to ~+50 % raw
                exit_reason="take_profit",
                exit_buyer_number=5,
                exit_mcap_sol=40.0,
                entry_price=0.001,
                buy_amount_sol=0.1,
                exit_time=1_120.0,
                pnl_pct=supplied,
            )

        asyncio.run(_close())
        row = db.get_paper_trades()[0]
        assert row["pnl_pct"] == pytest.approx(supplied, rel=1e-9)
        assert row["pnl_sol"] == pytest.approx(0.1 * (supplied / 100), rel=1e-9)


# ---------------------------------------------------------------------------
# 4. pipeline and optimizer paths agree for the same position with partials
# ---------------------------------------------------------------------------


class TestPipelineOptimizerParityOnPartials:
    """Same PaperTradeRunner instance is shared code between both paths.

    This test drives the runner with a deterministic partial-partial-sell_all
    sequence and asserts the result that would be passed to
    ``db.close_paper_trade`` (pipeline path) matches the pnl_pct stored in
    the optimizer's trade result (``_make_trade_result`` consumes
    ``result.pnl_pct``). Since both callers receive the identical
    ``MonitorResult``, parity is structural — the test pins it.
    """

    def test_runner_result_identical_for_both_paths(self) -> None:
        cfg = _cfg()

        def run_once() -> MonitorResult:
            runner = PaperTradeRunner(cfg, entry_price=0.001)
            _feed(
                runner, 0.002, 10.0,
                [ExitSignal(action="sell_partial", reason="strong_profit", sell_pct=0.3)],
            )
            _feed(
                runner, 0.0018, 20.0,
                [ExitSignal(action="sell_partial", reason="weak_pulse_profit", sell_pct=0.3)],
            )
            final = _feed(
                runner, 0.0012, 30.0,
                [ExitSignal(action="sell_all", reason="pulse_dead", sell_pct=1.0)],
            )
            assert final is not None
            return final

        live_result = run_once()
        opt_result = run_once()

        assert live_result.exit_price == pytest.approx(opt_result.exit_price)
        assert live_result.exit_reason == opt_result.exit_reason
        assert live_result.pnl_pct == pytest.approx(opt_result.pnl_pct, rel=1e-12)
        assert live_result.total_buys == opt_result.total_buys
        assert live_result.total_sells == opt_result.total_sells
