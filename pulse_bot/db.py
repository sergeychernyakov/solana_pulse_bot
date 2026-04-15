# pulse_bot/db.py
"""SQLite storage with WAL mode. All metrics stored as columns for backtesting SQL queries."""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from typing import TYPE_CHECKING

import aiosqlite

if TYPE_CHECKING:
    from pulse_bot.models import CreatorStats, ScoringResult, Token, Trade

logger = logging.getLogger(__name__)

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS tokens (
    mint TEXT PRIMARY KEY,
    name TEXT, symbol TEXT, creator TEXT,
    created_at REAL, uri TEXT, launchpad TEXT DEFAULT 'pumpfun'
);

CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    mint TEXT NOT NULL, wallet TEXT NOT NULL, tx_type TEXT NOT NULL,
    sol_amount REAL, token_amount REAL,
    market_cap_sol REAL, v_sol_in_bonding_curve REAL,
    timestamp REAL, is_creator INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS token_scores (
    mint TEXT,
    source TEXT DEFAULT 'live',  -- 'live' or 'backtest'
    symbol TEXT, name TEXT, creator TEXT,

    -- Decisions
    total_score INTEGER, decision TEXT,
    fast_decision TEXT DEFAULT '', fast_score INTEGER DEFAULT 0,
    fast_reasons TEXT DEFAULT '', reasons TEXT,

    -- Fast phase metrics
    fast_buy_count INTEGER DEFAULT 0, fast_volume_sol REAL DEFAULT 0.0,
    fast_buy_rate REAL DEFAULT 0.0, fast_unique_buyers INTEGER DEFAULT 0,
    fast_sell_ratio REAL DEFAULT 0.0, fast_elapsed REAL DEFAULT 0.0,
    fast_scored_at REAL DEFAULT 0.0, fast_entry_price REAL DEFAULT 0.0,
    pnl_at_fast_entry_pct REAL DEFAULT 0.0,

    -- Trade pattern metrics
    buy_count INTEGER DEFAULT 0, sell_count INTEGER DEFAULT 0,
    unique_buyers INTEGER DEFAULT 0, unique_sellers INTEGER DEFAULT 0,
    buy_volume_sol REAL DEFAULT 0.0, sell_volume_sol REAL DEFAULT 0.0,
    buy_diversity INTEGER DEFAULT 0, max_buy_sol REAL DEFAULT 0.0,
    creator_sold INTEGER DEFAULT 0, sell_pressure REAL DEFAULT 0.0,
    avg_buy_sol REAL DEFAULT 0.0, median_buy_sol REAL DEFAULT 0.0,
    std_buy_sol REAL DEFAULT 0.0, top3_buyer_pct REAL DEFAULT 0.0,
    repeat_buyer_count INTEGER DEFAULT 0, first_buy_sol REAL DEFAULT 0.0,
    buy_velocity_trend REAL DEFAULT 0.0, buy_size_trend REAL DEFAULT 0.0,
    time_to_first_buy REAL DEFAULT 0.0, buys_per_unique REAL DEFAULT 0.0,

    -- Bonding curve
    curve_progress_pct REAL DEFAULT 0.0, curve_velocity REAL DEFAULT 0.0,
    curve_acceleration REAL DEFAULT 0.0, sol_to_graduation REAL DEFAULT 0.0,
    market_cap_sol REAL DEFAULT 0.0,

    -- Price & P&L
    token_price_sol REAL DEFAULT 0.0, exit_price REAL DEFAULT 0.0,
    pnl_5th_pct REAL DEFAULT 0.0, pnl_10th_pct REAL DEFAULT 0.0,
    pnl_20th_pct REAL DEFAULT 0.0, pnl_50th_pct REAL DEFAULT 0.0,
    pnl_100th_pct REAL DEFAULT 0.0,

    -- Token metadata
    name_length INTEGER DEFAULT 0, symbol_length INTEGER DEFAULT 0,
    has_uri INTEGER DEFAULT 0, is_all_caps INTEGER DEFAULT 0,
    has_numbers INTEGER DEFAULT 0,

    -- Timing
    hour_utc INTEGER DEFAULT 0, creator_tokens_today INTEGER DEFAULT 0,
    gap_create_to_first_trade REAL DEFAULT 0.0,

    -- Market context
    tokens_last_5min INTEGER DEFAULT 0, concurrent_observations INTEGER DEFAULT 0,

    -- Trade data for exact replay
    fast_trade_count INTEGER DEFAULT 0,
    full_trade_count INTEGER DEFAULT 0,
    fast_trade_ids TEXT DEFAULT '',   -- comma-separated trade DB ids
    full_trade_ids TEXT DEFAULT '',   -- comma-separated trade DB ids
    creator_score INTEGER DEFAULT 0,
    creator_reason TEXT DEFAULT '',

    -- Timestamps
    created_at REAL, scored_at REAL,

    PRIMARY KEY (mint, source)
);

CREATE TABLE IF NOT EXISTS creators (
    wallet TEXT PRIMARY KEY,
    total_tokens_created INTEGER DEFAULT 0, times_seen INTEGER DEFAULT 0,
    tokens_where_creator_sold_early INTEGER DEFAULT 0,
    first_seen_at REAL, last_seen_at REAL, blacklisted INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS event_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_type TEXT, data TEXT, timestamp REAL
);

CREATE TABLE IF NOT EXISTS optimization_runs (
    run_id TEXT PRIMARY KEY,
    optimizer_session TEXT,
    params TEXT,
    entry_mode TEXT,
    total_trades INTEGER, wins INTEGER, losses INTEGER,
    win_rate REAL, total_pnl_sol REAL,
    gross_profit_sol REAL, gross_loss_sol REAL,
    profit_factor REAL,
    avg_win_pct REAL, avg_loss_pct REAL,
    avg_win_sol REAL, avg_loss_sol REAL,
    max_drawdown_pct REAL,
    initial_balance_sol REAL, final_balance_sol REAL,
    roi_pct REAL,
    avg_hold_seconds REAL,
    fast_buys INTEGER, full_buys INTEGER,
    exit_reasons TEXT,
    trades_json TEXT,
    created_at REAL
);

CREATE TABLE IF NOT EXISTS optimization_trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT,
    mint TEXT, symbol TEXT,
    entry_type TEXT, exit_reason TEXT,
    entry_price REAL, exit_price REAL,
    entry_time REAL, exit_time REAL,
    sol_invested REAL, sol_received REAL,
    pnl_sol REAL, pnl_pct REAL,
    hold_seconds REAL, partial_sells INTEGER
);

CREATE INDEX IF NOT EXISTS idx_trades_mint_ts ON trades(mint, timestamp);
CREATE INDEX IF NOT EXISTS idx_token_scores_scored ON token_scores(scored_at);
CREATE INDEX IF NOT EXISTS idx_token_scores_decision ON token_scores(decision);
CREATE INDEX IF NOT EXISTS idx_token_scores_fast ON token_scores(fast_decision);
CREATE TABLE IF NOT EXISTS live_decisions (
    mint TEXT PRIMARY KEY,
    symbol TEXT,
    fast_decision TEXT,
    fast_score INTEGER,
    full_decision TEXT,
    full_score INTEGER,
    buy_count INTEGER,
    unique_buyers INTEGER,
    buy_volume_sol REAL,
    created_at REAL,
    decided_at REAL
);

CREATE INDEX IF NOT EXISTS idx_opt_runs_session ON optimization_runs(optimizer_session);
CREATE INDEX IF NOT EXISTS idx_opt_runs_pf ON optimization_runs(profit_factor);
CREATE INDEX IF NOT EXISTS idx_opt_trades_run ON optimization_trades(run_id);
"""

# All columns in token_scores for INSERT (order must match VALUES)
_SCORE_COLUMNS = [
    "mint",
    "source",
    "symbol",
    "name",
    "creator",
    "total_score",
    "decision",
    "fast_decision",
    "fast_score",
    "fast_reasons",
    "reasons",
    "fast_buy_count",
    "fast_volume_sol",
    "fast_buy_rate",
    "fast_unique_buyers",
    "fast_sell_ratio",
    "fast_elapsed",
    "fast_scored_at",
    "fast_entry_price",
    "pnl_at_fast_entry_pct",
    "buy_count",
    "sell_count",
    "unique_buyers",
    "unique_sellers",
    "buy_volume_sol",
    "sell_volume_sol",
    "buy_diversity",
    "max_buy_sol",
    "creator_sold",
    "sell_pressure",
    "avg_buy_sol",
    "median_buy_sol",
    "std_buy_sol",
    "top3_buyer_pct",
    "repeat_buyer_count",
    "first_buy_sol",
    "buy_velocity_trend",
    "buy_size_trend",
    "time_to_first_buy",
    "buys_per_unique",
    "curve_progress_pct",
    "curve_velocity",
    "curve_acceleration",
    "sol_to_graduation",
    "market_cap_sol",
    "token_price_sol",
    "exit_price",
    "pnl_5th_pct",
    "pnl_10th_pct",
    "pnl_20th_pct",
    "pnl_50th_pct",
    "pnl_100th_pct",
    "name_length",
    "symbol_length",
    "has_uri",
    "is_all_caps",
    "has_numbers",
    "hour_utc",
    "creator_tokens_today",
    "gap_create_to_first_trade",
    "tokens_last_5min",
    "concurrent_observations",
    "fast_trade_count",
    "full_trade_count",
    "fast_trade_ids",
    "full_trade_ids",
    "creator_score",
    "creator_reason",
    "created_at",
    "scored_at",
]


class Database:
    """SQLite storage shared between pipeline (async writes) and dashboard (sync reads)."""

    def __init__(self, db_path: str) -> None:
        self.db_path = db_path

    def init_schema(self) -> None:
        """Create all tables. Called once on startup."""
        conn = self._get_sync_conn()
        try:
            conn.executescript(_SCHEMA_SQL)
            self._ensure_token_score_columns(conn)
            conn.commit()
        finally:
            conn.close()

    # ── Sync reads (Streamlit dashboard) ───────────────────

    def get_recent_scores(self, limit: int = 200, source: str = "live") -> list[dict]:
        """Most recent scored tokens, newest first."""
        conn = self._get_sync_conn()
        try:
            cur = conn.execute("SELECT * FROM token_scores WHERE source = ? ORDER BY scored_at DESC LIMIT ?", (source, limit))
            return [dict(row) for row in cur.fetchall()]
        finally:
            conn.close()

    def get_scores_by_date(self, date_str: str, source: str = "live") -> list[dict]:
        """All scored tokens for a given date, newest first."""
        conn = self._get_sync_conn()
        try:
            cur = conn.execute(
                "SELECT * FROM token_scores WHERE source = ? AND date(scored_at, 'unixepoch', 'localtime') = ? ORDER BY scored_at DESC",
                (source, date_str),
            )
            return [dict(row) for row in cur.fetchall()]
        finally:
            conn.close()

    def get_stats(self, source: str = "live") -> dict:
        """Aggregate stats."""
        conn = self._get_sync_conn()
        try:
            cur = conn.execute("""
                SELECT COUNT(*) as total_seen,
                    SUM(CASE WHEN decision = 'BUY' THEN 1 ELSE 0 END) as total_buy,
                    SUM(CASE WHEN decision = 'SKIP' THEN 1 ELSE 0 END) as total_skip,
                    SUM(CASE WHEN decision = 'BORDERLINE' THEN 1 ELSE 0 END) as total_borderline,
                    SUM(CASE WHEN fast_decision = 'FAST_BUY' THEN 1 ELSE 0 END) as total_fast_buy
                FROM token_scores WHERE source = ?
            """, (source,))
            row = cur.fetchone()
            return (
                dict(row)
                if row
                else {
                    "total_seen": 0,
                    "total_buy": 0,
                    "total_skip": 0,
                    "total_borderline": 0,
                    "total_fast_buy": 0,
                }
            )
        finally:
            conn.close()

    def get_stats_by_date(self, date_str: str, source: str = "live") -> dict:
        """Aggregate stats for a specific date."""
        conn = self._get_sync_conn()
        try:
            cur = conn.execute(
                """
                SELECT COUNT(*) as total_seen,
                    SUM(CASE WHEN decision = 'BUY' THEN 1 ELSE 0 END) as total_buy,
                    SUM(CASE WHEN decision = 'SKIP' THEN 1 ELSE 0 END) as total_skip,
                    SUM(CASE WHEN decision = 'BORDERLINE' THEN 1 ELSE 0 END) as total_borderline,
                    SUM(CASE WHEN fast_decision = 'FAST_BUY' THEN 1 ELSE 0 END) as total_fast_buy
                FROM token_scores WHERE source = ? AND date(scored_at, 'unixepoch', 'localtime') = ?
            """,
                (source, date_str),
            )
            row = cur.fetchone()
            return (
                dict(row)
                if row
                else {
                    "total_seen": 0,
                    "total_buy": 0,
                    "total_skip": 0,
                    "total_borderline": 0,
                    "total_fast_buy": 0,
                }
            )
        finally:
            conn.close()

    def get_creator_stats_sync(self, wallet: str) -> CreatorStats | None:
        """Lookup creator from cache."""
        from pulse_bot.models import CreatorStats

        conn = self._get_sync_conn()
        try:
            cur = conn.execute("SELECT * FROM creators WHERE wallet = ?", (wallet,))
            row = cur.fetchone()
            if not row:
                return None
            return CreatorStats(
                wallet=row["wallet"],
                total_tokens_created=row["total_tokens_created"],
                times_seen=row["times_seen"],
                tokens_where_creator_sold_early=row["tokens_where_creator_sold_early"],
                first_seen_at=row["first_seen_at"],
                last_seen_at=row["last_seen_at"],
                blacklisted=bool(row["blacklisted"]),
            )
        finally:
            conn.close()

    def get_creator_tokens_today_sync(self, wallet: str) -> int:
        """Count tokens created by wallet today."""
        conn = self._get_sync_conn()
        try:
            cur = conn.execute(
                "SELECT COUNT(*) as cnt FROM tokens WHERE creator = ? AND date(created_at, 'unixepoch', 'localtime') = date('now', 'localtime')",
                (wallet,),
            )
            row = cur.fetchone()
            return row["cnt"] if row else 0
        finally:
            conn.close()

    def get_tokens_last_5min_sync(self) -> int:
        """Count tokens created in last 5 minutes."""
        conn = self._get_sync_conn()
        try:
            cur = conn.execute(
                "SELECT COUNT(*) as cnt FROM tokens WHERE created_at > ?",
                (time.time() - 300,),
            )
            row = cur.fetchone()
            return row["cnt"] if row else 0
        finally:
            conn.close()

    def get_available_dates(self) -> list[str]:
        """Return dates that have scored tokens."""
        conn = self._get_sync_conn()
        try:
            cur = conn.execute(
                "SELECT DISTINCT date(scored_at, 'unixepoch', 'localtime') as d FROM token_scores ORDER BY d DESC LIMIT 30"
            )
            return [row["d"] for row in cur.fetchall()]
        finally:
            conn.close()

    # ── Optimizer reads (backtest dashboard) ─────────────────

    def get_optimization_runs(self, session: str | None = None, limit: int = 500) -> list[dict]:
        """Get optimization runs, optionally filtered by session."""
        conn = self._get_sync_conn()
        try:
            if session:
                cur = conn.execute(
                    "SELECT * FROM optimization_runs WHERE optimizer_session = ? ORDER BY profit_factor DESC LIMIT ?",
                    (session, limit),
                )
            else:
                cur = conn.execute(
                    "SELECT * FROM optimization_runs ORDER BY created_at DESC LIMIT ?", (limit,),
                )
            return [dict(row) for row in cur.fetchall()]
        finally:
            conn.close()

    def get_optimization_sessions(self) -> list[dict]:
        """Get list of optimizer sessions with summary stats."""
        conn = self._get_sync_conn()
        try:
            cur = conn.execute("""
                SELECT optimizer_session,
                    COUNT(*) as run_count,
                    MAX(profit_factor) as best_pf,
                    MAX(win_rate) as best_wr,
                    MAX(roi_pct) as best_roi,
                    MIN(created_at) as started_at,
                    MAX(created_at) as last_run_at
                FROM optimization_runs
                GROUP BY optimizer_session
                ORDER BY last_run_at DESC
            """)
            return [dict(row) for row in cur.fetchall()]
        finally:
            conn.close()

    def get_optimization_trades(self, run_id: str) -> list[dict]:
        """Get all trades for a specific optimization run."""
        conn = self._get_sync_conn()
        try:
            cur = conn.execute(
                "SELECT * FROM optimization_trades WHERE run_id = ? ORDER BY entry_time ASC",
                (run_id,),
            )
            return [dict(row) for row in cur.fetchall()]
        finally:
            conn.close()

    def save_optimization_run(self, run_data: dict) -> None:
        """Save a single optimization run result (sync, called from optimizer)."""
        conn = self._get_sync_conn()
        try:
            conn.execute(
                """INSERT OR REPLACE INTO optimization_runs
                (run_id, optimizer_session, params, entry_mode,
                 total_trades, wins, losses, win_rate, total_pnl_sol,
                 gross_profit_sol, gross_loss_sol, profit_factor,
                 avg_win_pct, avg_loss_pct, avg_win_sol, avg_loss_sol,
                 max_drawdown_pct, initial_balance_sol, final_balance_sol, roi_pct,
                 avg_hold_seconds, fast_buys, full_buys, exit_reasons, trades_json, created_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    run_data["run_id"], run_data["session"], run_data["params"],
                    run_data["entry_mode"],
                    run_data["total_trades"], run_data["wins"], run_data["losses"],
                    run_data["win_rate"], run_data["total_pnl_sol"],
                    run_data["gross_profit_sol"], run_data["gross_loss_sol"],
                    run_data["profit_factor"],
                    run_data["avg_win_pct"], run_data["avg_loss_pct"],
                    run_data["avg_win_sol"], run_data["avg_loss_sol"],
                    run_data["max_drawdown_pct"],
                    run_data["initial_balance_sol"], run_data["final_balance_sol"],
                    run_data["roi_pct"],
                    run_data["avg_hold_seconds"],
                    run_data["fast_buys"], run_data["full_buys"],
                    run_data["exit_reasons"], run_data["trades_json"],
                    run_data["created_at"],
                ),
            )
            # Save individual trades
            for t in run_data.get("trades", []):
                conn.execute(
                    """INSERT INTO optimization_trades
                    (run_id, mint, symbol, entry_type, exit_reason,
                     entry_price, exit_price, entry_time, exit_time,
                     sol_invested, sol_received, pnl_sol, pnl_pct,
                     hold_seconds, partial_sells)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        run_data["run_id"], t["mint"], t["symbol"],
                        t["entry_type"], t["exit_reason"],
                        t["entry_price"], t["exit_price"],
                        t["entry_time"], t["exit_time"],
                        t["sol_invested"], t["sol_received"],
                        t["pnl_sol"], t["pnl_pct"],
                        t["hold_seconds"], t["partial_sells"],
                    ),
                )
            conn.commit()
        finally:
            conn.close()

    def get_creator_stats_from_tokens_sync(self, wallet: str) -> CreatorStats | None:
        """Compute creator stats directly from tokens table (deterministic, no cache)."""
        from pulse_bot.models import CreatorStats
        conn = self._get_sync_conn()
        try:
            row = conn.execute(
                "SELECT COUNT(*) as total FROM tokens WHERE creator = ?", (wallet,),
            ).fetchone()
            total = row["total"] if row else 0
            if total == 0:
                return None
            return CreatorStats(
                wallet=wallet,
                total_tokens_created=total,
                times_seen=total,
                tokens_where_creator_sold_early=0,
                first_seen_at=0,
                last_seen_at=0,
                blacklisted=False,
            )
        finally:
            conn.close()

    def clear_creators(self) -> None:
        """Reset creator cache. Called before backtest to build from scratch."""
        conn = self._get_sync_conn()
        try:
            conn.execute("DELETE FROM creators")
            conn.commit()
        finally:
            conn.close()

    def clear_backtest_scores(self) -> None:
        """Remove backtest scores so replay starts fresh."""
        conn = self._get_sync_conn()
        try:
            conn.execute("DELETE FROM token_scores WHERE source = 'backtest'")
            conn.commit()
        finally:
            conn.close()

    def get_live_decisions(self) -> list[dict]:
        """Get all live decisions for comparison with backtest."""
        conn = self._get_sync_conn()
        try:
            cur = conn.execute("SELECT * FROM live_decisions ORDER BY created_at ASC")
            return [dict(row) for row in cur.fetchall()]
        finally:
            conn.close()

    # ── Async writes (pipeline) ────────────────────────────

    async def insert_token(self, token: Token) -> None:
        """Insert a newly detected token."""
        async with aiosqlite.connect(self.db_path) as conn:
            await conn.execute("PRAGMA journal_mode=WAL")
            await conn.execute(
                "INSERT OR IGNORE INTO tokens (mint, name, symbol, creator, created_at, uri, launchpad) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    token.mint,
                    token.name,
                    token.symbol,
                    token.creator,
                    token.created_at,
                    token.uri,
                    token.launchpad,
                ),
            )
            await conn.commit()

    async def insert_trades_batch(self, trades: list[Trade]) -> list[int]:
        """Insert a batch of observed trades. Returns list of DB row IDs."""
        if not trades:
            return []
        async with aiosqlite.connect(self.db_path) as conn:
            await conn.execute("PRAGMA journal_mode=WAL")
            await conn.executemany(
                "INSERT INTO trades (mint, wallet, tx_type, sol_amount, token_amount, market_cap_sol, v_sol_in_bonding_curve, timestamp, is_creator) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    (
                        t.mint,
                        t.wallet,
                        t.tx_type,
                        t.sol_amount,
                        t.token_amount,
                        t.market_cap_sol,
                        t.v_sol_in_bonding_curve,
                        t.timestamp,
                        int(t.is_creator),
                    )
                    for t in trades
                ],
            )
            await conn.commit()
            # Get IDs of inserted rows
            cur = await conn.execute(
                "SELECT id FROM trades WHERE mint = ? ORDER BY id DESC LIMIT ?",
                (trades[0].mint, len(trades)),
            )
            rows = await cur.fetchall()
            return [r[0] for r in reversed(rows)]

    async def upsert_scoring_result(self, result: ScoringResult) -> None:
        """Insert or update scoring result with all metrics."""
        placeholders = ", ".join(["?"] * len(_SCORE_COLUMNS))
        cols = ", ".join(_SCORE_COLUMNS)
        values = tuple(self._get_score_value(result, col) for col in _SCORE_COLUMNS)
        async with aiosqlite.connect(self.db_path) as conn:
            await conn.execute("PRAGMA journal_mode=WAL")
            await conn.execute(
                f"INSERT OR REPLACE INTO token_scores ({cols}) VALUES ({placeholders})",
                values,
            )
            await conn.commit()

    async def upsert_creator(self, wallet: str, sold_early: bool) -> None:
        """Update creator cache."""
        now = time.time()
        async with aiosqlite.connect(self.db_path) as conn:
            await conn.execute("PRAGMA journal_mode=WAL")
            await conn.execute(
                """INSERT INTO creators (wallet, total_tokens_created, times_seen, tokens_where_creator_sold_early, first_seen_at, last_seen_at)
                VALUES (?, 1, 1, ?, ?, ?) ON CONFLICT(wallet) DO UPDATE SET
                total_tokens_created = total_tokens_created + 1, times_seen = times_seen + 1,
                tokens_where_creator_sold_early = tokens_where_creator_sold_early + ?, last_seen_at = ?""",
                (wallet, int(sold_early), now, now, int(sold_early), now),
            )
            await conn.commit()

    async def log_event(self, event_type: str, data: dict) -> None:
        """Append to event_log."""
        async with aiosqlite.connect(self.db_path) as conn:
            await conn.execute("PRAGMA journal_mode=WAL")
            await conn.execute(
                "INSERT INTO event_log (event_type, data, timestamp) VALUES (?, ?, ?)",
                (event_type, json.dumps(data, default=str), time.time()),
            )
            await conn.commit()

    async def save_live_decision(self, data: dict) -> None:
        """Save a live pipeline decision for later comparison with backtest."""
        async with aiosqlite.connect(self.db_path) as conn:
            await conn.execute("PRAGMA journal_mode=WAL")
            await conn.execute(
                """INSERT OR REPLACE INTO live_decisions
                (mint, symbol, fast_decision, fast_score, full_decision, full_score,
                 buy_count, unique_buyers, buy_volume_sol, created_at, decided_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (data["mint"], data["symbol"], data["fast_decision"], data["fast_score"],
                 data["full_decision"], data["full_score"],
                 data["buy_count"], data["unique_buyers"], data["buy_volume_sol"],
                 data["created_at"], data["decided_at"]),
            )
            await conn.commit()

    # ── Internal ───────────────────────────────────────────

    def _get_sync_conn(self) -> sqlite3.Connection:
        """Get a sync connection with WAL mode and row factory."""
        conn = sqlite3.connect(self.db_path)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout = 5000")
        conn.row_factory = sqlite3.Row
        return conn

    @staticmethod
    def _ensure_token_score_columns(conn: sqlite3.Connection) -> None:
        """Add new token_scores columns for existing SQLite databases."""
        existing = {row["name"] for row in conn.execute("PRAGMA table_info(token_scores)")}
        columns = {
            "pnl_50th_pct": "REAL DEFAULT 0.0",
            "pnl_100th_pct": "REAL DEFAULT 0.0",
        }
        for name, definition in columns.items():
            if name not in existing:
                conn.execute(f"ALTER TABLE token_scores ADD COLUMN {name} {definition}")

    @staticmethod
    def _get_score_value(result: ScoringResult, col: str) -> object:
        """Extract value from ScoringResult for a DB column."""
        val = getattr(result, col, None)
        if col == "reasons":
            return result.reasons_summary
        if col == "creator_sold":
            return int(result.creator_sold)
        if col == "has_uri":
            return int(result.has_uri)
        if col == "is_all_caps":
            return int(result.is_all_caps)
        if col == "has_numbers":
            return int(result.has_numbers)
        if col == "buy_volume_sol":
            return result.buy_volume_sol
        if val is None:
            return 0
        return val
