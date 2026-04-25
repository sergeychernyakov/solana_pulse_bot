# pulse_bot/pipeline.py
"""Two-phase pipeline: fast entry (5s) + full confirmation (45s)."""

from __future__ import annotations

import asyncio
import logging
import os
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pulse_bot.config import PulseBotConfig
    from pulse_bot.db import Database
    from pulse_bot.filters.fast import FastFilter
    from pulse_bot.filters.scorer import Scorer
    from pulse_bot.helius_creator import CreatorSnapshotService
    from pulse_bot.helius_holders import HeliusHolderClient
    from pulse_bot.helius_onchain import HeliusOnchainClient
    from pulse_bot.launchpads.base import Launchpad
    from pulse_bot.ml.policy import EntryMLPolicy, EntryT30Policy
    from pulse_bot.models import CreatorStats, Token, Trade

from pulse_bot.ml.policy import (
    get_active_policy_name,
    load_entry_policy_if_available,
    load_entry_t30_policy_if_available,
)

logger = logging.getLogger(__name__)


class Pipeline:
    """Two-phase async pipeline.

    Phase 1 (fast, 5s): Collect trades → FastFilter → FAST_BUY or WAIT
    Phase 2 (full, 45s): Continue collecting → Scorer → BUY/SKIP/BORDERLINE
    Both results stored per token for analysis.
    """

    # Phase 4B — survival hazard model is queried at most once every
    # ``_SURVIVAL_TICK_SECONDS`` per open paper trade. XGBoost call
    # latency is ~5 ms but log spam + DB writes add up at large open
    # position counts; 10 s is the floor recommended by the roadmap.
    _SURVIVAL_TICK_SECONDS: float = 10.0
    # Phase 5 — entry-timing checkpoint cadence. Runs every 15 s
    # starting from the first checkpoint at T+15.
    _TIMING_CHECKPOINT_SECONDS: float = 15.0
    _TIMING_FIRST_CHECKPOINT: float = 15.0
    _TIMING_CONFIDENCE_FLOOR: float = 0.6

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

        # ── Phase 3 / 4B / 5 deployment switches (default: OFF) ──────
        # All three integrations are opt-in. With env switches unset the
        # corresponding policy is never loaded and the new code paths are
        # short-circuited at the very top — bot behaviour is bit-for-bit
        # identical to pre-integration. Each switch checked once at boot
        # so a flip requires a restart (matches PULSE_POLICY semantics).
        self._entry_t30_active: bool = (
            os.environ.get("PULSE_ENTRY_T30_ACTIVE", "0") == "1"
        )
        self._survival_active: bool = (
            os.environ.get("PULSE_SURVIVAL_ACTIVE", "0") == "1"
        )
        self._timing_active: bool = os.environ.get("PULSE_TIMING_ACTIVE", "0") == "1"
        self._ml_entry_t30_policy: "EntryT30Policy | None" = None
        if self._entry_t30_active:
            self._ml_entry_t30_policy = load_entry_t30_policy_if_available()
            if self._ml_entry_t30_policy is None:
                logger.warning(
                    "PULSE_ENTRY_T30_ACTIVE=1 but no T+30 model could be "
                    "loaded — early-decision hook is a no-op."
                )
            else:
                logger.info(
                    "Entry T+30 ACTIVE: model_hash=%s buy_ceiling=%.3f "
                    "skip_floor=%.3f",
                    self._ml_entry_t30_policy.model_hash[:16],
                    self._ml_entry_t30_policy.buy_ceiling,
                    self._ml_entry_t30_policy.skip_floor,
                )
        # Survival model is loaded lazily on the first paper trade tick
        # (the .ubj load itself is cheap, but we don't want to import
        # xgboost at boot when the switch is off).
        self._survival_model: tuple[Any, dict] | None = None
        self._survival_load_attempted: bool = False
        if self._survival_active:
            logger.info(
                "Survival exit ACTIVE: model will load on first paper "
                "trade tick. min_hold=%.0fs",
                self._config.exit_ml_min_hold_seconds,
            )
        # Entry-timing classifier — store as model_path so each predict
        # call re-uses the loaded booster (predict_entry_timing reloads
        # internally; cached in _timing_booster on first hit).
        self._timing_model_path: Path | None = None
        self._timing_booster_cache: tuple[Any, dict] | None = None
        if self._timing_active:
            from pulse_bot.ml.entry_timing import TIMING_SCHEMA_VERSION

            default_path = Path("data/ml/entry_timing_model.ubj")
            self._timing_model_path = default_path
            if not default_path.exists():
                logger.warning(
                    "PULSE_TIMING_ACTIVE=1 but no entry-timing model at "
                    "%s — checkpoint hook is a no-op.",
                    default_path,
                )
                self._timing_model_path = None
            else:
                logger.info(
                    "Entry-timing checkpoint ACTIVE: model=%s "
                    "schema=%s checkpoint_every=15s confidence_floor=0.6",
                    default_path,
                    TIMING_SCHEMA_VERSION,
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
        """Schedule 3 holder snapshots (T+30s, T+60s, T+120s) for a new
        token. Bounded by semaphore (concurrent RPC cap) to prevent
        unbounded growth when many tokens await their capture slot.

        Pre-capture death detection via DB trades was removed — trades
        aren't inserted until after the 45s observation window, so at
        T+10 the check would always return "no trades" and censor the
        entire dataset. Analysis instead treats a mint with 3× parse_error
        in ``holder_capture_failures`` (and zero snapshot rows) as a
        pre-capture death class.

        Lag instrumentation (2026-04-25, Phase 3 prereq): logs
        ``Helius T+N capture lag: actual=X scheduled=Y delta=Δ`` per
        capture so we can quantify how far past the target age the
        snapshot actually fired. Three lag sources we measure:

        * ``loop_lag``  — wakeup-from-sleep jitter (asyncio scheduler).
        * ``sem_wait``  — time queued at the concurrency semaphore.
        * ``rpc_time``  — HTTP roundtrip itself.

        Fixes vs. pre-2026-04-25 implementation:

        1. Semaphore raised 50 → 100. At 200 tokens × 3 captures, a 50-
           slot cap with ~500 ms p50 RPC time serialised T+30 bursts:
           bursts of 100+ simultaneous wakeups had to wait ~1 RPC each
           to acquire. Helius free tier comfortably handles 100
           concurrent. Override via ``PULSE_HELIUS_HOLDER_CONCURRENCY``.
        2. Use ``loop.time()`` for the semaphore wait measurement
           (monotonic, immune to wall-clock jumps).
        """
        if self._holder_client is None:
            return
        from pulse_bot.helius_holders import CAPTURE_AGE_SECONDS

        if self._holder_sem is None:
            import os as _os

            sem_size = int(_os.environ.get("PULSE_HELIUS_HOLDER_CONCURRENCY", "100"))
            self._holder_sem = asyncio.Semaphore(sem_size)

        loop = asyncio.get_event_loop()

        async def _run_one(target_age: float) -> None:
            t0 = time.time()
            scheduled_at_wall = created_at + target_age
            try:
                delay = scheduled_at_wall - t0
                if delay > 0:
                    await asyncio.sleep(delay)
                # loop_lag = how much later than scheduled we woke up.
                woke_at = time.time()
                loop_lag = max(0.0, woke_at - scheduled_at_wall)
                # sem_wait = time queued at the semaphore (monotonic).
                sem_wait_start = loop.time()
                async with self._holder_sem:  # type: ignore[arg-type]
                    sem_wait = max(0.0, loop.time() - sem_wait_start)
                    rpc_start = time.time()
                    snap = await self._holder_client.fetch(mint, target_age)
                    rpc_time = max(0.0, time.time() - rpc_start)
                actual_age = (
                    snap.observed_at - created_at if snap is not None else float("nan")
                )
                total_lag = (
                    actual_age - target_age if snap is not None else float("nan")
                )
                logger.info(
                    "Helius T+%.0f capture lag: actual=%.2fs scheduled=%.2fs "
                    "delta=%+.2fs (loop=%.2fs sem=%.2fs rpc=%.2fs) mint=%s",
                    target_age,
                    actual_age,
                    float(target_age),
                    total_lag,
                    loop_lag,
                    sem_wait,
                    rpc_time,
                    mint[:12],
                )
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
        return self._db.count_open_paper_trades()

    async def _resume_open_trades(self) -> None:
        """Resume monitoring open paper trades from a previous pipeline run.

        Re-subscribes to WS and restarts _paper_trade tasks so positions
        are managed the same way as freshly opened ones.
        """
        from pulse_bot.models import Token

        open_trades = self._db._sync_query(
            """SELECT p.id, p.mint, p.symbol, p.entry_price, p.entry_time,
                      p.entry_mcap_sol, p.entry_buyer_number, p.entry_type,
                      p.entry_score, p.buy_amount_sol, p.price_updated_at,
                      t.creator, t.created_at
               FROM paper_trades p
               LEFT JOIN tokens t ON t.mint = p.mint
               WHERE p.status='open'"""
        )

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
            # Phase 3 / 5 deployment hook: when an early-decision policy
            # is active, a parallel checkpoint task watches accumulated
            # trades and may set a verdict (BUY_EARLY / SKIP_EARLY)
            # mid-window. With both switches off the task is never
            # spawned and the collection loop is identical to before.
            checkpoint_state: dict[str, Any] = {"verdict": None}
            checkpoint_task: asyncio.Task | None = None
            if not is_replay and (
                (self._entry_t30_active and self._ml_entry_t30_policy is not None)
                or (self._timing_active and self._timing_model_path is not None)
            ):
                checkpoint_task = asyncio.create_task(
                    self._observation_checkpoint_loop(
                        token, collected, creator_snapshot, checkpoint_state
                    )
                )
            try:
                async for trade in self._launchpad.stream_trades(
                    token.mint, collect_duration
                ):
                    collected.append(trade)
                    if checkpoint_state["verdict"] is not None:
                        # Checkpoint already decided — stop collecting and
                        # let the post-loop logic act on the verdict.
                        break
            finally:
                if checkpoint_task is not None and not checkpoint_task.done():
                    checkpoint_task.cancel()
                    try:
                        await checkpoint_task
                    except (asyncio.CancelledError, Exception):
                        pass

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
                # Phase E: keep wallet_activity materialized view fresh for
                # top-3 buyer prior-stats features. Running here (after trade
                # insert, before scoring) guarantees the aggregate reflects
                # everything the scorer will see.
                try:
                    await self._db.upsert_wallet_activity_from_trades(all_trades)
                except Exception as exc:
                    logger.warning("wallet_activity upsert failed (non-fatal): %s", exc)
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

            # Phase E — top-3 buyer prior stats computed once per scoring
            # and reused by the shadow-logging + decide_with_confidence
            # call sites below. Both paths call the same features.py
            # helpers so train/serve parity is enforced by construction.
            try:
                from pulse_bot.ml.features import compute_top3_buyer_wallets

                top3_wallets = compute_top3_buyer_wallets(all_trades)
                wallet_prior_stats = (
                    self._db.get_wallet_prior_stats_sync(
                        top3_wallets,
                        exclude_mint=token.mint,
                        cutoff_ts=float(result.scored_at or 0.0),
                    )
                    if top3_wallets
                    else {}
                )
            except Exception as exc:
                logger.warning(
                    "wallet_prior_stats lookup failed for %s: %s",
                    token.mint[:12],
                    exc,
                )
                top3_wallets = []
                wallet_prior_stats = {}
            scoring_cutoff_ts = float(result.scored_at or 0.0)

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
                        result,
                        creator_snapshot=creator_snapshot,
                        wallet_prior_stats=wallet_prior_stats,
                        top3_buyer_wallets=top3_wallets,
                        cutoff_ts=scoring_cutoff_ts,
                    )
                    feat_json = self._ml_entry_policy.dump_features_json(
                        result,
                        creator_snapshot=creator_snapshot,
                        wallet_prior_stats=wallet_prior_stats,
                        top3_buyer_wallets=top3_wallets,
                        cutoff_ts=scoring_cutoff_ts,
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
                            wallet_prior_stats=wallet_prior_stats,
                            top3_buyer_wallets=top3_wallets,
                            cutoff_ts=scoring_cutoff_ts,
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

            # ── Phase 3 / 5 early-decision override ──────────────────
            # If the checkpoint loop set a verdict mid-window, apply it
            # *after* the standard rules+hybrid path so the cumulative
            # decision history is still logged. Defaults-OFF: verdict is
            # always None (loop never spawned), this branch is a no-op.
            cp_verdict = checkpoint_state.get("verdict")
            if cp_verdict is not None:
                source = checkpoint_state.get("source", "checkpoint")
                cp_proba = checkpoint_state.get("proba")
                if cp_verdict == "BUY_EARLY" and not should_enter:
                    logger.warning(
                        "EARLY OVERRIDE %s: rules=SKIP → %s=BUY (proba=%.3f)",
                        mint_short,
                        source,
                        float(cp_proba) if cp_proba is not None else float("nan"),
                    )
                    should_enter = True
                    entry_type = source
                elif cp_verdict == "BUY_EARLY":
                    logger.info(
                        "EARLY agree %s: %s=BUY confirms rules=BUY (proba=%.3f)",
                        mint_short,
                        source,
                        float(cp_proba) if cp_proba is not None else float("nan"),
                    )
                    entry_type = source
                elif cp_verdict == "SKIP_EARLY" and should_enter:
                    logger.warning(
                        "EARLY OVERRIDE %s: rules=BUY → %s=SKIP (proba=%.3f)",
                        mint_short,
                        source,
                        float(cp_proba) if cp_proba is not None else float("nan"),
                    )
                    should_enter = False

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
                # SKIP/RULES path: optionally keep collecting trades post-
                # scoring so ML label/sweep pipelines see >observe_seconds
                # of activity. Background task; does not delay decisions.
                extra = float(self._config.pulse_extended_observe_seconds)
                if extra > 0 and not is_replay:
                    asyncio.create_task(self._extended_observation(token.mint, extra))
                else:
                    await self._launchpad.unsubscribe_trades(token.mint)

        except Exception:
            logger.exception("Error processing token %s (%s)", token.symbol, mint_short)
            await self._launchpad.unsubscribe_trades(token.mint)

    def _resolve_tick_seconds(self) -> float:
        """Resolve the timer-tick interval for paper trades (Phase 4A).

        Precedence: ``PULSE_TICK_SECONDS`` env var > ``config.pulse_tick_seconds``
        attr (if present) > 5.0 default. Returning 0 disables the tick task
        entirely — same behaviour as before Phase 4A.

        Reading the env var directly (rather than threading a new config
        field) keeps this orthogonal to the in-flight ``config.py`` commit
        so the two changes can land independently.
        """
        import os

        raw = os.environ.get("PULSE_TICK_SECONDS")
        if raw is not None:
            try:
                return float(raw)
            except ValueError:
                logger.warning("PULSE_TICK_SECONDS=%r is not a number; ignoring", raw)
        # Allow tests / future config to override via attribute without
        # requiring an env var.
        return float(getattr(self._config, "pulse_tick_seconds", 5.0))

    async def _observation_checkpoint_loop(
        self,
        token: "Token",
        collected: list["Trade"],
        creator_snapshot: "CreatorStats | None",
        state: dict,
    ) -> None:
        """Phase 3 + Phase 5 in-window early-decision checkpoint.

        Runs in parallel with the trade-stream collection loop in
        ``_handle_token``. Wakes at fixed offsets (T+15, T+30, T+45,
        T+60, T+75) and asks the registered policies whether the bot
        should jump to entry early or skip immediately.

        Decision priority:
          1. T+30 model (Phase 3) — wakes only at T+30.
          2. Entry-timing classifier (Phase 5) — wakes every 15 s.
          3. If neither fires a verdict by T+90, fall through (caller's
             collection loop will hit its natural deadline).

        Per the deployment spec, T+30 BUY supersedes a same-tick
        timing-classifier verdict — so we evaluate T+30 first at its
        single checkpoint and only run timing as fallback.

        State communication: ``state`` dict is mutated in place with
            ``verdict``: ``"BUY_EARLY"`` / ``"SKIP_EARLY"`` / ``None``,
            ``source``: ``"t30"`` / ``"timing"`` for logs / db.source,
            ``proba``:  numeric used in WARN log.

        On any unhandled exception we log and exit cleanly — the
        collection loop must continue regardless of policy errors.
        """
        try:
            now_offset = self._TIMING_FIRST_CHECKPOINT
            t30_done = False
            window_end = float(self._config.observe_seconds)
            while now_offset < window_end:
                # Sleep until the next checkpoint relative to token creation.
                target_wall = float(token.created_at) + now_offset
                delay = target_wall - time.time()
                if delay > 0:
                    await asyncio.sleep(delay)
                if state.get("verdict") is not None:
                    return  # already decided

                # ── T+30 hook (Phase 3) ─────────────────────────────
                if (
                    not t30_done
                    and self._entry_t30_active
                    and self._ml_entry_t30_policy is not None
                    and abs(now_offset - 30.0) < 1e-6
                ):
                    t30_done = True
                    verdict = await self._evaluate_t30_checkpoint(
                        token, list(collected), creator_snapshot
                    )
                    if verdict is not None:
                        action, proba = verdict
                        state["source"] = "t30"
                        state["proba"] = proba
                        if action == "BUY":
                            state["verdict"] = "BUY_EARLY"
                            return
                        if action == "SKIP":
                            state["verdict"] = "SKIP_EARLY"
                            return

                # ── Entry-timing hook (Phase 5) ─────────────────────
                if self._timing_active and self._timing_model_path is not None:
                    verdict = self._evaluate_timing_checkpoint(
                        token, list(collected), now_offset
                    )
                    if verdict is not None:
                        action, proba = verdict
                        state["source"] = "timing"
                        state["proba"] = proba
                        if action == "BUY":
                            state["verdict"] = "BUY_EARLY"
                            return
                        if action == "SKIP":
                            state["verdict"] = "SKIP_EARLY"
                            return

                now_offset += self._TIMING_CHECKPOINT_SECONDS
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception(
                "checkpoint loop crashed for %s; falling back to T+90 path",
                token.mint[:12],
            )

    async def _evaluate_t30_checkpoint(
        self,
        token: "Token",
        trades_so_far: list["Trade"],
        creator_snapshot: "CreatorStats | None",
    ) -> tuple[str, float] | None:
        """Run the T+30 dual-snapshot model on the partial trade stream.

        Returns ``(action, proba)`` where ``action`` is ``"BUY"``,
        ``"SKIP"`` or ``"DEFER"`` (DEFER → caller continues to T+90).
        Returns ``None`` on any error so the rest of the pipeline keeps
        moving — early-decision is purely additive.

        Builds a partial ScoringResult by re-running the live scorer on
        the subset of trades visible by T+30, then asks
        :class:`EntryT30Policy` for a 3-way verdict.
        """
        try:
            policy = self._ml_entry_t30_policy
            if policy is None:
                return None
            t30_cutoff = token.created_at + 30.0
            visible = [t for t in trades_so_far if t.timestamp <= t30_cutoff]
            tokens_5min = self._db.get_tokens_last_5min_sync(ref_mint=token.mint)
            concurrent = self._db.get_concurrent_observations_sync(
                ref_mint=token.mint, observe_seconds=30.0
            )
            creator_tokens_today = self._db.get_creator_tokens_on_day_sync(
                token.creator, ref_mint=token.mint
            )
            partial = self._scorer.score(
                token,
                visible,
                tokens_last_5min=tokens_5min,
                concurrent_observations=concurrent,
                creator_snapshot=creator_snapshot,
                creator_tokens_today=creator_tokens_today,
            )
            # Holder snapshot @T+30 (best-effort: capture may not have
            # landed yet when the bot polls). We pass None on miss; the
            # T30 policy zero-fills HELIUS_FEATURES_T30 which mirrors
            # the training-time fallback for late captures.
            holder_t30 = None
            try:
                holder_t30 = await asyncio.to_thread(
                    self._fetch_holder_snapshot_t30, token.mint
                )
            except Exception:
                logger.debug(
                    "t30 holder fetch failed for %s", token.mint[:12], exc_info=True
                )
            action, proba = policy.decide_with_confidence(
                partial,
                holder_snapshot_t30=holder_t30,
                creator_snapshot=creator_snapshot,
            )
            logger.info(
                "T+30 decision %s: %s proba=%.3f buys=%d (ceiling=%.2f floor=%.2f)",
                token.mint[:12],
                action,
                proba,
                len(visible),
                policy.buy_ceiling,
                policy.skip_floor,
            )
            return action, proba
        except Exception:
            logger.exception(
                "T+30 evaluation crashed for %s — deferring to T+90",
                token.mint[:12],
            )
            return None

    def _fetch_holder_snapshot_t30(self, mint: str) -> dict | None:
        """Best-effort sync DB lookup for the @T+30 holder snapshot.

        Returns a plain dict keyed by ``top1_30 / top5_30 / top10_30 /
        hc_30`` so the T+30 feature extractor sees the names it expects.
        Missing snapshot → ``None`` and the caller zero-fills.
        """
        rows = self._db._sync_query(
            "SELECT top1_pct, top5_pct, top10_pct, holder_count "
            "FROM holder_snapshots "
            "WHERE mint = %s AND capture_at_age_sec = 30.0 "
            "AND is_negative_row = FALSE LIMIT 1",
            mint,
        )
        if not rows:
            return None
        r = rows[0]
        return {
            "top1_30": float(r.get("top1_pct") or 0.0),
            "top5_30": float(r.get("top5_pct") or 0.0),
            "top10_30": float(r.get("top10_pct") or 0.0),
            "hc_30": float(r.get("holder_count") or 0.0),
        }

    def _evaluate_timing_checkpoint(
        self,
        token: "Token",
        trades_so_far: list["Trade"],
        snapshot_t: float,
    ) -> tuple[str, float] | None:
        """Run the per-snapshot entry-timing classifier (Phase 5).

        Returns ``(action, proba_max)`` where ``action`` is one of
        ``BUY`` / ``SKIP`` / ``WAIT_MORE``. Confidence is the predicted
        probability of the argmax class — caller acts only when this
        clears :data:`_TIMING_CONFIDENCE_FLOOR` (default 0.6).
        """
        try:
            from pulse_bot.ml.entry_timing import (
                CLASS_BUY_NOW,
                CLASS_NAMES,
                CLASS_SKIP,
                extract_snapshot_features,
                predict_entry_timing,
            )

            if self._timing_model_path is None:
                return None
            feats = extract_snapshot_features(
                trades_so_far, snapshot_t, float(token.created_at)
            )
            pred = predict_entry_timing(feats, self._timing_model_path)
            probs = pred.as_vector()
            max_proba = max(probs)
            logger.info(
                "TIMING checkpoint %s @T+%.0f: %s (p_wait=%.2f p_buy=%.2f "
                "p_skip=%.2f)",
                token.mint[:12],
                snapshot_t,
                pred.decision,
                probs[0],
                probs[1],
                probs[2],
            )
            if pred.decision == CLASS_NAMES[CLASS_BUY_NOW] and (
                probs[CLASS_BUY_NOW] >= self._TIMING_CONFIDENCE_FLOOR
            ):
                return ("BUY", float(probs[CLASS_BUY_NOW]))
            if pred.decision == CLASS_NAMES[CLASS_SKIP] and (
                probs[CLASS_SKIP] >= self._TIMING_CONFIDENCE_FLOOR
            ):
                return ("SKIP", float(probs[CLASS_SKIP]))
            return ("WAIT_MORE", float(max_proba))
        except Exception:
            logger.exception(
                "timing-classifier checkpoint crashed for %s @T+%.0f",
                token.mint[:12],
                snapshot_t,
            )
            return None

    async def _maybe_survival_exit(
        self,
        runner: Any,
        entry_ts: float,
        now: float,
    ) -> Any:
        """Phase 4B — query the survival model and return an early-exit
        result if predicted remaining life is below the configured floor.

        Returns the same shape as ``runner.tick()`` (a ``MonitorResult``
        with ``exit_reason='survival_predict'`` set) so
        ``_close_via_runner_result`` can reuse the standard close path.
        Returns ``None`` to keep holding.
        """
        # Defaults-OFF gate: if the env switch is off we never even
        # attempt to load the model. This keeps boot semantically equal
        # to pre-Phase-4B regardless of any model file on disk.
        if not self._survival_active:
            return None
        try:
            elapsed = max(0.0, now - entry_ts)
            min_hold = float(self._config.exit_ml_min_hold_seconds or 0.0)
            if elapsed < min_hold:
                return None
            if self._survival_model is None and not self._survival_load_attempted:
                self._survival_load_attempted = True
                try:
                    from pulse_bot.ml.survival import load_survival_model

                    self._survival_model = load_survival_model(
                        Path("data/ml/survival_model.ubj")
                    )
                    logger.info(
                        "Survival model loaded: %d features",
                        len(self._survival_model[1].get("features", [])),
                    )
                except FileNotFoundError:
                    logger.info(
                        "Survival ACTIVE but no model at "
                        "data/ml/survival_model.ubj — hook is a no-op."
                    )
                    self._survival_model = None
                except Exception:
                    logger.exception("Failed to load survival model")
                    self._survival_model = None
            if self._survival_model is None:
                return None
            from pulse_bot.ml.survival import predict_remaining_life

            model, meta = self._survival_model
            features_now = self._survival_features_from_runner(runner, elapsed)
            pred = predict_remaining_life(
                model,
                features_now,
                feature_order=meta["features"],
                bucket_seconds=float(meta.get("bucket_seconds", 5.0)),
                max_horizon_seconds=float(meta.get("max_horizon_seconds", 180.0)),
                now_elapsed_seconds=elapsed,
            )
            if pred.remaining_life_seconds < 30.0:
                logger.warning(
                    "Survival exit: predicted_remaining=%.0fs elapsed=%.0fs "
                    "confidence=%.2f — closing as survival_predict",
                    pred.remaining_life_seconds,
                    elapsed,
                    pred.confidence,
                )
                # Build a result via runner.timeout_result then override
                # the reason — keeps the exit_price/PnL plumbing identical
                # to the regular close path.
                result = runner.timeout_result()
                # Frozen dataclass may reject assignment; default reason is acceptable.
                try:
                    result.exit_reason = "survival_predict"
                except Exception:  # nosec B110
                    pass
                return result
            return None
        except Exception:
            logger.exception("survival check crashed; continuing to hold")
            return None

    def _survival_features_from_runner(self, runner: Any, elapsed: float) -> dict:
        """Best-effort feature dict for survival inference.

        The training script writes whatever numeric columns survive
        ``_select_feature_columns`` into ``meta['features']``. At the
        very least it always includes ``elapsed_seconds`` and
        ``bucket_index``; richer features (entry_score, entry_mcap_sol,
        entry_buyer_number) come from the paper_trades row but are not
        always reachable from the runner state. Missing keys are
        zero-filled by ``predict_remaining_life``.
        """
        feats: dict[str, float] = {
            "elapsed_seconds": float(elapsed),
            "bucket_index": float(elapsed) / 5.0,
        }
        # Pull whatever numerical state is exposed on the runner.
        for attr in (
            "current_price",
            "total_buys",
            "total_sells",
            "peak_price",
        ):
            # Opportunistic feature extraction; missing/non-numeric attrs ok.
            try:
                v = getattr(runner, attr, None)
                if v is not None:
                    feats[attr] = float(v)
            except Exception:  # nosec B112
                continue
        return feats

    async def _extended_observation(self, mint: str, duration_seconds: float) -> None:
        """Continue saving trades for `duration_seconds` after a SKIP decision.

        Lets ML label / sweep pipelines extend beyond the scoring window
        without changing live entry behavior. Runs as a background task,
        unsubscribes when done. Inactivity bound matches ``exit_inactivity_seconds``
        so a token going silent stops the WS subscription early.
        """
        inactivity = float(self._config.exit_inactivity_seconds or 0.0)
        try:
            async for trade in self._launchpad.stream_trades(
                mint, duration_seconds, inactivity_timeout=inactivity
            ):
                try:
                    await self._db.insert_trades_batch([trade])
                except Exception as exc:
                    logger.debug(
                        "extended observation insert failed (non-fatal): %s", exc
                    )
        finally:
            await self._launchpad.unsubscribe_trades(mint)

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

        # Phase 4A timer-tick infrastructure. The tick task fires every
        # ``pulse_tick_seconds`` and re-evaluates ExitManager against the
        # current pulse window — so a quiet token can close on
        # pulse_dead / no_new_blood without waiting for ``inactivity_timeout``.
        # Set ``PULSE_TICK_SECONDS=0`` (or via config attr) to disable
        # and fall back to the pre-Phase-4A behaviour exactly.
        tick_seconds = self._resolve_tick_seconds()
        # Single-flight guard so trade-stream and tick task never both
        # close the same trade. asyncio is single-threaded but we await
        # DB calls between the check and the close; the lock serialises
        # those critical sections.
        close_lock = asyncio.Lock()
        closed = False
        # Track the close so the trade-stream coroutine can short-circuit
        # the post-loop timeout/dead_token branch when the tick path closed
        # the trade first.
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
            await self._db.close_paper_trade(
                trade_id,
                result.exit_price,
                result.exit_reason,
                result.total_buys + result.total_sells,
                trade_market_cap,
                entry_price,
                self._config.buy_amount_sol,
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

        deadline = self._config.exit_max_hold_seconds
        inactivity = self._config.exit_inactivity_seconds

        async def _trade_stream_loop() -> None:
            """Original per-trade processing path. Closes via lock-guarded helper."""
            nonlocal last_event_ts
            async for trade in self._launchpad.stream_trades(
                token.mint, deadline, inactivity_timeout=inactivity
            ):
                if closed:
                    return
                last_event_ts = trade.timestamp

                if not is_replay:
                    await self._db.insert_trades_batch([trade])
                    try:
                        await self._db.upsert_wallet_activity_from_trades([trade])
                    except Exception as exc:
                        logger.warning(
                            "wallet_activity upsert (monitor) failed: %s", exc
                        )

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

                result = runner.process_trade(trade, entry_ts)
                if result:
                    await _close_via_runner_result(
                        result,
                        trade_market_cap=trade.market_cap_sol,
                        exit_time=trade.timestamp,
                    )
                    return

        async def _tick_loop() -> None:
            """Periodic ExitManager re-evaluation when stream is quiet.

            No-op when ``tick_seconds <= 0`` (back-compat). Replay sources
            do not have a real-time clock anyway, but we still gate on the
            ``replay`` flag so deterministic backtests don't drift.

            Phase 4B hook: when ``PULSE_SURVIVAL_ACTIVE=1`` and a survival
            model is loadable, every ``_SURVIVAL_TICK_SECONDS`` (10s, to
            cap XGBoost call cost + log noise) we ask the hazard model
            for the predicted remaining life. If it dips below 30s and
            we've already cleared ``exit_ml_min_hold_seconds``, force a
            ``survival_predict`` close. Defaults-OFF: model never loaded,
            hot loop exactly matches pre-Phase-4B.
            """
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
                    # Survival model — Phase 4B. Throttled to once per
                    # _SURVIVAL_TICK_SECONDS to avoid re-running XGBoost
                    # on every tick when ``tick_seconds`` is very small
                    # (e.g. tests use 0.05s). ``getattr`` keeps the
                    # branch inert for tests that bypass __init__ and
                    # never set ``_survival_active``.
                    if getattr(self, "_survival_active", False) and (
                        now - last_survival_check_ts >= self._SURVIVAL_TICK_SECONDS
                    ):
                        last_survival_check_ts = now
                        survived = await self._maybe_survival_exit(
                            runner=runner,
                            entry_ts=entry_ts,
                            now=now,
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
                # Cancel the laggard so we don't leave a dangling timer or
                # half-consumed stream iterator behind.
                for t in pending:
                    t.cancel()
                if pending:
                    await asyncio.gather(*pending, return_exceptions=True)
                # Surface exceptions from the completed task(s).
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
            # Don't double-close: skip if either path already finalised.
            if not closed:
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
