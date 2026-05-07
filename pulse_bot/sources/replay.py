# pulse_bot/sources/replay.py
"""ReplayLaunchpad — replays historical data through the same interface as live WS.

This allows the pipeline to process backtest data using EXACTLY the same code
as live trading. The only difference is the source of events.

Time-based windows, anchored on token.created_at:
  Scoring phases yield trades with timestamp in (cursor, cursor + duration].
  Monitor phase yields all remaining trades, honoring inactivity_timeout.

No coupling to live-stored fast/full trade IDs: the window is derived purely
from created_at and config, so live, replay, provider-data backtest, and the
optimizer all see the same trade set for a given token.
"""

from __future__ import annotations

import asyncio
import logging
from typing import AsyncIterator

import psycopg2
import psycopg2.extras

from pulse_bot.db import _resolve_dsn
from pulse_bot.launchpads.base import Launchpad
from pulse_bot.models import Token, Trade

logger = logging.getLogger(__name__)


class ReplayLaunchpad(Launchpad):
    """Replays tokens and trades from SQLite as if they came from WebSocket."""

    name = "replay"
    ws_url = "sqlite"

    def __init__(
        self,
        db_path: str,
        speed: float = 0.0,
        limit: int | None = None,
    ) -> None:
        self._db_path = db_path
        self._speed = speed
        self._limit = limit  # None = all tokens; e.g. 300 = fast verify sweep
        self._trade_queues: dict[str, asyncio.Queue[Trade]] = {}
        self._token_creators: dict[str, str] = {}
        self._token_created_at: dict[str, float] = {}
        self._stream_cursor: dict[str, float] = {}
        self._running = False

    async def connect(self) -> None:
        self._running = True
        logger.info("ReplayLaunchpad connected (speed=%.1f)", self._speed)

    async def disconnect(self) -> None:
        self._running = False
        self._trade_queues.clear()

    async def stream_new_tokens(self) -> AsyncIterator[Token]:
        """Yield tokens from DB in chronological order."""
        conn = psycopg2.connect(_resolve_dsn(self._db_path))
        try:
            cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
            # Most-recent-first when ``limit`` is set so the verify sample
            # covers the freshest config window (matches what live ran on);
            # full replay stays chronological for backtest semantics.
            if self._limit is not None and self._limit > 0:
                cur.execute(
                    "SELECT * FROM tokens ORDER BY created_at DESC LIMIT %s",
                    (int(self._limit),),
                )
            else:
                cur.execute("SELECT * FROM tokens ORDER BY created_at ASC")
            prev_ts = 0.0
            for row in cur:
                if not self._running:
                    break
                token = Token(
                    mint=row["mint"],
                    name=row["name"] or "",
                    symbol=row["symbol"] or "",
                    creator=row["creator"] or "",
                    created_at=row["created_at"],
                    uri=row["uri"] or "",
                    launchpad="pumpfun",
                )
                self._token_creators[token.mint] = token.creator
                self._token_created_at[token.mint] = token.created_at

                if self._speed > 0 and prev_ts > 0:
                    delay = (token.created_at - prev_ts) * self._speed
                    if delay > 0:
                        await asyncio.sleep(min(delay, 1.0))
                prev_ts = token.created_at

                yield token
                await asyncio.sleep(0)
        finally:
            conn.close()

    async def subscribe_trades(self, mint: str) -> None:
        """Create a queue for this mint and load all historical trades."""
        if mint not in self._trade_queues:
            self._trade_queues[mint] = asyncio.Queue()
            await self._preload_all_trades(mint)
            self._stream_cursor[mint] = self._token_created_at.get(mint, 0.0)

    async def _preload_all_trades(self, mint: str) -> None:
        """Load every trade for this mint, in timestamp order, into the queue."""
        conn = psycopg2.connect(_resolve_dsn(self._db_path))
        try:
            cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
            cur.execute(
                "SELECT * FROM trades WHERE mint = %s ORDER BY timestamp ASC, id ASC",
                (mint,),
            )
            creator = self._token_creators.get(mint, "")
            queue = self._trade_queues.get(mint)
            if queue is None:
                return
            for row in cur:
                trade = self._row_to_trade(row, mint, creator)
                await queue.put(trade)
        finally:
            conn.close()

    async def unsubscribe_trades(self, mint: str) -> None:
        self._trade_queues.pop(mint, None)
        self._token_creators.pop(mint, None)
        self._token_created_at.pop(mint, None)
        self._stream_cursor.pop(mint, None)

    async def stream_trades(
        self,
        mint: str,
        duration_seconds: float,
        inactivity_timeout: float = 0,
    ) -> AsyncIterator[Trade]:
        """Yield trades for a scoring window, or the monitor stream.

        inactivity_timeout > 0  → monitor phase: yield all remaining trades
        inactivity_timeout == 0 → scoring phase: yield (cursor, cursor+duration]
        """
        queue = self._trade_queues.get(mint)
        if not queue:
            return

        if inactivity_timeout > 0:
            async for trade in self._drain_all(queue, inactivity_timeout):
                yield trade
            return

        cursor = self._stream_cursor.get(mint, self._token_created_at.get(mint, 0.0))
        until_ts = cursor + duration_seconds
        async for trade in self._drain_until(queue, until_ts):
            yield trade
        self._stream_cursor[mint] = until_ts

    async def _drain_until(
        self, queue: asyncio.Queue[Trade], until_ts: float
    ) -> AsyncIterator[Trade]:
        """Yield trades with timestamp <= until_ts. Keep rest in queue."""
        temp_buffer: list[Trade] = []
        while not queue.empty():
            try:
                trade = queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            if trade.timestamp <= until_ts:
                yield trade
            else:
                temp_buffer.append(trade)
                break
        for t in temp_buffer:
            await queue.put(t)

    async def _drain_all(
        self, queue: asyncio.Queue[Trade], inactivity_timeout: float = 0
    ) -> AsyncIterator[Trade]:
        """Yield trades while the inter-trade gap stays within inactivity_timeout.

        Mirrors live stream semantics: after a silence longer than the timeout
        the consumer treats the token as dead. Remaining trades are put back
        into the queue so they can be drained by a later call if needed.
        """
        last_ts: float | None = None
        temp_buffer: list[Trade] = []
        while not queue.empty():
            try:
                trade = queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            if (
                inactivity_timeout > 0
                and last_ts is not None
                and trade.timestamp - last_ts > inactivity_timeout
            ):
                temp_buffer.append(trade)
                while not queue.empty():
                    try:
                        temp_buffer.append(queue.get_nowait())
                    except asyncio.QueueEmpty:
                        break
                break
            last_ts = trade.timestamp
            yield trade
        for t in temp_buffer:
            await queue.put(t)

    def parse_create_event(self, raw: dict) -> Token:
        raise NotImplementedError("ReplayLaunchpad doesn't parse raw events")

    def parse_trade_event(self, raw: dict, creator: str) -> Trade:
        raise NotImplementedError("ReplayLaunchpad doesn't parse raw events")

    def compute_curve_progress(self, v_sol_in_bonding_curve: float) -> float:
        return min((v_sol_in_bonding_curve / 85.0) * 100.0, 100.0)

    def _row_to_trade(self, row, mint: str, creator: str) -> Trade:
        """Convert a DB row to a Trade object."""
        trade = Trade(
            mint=mint,
            wallet=row["wallet"],
            tx_type=row["tx_type"],
            sol_amount=row["sol_amount"] or 0.0,
            token_amount=(row["token_amount"] if "token_amount" in row.keys() else 0.0),
            new_token_balance=0.0,
            bonding_curve_key="",
            v_sol_in_bonding_curve=row["v_sol_in_bonding_curve"] or 0.0,
            v_tokens_in_bonding_curve=0.0,
            market_cap_sol=row["market_cap_sol"] or 0.0,
            timestamp=row["timestamp"],
            is_creator=(row["wallet"] == creator),
        )
        trade._db_id = row["id"]  # type: ignore[attr-defined]
        return trade
