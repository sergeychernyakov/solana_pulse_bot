# pulse_bot/sources/replay.py
"""ReplayLaunchpad — replays historical data through the same interface as live WS.

This allows the pipeline to process backtest data using EXACTLY the same code
as live trading. The only difference is the source of events.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
import time
from typing import AsyncIterator

from pulse_bot.launchpads.base import Launchpad
from pulse_bot.models import Token, Trade

logger = logging.getLogger(__name__)


class ReplayLaunchpad(Launchpad):
    """Replays tokens and trades from SQLite as if they came from WebSocket.

    Implements the same Launchpad interface as PumpFunLaunchpad.
    Pipeline doesn't know the difference — uses same code for both.
    """

    name = "replay"
    ws_url = "sqlite"

    def __init__(self, db_path: str, speed: float = 0.0) -> None:
        """
        Args:
            db_path: Path to SQLite DB with tokens and trades.
            speed: Replay speed. 0 = instant (backtest), 1.0 = real-time, 0.1 = 10x faster.
        """
        self._db_path = db_path
        self._speed = speed
        self._trade_queues: dict[str, asyncio.Queue[Trade]] = {}
        self._token_creators: dict[str, str] = {}
        self._live_counts: dict[str, dict] = {}  # mint → {fast_trade_count, full_trade_count}
        self._trades_yielded: dict[str, int] = {}  # mint → count of trades yielded so far
        self._running = False
        self._feeder_task: asyncio.Task | None = None

    async def connect(self) -> None:
        """Start the replay feeder that pushes trades into queues."""
        self._running = True
        self._feeder_task = asyncio.create_task(self._feed_trades())
        logger.info("ReplayLaunchpad connected (speed=%.1f)", self._speed)

    async def disconnect(self) -> None:
        self._running = False
        if self._feeder_task and not self._feeder_task.done():
            self._feeder_task.cancel()
            try:
                await self._feeder_task
            except asyncio.CancelledError:
                pass
        self._trade_queues.clear()

    async def stream_new_tokens(self) -> AsyncIterator[Token]:
        """Yield tokens from DB in chronological order."""
        conn = sqlite3.connect(self._db_path)
        conn.row_factory = sqlite3.Row
        try:
            cur = conn.execute("SELECT * FROM tokens ORDER BY created_at ASC")
            prev_ts = 0.0
            for row in cur:
                if not self._running:
                    break
                token = Token(
                    mint=row["mint"], name=row["name"] or "", symbol=row["symbol"] or "",
                    creator=row["creator"] or "", created_at=row["created_at"],
                    uri=row["uri"] or "", launchpad="pumpfun",
                )
                self._token_creators[token.mint] = token.creator

                # Simulate time delay between tokens
                if self._speed > 0 and prev_ts > 0:
                    delay = (token.created_at - prev_ts) * self._speed
                    if delay > 0:
                        await asyncio.sleep(min(delay, 1.0))
                prev_ts = token.created_at

                yield token
                # Let feeder and pipeline process before next token
                await asyncio.sleep(0)
        finally:
            conn.close()

    async def subscribe_trades(self, mint: str) -> None:
        """Create a queue for this mint and immediately load historical trades."""
        if mint not in self._trade_queues:
            self._trade_queues[mint] = asyncio.Queue()
            await self._preload_trades(mint)
            # Load live trade counts for exact replay
            self._load_live_counts(mint)

    def _load_live_counts(self, mint: str) -> None:
        """Load exact trade IDs from live scoring for exact replay."""
        conn = sqlite3.connect(self._db_path)
        conn.row_factory = sqlite3.Row
        try:
            row = conn.execute(
                "SELECT fast_trade_count, full_trade_count, fast_trade_ids, full_trade_ids FROM token_scores WHERE mint = ? AND source = 'live'",
                (mint,),
            ).fetchone()
            if row and row["fast_trade_ids"]:
                self._live_counts[mint] = {
                    "fast": row["fast_trade_count"],
                    "full": row["full_trade_count"],
                    "fast_ids": set(int(x) for x in row["fast_trade_ids"].split(",") if x),
                    "full_ids": set(int(x) for x in row["full_trade_ids"].split(",") if x),
                }
        finally:
            conn.close()

    async def _preload_trades(self, mint: str) -> None:
        """Load trades from DB. If live IDs available, load only those trades."""
        conn = sqlite3.connect(self._db_path)
        conn.row_factory = sqlite3.Row
        try:
            counts = self._live_counts.get(mint)
            if counts and "full_ids" in counts and counts["full_ids"]:
                # Exact replay: load only the trades that live pipeline saw
                full_ids = counts["full_ids"]
                cur = conn.execute(
                    f"SELECT * FROM trades WHERE id IN ({','.join(str(i) for i in sorted(full_ids))}) ORDER BY id ASC"
                )
            else:
                # No live data: load all trades for this mint
                cur = conn.execute("SELECT * FROM trades WHERE mint = ? ORDER BY timestamp ASC, id ASC", (mint,))

            creator = self._token_creators.get(mint, "")
            for row in cur:
                trade = Trade(
                    mint=mint, wallet=row["wallet"], tx_type=row["tx_type"],
                    sol_amount=row["sol_amount"] or 0.0,
                    token_amount=row["token_amount"] if "token_amount" in row.keys() else 0.0,
                    new_token_balance=0.0, bonding_curve_key="",
                    v_sol_in_bonding_curve=row["v_sol_in_bonding_curve"] or 0.0,
                    v_tokens_in_bonding_curve=0.0,
                    market_cap_sol=row["market_cap_sol"] or 0.0,
                    timestamp=row["timestamp"],
                    is_creator=(row["wallet"] == creator),
                )
                trade._db_id = row["id"]  # tag with DB id
                queue = self._trade_queues.get(mint)
                if queue:
                    await queue.put(trade)
        finally:
            conn.close()

    async def unsubscribe_trades(self, mint: str) -> None:
        self._trade_queues.pop(mint, None)
        self._token_creators.pop(mint, None)

    async def stream_trades(self, mint: str, duration_seconds: float) -> AsyncIterator[Trade]:
        """Yield exactly the same trades that live pipeline collected.

        Uses ID-based filtering from live scores for 100% exact match.
        First call = fast trades, second call = remaining full trades.
        """
        queue = self._trade_queues.get(mint)
        if not queue or queue.empty():
            return

        counts = self._live_counts.get(mint)
        already_yielded = self._trades_yielded.get(mint, 0)

        if counts and "fast_ids" in counts:
            fast_ids = counts["fast_ids"]
            full_ids = counts["full_ids"]

            if already_yielded == 0:
                # First call = fast phase: yield trades whose ID is in fast_ids
                target_ids = fast_ids
            else:
                # Second call = remaining: yield trades in full_ids but not in fast_ids
                target_ids = full_ids - fast_ids
        else:
            target_ids = None

        yielded = 0
        temp_buffer = []

        while not queue.empty():
            try:
                trade = queue.get_nowait()
                db_id = getattr(trade, "_db_id", None)

                if target_ids is not None:
                    if db_id in target_ids:
                        yielded += 1
                        self._trades_yielded[mint] = already_yielded + yielded
                        yield trade
                    else:
                        temp_buffer.append(trade)
                else:
                    yielded += 1
                    self._trades_yielded[mint] = already_yielded + yielded
                    yield trade
            except asyncio.QueueEmpty:
                break

        # Put back buffered trades for next phase
        for t in temp_buffer:
            await queue.put(t)

    def parse_create_event(self, raw: dict) -> Token:
        raise NotImplementedError("ReplayLaunchpad doesn't parse raw events")

    def parse_trade_event(self, raw: dict, creator: str) -> Trade:
        raise NotImplementedError("ReplayLaunchpad doesn't parse raw events")

    def compute_curve_progress(self, v_sol_in_bonding_curve: float) -> float:
        return min((v_sol_in_bonding_curve / 85.0) * 100.0, 100.0)

    async def _feed_trades(self) -> None:
        """Background task: read all trades from DB and push to subscriber queues."""
        conn = sqlite3.connect(self._db_path)
        conn.row_factory = sqlite3.Row
        try:
            cur = conn.execute("SELECT * FROM trades ORDER BY timestamp ASC, id ASC")
            prev_ts = 0.0
            for row in cur:
                if not self._running:
                    break

                mint = row["mint"]
                queue = self._trade_queues.get(mint)
                if queue is None:
                    continue

                creator = self._token_creators.get(mint, "")
                trade = Trade(
                    mint=mint, wallet=row["wallet"], tx_type=row["tx_type"],
                    sol_amount=row["sol_amount"] or 0.0,
                    token_amount=row["token_amount"] if "token_amount" in row.keys() else 0.0,
                    new_token_balance=0.0, bonding_curve_key="",
                    v_sol_in_bonding_curve=row["v_sol_in_bonding_curve"] or 0.0,
                    v_tokens_in_bonding_curve=0.0,
                    market_cap_sol=row["market_cap_sol"] or 0.0,
                    timestamp=row["timestamp"],
                    is_creator=(row["wallet"] == creator),
                )

                # Simulate time passing
                if self._speed > 0 and prev_ts > 0:
                    delay = (trade.timestamp - prev_ts) * self._speed
                    if delay > 0:
                        await asyncio.sleep(min(delay, 0.1))
                prev_ts = trade.timestamp

                await queue.put(trade)

                # Yield control to let pipeline process
                await asyncio.sleep(0)
        except asyncio.CancelledError:
            pass
        finally:
            conn.close()
            logger.info("ReplayLaunchpad feeder done")
