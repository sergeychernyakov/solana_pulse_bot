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
            # Pre-load trades from DB — feeder may have already passed them
            await self._preload_trades(mint)

    async def _preload_trades(self, mint: str) -> None:
        """Load all trades for this mint from DB into its queue."""
        conn = sqlite3.connect(self._db_path)
        conn.row_factory = sqlite3.Row
        try:
            cur = conn.execute("SELECT * FROM trades WHERE mint = ? ORDER BY timestamp ASC", (mint,))
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
                queue = self._trade_queues.get(mint)
                if queue:
                    await queue.put(trade)
        finally:
            conn.close()

    async def unsubscribe_trades(self, mint: str) -> None:
        self._trade_queues.pop(mint, None)
        self._token_creators.pop(mint, None)

    async def stream_trades(self, mint: str, duration_seconds: float) -> AsyncIterator[Trade]:
        """Yield trades from queue within the simulated time window.

        For replay: reads all preloaded trades whose timestamp falls within
        [first_trade_ts, first_trade_ts + duration_seconds].
        """
        queue = self._trade_queues.get(mint)
        if not queue or queue.empty():
            return

        # Peek first trade to get base timestamp
        first_trade = await queue.get()
        base_ts = first_trade.timestamp
        yield first_trade

        while not queue.empty():
            try:
                trade = queue.get_nowait()
                if trade.timestamp > base_ts + duration_seconds:
                    # Past the window — put it back for later phases
                    await queue.put(trade)
                    break
                yield trade
            except asyncio.QueueEmpty:
                break

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
