# pulse_bot/sources/backtest.py
"""BacktestSource — replays tokens and trades from SQLite chronologically."""

from __future__ import annotations

import logging
import sqlite3
from typing import TYPE_CHECKING, Iterator

from pulse_bot.models import Token, Trade

if TYPE_CHECKING:
    from pulse_bot.clock import SimulatedClock

logger = logging.getLogger(__name__)


class BacktestSource:
    """Reads historical tokens and trades from SQLite for replay.

    Yields tokens in chronological order. For each token, provides
    trades from the observation window split into fast and full phases.
    """

    def __init__(self, db_path: str, clock: SimulatedClock) -> None:
        self.db_path = db_path
        self.clock = clock

    def iter_tokens(self) -> Iterator[Token]:
        """Yield all tokens ordered by created_at."""
        conn = self._conn()
        try:
            cur = conn.execute("SELECT * FROM tokens ORDER BY created_at ASC")
            for row in cur:
                token = Token(
                    mint=row["mint"],
                    name=row["name"] or "",
                    symbol=row["symbol"] or "",
                    creator=row["creator"] or "",
                    created_at=row["created_at"],
                    uri=row["uri"] or "",
                    launchpad=row["launchpad"] or "pumpfun",
                )
                self.clock.advance_to(token.created_at)
                yield token
        finally:
            conn.close()

    def iter_trades(self) -> Iterator[Trade]:
        """Yield all trades ordered by timestamp."""
        conn = self._conn()
        try:
            cur = conn.execute("SELECT * FROM trades ORDER BY timestamp ASC, id ASC")
            for row in cur:
                yield self._row_to_trade(row, row["mint"])
        finally:
            conn.close()

    def get_trades(self, mint: str, from_ts: float, to_ts: float) -> list[Trade]:
        """Get trades for a mint within a time window."""
        conn = self._conn()
        try:
            cur = conn.execute(
                "SELECT * FROM trades WHERE mint = ? AND timestamp >= ? AND timestamp <= ? ORDER BY timestamp ASC",
                (mint, from_ts, to_ts),
            )
            return [self._row_to_trade(row, mint) for row in cur]
        finally:
            conn.close()

    def get_fast_trades(
        self, mint: str, created_at: float, fast_seconds: float
    ) -> list[Trade]:
        """Get trades for fast phase window."""
        return self.get_trades(mint, created_at, created_at + fast_seconds)

    def get_full_trades(
        self, mint: str, created_at: float, full_seconds: float
    ) -> list[Trade]:
        """Get all trades for full observation window."""
        return self.get_trades(mint, created_at, created_at + full_seconds)

    def get_all_trades_after(self, mint: str, from_ts: float) -> list[Trade]:
        """Get ALL trades after a timestamp (for pulse monitoring simulation)."""
        conn = self._conn()
        try:
            cur = conn.execute(
                "SELECT * FROM trades WHERE mint = ? AND timestamp >= ? ORDER BY timestamp ASC",
                (mint, from_ts),
            )
            return [self._row_to_trade(row, mint) for row in cur]
        finally:
            conn.close()

    def get_token_count(self) -> int:
        """Total tokens available for backtest."""
        conn = self._conn()
        try:
            return conn.execute("SELECT COUNT(*) FROM tokens").fetchone()[0]
        finally:
            conn.close()

    def get_trade_count(self) -> int:
        """Total trades available."""
        conn = self._conn()
        try:
            return conn.execute("SELECT COUNT(*) FROM trades").fetchone()[0]
        finally:
            conn.close()

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    @staticmethod
    def _row_to_trade(row: sqlite3.Row, mint: str) -> Trade:
        return Trade(
            mint=mint,
            wallet=row["wallet"],
            tx_type=row["tx_type"],
            sol_amount=row["sol_amount"] or 0.0,
            token_amount=row["token_amount"] if "token_amount" in row.keys() else 0.0,
            new_token_balance=0.0,
            bonding_curve_key="",
            v_sol_in_bonding_curve=row["v_sol_in_bonding_curve"] or 0.0,
            v_tokens_in_bonding_curve=0.0,
            market_cap_sol=row["market_cap_sol"] or 0.0,
            timestamp=row["timestamp"],
            is_creator=bool(row["is_creator"]),
        )
