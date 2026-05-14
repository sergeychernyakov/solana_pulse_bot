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
from typing import TYPE_CHECKING, Any

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
        config_id: str = "LIVE",
        sim_entry_tokens_raw: int | None = None,
    ) -> None:
        """Open + monitor + close a paper trade. Same contract as the
        old ``Pipeline._paper_trade``.

        ``config_id`` (2026-05-13 multi-config A/B): tag the paper_trades
        row with which entry-decision policy opened it. Defaults to
        ``"LIVE"`` so callers that haven't migrated still work.

        ``sim_entry_tokens_raw`` (2026-05-14 real-sim exit): how many
        tokens the entry simulation said this size would buy. When set
        and this is the LIVE config, the close path runs a bonding-curve
        sell estimate and records it into ``sim_metadata.exit``.
        """
        ctx = self._ctx
        is_replay = ctx._launchpad.name == "replay"

        # Per-config exit overrides (2026-05-14 multi-config A/B): a
        # shadow config may A/B a different exit policy (TP / trailing /
        # max_hold). Build an exit-overridden copy of the global config
        # for THIS config's portfolio. Configs that set no exit overrides
        # (including LIVE) get ctx._config unchanged — zero behaviour
        # drift on the production portfolio.
        effective_config = ctx._config
        _registry = getattr(ctx, "_config_registry", None)
        if _registry is not None:
            try:
                _overrides = _registry.by_id(config_id).exit_overrides()
                if _overrides:
                    import dataclasses

                    effective_config = dataclasses.replace(ctx._config, **_overrides)
                    logger.info(
                        "PAPER [%s]: per-config exit overrides %s",
                        config_id,
                        _overrides,
                    )
            except Exception as cfg_exc:  # noqa: BLE001
                # Never block a paper trade on config resolution — fall
                # back to the global exit config.
                logger.warning(
                    "PAPER [%s]: exit-override resolution failed (%s) — "
                    "using global exit config",
                    config_id,
                    cfg_exc,
                )
                effective_config = ctx._config

        if resume_trade_id is not None:
            trade_id = resume_trade_id
            # Pull the existing row's buy_amount_sol so the resumed
            # runner uses the same size that was recorded at entry.
            resume_rows = ctx._db._sync_query(
                "SELECT buy_amount_sol FROM paper_trades WHERE id = ?",
                (resume_trade_id,),
                one=True,
            )
            buy_amount = float(
                (resume_rows or {}).get("buy_amount_sol") or ctx._config.buy_amount_sol
            )
            logger.info(
                "PAPER RESUME %s: price=%e buyer#%d size=%.3fSOL",
                token.symbol,
                entry_price,
                entry_buyer_num,
                buy_amount,
            )
            await ctx._launchpad.subscribe_trades(token.mint)
        else:
            # Dynamic position sizing: when enabled, compute next buy
            # from realized balance so wins compound and drawdowns shrink
            # the position. Falls back to fixed cfg.buy_amount_sol when
            # PULSE_DYNAMIC_SIZING_PCT=0 (default).
            from pulse_bot.config import compute_buy_amount_sol

            # Multi-config A/B: scope realized balance to THIS config so
            # each parallel paper portfolio compounds / draws down on its
            # own PnL, not a shared pool.
            realized_balance = ctx._db.get_realized_balance_sync(
                ctx._config.portfolio_initial_sol,
                config_id=config_id,
            )
            buy_amount = compute_buy_amount_sol(ctx._config, realized_balance)
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
                    "buy_amount_sol": buy_amount,
                    "config_id": config_id,
                }
            )
            logger.info(
                "PAPER BUY %s [%s]: price=%e buyer#%d mcap=%.1f type=%s score=%d "
                "size=%.3fSOL (balance=%.3f)",
                token.symbol,
                config_id,
                entry_price,
                entry_buyer_num,
                entry_mcap,
                entry_type,
                entry_score,
                buy_amount,
                realized_balance,
            )

        from pulse_bot.core import PaperTradeRunner

        runner = PaperTradeRunner(
            effective_config,
            entry_price,
            mint=token.mint,
            scored_at=entry_ts,
            buy_amount_sol_override=buy_amount,
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
                buy_amount,
                exit_time=exit_time,
                pnl_pct=result.pnl_pct,
            )
            logger.info(
                "PAPER SELL %s: pnl=%+.1f%% reason=%s hold=%.0fs",
                token.symbol,
                result.pnl_pct,
                result.exit_reason,
                max(exit_time - entry_ts, 0.0),
            )
            return True

        deadline = effective_config.exit_max_hold_seconds
        inactivity = effective_config.exit_inactivity_seconds

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
                    trade_id,
                    runner.current_price,
                    entry_price,
                    runner.total_buys,
                    runner.total_sells,
                    trade.market_cap_sol,
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
                            result,
                            trade_market_cap=0.0,
                            exit_time=now,
                        )
                        return
                    if (
                        getattr(ctx, "_survival_active", False)
                        or shadow.survival_shadow_enabled()
                    ) and (now - last_survival_check_ts >= ctx._SURVIVAL_TICK_SECONDS):
                        last_survival_check_ts = now
                        survived = await ctx._maybe_survival_exit(
                            runner=runner,
                            entry_ts=entry_ts,
                            now=now,
                            mint=token.mint,
                        )
                        if survived is not None:
                            await _close_via_runner_result(
                                survived,
                                trade_market_cap=0.0,
                                exit_time=now,
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
                trade_id,
                timeout.exit_price,
                reason,
                timeout.total_buys + timeout.total_sells,
                0,
                entry_price,
                buy_amount,
                exit_time=exit_ts,
                pnl_pct=timeout.pnl_pct,
            )
            logger.info(
                "PAPER %s %s (inactive %.0fs)",
                reason.upper(),
                token.symbol,
                inactive,
            )

        except Exception:
            logger.exception("Paper trade error for %s", token.symbol)
            if not closed:
                try:
                    timeout = runner.timeout_result()
                    await ctx._db.close_paper_trade(
                        trade_id,
                        timeout.exit_price,
                        "error",
                        timeout.total_buys + timeout.total_sells,
                        0,
                        entry_price,
                        buy_amount,
                        exit_time=last_event_ts,
                        pnl_pct=timeout.pnl_pct,
                    )
                except Exception:
                    logger.debug("Failed to close errored trade %s", token.symbol)
        finally:
            # 2026-05-14 real-sim exit: once the trade has closed, record
            # a bonding-curve sell estimate into sim_metadata.exit. Only
            # for the LIVE config (the entry sim is attached to that row)
            # and only when the entry sim gave us a token amount. Pure
            # curve math against live state — no real position needed,
            # 0 SOL. Best-effort: never let it break the finally block.
            sim_exec = getattr(ctx, "_sim_executor", None)
            if (
                closed
                and config_id == ctx._live_config_id
                and sim_entry_tokens_raw
                and sim_exec is not None
                and getattr(sim_exec, "enabled", False)
            ):
                try:
                    exit_res = await sim_exec.estimate_exit_curve_math(
                        token.mint, int(sim_entry_tokens_raw)
                    )
                    ctx._db.set_paper_trade_sim_metadata(
                        trade_id,
                        {
                            "exit": {
                                "success": exit_res.success,
                                "expected_sol_out_lamports": exit_res.expected_sol_out_lamports,
                                "tokens_in_raw": exit_res.tokens_in_raw,
                                "slippage_bps_cap": exit_res.slippage_bps_cap,
                                "err": (
                                    str(exit_res.err)
                                    if exit_res.err is not None
                                    else None
                                ),
                                "reason": close_outcome.get("reason"),
                            }
                        },
                    )
                    logger.info(
                        "REAL_SIM exit %s [%s]: success=%s sol_out=%d err=%s",
                        token.symbol,
                        config_id,
                        exit_res.success,
                        exit_res.expected_sol_out_lamports,
                        exit_res.err,
                    )
                except Exception as exc:
                    logger.warning(
                        "REAL_SIM exit estimate failed for %s: %s",
                        token.symbol,
                        exc,
                    )
            # 2026-05-13 multi-config: decrement THIS config's slot
            # counter, not a global one — each parallel paper portfolio
            # owns its own portfolio_max_positions budget. Falls back to
            # the LIVE bucket if the config_id is somehow unknown.
            slots = getattr(ctx, "_open_slots_by_config", None)
            if isinstance(slots, dict):
                key = config_id if config_id in slots else "LIVE"
                if slots.get(key, 0) > 0:
                    slots[key] -= 1
            elif ctx._open_slots > 0:  # legacy single-config fallback
                ctx._open_slots -= 1
            await ctx._launchpad.unsubscribe_trades(token.mint)
