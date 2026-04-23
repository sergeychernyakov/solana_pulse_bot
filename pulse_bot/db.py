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
    from pulse_bot.helius_holders import HolderSnapshot
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

    -- Live P&L tracking
    current_price REAL DEFAULT 0.0,
    live_pnl_pct REAL DEFAULT 0.0,
    price_updated_at REAL DEFAULT 0.0,

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

CREATE TABLE IF NOT EXISTS paper_trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    mint TEXT NOT NULL,
    symbol TEXT,
    status TEXT DEFAULT 'open',          -- 'open' | 'closed'

    -- Entry
    entry_price REAL,
    entry_time REAL,
    entry_mcap_sol REAL DEFAULT 0.0,
    entry_buyer_number INTEGER DEFAULT 0, -- we'd be Nth buyer
    entry_type TEXT DEFAULT 'full',       -- 'fast' | 'full'
    entry_score INTEGER DEFAULT 0,

    -- Current (updated while open)
    current_price REAL DEFAULT 0.0,
    current_pnl_pct REAL DEFAULT 0.0,
    current_mcap_sol REAL DEFAULT 0.0,
    total_buys INTEGER DEFAULT 0,
    total_sells INTEGER DEFAULT 0,
    price_updated_at REAL DEFAULT 0.0,

    -- Exit (filled when closed)
    exit_price REAL DEFAULT 0.0,
    exit_time REAL DEFAULT 0.0,
    exit_reason TEXT DEFAULT '',
    exit_buyer_number INTEGER DEFAULT 0,  -- total buys at exit
    exit_mcap_sol REAL DEFAULT 0.0,
    pnl_pct REAL DEFAULT 0.0,
    pnl_sol REAL DEFAULT 0.0,
    hold_seconds REAL DEFAULT 0.0,

    -- Config at time of trade
    buy_amount_sol REAL DEFAULT 0.03
);

CREATE INDEX IF NOT EXISTS idx_paper_status ON paper_trades(status);
CREATE INDEX IF NOT EXISTS idx_paper_mint ON paper_trades(mint);

CREATE INDEX IF NOT EXISTS idx_trades_mint_ts ON trades(mint, timestamp);
CREATE INDEX IF NOT EXISTS idx_trades_ts ON trades(timestamp);
CREATE INDEX IF NOT EXISTS idx_tokens_created ON tokens(created_at);
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

-- Creator snapshots: point-in-time aggregate view of a creator's history.
-- `observed_at` = when we queried (the only time axis backtest must respect).
-- `computed_through_ts` = latest source-event included in the aggregate.
-- Append-only. Backtest reads: observed_at <= token.created_at.
CREATE TABLE IF NOT EXISTS creator_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    creator TEXT NOT NULL,
    observed_at REAL NOT NULL,
    computed_through_ts REAL NOT NULL,
    api_source TEXT NOT NULL,             -- 'helius' | 'backfill' | 'local'
    feature_version INTEGER DEFAULT 1,
    -- Promoted typed fields (queryable without JSON parsing)
    total_prior_tokens INTEGER DEFAULT 0,
    rug_count INTEGER DEFAULT 0,
    graduated_count INTEGER DEFAULT 0,
    rug_rate REAL DEFAULT 0.0,
    graduation_rate REAL DEFAULT 0.0,
    median_peak_mc_sol REAL DEFAULT 0.0,
    avg_ttl_sec REAL DEFAULT 0.0,
    inter_token_interval_sec REAL DEFAULT 0.0,
    creator_age_days REAL DEFAULT 0.0,
    creator_balance_sol REAL DEFAULT 0.0,
    data_json TEXT                        -- raw payload + extension fields
);
CREATE INDEX IF NOT EXISTS idx_creator_snap_creator_obs
    ON creator_snapshots(creator, observed_at);

-- Token holder concentration snapshots (collection-first, no scorer gates
-- yet). Used to validate if top1/top5 concentration at T+N seconds of
-- token life predicts graduation / peak MC / death.
--
-- Each token gets captured at 3 timepoints (T+3, T+10, T+30) so we can
-- measure both absolute concentration AND the derivative (distribution
-- velocity). Pre-T+3 deaths are recorded as "negative rows" (is_negative_row=1).
CREATE TABLE IF NOT EXISTS token_holders_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    mint TEXT NOT NULL,
    observed_at REAL NOT NULL,
    capture_at_age_sec REAL NOT NULL,       -- token age when captured (3, 10, 30, or -1 for negative)
    total_supply_raw TEXT,                  -- NULL for negative rows
    top1_raw TEXT,
    top5_raw TEXT,
    top10_raw TEXT,
    top1_pct REAL,
    top5_pct REAL,
    top10_pct REAL,
    holder_count INTEGER,
    is_partial INTEGER DEFAULT 0,           -- 1 if holder_count < 20 (truncated supply)
    is_negative_row INTEGER DEFAULT 0,      -- 1 if token died before capture (no RPC call)
    api_source TEXT DEFAULT 'helius'
);
CREATE INDEX IF NOT EXISTS idx_holder_snap_mint_obs
    ON token_holders_snapshots(mint, observed_at);
CREATE INDEX IF NOT EXISTS idx_holder_snap_mint_age
    ON token_holders_snapshots(mint, capture_at_age_sec);

-- Track RPC failures to detect correlated-with-pump-wave drops (silent
-- timeouts would otherwise bias the dataset toward uncongested moments).
CREATE TABLE IF NOT EXISTS holder_capture_failures (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    mint TEXT NOT NULL,
    attempted_at REAL NOT NULL,
    target_age_sec REAL NOT NULL,
    error_type TEXT NOT NULL,               -- 'timeout' | 'http_error' | 'parse_error' | 'exception'
    error_detail TEXT
);
CREATE INDEX IF NOT EXISTS idx_holder_fail_attempt
    ON holder_capture_failures(attempted_at);

-- Append-only flag history. A creator's current flag = row with valid_to IS NULL.
-- Backtest-safe lookup: flag active at ts = row where valid_from <= ts < COALESCE(valid_to, +inf).
CREATE TABLE IF NOT EXISTS creator_flag_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    creator TEXT NOT NULL,
    flag TEXT NOT NULL,                   -- 'blacklist' | 'whitelist' | 'suspicious' | 'clear'
    reason TEXT,
    source TEXT DEFAULT 'auto',           -- 'auto' | 'manual'
    valid_from REAL NOT NULL,
    valid_to REAL,                        -- NULL = currently active
    posterior_rug_prob REAL,
    evidence_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_creator_flag_creator_valid
    ON creator_flag_history(creator, valid_from, valid_to);
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
    "first_half_buy_rate",
    "second_half_buy_rate",
    "avg_first_half_buy_sol",
    "avg_second_half_buy_sol",
    "time_gap_median_first20",
    "buy_volume_first10s",
    "unique_buyers_first30s",
    "unique_buyers_last30s",
    "curve_progress_at_t30",
    "curve_progress_at_t60",
    "curve_progress_at_t90",
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
            self._ensure_token_columns(conn)
            self._ensure_token_score_columns(conn)
            conn.commit()
        finally:
            conn.close()

    # ── Sync reads (Streamlit dashboard) ───────────────────

    def get_recent_scores(self, limit: int = 200, source: str = "live") -> list[dict]:
        """Most recent scored tokens, newest first."""
        conn = self._get_sync_conn()
        try:
            cur = conn.execute(
                "SELECT * FROM token_scores WHERE source = ? ORDER BY scored_at DESC LIMIT ?",
                (source, limit),
            )
            return [dict(row) for row in cur.fetchall()]
        finally:
            conn.close()

    def get_scores_last_hours(
        self, hours: int = 24, source: str = "live"
    ) -> list[dict]:
        """Scored tokens from the last N hours, newest first."""
        conn = self._get_sync_conn()
        try:
            cur = conn.execute(
                "SELECT * FROM token_scores WHERE source = ? AND scored_at > ? ORDER BY scored_at DESC",
                (source, time.time() - hours * 3600),
            )
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
            cur = conn.execute(
                """
                SELECT COUNT(*) as total_seen,
                    SUM(CASE WHEN decision = 'BUY' THEN 1 ELSE 0 END) as total_buy,
                    SUM(CASE WHEN decision = 'SKIP' THEN 1 ELSE 0 END) as total_skip,
                    SUM(CASE WHEN decision = 'BORDERLINE' THEN 1 ELSE 0 END) as total_borderline,
                    SUM(CASE WHEN fast_decision = 'FAST_BUY' THEN 1 ELSE 0 END) as total_fast_buy
                FROM token_scores WHERE source = ?
            """,
                (source,),
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

    def get_creator_stats_sync(
        self, wallet: str, source_db_path: str | None = None
    ) -> CreatorStats | None:
        """Lookup creator from the creators table.

        `source_db_path` lets the optimizer read from an isolated snapshot
        instead of the live DB; defaults to this Database's path.
        """
        from pulse_bot.models import CreatorStats

        if source_db_path:
            conn = sqlite3.connect(source_db_path)
            conn.row_factory = sqlite3.Row
        else:
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

    def get_creator_stats_as_of_sync(
        self,
        wallet: str,
        ref_mint: str,
        source_db_path: str | None = None,
    ) -> CreatorStats | None:
        """Reconstruct CreatorStats as of the moment ``ref_mint`` was inserted.

        Anchored on ROWID, not created_at: in live the WS occasionally
        delivers events out of chronological order, so two tokens with
        ts_A > ts_B may be inserted as rowid_A < rowid_B. A ts-based
        as-of filter would then count B in A's prior-set even though, at
        the moment live scored A, B had not yet been inserted. Using
        rowid < ref_rowid reproduces exactly what live saw and stays
        deterministic in replay/optimizer because rowids are stable.
        """
        from pulse_bot.models import CreatorStats

        path = source_db_path or self.db_path
        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
        try:
            ref = conn.execute(
                "SELECT ROWID AS rid, created_at AS cts "
                "FROM tokens WHERE mint = ? LIMIT 1",
                (ref_mint,),
            ).fetchone()
            if ref is None:
                return None
            cur = conn.execute(
                """SELECT COUNT(*) AS cnt,
                          MIN(created_at) AS first_ts,
                          MAX(created_at) AS last_ts
                   FROM tokens
                   WHERE creator = ? AND ROWID < ?""",
                (wallet, ref["rid"]),
            )
            row = cur.fetchone()
            total = int(row["cnt"] or 0)
            if total == 0:
                return None
            # Leak-free enrichment from creator_snapshots: pick the
            # latest snapshot observed strictly before the ref token's
            # created_at. Read-only — never writes, never triggers a
            # refresh. Safe for backtest/optimizer and deterministic.
            snap_row = None
            ref_ts = float(ref["cts"] or 0)
            if ref_ts > 0:
                snap_row = conn.execute(
                    """SELECT rug_count, graduated_count,
                              total_prior_tokens,
                              median_peak_mc_sol, creator_age_days,
                              inter_token_interval_sec, creator_balance_sol
                       FROM creator_snapshots
                       WHERE creator = ? AND observed_at < ?
                       ORDER BY observed_at DESC, id DESC
                       LIMIT 1""",
                    (wallet, ref_ts),
                ).fetchone()

            rug_rate = 0.0
            graduation_rate = 0.0
            median_peak_mc_sol = 0.0
            creator_age_days = 0.0
            inter_token_interval_sec = 0.0
            creator_balance_sol = 0.0
            rug_count = 0
            graduated_count = 0
            snapshot_prior_tokens = 0
            if snap_row is not None:
                snapshot_prior_tokens = int(snap_row["total_prior_tokens"] or 0)
                rug_count = int(snap_row["rug_count"] or 0)
                graduated_count = int(snap_row["graduated_count"] or 0)
                if snapshot_prior_tokens > 0:
                    rug_rate = rug_count / snapshot_prior_tokens
                    graduation_rate = graduated_count / snapshot_prior_tokens
                median_peak_mc_sol = float(snap_row["median_peak_mc_sol"] or 0.0)
                creator_age_days = float(snap_row["creator_age_days"] or 0.0)
                inter_token_interval_sec = float(
                    snap_row["inter_token_interval_sec"] or 0.0
                )
                creator_balance_sol = float(snap_row["creator_balance_sol"] or 0.0)

            return CreatorStats(
                wallet=wallet,
                total_tokens_created=total,
                times_seen=total,
                tokens_where_creator_sold_early=0,
                first_seen_at=float(row["first_ts"] or 0),
                last_seen_at=float(row["last_ts"] or 0),
                blacklisted=False,
                rug_rate=rug_rate,
                graduation_rate=graduation_rate,
                median_peak_mc_sol=median_peak_mc_sol,
                creator_age_days=creator_age_days,
                inter_token_interval_sec=inter_token_interval_sec,
                creator_balance_sol=creator_balance_sol,
                rug_count=rug_count,
                graduated_count=graduated_count,
                snapshot_prior_tokens=snapshot_prior_tokens,
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

    def get_creator_tokens_on_day_sync(
        self, wallet: str, ref_mint: str, source_db_path: str | None = None
    ) -> int:
        """Count tokens by ``wallet`` on the same calendar day as ``ref_mint``,
        restricted to tokens inserted into the DB strictly before ``ref_mint``.

        Anchored on ROWID (insert order) rather than ``created_at`` so that
        live and replay agree even when WS delivers create events out of
        chronological order.
        """
        path = source_db_path or self.db_path
        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
        try:
            ref = conn.execute(
                "SELECT ROWID AS rid, created_at FROM tokens WHERE mint = ? LIMIT 1",
                (ref_mint,),
            ).fetchone()
            if ref is None:
                return 0
            cur = conn.execute(
                """SELECT COUNT(*) AS cnt
                   FROM tokens
                   WHERE creator = ?
                     AND ROWID < ?
                     AND date(created_at, 'unixepoch', 'localtime')
                         = date(?, 'unixepoch', 'localtime')""",
                (wallet, ref["rid"], ref["created_at"]),
            )
            row = cur.fetchone()
            return row["cnt"] if row else 0
        finally:
            conn.close()

    def get_tokens_last_5min_sync(self, ref_mint: str | None = None) -> int:
        """Count tokens in the 5-minute window preceding ``ref_mint``'s insertion.

        Anchored on ROWID (insert order) so live and replay agree even when
        WS delivers events out of chronological order. Passing ``None``
        counts tokens in the 5-minute window ending now — used as a
        fallback from live CLI utilities that have no token to anchor on.
        """
        if ref_mint is None:
            now = time.time()
            conn = self._get_sync_conn()
            try:
                cur = conn.execute(
                    "SELECT COUNT(*) as cnt FROM tokens "
                    "WHERE created_at > ? AND created_at < ?",
                    (now - 300.0, now),
                )
                row = cur.fetchone()
                return row["cnt"] if row else 0
            finally:
                conn.close()
        conn = self._get_sync_conn()
        try:
            ref = conn.execute(
                "SELECT ROWID AS rid, created_at FROM tokens WHERE mint = ? LIMIT 1",
                (ref_mint,),
            ).fetchone()
            if ref is None:
                return 0
            cur = conn.execute(
                "SELECT COUNT(*) as cnt FROM tokens "
                "WHERE ROWID < ? AND created_at > ? AND created_at < ?",
                (ref["rid"], ref["created_at"] - 300.0, ref["created_at"]),
            )
            row = cur.fetchone()
            return row["cnt"] if row else 0
        finally:
            conn.close()

    def get_concurrent_observations_sync(
        self, ref_mint: str, observe_seconds: float
    ) -> int:
        """Count tokens concurrently under observation when ``ref_mint`` was inserted.

        A token is "concurrent" when its created_at falls in
        ``[ref_ts - observe_seconds, ref_ts)`` and it was inserted before
        ``ref_mint`` (ROWID-anchored) — matching what live saw at the
        moment the current token was scored.
        """
        conn = self._get_sync_conn()
        try:
            ref = conn.execute(
                "SELECT ROWID AS rid, created_at FROM tokens WHERE mint = ? LIMIT 1",
                (ref_mint,),
            ).fetchone()
            if ref is None:
                return 0
            cur = conn.execute(
                "SELECT COUNT(*) as cnt FROM tokens "
                "WHERE ROWID < ? AND created_at >= ? AND created_at < ?",
                (
                    ref["rid"],
                    ref["created_at"] - observe_seconds,
                    ref["created_at"],
                ),
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

    def get_optimization_runs(
        self,
        session: str | None = None,
        limit: int = 500,
        min_trades: int = 5,
    ) -> list[dict]:
        """Get optimization runs, optionally filtered by session.

        ``min_trades`` suppresses 0-trade combos that otherwise outrank every
        executed-but-losing config under a raw ``pnl`` sort (codex v5 leak
        fix — matches ``MIN_TRADES_FOR_RANK`` used by the printed leaderboard
        and holdout evaluation). Pass ``min_trades=0`` to inspect raw rows.
        """
        conn = self._get_sync_conn()
        try:
            if session:
                cur = conn.execute(
                    "SELECT * FROM optimization_runs "
                    "WHERE optimizer_session = ? AND total_trades >= ? "
                    "ORDER BY profit_factor DESC LIMIT ?",
                    (session, min_trades, limit),
                )
            else:
                cur = conn.execute(
                    "SELECT * FROM optimization_runs "
                    "WHERE total_trades >= ? "
                    "ORDER BY created_at DESC LIMIT ?",
                    (min_trades, limit),
                )
            return [dict(row) for row in cur.fetchall()]
        finally:
            conn.close()

    def get_optimization_sessions(self, min_trades: int = 5) -> list[dict]:
        """Get list of optimizer sessions with summary stats.

        ``run_count`` and best-of aggregates are computed over rank-eligible
        runs only (``total_trades >= min_trades``) so 0-trade combos with
        ``pnl=0``/``pf=0`` don't displace losing-but-executed configs in
        session-level summaries.
        """
        conn = self._get_sync_conn()
        try:
            cur = conn.execute(
                """
                SELECT optimizer_session,
                    COUNT(*) as run_count,
                    MAX(profit_factor) as best_pf,
                    MAX(win_rate) as best_wr,
                    MAX(roi_pct) as best_roi,
                    MIN(created_at) as started_at,
                    MAX(created_at) as last_run_at
                FROM optimization_runs
                WHERE total_trades >= ?
                GROUP BY optimizer_session
                ORDER BY last_run_at DESC
            """,
                (min_trades,),
            )
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
                    run_data["run_id"],
                    run_data["session"],
                    run_data["params"],
                    run_data["entry_mode"],
                    run_data["total_trades"],
                    run_data["wins"],
                    run_data["losses"],
                    run_data["win_rate"],
                    run_data["total_pnl_sol"],
                    run_data["gross_profit_sol"],
                    run_data["gross_loss_sol"],
                    run_data["profit_factor"],
                    run_data["avg_win_pct"],
                    run_data["avg_loss_pct"],
                    run_data["avg_win_sol"],
                    run_data["avg_loss_sol"],
                    run_data["max_drawdown_pct"],
                    run_data["initial_balance_sol"],
                    run_data["final_balance_sol"],
                    run_data["roi_pct"],
                    run_data["avg_hold_seconds"],
                    run_data["fast_buys"],
                    run_data["full_buys"],
                    run_data["exit_reasons"],
                    run_data["trades_json"],
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
                        run_data["run_id"],
                        t["mint"],
                        t["symbol"],
                        t["entry_type"],
                        t["exit_reason"],
                        t["entry_price"],
                        t["exit_price"],
                        t["entry_time"],
                        t["exit_time"],
                        t["sol_invested"],
                        t["sol_received"],
                        t["pnl_sol"],
                        t["pnl_pct"],
                        t["hold_seconds"],
                        t["partial_sells"],
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
                "SELECT COUNT(*) as total FROM tokens WHERE creator = ?",
                (wallet,),
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

    # ── Creator snapshots (#48) ────────────────────────────
    #
    # Append-only. `observed_at` is the single time axis that backtest
    # reads must respect: a snapshot recorded at T=100 must never feed a
    # decision for a token created at T<100. `computed_through_ts` is
    # carried for future provenance (e.g. when Helius lags the chain).

    def save_creator_snapshot(
        self,
        creator: str,
        observed_at: float,
        computed_through_ts: float,
        api_source: str,
        total_prior_tokens: int = 0,
        rug_count: int = 0,
        graduated_count: int = 0,
        median_peak_mc_sol: float = 0.0,
        avg_ttl_sec: float = 0.0,
        inter_token_interval_sec: float = 0.0,
        creator_age_days: float = 0.0,
        creator_balance_sol: float = 0.0,
        feature_version: int = 1,
        data_json: str | None = None,
    ) -> int:
        rug_rate = (rug_count / total_prior_tokens) if total_prior_tokens else 0.0
        graduation_rate = (
            graduated_count / total_prior_tokens if total_prior_tokens else 0.0
        )
        conn = self._get_sync_conn()
        try:
            cur = conn.execute(
                """
                INSERT INTO creator_snapshots (
                    creator, observed_at, computed_through_ts, api_source,
                    feature_version, total_prior_tokens, rug_count, graduated_count,
                    rug_rate, graduation_rate, median_peak_mc_sol, avg_ttl_sec,
                    inter_token_interval_sec, creator_age_days, creator_balance_sol,
                    data_json
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    creator,
                    observed_at,
                    computed_through_ts,
                    api_source,
                    feature_version,
                    total_prior_tokens,
                    rug_count,
                    graduated_count,
                    rug_rate,
                    graduation_rate,
                    median_peak_mc_sol,
                    avg_ttl_sec,
                    inter_token_interval_sec,
                    creator_age_days,
                    creator_balance_sol,
                    data_json,
                ),
            )
            conn.commit()
            return int(cur.lastrowid or 0)
        finally:
            conn.close()

    def save_holder_snapshot(self, snap: "HolderSnapshot") -> int:
        """Persist a token holder concentration snapshot."""
        conn = self._get_sync_conn()
        try:
            cur = conn.execute(
                """
                INSERT INTO token_holders_snapshots (
                    mint, observed_at, capture_at_age_sec,
                    total_supply_raw, top1_raw, top5_raw, top10_raw,
                    top1_pct, top5_pct, top10_pct,
                    holder_count, is_partial, is_negative_row, api_source
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    snap.mint,
                    snap.observed_at,
                    snap.capture_at_age_sec,
                    str(snap.total_supply_raw) if snap.total_supply_raw else None,
                    str(snap.top1_raw) if snap.top1_raw else None,
                    str(snap.top5_raw) if snap.top5_raw else None,
                    str(snap.top10_raw) if snap.top10_raw else None,
                    snap.top1_pct,
                    snap.top5_pct,
                    snap.top10_pct,
                    snap.holder_count,
                    1 if snap.is_partial else 0,
                    1 if snap.is_negative_row else 0,
                    snap.api_source,
                ),
            )
            conn.commit()
            return int(cur.lastrowid or 0)
        finally:
            conn.close()

    def save_holder_capture_failure(
        self,
        mint: str,
        target_age_sec: float,
        error_type: str,
        error_detail: str | None = None,
    ) -> None:
        """Record a holder-capture RPC failure for later bias analysis
        (timeouts correlated with pump-waves would silently skew the
        snapshot dataset toward uncongested time slots)."""
        conn = self._get_sync_conn()
        try:
            conn.execute(
                """
                INSERT INTO holder_capture_failures
                (mint, attempted_at, target_age_sec, error_type, error_detail)
                VALUES (?,?,?,?,?)
                """,
                (mint, time.time(), target_age_sec, error_type, error_detail),
            )
            conn.commit()
        finally:
            conn.close()

    def get_creator_snapshot_as_of(self, creator: str, ref_ts: float) -> dict | None:
        """Latest snapshot with ``observed_at <= ref_ts``. Backtest-safe.

        Tie-break on id DESC — when two snapshots share an observed_at
        (same-clock second), the later-written row wins deterministically.
        """
        conn = self._get_sync_conn()
        try:
            row = conn.execute(
                """
                SELECT * FROM creator_snapshots
                WHERE creator = ? AND observed_at <= ?
                ORDER BY observed_at DESC, id DESC
                LIMIT 1
                """,
                (creator, ref_ts),
            ).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    def get_creator_snapshot_latest(self, creator: str) -> dict | None:
        """Most recent snapshot regardless of ref time. Live-path only."""
        conn = self._get_sync_conn()
        try:
            row = conn.execute(
                """
                SELECT * FROM creator_snapshots
                WHERE creator = ?
                ORDER BY observed_at DESC, id DESC
                LIMIT 1
                """,
                (creator,),
            ).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    # ── Creator flag history (append-only) ─────────────────
    #
    # A flag is "active" at time T when valid_from <= T < COALESCE(valid_to, +inf).
    # Adding a new flag closes the previously active row (valid_to := new.valid_from).

    def add_creator_flag(
        self,
        creator: str,
        flag: str,
        reason: str | None,
        set_at: float,
        source: str = "auto",
        posterior_rug_prob: float | None = None,
        evidence_json: str | None = None,
    ) -> int:
        conn = self._get_sync_conn()
        try:
            conn.execute(
                """
                UPDATE creator_flag_history
                SET valid_to = ?
                WHERE creator = ? AND valid_to IS NULL
                """,
                (set_at, creator),
            )
            cur = conn.execute(
                """
                INSERT INTO creator_flag_history (
                    creator, flag, reason, source,
                    valid_from, valid_to, posterior_rug_prob, evidence_json
                ) VALUES (?,?,?,?,?,NULL,?,?)
                """,
                (
                    creator,
                    flag,
                    reason,
                    source,
                    set_at,
                    posterior_rug_prob,
                    evidence_json,
                ),
            )
            conn.commit()
            return int(cur.lastrowid or 0)
        finally:
            conn.close()

    def get_creator_flag_as_of(self, creator: str, ref_ts: float) -> dict | None:
        """Flag active at ``ref_ts``. Backtest-safe (freezes historical decisions)."""
        conn = self._get_sync_conn()
        try:
            row = conn.execute(
                """
                SELECT * FROM creator_flag_history
                WHERE creator = ?
                  AND valid_from <= ?
                  AND (valid_to IS NULL OR valid_to > ?)
                ORDER BY valid_from DESC, id DESC
                LIMIT 1
                """,
                (creator, ref_ts, ref_ts),
            ).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    def get_creator_flag_latest(self, creator: str) -> dict | None:
        """Currently active flag (valid_to IS NULL). Live-path only."""
        conn = self._get_sync_conn()
        try:
            row = conn.execute(
                """
                SELECT * FROM creator_flag_history
                WHERE creator = ? AND valid_to IS NULL
                ORDER BY id DESC
                LIMIT 1
                """,
                (creator,),
            ).fetchone()
            return dict(row) if row else None
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
            await conn.execute("PRAGMA synchronous=NORMAL")
            await conn.execute("PRAGMA busy_timeout = 5000")
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
            await conn.execute("PRAGMA synchronous=NORMAL")
            await conn.execute("PRAGMA busy_timeout = 5000")
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
            return [r[0] for r in list(reversed(list(rows)))]

    async def upsert_scoring_result(self, result: ScoringResult) -> None:
        """Insert or update scoring result with all metrics."""
        placeholders = ", ".join(["?"] * len(_SCORE_COLUMNS))
        cols = ", ".join(_SCORE_COLUMNS)
        values = tuple(self._get_score_value(result, col) for col in _SCORE_COLUMNS)
        async with aiosqlite.connect(self.db_path) as conn:
            await conn.execute("PRAGMA journal_mode=WAL")
            await conn.execute("PRAGMA synchronous=NORMAL")
            await conn.execute("PRAGMA busy_timeout = 5000")
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
            await conn.execute("PRAGMA synchronous=NORMAL")
            await conn.execute("PRAGMA busy_timeout = 5000")
            await conn.execute(
                """INSERT INTO creators (wallet, total_tokens_created, times_seen, tokens_where_creator_sold_early, first_seen_at, last_seen_at)
                VALUES (?, 1, 1, ?, ?, ?) ON CONFLICT(wallet) DO UPDATE SET
                total_tokens_created = total_tokens_created + 1, times_seen = times_seen + 1,
                tokens_where_creator_sold_early = tokens_where_creator_sold_early + ?, last_seen_at = ?""",
                (wallet, int(sold_early), now, now, int(sold_early), now),
            )
            await conn.commit()

    async def open_paper_trade(self, data: dict) -> int:
        """Open a virtual paper trade position. Returns row ID."""
        async with aiosqlite.connect(self.db_path) as conn:
            await conn.execute("PRAGMA journal_mode=WAL")
            await conn.execute("PRAGMA synchronous=NORMAL")
            await conn.execute("PRAGMA busy_timeout = 5000")
            cur = await conn.execute(
                """INSERT INTO paper_trades
                (mint, symbol, status, entry_price, entry_time, entry_mcap_sol,
                 entry_buyer_number, entry_type, entry_score, current_price,
                 current_pnl_pct, buy_amount_sol)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    data["mint"],
                    data["symbol"],
                    "open",
                    data["entry_price"],
                    data["entry_time"],
                    data.get("entry_mcap_sol", 0),
                    data.get("entry_buyer_number", 0),
                    data.get("entry_type", "full"),
                    data.get("entry_score", 0),
                    data["entry_price"],
                    0.0,
                    data.get("buy_amount_sol", 0.03),
                ),
            )
            await conn.commit()
            return cur.lastrowid or 0

    async def update_paper_trade(
        self,
        trade_id: int,
        current_price: float,
        entry_price: float,
        total_buys: int,
        total_sells: int,
        mcap_sol: float,
    ) -> None:
        """Update an open paper trade with current price."""
        pnl = (
            ((current_price - entry_price) / entry_price) * 100
            if entry_price > 0
            else 0
        )
        async with aiosqlite.connect(self.db_path) as conn:
            await conn.execute("PRAGMA journal_mode=WAL")
            await conn.execute("PRAGMA synchronous=NORMAL")
            await conn.execute("PRAGMA busy_timeout = 5000")
            await conn.execute(
                """UPDATE paper_trades SET current_price=?, current_pnl_pct=?,
                   total_buys=?, total_sells=?, current_mcap_sol=?, price_updated_at=?
                   WHERE id=?""",
                (
                    current_price,
                    pnl,
                    total_buys,
                    total_sells,
                    mcap_sol,
                    time.time(),
                    trade_id,
                ),
            )
            await conn.commit()

    async def close_paper_trade(
        self,
        trade_id: int,
        exit_price: float,
        exit_reason: str,
        exit_buyer_number: int,
        exit_mcap_sol: float,
        entry_price: float,
        buy_amount_sol: float,
        exit_time: float | None = None,
        pnl_pct: float | None = None,
    ) -> None:
        """Close a paper trade with exit details.

        ``exit_time`` defaults to wall-clock ``time.time()`` for live trading.
        Replay/backtest pass a virtual timestamp so ``hold_seconds`` reflects
        the real elapsed time between entry and exit events, not wall-clock.
        ``hold_seconds`` is computed in SQL from the stored ``entry_time`` so
        callers never need to pass it — this closes a class of bugs where
        callers accidentally passed ``token.created_at`` (token age) instead
        of the actual position open time.

        ``pnl_pct`` may be supplied by callers that already computed a
        fee-adjusted or partial-fill-weighted P&L (e.g. PaperTradeRunner).
        When omitted the DB falls back to the same fee-adjusted helper used
        by PaperTradeRunner so persisted live PnL matches optimizer PnL.
        """
        close_ts = time.time() if exit_time is None else exit_time
        if pnl_pct is None:
            from pulse_bot.core import calc_pnl_pct

            pnl_pct = calc_pnl_pct(entry_price, exit_price, buy_amount_sol)
        pnl_sol = buy_amount_sol * (pnl_pct / 100)
        async with aiosqlite.connect(self.db_path) as conn:
            await conn.execute("PRAGMA journal_mode=WAL")
            await conn.execute("PRAGMA synchronous=NORMAL")
            await conn.execute("PRAGMA busy_timeout = 5000")
            await conn.execute(
                """UPDATE paper_trades SET status='closed', exit_price=?, exit_time=?,
                   exit_reason=?, exit_buyer_number=?, exit_mcap_sol=?,
                   pnl_pct=?, pnl_sol=?, hold_seconds=MAX(? - entry_time, 0),
                   current_price=?, current_pnl_pct=?
                   WHERE id=?""",
                (
                    exit_price,
                    close_ts,
                    exit_reason,
                    exit_buyer_number,
                    exit_mcap_sol,
                    pnl_pct,
                    pnl_sol,
                    close_ts,
                    exit_price,
                    pnl_pct,
                    trade_id,
                ),
            )
            await conn.commit()

    def get_paper_trades(self, status: str | None = None) -> list[dict]:
        """Get paper trades, optionally filtered by status."""
        conn = self._get_sync_conn()
        try:
            if status:
                cur = conn.execute(
                    "SELECT * FROM paper_trades WHERE status=? ORDER BY entry_time DESC",
                    (status,),
                )
            else:
                cur = conn.execute(
                    "SELECT * FROM paper_trades ORDER BY entry_time DESC"
                )
            return [dict(row) for row in cur.fetchall()]
        finally:
            conn.close()

    async def update_live_price(
        self, mint: str, current_price: float, entry_price: float
    ) -> None:
        """Update current price and live P&L for a token."""
        pnl = (
            ((current_price - entry_price) / entry_price) * 100
            if entry_price > 0
            else 0
        )
        async with aiosqlite.connect(self.db_path) as conn:
            await conn.execute("PRAGMA journal_mode=WAL")
            await conn.execute("PRAGMA synchronous=NORMAL")
            await conn.execute("PRAGMA busy_timeout = 5000")
            await conn.execute(
                "UPDATE token_scores SET current_price=?, live_pnl_pct=?, price_updated_at=? WHERE mint=? AND source='live'",
                (current_price, pnl, time.time(), mint),
            )
            await conn.commit()

    async def log_event(self, event_type: str, data: dict) -> None:
        """Append to event_log."""
        async with aiosqlite.connect(self.db_path) as conn:
            await conn.execute("PRAGMA journal_mode=WAL")
            await conn.execute("PRAGMA synchronous=NORMAL")
            await conn.execute("PRAGMA busy_timeout = 5000")
            await conn.execute(
                "INSERT INTO event_log (event_type, data, timestamp) VALUES (?, ?, ?)",
                (event_type, json.dumps(data, default=str), time.time()),
            )
            await conn.commit()

    async def save_mint_onchain_state(
        self,
        mint: str,
        mint_authority_revoked: bool,
        freeze_authority_revoked: bool,
    ) -> None:
        """Persist SPL mint authority state for a token. Called fire-and-
        forget from pipeline after Helius ``getAccountInfo`` parse. NULL
        means we never checked (default); int 0/1 means we did."""
        async with aiosqlite.connect(self.db_path) as conn:
            await conn.execute("PRAGMA journal_mode=WAL")
            await conn.execute("PRAGMA synchronous=NORMAL")
            await conn.execute("PRAGMA busy_timeout = 5000")
            await conn.execute(
                """UPDATE tokens
                   SET mint_authority_revoked = ?,
                       freeze_authority_revoked = ?,
                       onchain_checked_at = ?
                   WHERE mint = ?""",
                (
                    int(mint_authority_revoked),
                    int(freeze_authority_revoked),
                    time.time(),
                    mint,
                ),
            )
            await conn.commit()

    async def save_sol_price(self, mint: str, sol_price_usd: float) -> None:
        """Log SOL/USD price observed at scoring time for this token."""
        async with aiosqlite.connect(self.db_path) as conn:
            await conn.execute("PRAGMA journal_mode=WAL")
            await conn.execute("PRAGMA synchronous=NORMAL")
            await conn.execute("PRAGMA busy_timeout = 5000")
            await conn.execute(
                "UPDATE token_scores SET sol_price_usd = ? "
                "WHERE mint = ? AND source = 'live'",
                (float(sol_price_usd), mint),
            )
            await conn.commit()

    async def save_ml_prediction(
        self,
        mint: str,
        proba: float,
        model_hash: str,
        feature_vector_json: str,
        schema_version: str,
    ) -> None:
        """Record an ML prediction against an existing token_scores row.

        Called after ``upsert_scoring_result``. Shadow mode writes this on
        every scored token regardless of the active policy — enabling
        later comparison of "what would ML have decided?" vs the rules
        track. Writing to existing row (not new) — row is keyed by
        (mint, source='live').
        """
        async with aiosqlite.connect(self.db_path) as conn:
            await conn.execute("PRAGMA journal_mode=WAL")
            await conn.execute("PRAGMA synchronous=NORMAL")
            await conn.execute("PRAGMA busy_timeout = 5000")
            await conn.execute(
                """UPDATE token_scores
                   SET ml_entry_proba = ?,
                       ml_model_hash = ?,
                       ml_feature_vector = ?,
                       ml_feature_schema = ?
                   WHERE mint = ? AND source = 'live'""",
                (proba, model_hash, feature_vector_json, schema_version, mint),
            )
            await conn.commit()

    async def save_live_decision(self, data: dict) -> None:
        """Save a live pipeline decision for later comparison with backtest."""
        async with aiosqlite.connect(self.db_path) as conn:
            await conn.execute("PRAGMA journal_mode=WAL")
            await conn.execute("PRAGMA synchronous=NORMAL")
            await conn.execute("PRAGMA busy_timeout = 5000")
            await conn.execute(
                """INSERT OR REPLACE INTO live_decisions
                (mint, symbol, fast_decision, fast_score, full_decision, full_score,
                 buy_count, unique_buyers, buy_volume_sol, created_at, decided_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    data["mint"],
                    data["symbol"],
                    data["fast_decision"],
                    data["fast_score"],
                    data["full_decision"],
                    data["full_score"],
                    data["buy_count"],
                    data["unique_buyers"],
                    data["buy_volume_sol"],
                    data["created_at"],
                    data["decided_at"],
                ),
            )
            await conn.commit()

    # ── Internal ───────────────────────────────────────────

    def _get_sync_conn(self) -> sqlite3.Connection:
        """Get a sync connection with WAL mode and row factory."""
        conn = sqlite3.connect(self.db_path)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout = 5000")
        conn.row_factory = sqlite3.Row
        return conn

    @staticmethod
    def _ensure_token_columns(conn: sqlite3.Connection) -> None:
        """Add on-chain state columns to existing ``tokens`` tables.

        Mint + freeze authority flags are captured once per token via
        Helius ``getAccountInfo``. Stored but NOT yet added to ML feature
        schema — codex 2026-04-22 note: pump.fun auto-revokes so current
        variance is ~0, but we capture anyway for future-launchpad
        readiness and because the cost is a single column + one RPC.
        """
        existing = {row["name"] for row in conn.execute("PRAGMA table_info(tokens)")}
        columns = {
            "mint_authority_revoked": "INTEGER DEFAULT NULL",
            "freeze_authority_revoked": "INTEGER DEFAULT NULL",
            "onchain_checked_at": "REAL DEFAULT NULL",
        }
        for name, definition in columns.items():
            if name not in existing:
                conn.execute(f"ALTER TABLE tokens ADD COLUMN {name} {definition}")

    @staticmethod
    def _ensure_token_score_columns(conn: sqlite3.Connection) -> None:
        """Add new token_scores columns for existing SQLite databases."""
        existing = {
            row["name"] for row in conn.execute("PRAGMA table_info(token_scores)")
        }
        columns = {
            "pnl_50th_pct": "REAL DEFAULT 0.0",
            "pnl_100th_pct": "REAL DEFAULT 0.0",
            "current_price": "REAL DEFAULT 0.0",
            "live_pnl_pct": "REAL DEFAULT 0.0",
            "price_updated_at": "REAL DEFAULT 0.0",
            # ML prediction logging (shadow + live). NULL = no model loaded.
            "ml_entry_proba": "REAL DEFAULT NULL",
            "ml_model_hash": "TEXT DEFAULT NULL",
            "ml_feature_vector": "TEXT DEFAULT NULL",
            "ml_feature_schema": "TEXT DEFAULT NULL",
            # Raw halves (codex 2026-04-22, decoupled from ratio fields)
            "first_half_buy_rate": "REAL DEFAULT 0.0",
            "second_half_buy_rate": "REAL DEFAULT 0.0",
            "avg_first_half_buy_sol": "REAL DEFAULT 0.0",
            "avg_second_half_buy_sol": "REAL DEFAULT 0.0",
            # 2026-04-23 feature additions
            "time_gap_median_first20": "REAL DEFAULT 0.0",
            "buy_volume_first10s": "REAL DEFAULT 0.0",
            "unique_buyers_first30s": "INTEGER DEFAULT 0",
            "unique_buyers_last30s": "INTEGER DEFAULT 0",
            "curve_progress_at_t30": "REAL DEFAULT 0.0",
            "curve_progress_at_t60": "REAL DEFAULT 0.0",
            "curve_progress_at_t90": "REAL DEFAULT 0.0",
            # External market context. Captured side-column, NOT yet in
            # ENTRY_FEATURE_ORDER — historical data has NULL so a feature
            # would train-serve-skew until we accumulate enough live rows.
            "sol_price_usd": "REAL DEFAULT NULL",
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
