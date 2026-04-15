# pulse_bot/pipeline.py
"""Two-phase pipeline: fast entry (5s) + full confirmation (45s)."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pulse_bot.config import PulseBotConfig
    from pulse_bot.db import Database
    from pulse_bot.filters.fast import FastFilter
    from pulse_bot.filters.scorer import Scorer
    from pulse_bot.launchpads.pumpfun import PumpFunLaunchpad
    from pulse_bot.models import Token, Trade

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
        launchpad: PumpFunLaunchpad,
        scorer: Scorer,
        fast_filter: FastFilter,
    ) -> None:
        self._config = config
        self._db = db
        self._launchpad = launchpad
        self._scorer = scorer
        self._fast_filter = fast_filter
        self._semaphore = asyncio.Semaphore(config.max_concurrent_observations)
        self._running = False
        self._tokens_seen = 0
        self._tokens_scored = 0
        self._fast_buys = 0

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

        try:
            async for token in self._launchpad.stream_new_tokens():
                if not self._running:
                    break
                self._tokens_seen += 1
                asyncio.create_task(self._handle_token_bounded(token))
        except asyncio.CancelledError:
            logger.info("Pipeline cancelled")
        finally:
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

    async def _handle_token_bounded(self, token: Token) -> None:
        """Acquire semaphore, then process token."""
        async with self._semaphore:
            await self._handle_token(token)

    async def _handle_token(self, token: Token) -> None:
        """Two-phase pipeline for one token."""
        mint_short = token.mint[:12]

        try:
            await self._db.insert_token(token)
            logger.info("New token: %s (%s) by %s", token.symbol, mint_short, token.creator[:12])

            await self._launchpad.subscribe_trades(token.mint)

            # ── Phase 1: Fast (5 seconds) ──────────────────
            fast_trades = await self._collect_trades(token, self._config.fast_observe_seconds)
            fast_result = self._fast_filter.evaluate(token, fast_trades)

            fast_entry_price = 0.0
            if fast_trades:
                fast_buys = [t for t in fast_trades if t.tx_type == "buy" and t.token_amount > 0]
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

            # ── Phase 2: Full (remaining seconds) ──────────
            remaining = self._config.observe_seconds - self._config.fast_observe_seconds
            full_trades_extra = await self._collect_trades(token, max(remaining, 0))
            all_trades = fast_trades + full_trades_extra

            # Update creator cache
            creator_sold = any(t.wallet == token.creator and t.tx_type == "sell" for t in all_trades)
            await self._db.upsert_creator(token.creator, creator_sold)

            # Store all trades
            await self._db.insert_trades_batch(all_trades)

            # Full scoring with market context
            tokens_5min = self._db.get_tokens_last_5min_sync()
            concurrent = self._config.max_concurrent_observations - self._semaphore._value  # noqa: SLF001
            result = self._scorer.score(
                token,
                all_trades,
                tokens_last_5min=tokens_5min,
                concurrent_observations=concurrent,
            )

            # Attach fast phase data
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
                result.pnl_at_fast_entry_pct = ((result.exit_price - fast_entry_price) / fast_entry_price) * 100.0

            # Store
            await self._db.upsert_scoring_result(result)
            self._tokens_scored += 1

            # Log
            log_fn = logger.info if result.decision == "BUY" or fast_result.decision == "FAST_BUY" else logger.debug
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

        except Exception:
            logger.exception("Error processing token %s (%s)", token.symbol, mint_short)
        finally:
            await self._launchpad.unsubscribe_trades(token.mint)

    async def _collect_trades(self, token: Token, duration: float) -> list[Trade]:
        """Collect trades during a time window."""
        trades: list[Trade] = []
        if duration <= 0:
            return trades
        async for trade in self._launchpad.stream_trades(token.mint, duration):
            trades.append(trade)
        return trades
