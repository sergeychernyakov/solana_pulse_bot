# pulse_bot/pipeline.py
"""Two-phase pipeline: fast entry (5s) + full confirmation (45s)."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pulse_bot.config import PulseBotConfig
    from pulse_bot.db import Database
    from pulse_bot.filters.fast import FastFilter
    from pulse_bot.filters.scorer import Scorer
    from pulse_bot.helius_creator import CreatorSnapshotService
    from pulse_bot.helius_holders import HeliusHolderClient
    from pulse_bot.helius_onchain import HeliusOnchainClient
    from pulse_bot.launchpads.base import Launchpad
    from pulse_bot.ml.policy import EntryMLPolicy
    from pulse_bot.models import CreatorStats, Token, Trade

from pulse_bot.ml.policy import (get_active_policy_name,
                                 load_entry_policy_if_available)

logger = logging.getLogger(__name__)


class Pipeline:
    """Two-phase async pipeline.

    Phase 1 (fast, 5s): Collect trades → FastFilter → FAST_BUY or WAIT
    Phase 2 (full, 45s): Continue collecting → Scorer → BUY/SKIP/BORDERLINE
    Both results stored per token for analysis.
    """

    def __init__(
        self,
        config: PulseBotConfig,
        db: Database,
        launchpad: Launchpad,
        scorer: Scorer,
        fast_filter: FastFilter,
        creator_snap_service: CreatorSnapshotService | None = None,
        holder_client: "HeliusHolderClient | None" = None,
        onchain_client: "HeliusOnchainClient | None" = None,
    ) -> None:
        self._config = config
        self._db = db
        self._launchpad = launchpad
        self._scorer = scorer
        self._fast_filter = fast_filter
        self._creator_snap_service = creator_snap_service
        self._holder_client = holder_client
        # Optional on-chain state fetcher. Populates tokens.mint_authority_revoked
        # + tokens.freeze_authority_revoked. Data flows to DB only — these
        # are NOT in ENTRY_FEATURE_ORDER yet (zero-variance on pump.fun,
        # per codex 2026-04-22).
        self._onchain_client = onchain_client
        self._onchain_tasks: set[asyncio.Task] = set()
        # SOL/USD price cache — logs current price at scoring time to
        # token_scores.sol_price_usd. Same "capture now, feature later"
        # pattern as onchain state.
        from pulse_bot.market_context import SOLPriceCache

        self._sol_price = SOLPriceCache()
        # Holder-snapshot capture tasks (non-blocking). Bounded via
        # ``_holder_sem`` (50 concurrent fetches max) to prevent
        # unbounded growth when many tokens are awaiting their T+30
        # capture slot (codex v8 audit).
        self._holder_tasks: set[asyncio.Task] = set()
        self._holder_sem: asyncio.Semaphore | None = None
        # Wire DB failure callback so RPC drops don't silently bias data.
        if holder_client is not None:

            def _on_fail(mint, target_age, err_type, detail):
                try:
                    self._db.save_holder_capture_failure(
                        mint, target_age, err_type, detail
                    )
                except Exception:
                    logger.debug("fail-log insert failed", exc_info=True)

            holder_client.on_failure = _on_fail
        self._semaphore = asyncio.Semaphore(config.max_concurrent_observations)
        self._running = False
        self._tokens_seen = 0
        self._tokens_scored = 0
        self._fast_buys = 0
        # Shadow-capture tasks we've kicked off; retained only so we can
        # log exceptions without blocking the main stream.
        self._shadow_tasks: set[asyncio.Task] = set()
        # Atomic slot reservation: _count_open_trades() reads from DB as the
        # source of truth on startup (_resume_open_trades seeds this), but at
        # runtime we reserve slots synchronously in _open_slots so concurrent
        # _handle_token coroutines cannot all pass the cap guard before the
        # first one persists its INSERT (portfolio_max_positions race).
        self._open_slots: int = 0
        # ML entry policy: loads entry model once at start, None if the
        # model file is missing (fresh clone before weekly_retrain).
        #
        # Two modes, controlled by ``PULSE_POLICY`` env var:
        #   * ``rules`` (default): ML proba + feature vector written to
        #     token_scores alongside rule-based decisions for post-hoc
        #     comparison. ML never drives live BUY/SELL.
        #   * ``hybrid``: ML's confidence-gated verdict OVERRIDES rules
        #     when it is confident (``BUY`` / ``SKIP``). Rules handle
        #     the grey zone. Every ML override is logged at WARN so
        #     regressions are visible immediately.
        self._ml_entry_policy: "EntryMLPolicy | None" = load_entry_policy_if_available()
        self._policy_mode: str = get_active_policy_name()
        if self._ml_entry_policy is not None:
            logger.info(
                "ML entry policy loaded (mode=%s). model_hash=%s threshold=%.2f "
                "floor=%.3f ceiling=%.3f",
                self._policy_mode,
                self._ml_entry_policy.model_hash[:16],
                self._ml_entry_policy.threshold,
                self._ml_entry_policy.proba_floor,
                self._ml_entry_policy.proba_ceiling,
            )
        elif self._policy_mode == "hybrid":
            logger.warning(
                "PULSE_POLICY=hybrid but no entry model available — "
                "falling back to rules-only until model is trained.",
            )
            self._policy_mode = "rules"
        self._ml_overrides_buy: int = 0
        self._ml_overrides_skip: int = 0
        # Exit ML status log — confirms at boot whether the exit advisor
        # is loaded and whether it can actively escalate rule-based holds.
        from pulse_bot.ml.policy import load_exit_policy_if_available

        _exit_pol = load_exit_policy_if_available()
        if _exit_pol is None:
            logger.info("Exit ML: no model loaded (advisor disabled).")
        elif getattr(self._config, "exit_ml_active", False):
            logger.info(
                "Exit ML ACTIVE: model_hash=%s threshold=%.2f min_hold=%.0fs "
                "— will escalate hold→sell_all when proba>=threshold. "
                "Hard rules (creator_dump/hard_stop/timeout/etc.) remain "
                "immutable.",
                _exit_pol.model_hash[:16],
                self._config.exit_ml_sell_threshold,
                self._config.exit_ml_min_hold_seconds,
            )
        else:
            logger.info(
                "Exit ML shadow-only: model_hash=%s — proba logged to "
                "ExitSignal.ml_exit_proba but never overrides rules. "
                "Set PULSE_EXIT_ML_ACTIVE=1 to activate.",
                _exit_pol.model_hash[:16],
            )

    async def run(self) -> None:
        """Main entry point. Connect to WS and process tokens until interrupted."""
        self._running = True
        logger.info(
            "Pipeline starting — fast=%ds, full=%ds, max_concurrent=%d, fast_threshold=%d, full_threshold=%d",
            self._config.fast_observe_seconds,
            self._config.observe_seconds,
            self._config.max_concurrent_observations,
            self._config.fast_score_threshold,
            self._config.score_threshold_buy,
        )

        await self._launchpad.connect()
        tasks: list[asyncio.Task] = []

        # Resume monitoring open trades from previous run
        await self._resume_open_trades()

        try:
            async for token in self._launchpad.stream_new_tokens():
                if not self._running:
                    break
                self._tokens_seen += 1

                # Single code path: insert is idempotent; creator snapshot
                # is derived leak-free from the tokens table as-of the new
                # token's created_at, so live and replay see identical input.
                await self._db.insert_token(token)
                # Fire-and-forget on-chain state fetch. Captures
                # mint/freeze authority for this token. Cheap (1 RPC) and
                # adds a DB row that is NOT in feature schema yet — so
                # no risk to current model. When variance > 0 we can
                # enable as feature by bumping FEATURE_SCHEMA_VERSION.
                if self._onchain_client is not None:
                    self._schedule_onchain_capture(token.mint)
                creator_snapshot = self._db.get_creator_stats_as_of_sync(
                    token.creator, ref_mint=token.mint
                )
                if self._launchpad.name != "replay":
                    # Side-effects for live only: the cumulative creators
                    # table is now cache/metadata, never read for scoring.
                    await self._db.upsert_creator(token.creator, sold_early=False)
                    self._shadow_capture_creator(token.creator)

                # Both live and replay: parallel processing, deterministic snapshot
                task = asyncio.create_task(
                    self._handle_token_bounded(token, creator_snapshot)
                )
                tasks.append(task)
        except asyncio.CancelledError:
            logger.info("Pipeline cancelled")

        # Wait for all in-flight token handlers to complete
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

        # Drain in-flight holder captures (up to ~125s of T+120 delay
        # tasks) with a bounded timeout so shutdown stays responsive.
        if self._holder_tasks:
            try:
                await asyncio.wait_for(
                    asyncio.gather(*self._holder_tasks, return_exceptions=True),
                    timeout=125.0,
                )
            except TimeoutError:
                logger.warning(
                    "Holder capture drain timed out with %d tasks pending",
                    len(self._holder_tasks),
                )
        if self._holder_client is not None:
            try:
                await self._holder_client.close()
            except Exception:
                logger.debug("holder session close failed", exc_info=True)
        if self._onchain_client is not None:
            try:
                await self._onchain_client.close()
            except Exception:
                logger.debug("onchain session close failed", exc_info=True)
        try:
            await self._sol_price.close()
        except Exception:
            logger.debug("sol price session close failed", exc_info=True)

        await self._launchpad.disconnect()
        logger.info(
            "Pipeline stopped — seen=%d, scored=%d, fast_buys=%d",
            self._tokens_seen,
            self._tokens_scored,
            self._fast_buys,
        )

    def stop(self) -> None:
        """Signal the pipeline to stop gracefully."""
        self._running = False

    def _shadow_capture_creator(self, creator: str) -> None:
        """Fire-and-forget creator snapshot refresh for the new enrichment
        pipeline (#48). Runs in shadow mode: the result is persisted for
        analysis but is not consumed by Scorer — that wire-up happens in a
        later phase once we've verified data quality."""
        if self._creator_snap_service is None:
            return

        async def _run() -> None:
            try:
                await self._creator_snap_service.get_for_live(creator)
            except Exception:
                logger.exception("shadow creator capture failed for %s", creator)

        task = asyncio.create_task(_run())
        self._shadow_tasks.add(task)
        task.add_done_callback(self._shadow_tasks.discard)

    def _schedule_onchain_capture(self, mint: str) -> None:
        """Fire-and-forget SPL Mint state fetch for a new token.

        Reads mint_authority + freeze_authority state once (not
        time-series; these flags don't change post-launch except via
        explicit revoke txs). Writes to tokens.mint_authority_revoked +
        freeze_authority_revoked for future feature use.
        """
        if self._onchain_client is None:
            return

        async def _run() -> None:
            try:
                state = await self._onchain_client.fetch_mint_state(mint)
                if state is None or state.parse_error:
                    return
                await self._db.save_mint_onchain_state(
                    mint,
                    mint_authority_revoked=state.mint_authority_revoked,
                    freeze_authority_revoked=state.freeze_authority_revoked,
                )
            except Exception:
                logger.debug("onchain capture failed for %s", mint, exc_info=True)

        task = asyncio.create_task(_run())
        self._onchain_tasks.add(task)
        task.add_done_callback(self._onchain_tasks.discard)

    def _schedule_holder_capture(self, mint: str, created_at: float) -> None:
        """Schedule 3 holder snapshots (T+10s, T+30s, T+60s) for a new
        token. Bounded by semaphore (max 50 concurrent RPC calls).

        Pre-capture death detection via DB trades was removed — trades
        aren't inserted until after the 45s observation window, so at
        T+10 the check would always return "no trades" and censor the
        entire dataset. Analysis instead treats a mint with 3× parse_error
        in ``holder_capture_failures`` (and zero snapshot rows) as a
        pre-capture death class.
        """
        if self._holder_client is None:
            return
        from pulse_bot.helius_holders import CAPTURE_AGE_SECONDS

        if self._holder_sem is None:
            self._holder_sem = asyncio.Semaphore(50)

        async def _run_one(target_age: float) -> None:
            try:
                delay = target_age - (time.time() - created_at)
                if delay > 0:
                    await asyncio.sleep(delay)
                async with self._holder_sem:  # type: ignore[arg-type]
                    snap = await self._holder_client.fetch(mint, target_age)
                if snap is not None:
                    await asyncio.to_thread(self._db.save_holder_snapshot, snap)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("holder capture failed for %s", mint[:12])

        for age in CAPTURE_AGE_SECONDS:
            t = asyncio.create_task(_run_one(age))
            self._holder_tasks.add(t)
            t.add_done_callback(self._holder_tasks.discard)

    def _count_open_trades(self) -> int:
        """Count open paper trades from DB — the single source of truth."""
        import sqlite3

        conn = sqlite3.connect(self._config.db_path)
        count = conn.execute(
            "SELECT COUNT(*) FROM paper_trades WHERE status='open'"
        ).fetchone()[0]
        conn.close()
        return count

    async def _resume_open_trades(self) -> None:
        """Resume monitoring open paper trades from a previous pipeline run.

        Re-subscribes to WS and restarts _paper_trade tasks so positions
        are managed the same way as freshly opened ones.
        """
        import sqlite3

        from pulse_bot.models import Token

        conn = sqlite3.connect(self._config.db_path)
        conn.row_factory = sqlite3.Row
        open_trades = conn.execute(
            """SELECT p.id, p.mint, p.symbol, p.entry_price, p.entry_time,
                      p.entry_mcap_sol, p.entry_buyer_number, p.entry_type,
                      p.entry_score, p.buy_amount_sol, p.price_updated_at,
                      t.creator, t.created_at
               FROM paper_trades p
               LEFT JOIN tokens t ON t.mint = p.mint
               WHERE p.status='open'"""
        ).fetchall()
        conn.close()

        if not open_trades:
            return

        logger.info("Resuming %d open trades from previous run", len(open_trades))
        # Seed the in-memory slot counter so the cap enforces correctly on
        # resume. _paper_trade's finally-block will decrement as each closes.
        self._open_slots += len(open_trades)

        for t in open_trades:
            token = Token(
                mint=t["mint"],
                name=t["symbol"] or "",
                symbol=t["symbol"] or "",
                creator=t["creator"] or "",
                created_at=t["created_at"] or t["entry_time"] or 0,
                uri="",
                launchpad="pumpfun",
            )
            # Preserve the original open timestamp so hold_seconds stays
            # anchored to when the position was actually opened, not now.
            resume_entry_ts = float(t["entry_time"] or time.time())
            # Restore the last observed activity so a position that was
            # already idle pre-restart can close via dead_token immediately
            # instead of getting a fresh inactivity window.
            resume_last_event_ts = float(
                t["price_updated_at"] or t["entry_time"] or time.time()
            )
            asyncio.create_task(
                self._paper_trade(
                    token,
                    t["entry_price"] or 0,
                    t["entry_mcap_sol"] or 0,
                    t["entry_buyer_number"] or 0,
                    t["entry_type"] or "fast",
                    t["entry_score"] or 0,
                    resume_entry_ts,
                    resume_trade_id=t["id"],
                    resume_last_event_ts=resume_last_event_ts,
                )
            )

    async def _handle_token_bounded(
        self, token: Token, creator_snapshot: CreatorStats | None = None
    ) -> None:
        """Acquire semaphore, then process token."""
        async with self._semaphore:
            await self._handle_token(token, creator_snapshot)

    async def _handle_token(
        self, token: Token, creator_snapshot: CreatorStats | None = None
    ) -> None:
        """Two-phase pipeline for one token."""
        mint_short = token.mint[:12]

        try:
            logger.info(
                "New token: %s (%s) by %s", token.symbol, mint_short, token.creator[:12]
            )

            # Holder concentration collector (Apr 2026 redesign): fire for
            # EVERY new token, independent of any bot decision. See
            # helius_holders.py for why — in short, gating on FAST_BUY
            # contaminates the dataset with the broken filter's biases.
            # Ground truth comes from objective outcomes (graduation /
            # peak MC / death), not bot PnL. Replay launchpad doesn't
            # expose point-in-time on-chain state so skip there.
            if self._holder_client is not None and self._launchpad.name != "replay":
                self._schedule_holder_capture(token.mint, token.created_at)

            await self._launchpad.subscribe_trades(token.mint)

            # Single collection over the full observation window. We filter
            # trades by trade.timestamp after collection so live and replay
            # see the same deterministic set regardless of wall-clock jitter
            # or WS arrival latency. Live needs a small tail buffer so late-
            # arriving trades (whose block ts is inside the window but WS
            # delivery lagged) are captured before we cut.
            is_replay = self._launchpad.name == "replay"
            collect_duration = self._config.observe_seconds + (
                0.0 if is_replay else 2.0
            )
            collected: list[Trade] = []
            async for trade in self._launchpad.stream_trades(
                token.mint, collect_duration
            ):
                collected.append(trade)

            fast_end = token.created_at + self._config.fast_observe_seconds
            full_end = token.created_at + self._config.observe_seconds
            fast_trades = [t for t in collected if t.timestamp <= fast_end]
            all_trades = [t for t in collected if t.timestamp <= full_end]

            fast_result = self._fast_filter.evaluate(token, fast_trades)

            fast_entry_price = 0.0
            if fast_trades:
                fast_buys = [
                    t for t in fast_trades if t.tx_type == "buy" and t.token_amount > 0
                ]
                if fast_buys:
                    last = fast_buys[-1]
                    fast_entry_price = last.sol_amount / last.token_amount

            if fast_result.decision == "FAST_BUY":
                self._fast_buys += 1
                logger.info(
                    "FAST_BUY %s (%s): score=%d buyers=%d vol=%.2f rate=%.1f/s | %s",
                    token.symbol,
                    mint_short,
                    fast_result.score,
                    fast_result.unique_buyers,
                    fast_result.volume_sol,
                    fast_result.buy_rate,
                    fast_result.reasons[:80],
                )

            # Store all trades and get DB IDs (skip in replay — trades already in DB)
            if not is_replay:
                trade_ids = await self._db.insert_trades_batch(all_trades)
            else:
                trade_ids = [getattr(t, "_db_id", 0) for t in all_trades]
            fast_ids = trade_ids[: len(fast_trades)] if trade_ids else []
            full_ids = trade_ids if trade_ids else []

            # Market context derived deterministically from the tokens
            # table as-of this token's created_at. Same code for live and
            # replay — no semaphore or wall-clock reads.
            tokens_5min = self._db.get_tokens_last_5min_sync(ref_mint=token.mint)
            concurrent = self._db.get_concurrent_observations_sync(
                ref_mint=token.mint,
                observe_seconds=self._config.observe_seconds,
            )
            creator_tokens_today = self._db.get_creator_tokens_on_day_sync(
                token.creator, ref_mint=token.mint
            )
            result = self._scorer.score(
                token,
                all_trades,
                tokens_last_5min=tokens_5min,
                concurrent_observations=concurrent,
                creator_snapshot=creator_snapshot,
                creator_tokens_today=creator_tokens_today,
            )

            # Attach fast phase data
            result.source = "backtest" if self._launchpad.name == "replay" else "live"
            result.fast_trade_count = len(fast_trades)
            result.full_trade_count = len(all_trades)
            result.fast_trade_ids = ",".join(str(i) for i in fast_ids)
            result.full_trade_ids = ",".join(str(i) for i in full_ids)
            result.fast_decision = fast_result.decision
            result.fast_score = fast_result.score
            result.fast_reasons = fast_result.reasons
            result.fast_buy_count = fast_result.buy_count
            result.fast_volume_sol = fast_result.volume_sol
            result.fast_buy_rate = fast_result.buy_rate
            result.fast_unique_buyers = fast_result.unique_buyers
            result.fast_sell_ratio = fast_result.sell_ratio
            result.fast_elapsed = fast_result.elapsed
            result.fast_scored_at = token.created_at + self._config.fast_observe_seconds
            result.fast_entry_price = fast_entry_price

            # P&L at fast entry point vs end of full observation
            if fast_entry_price > 0 and result.exit_price > 0:
                result.pnl_at_fast_entry_pct = (
                    (result.exit_price - fast_entry_price) / fast_entry_price
                ) * 100.0

            await self._db.upsert_scoring_result(result)
            self._tokens_scored += 1

            # Fire-and-forget SOL price capture for external context.
            # Cached per-minute inside SOLPriceCache — cheap per call.
            sol_price = await self._sol_price.get()
            if sol_price is not None:
                try:
                    await self._db.save_sol_price(token.mint, sol_price)
                except Exception:
                    logger.debug("save_sol_price failed", exc_info=True)

            # Shadow ML logging — never drives live decisions; writes
            # ml_entry_proba + feature vector alongside rules output so
            # we can post-hoc compare ML-vs-rules without trading risk.
            # creator_snapshot is critical: prior to 2026-04-23 it was
            # never passed → 4 CREATOR_FEATURES silently zeroed at live
            # while the trained model expected real values (train/serve
            # skew). Now forwarded explicitly.
            if self._ml_entry_policy is not None:
                try:
                    proba = self._ml_entry_policy.predict_proba(
                        result, creator_snapshot=creator_snapshot
                    )
                    feat_json = self._ml_entry_policy.dump_features_json(
                        result, creator_snapshot=creator_snapshot
                    )
                    await self._db.save_ml_prediction(
                        mint=token.mint,
                        proba=proba,
                        model_hash=self._ml_entry_policy.model_hash,
                        feature_vector_json=feat_json,
                        schema_version=self._ml_entry_policy.schema_version,
                    )
                    # Reset the fail counter on success so transient blips
                    # don't accumulate into a false alarm.
                    self._ml_shadow_fails = 0
                except Exception as e:
                    # 2026-04-23: was logger.debug — that hid the creator
                    # skew bug for 3 months. Loud by default now: WARN on
                    # every failure with exc_info, ERROR escalation after
                    # 10 consecutive fails so an ongoing silent regression
                    # cannot sit in debug logs unnoticed.
                    self._ml_shadow_fails = getattr(self, "_ml_shadow_fails", 0) + 1
                    logger.warning(
                        "ML shadow predict FAILED for %s (%s): %s",
                        token.mint[:12],
                        type(e).__name__,
                        e,
                        exc_info=True,
                    )
                    if self._ml_shadow_fails >= 10:
                        logger.error(
                            "ML shadow predict has failed %d times in a "
                            "row. Model or feature pipeline is broken — "
                            "do not trust ml_entry_proba rows after this. "
                            "Check model schema + CreatorStats wiring.",
                            self._ml_shadow_fails,
                        )

            # Log
            log_fn = (
                logger.info
                if result.decision == "BUY" or fast_result.decision == "FAST_BUY"
                else logger.debug
            )
            log_fn(
                "Scored %s (%s): fast=%s(%d) full=%s(%d) buyers=%d vol=%.1f pnl_fast=%+.0f%%",
                token.symbol,
                mint_short,
                fast_result.decision,
                fast_result.score,
                result.decision,
                result.total_score,
                result.unique_buyers,
                result.buy_volume_sol,
                result.pnl_at_fast_entry_pct,
            )

            # Save live decision for backtest comparison
            await self._db.save_live_decision(
                {
                    "mint": token.mint,
                    "symbol": token.symbol,
                    "fast_decision": fast_result.decision,
                    "fast_score": fast_result.score,
                    "full_decision": result.decision,
                    "full_score": result.total_score,
                    "buy_count": result.buy_count,
                    "unique_buyers": result.unique_buyers,
                    "buy_volume_sol": result.buy_volume_sol,
                    "created_at": token.created_at,
                    "decided_at": result.scored_at,
                }
            )

            await self._db.log_event(
                "score",
                {
                    "mint": token.mint,
                    "symbol": token.symbol,
                    "fast": fast_result.decision,
                    "full": result.decision,
                    "fast_score": fast_result.score,
                    "full_score": result.total_score,
                },
            )

            # Entry decision — shared core logic
            from pulse_bot.core import decide_entry

            should_enter, entry_type, entry_score, entry_buyer_num = decide_entry(
                fast_result, result, self._config
            )

            # ── ML confidence-gating (hybrid mode) ────────────────
            # In hybrid mode ML has authority over entry when it is
            # confident. Mechanics:
            #   * action=BUY   → force should_enter=True (override rules
            #                     skip; ML saw a winner pattern).
            #   * action=SKIP  → force should_enter=False (override rules
            #                     buy; ML saw a loser pattern).
            #   * action=RULES → keep rules' verdict (grey zone).
            # Every override logs at WARN so regressions stay visible.
            ml_action = "N/A"
            ml_proba = None
            if self._policy_mode == "hybrid" and self._ml_entry_policy is not None:
                try:
                    ml_action, ml_proba, ml_cal = (
                        self._ml_entry_policy.decide_with_confidence(
                            result,
                            creator_snapshot=creator_snapshot,
                        )
                    )
                    if ml_action == "BUY" and not should_enter:
                        logger.warning(
                            "ML OVERRIDE %s: rules=SKIP → ML=BUY "
                            "(p_raw=%.3f p_cal=%.3f)",
                            mint_short,
                            ml_proba,
                            ml_cal,
                        )
                        should_enter = True
                        self._ml_overrides_buy += 1
                    elif ml_action == "SKIP" and should_enter:
                        logger.warning(
                            "ML OVERRIDE %s: rules=BUY → ML=SKIP "
                            "(p_raw=%.3f p_cal=%.3f)",
                            mint_short,
                            ml_proba,
                            ml_cal,
                        )
                        should_enter = False
                        self._ml_overrides_skip += 1
                    # else: agreement or grey zone → no change, no noise.
                except Exception as e:
                    # A broken ML policy must not silently sink live
                    # trading — loud WARN + fall back to rules.
                    logger.warning(
                        "ML decide_with_confidence FAILED for %s (%s): %s; "
                        "falling back to rules decision.",
                        mint_short,
                        type(e).__name__,
                        e,
                        exc_info=True,
                    )

            # Reserve a portfolio slot atomically before scheduling the paper
            # trade. asyncio is single-threaded, so the check+increment below
            # is race-free against other _handle_token coroutines — unlike a
            # DB-only count that they could all read before any INSERT lands.
            reserved = False
            # Collector mode: score every token for diagnostics but never
            # open paper trades — used to accumulate signal data (48-72h
            # target) without contaminating paper_trades during config tuning.
            if self._config.collector_only:
                should_enter = False
            if should_enter and self._open_slots < self._config.portfolio_max_positions:
                self._open_slots += 1
                reserved = True

            if reserved:
                # Entry timestamp lives in the SAME clock as the trade stream
                # so ExitManager.elapsed = trade.timestamp − entry_ts is not
                # skewed by provider latency or wall-clock drift. We prefer
                # the last observed scoring trade; fall back to end-of-window
                # (replay) or wall-clock (live with zero activity).
                if all_trades:
                    entry_ts = all_trades[-1].timestamp
                elif self._launchpad.name == "replay":
                    entry_ts = token.created_at + self._config.observe_seconds
                else:
                    entry_ts = time.time()
                # TODO(cross-model entry signal): entry_ml_proba was
                # threaded into _paper_trade here in exit_v2. Removed
                # with exit_v3 — restore alongside the feature (see
                # task #123) using the grey-zone/RULES + |pred|<3% gate.
                asyncio.create_task(
                    self._paper_trade(
                        token,
                        result.exit_price,
                        result.market_cap_sol,
                        entry_buyer_num,
                        entry_type,
                        entry_score,
                        entry_ts,
                    )
                )
            else:
                await self._launchpad.unsubscribe_trades(token.mint)

        except Exception:
            logger.exception("Error processing token %s (%s)", token.symbol, mint_short)
            await self._launchpad.unsubscribe_trades(token.mint)

    async def _paper_trade(
        self,
        token: Token,
        entry_price: float,
        entry_mcap: float,
        entry_buyer_num: int,
        entry_type: str,
        entry_score: int,
        entry_ts: float,
        resume_trade_id: int | None = None,
        resume_last_event_ts: float | None = None,
    ) -> None:
        """Virtual paper trade: open position, monitor with PulseMonitor, close on exit signal.

        ``entry_ts`` is the position-open timestamp in the event-stream clock
        (Helius/WS trade timestamps for live, replay-virtual trade timestamps
        for backtest). Using the same clock for both entry and elapsed avoids
        wall-clock drift against provider event timestamps. ``resume_last_event_ts``
        (live-only) carries the last-observed activity across restarts so an
        already-idle position can close via dead_token immediately instead of
        being granted a fresh inactivity window.
        """
        is_replay = self._launchpad.name == "replay"

        if resume_trade_id is not None:
            trade_id = resume_trade_id
            logger.info(
                "PAPER RESUME %s: price=%e buyer#%d",
                token.symbol,
                entry_price,
                entry_buyer_num,
            )
            await self._launchpad.subscribe_trades(token.mint)
        else:
            trade_id = await self._db.open_paper_trade(
                {
                    "mint": token.mint,
                    "symbol": token.symbol,
                    "entry_price": entry_price,
                    "entry_time": entry_ts,
                    "entry_mcap_sol": entry_mcap,
                    "entry_buyer_number": entry_buyer_num,
                    "entry_type": entry_type,
                    "entry_score": entry_score,
                    "buy_amount_sol": self._config.buy_amount_sol,
                }
            )
            logger.info(
                "PAPER BUY %s: price=%e buyer#%d mcap=%.1f type=%s score=%d",
                token.symbol,
                entry_price,
                entry_buyer_num,
                entry_mcap,
                entry_type,
                entry_score,
            )

        from pulse_bot.core import PaperTradeRunner

        runner = PaperTradeRunner(self._config, entry_price)
        # Last observed event, in the trade-stream clock. For resume, start
        # from the DB-recorded activity so a long idle period before restart
        # is NOT forgiven by a fresh inactivity window.
        last_event_ts = (
            resume_last_event_ts if resume_last_event_ts is not None else entry_ts
        )

        try:
            deadline = self._config.exit_max_hold_seconds
            inactivity = self._config.exit_inactivity_seconds
            async for trade in self._launchpad.stream_trades(
                token.mint, deadline, inactivity_timeout=inactivity
            ):
                last_event_ts = trade.timestamp

                # Save monitor trades to DB so replay/optimizer can use them
                if not is_replay:
                    await self._db.insert_trades_batch([trade])

                # Update paper trade in DB
                await self._db.update_paper_trade(
                    trade_id,
                    runner.current_price,
                    entry_price,
                    runner.total_buys,
                    runner.total_sells,
                    trade.market_cap_sol,
                )
                await self._db.update_live_price(
                    token.mint, runner.current_price, entry_price
                )

                # Core exit logic — same code as optimizer
                result = runner.process_trade(trade, entry_ts)
                if result:
                    await self._db.close_paper_trade(
                        trade_id,
                        result.exit_price,
                        result.exit_reason,
                        result.total_buys + result.total_sells,
                        trade.market_cap_sol,
                        entry_price,
                        self._config.buy_amount_sol,
                        exit_time=trade.timestamp,
                        pnl_pct=result.pnl_pct,
                    )
                    logger.info(
                        "PAPER SELL %s: pnl=%+.1f%% reason=%s hold=%.0fs",
                        token.symbol,
                        result.pnl_pct,
                        result.exit_reason,
                        max(trade.timestamp - entry_ts, 0.0),
                    )
                    return

            # Stream ended — timeout or dead token.
            # ``inactivity == 0`` means tracking is disabled: we cannot call
            # it a dead_token, so exit as ``timeout`` at the last event.
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
            await self._db.close_paper_trade(
                trade_id,
                timeout.exit_price,
                reason,
                timeout.total_buys + timeout.total_sells,
                0,
                entry_price,
                self._config.buy_amount_sol,
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
            try:
                timeout = runner.timeout_result()
                await self._db.close_paper_trade(
                    trade_id,
                    timeout.exit_price,
                    "error",
                    timeout.total_buys + timeout.total_sells,
                    0,
                    entry_price,
                    self._config.buy_amount_sol,
                    exit_time=last_event_ts,
                    pnl_pct=timeout.pnl_pct,
                )
            except Exception:
                logger.debug("Failed to close errored trade %s", token.symbol)
        finally:
            # Release the portfolio slot reserved at entry time. Guard against
            # going negative in case a resumed trade was double-counted.
            if self._open_slots > 0:
                self._open_slots -= 1
            await self._launchpad.unsubscribe_trades(token.mint)
