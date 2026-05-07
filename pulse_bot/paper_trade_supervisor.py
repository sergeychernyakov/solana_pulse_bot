# pulse_bot/paper_trade_supervisor.py
"""PaperTradeSupervisor — owns the paper-trade lifecycle.

Codex review 2026-04-28 (architecture phase B step 2): this is a
relocation of ``Pipeline._paper_trade`` (310 lines) into a dedicated
module without changing the runtime semantics. The hot path is
**bit-identical** to the previous inline implementation:

* Same DB calls in the same order.
* Same close_lock single-flight guard.
* Same trade_stream + tick_loop concurrency pattern.
* Same exit_ts arithmetic for timeout / dead_token.
* Same finally-block: open_slots decrement + launchpad unsubscribe.

What this buys us:
* ``Pipeline`` shrinks by ~310 lines; the god-object loses its biggest
  single method.
* The supervisor accepts ``ctx`` (the Pipeline instance) as the
  dependency carrier — same code, different file. Future refactors
  can replace ``ctx`` with a typed protocol once we've decoupled.
* Critical-invariant tests (``test_critical_invariants.py`` resume
  semantics, hard-exit-not-blockable-by-ML) have a clearer surface
  to target.

This file does NOT attempt to fully decouple the supervisor from
Pipeline yet — that's Phase B step 3 (ObservationSession). Doing it
in one shot was rejected as too risky on a live bot.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from pulse_bot.models import Token

from pulse_bot.ml import shadow

logger = logging.getLogger(__name__)


class PaperTradeSupervisor:
    """Run a single paper trade end-to-end.

    Args:
        ctx: A ``Pipeline``-shaped object exposing ``_db``, ``_launchpad``,
            ``_config``, ``_open_slots``, ``_resolve_tick_seconds``,
            ``_maybe_survival_exit``, ``_survival_active``,
            ``_SURVIVAL_TICK_SECONDS``. Treated as an opaque dependency
            container — the supervisor calls back into it for everything
            that crosses the bot/storage boundary.

    Use:
        supervisor = PaperTradeSupervisor(pipeline)
        await supervisor.run(token, entry_price=..., ...)
    """

    def __init__(self, ctx: Any) -> None:
        self._ctx = ctx

    async def run(
        self,
        token: "Token",
        entry_price: float,
        entry_mcap: float,
        entry_buyer_num: int,
        entry_type: str,
        entry_score: int,
        entry_ts: float,
        resume_trade_id: int | None = None,
        resume_last_event_ts: float | None = None,
    ) -> None:
        """Open + monitor + close a paper trade. Same contract as the
        old ``Pipeline._paper_trade``."""
        ctx = self._ctx
        is_replay = ctx._launchpad.name == "replay"

        if resume_trade_id is not None:
            trade_id = resume_trade_id
            logger.info(
                "PAPER RESUME %s: price=%e buyer#%d",
                token.symbol, entry_price, entry_buyer_num,
            )
            await ctx._launchpad.subscribe_trades(token.mint)
        else:
            trade_id = await ctx._db.open_paper_trade(
                {
                    "mint": token.mint,
                    "symbol": token.symbol,
                    "entry_price": entry_price,
                    "entry_time": entry_ts,
                    "entry_mcap_sol": entry_mcap,
                    "entry_buyer_number": entry_buyer_num,
                    "entry_type": entry_type,
                    "entry_score": entry_score,
                    "buy_amount_sol": ctx._config.buy_amount_sol,
                }
            )
            logger.info(
                "PAPER BUY %s: price=%e buyer#%d mcap=%.1f type=%s score=%d",
                token.symbol, entry_price, entry_buyer_num,
                entry_mcap, entry_type, entry_score,
            )

        from pulse_bot.core import PaperTradeRunner

        runner = PaperTradeRunner(
            ctx._config, entry_price, mint=token.mint, scored_at=entry_ts,
        )
        last_event_ts = (
            resume_last_event_ts if resume_last_event_ts is not None else entry_ts
        )
        tick_seconds = ctx._resolve_tick_seconds()
        close_lock = asyncio.Lock()
        closed = False
        close_outcome: dict[str, object] = {}

        async def _close_via_runner_result(
            result, trade_market_cap: float, exit_time: float
        ) -> bool:
            """Atomic close. Returns True if this caller performed the close."""
            nonlocal closed
            async with close_lock:
                if closed:
                    return False
                closed = True
                close_outcome["reason"] = result.exit_reason
            await ctx._db.close_paper_trade(
                trade_id,
                result.exit_price,
                result.exit_reason,
                result.total_buys + result.total_sells,
                trade_market_cap,
                entry_price,
                ctx._config.buy_amount_sol,
                exit_time=exit_time,
                pnl_pct=result.pnl_pct,
            )
            logger.info(
                "PAPER SELL %s: pnl=%+.1f%% reason=%s hold=%.0fs",
                token.symbol, result.pnl_pct, result.exit_reason,
                max(exit_time - entry_ts, 0.0),
            )
            return True

        deadline = ctx._config.exit_max_hold_seconds
        inactivity = ctx._config.exit_inactivity_seconds

        async def _trade_stream_loop() -> None:
            nonlocal last_event_ts
            async for trade in ctx._launchpad.stream_trades(
                token.mint, deadline, inactivity_timeout=inactivity
            ):
                if closed:
                    return
                last_event_ts = trade.timestamp

                if not is_replay:
                    await ctx._db.insert_trades_batch([trade])
                    try:
                        await ctx._db.upsert_wallet_activity_from_trades([trade])
                    except Exception as exc:
                        logger.warning(
                            "wallet_activity upsert (monitor) failed: %s", exc
                        )

                await ctx._db.update_paper_trade(
                    trade_id, runner.current_price, entry_price,
                    runner.total_buys, runner.total_sells, trade.market_cap_sol,
                )
                await ctx._db.update_live_price(
                    token.mint, runner.current_price, entry_price
                )

                result = runner.process_trade(trade, entry_ts)
                if result:
                    await _close_via_runner_result(
                        result,
                        trade_market_cap=trade.market_cap_sol,
                        exit_time=trade.timestamp,
                    )
                    return

        async def _tick_loop() -> None:
            if tick_seconds <= 0 or is_replay:
                return
            last_survival_check_ts: float = 0.0
            try:
                while not closed:
                    await asyncio.sleep(tick_seconds)
                    if closed:
                        return
                    now = time.time()
                    result = runner.tick(now, entry_ts)
                    if result:
                        await _close_via_runner_result(
                            result, trade_market_cap=0.0, exit_time=now,
                        )
                        return
                    if (
                        getattr(ctx, "_survival_active", False)
                        or shadow.survival_shadow_enabled()
                    ) and (
                        now - last_survival_check_ts >= ctx._SURVIVAL_TICK_SECONDS
                    ):
                        last_survival_check_ts = now
                        survived = await ctx._maybe_survival_exit(
                            runner=runner, entry_ts=entry_ts,
                            now=now, mint=token.mint,
                        )
                        if survived is not None:
                            await _close_via_runner_result(
                                survived, trade_market_cap=0.0, exit_time=now,
                            )
                            return
            except asyncio.CancelledError:
                raise

        try:
            stream_task = asyncio.create_task(_trade_stream_loop())
            tick_task = asyncio.create_task(_tick_loop())
            try:
                done, pending = await asyncio.wait(
                    {stream_task, tick_task},
                    return_when=asyncio.FIRST_COMPLETED,
                )
                for t in pending:
                    t.cancel()
                if pending:
                    await asyncio.gather(*pending, return_exceptions=True)
                for t in done:
                    exc = t.exception()
                    if exc is not None:
                        raise exc
            finally:
                if not stream_task.done():
                    stream_task.cancel()
                if not tick_task.done():
                    tick_task.cancel()

            if closed:
                return

            # Stream ended without an exit decision — timeout / dead_token.
            exit_ts: float | None
            if inactivity > 0:
                reason = "dead_token"
                exit_ts = last_event_ts + inactivity
                inactive = float(inactivity)
            else:
                reason = "timeout"
                exit_ts = last_event_ts
                inactive = 0.0

            timeout = runner.timeout_result()
            async with close_lock:
                if closed:
                    return
                closed = True
            await ctx._db.close_paper_trade(
                trade_id, timeout.exit_price, reason,
                timeout.total_buys + timeout.total_sells, 0,
                entry_price, ctx._config.buy_amount_sol,
                exit_time=exit_ts, pnl_pct=timeout.pnl_pct,
            )
            logger.info(
                "PAPER %s %s (inactive %.0fs)",
                reason.upper(), token.symbol, inactive,
            )

        except Exception:
            logger.exception("Paper trade error for %s", token.symbol)
            if not closed:
                try:
                    timeout = runner.timeout_result()
                    await ctx._db.close_paper_trade(
                        trade_id, timeout.exit_price, "error",
                        timeout.total_buys + timeout.total_sells, 0,
                        entry_price, ctx._config.buy_amount_sol,
                        exit_time=last_event_ts, pnl_pct=timeout.pnl_pct,
                    )
                except Exception:
                    logger.debug("Failed to close errored trade %s", token.symbol)
        finally:
            if ctx._open_slots > 0:
                ctx._open_slots -= 1
            await ctx._launchpad.unsubscribe_trades(token.mint)
