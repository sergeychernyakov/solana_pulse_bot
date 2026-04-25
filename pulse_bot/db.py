# pulse_bot/db.py
"""PostgreSQL storage. Migrated from SQLite 2026-04-24.

Two connection pools:
* **asyncpg pool** for async writes/reads from the pipeline (one loop,
  many coroutines).
* **psycopg2 ThreadedConnectionPool** for sync reads called from
  non-async contexts (optimizer, backtest, dashboards).

Public API preserved from the SQLite era so callers don't need changes.
SQL differences are translated inline:
* ``?`` / ``$1`` → ``%s`` (psycopg2) or ``$1``/``$2`` (asyncpg).
* ``INSERT OR REPLACE`` → ``ON CONFLICT (pk) DO UPDATE SET ...``.
* ``INSERT OR IGNORE`` → ``ON CONFLICT DO NOTHING``.
* ``ROWID`` → ``insert_order`` (BIGSERIAL column, deterministic).
* ``date(ts, 'unixepoch', 'localtime')`` → ``to_char(to_timestamp(ts)::date, 'YYYY-MM-DD')``.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

import asyncpg
import psycopg2
import psycopg2.extras
import psycopg2.pool

if TYPE_CHECKING:
    from pulse_bot.helius_holders import HolderSnapshot
    from pulse_bot.models import CreatorStats, ScoringResult, Token, Trade

logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────────────────────────────
# Connection configuration
# ──────────────────────────────────────────────────────────────────────

# Legacy callers pass a filesystem path like ``pulse_bot.db``. We ignore
# the path and always connect to the PG database named ``pulse_bot``
# (or override via ``PULSE_PG_DSN`` env). This keeps backwards-compat
# call sites `Database("pulse_bot.db")` working.
_DEFAULT_PG_DSN = os.environ.get(
    "PULSE_PG_DSN",
    "postgresql://sergeychernyakov@localhost/pulse_bot",
)


def _resolve_dsn(path_or_dsn: str | None) -> str:
    """Legacy path is ignored; accept DSN override or use default."""
    if path_or_dsn and path_or_dsn.startswith("postgres"):
        return path_or_dsn
    return _DEFAULT_PG_DSN


# Global pools (lazy-init per process).
_asyncpg_pool: asyncpg.Pool | None = None
_psycopg_pool: psycopg2.pool.ThreadedConnectionPool | None = None


async def _get_async_pool(dsn: str) -> asyncpg.Pool:
    """Return the process-wide asyncpg pool, rebuilding it if it's bound to
    a stale event loop (common in tests where asyncio.run() creates a new
    loop each call). Idempotent in the long-running bot because the same
    loop runs forever."""
    global _asyncpg_pool
    import asyncio

    if _asyncpg_pool is not None:
        try:
            current_loop = asyncio.get_running_loop()
            pool_loop = getattr(_asyncpg_pool, "_loop", None)
            if pool_loop is not None and pool_loop is not current_loop:
                # Stale pool from a previous asyncio.run() call.
                # Best-effort terminate; pool is replaced below regardless.
                try:
                    _asyncpg_pool.terminate()
                except Exception:  # nosec B110
                    pass
                _asyncpg_pool = None
        except RuntimeError:
            pass
    if _asyncpg_pool is None:
        _asyncpg_pool = await asyncpg.create_pool(
            dsn=dsn,
            min_size=2,
            max_size=20,
            command_timeout=30.0,
        )
    return _asyncpg_pool


def _get_sync_pool(dsn: str) -> psycopg2.pool.ThreadedConnectionPool:
    global _psycopg_pool
    if _psycopg_pool is None:
        # psycopg2 DSN syntax: keyword=value
        _psycopg_pool = psycopg2.pool.ThreadedConnectionPool(
            minconn=1,
            maxconn=10,
            dsn=dsn,
        )
    return _psycopg_pool


@contextlib.contextmanager
def _sync_conn(dsn: str):
    """Borrow a psycopg2 connection from the pool, return on exit."""
    pool = _get_sync_pool(dsn)
    conn = pool.getconn()
    try:
        yield conn
    finally:
        pool.putconn(conn)


# Load schema DDL from the sidecar file.
_SCHEMA_SQL_PATH = Path(__file__).with_name("db_schema_pg.sql")


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


# ──────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────


def _qmarks_to_pgparams(sql: str) -> str:
    """Rewrite ``?`` placeholders to ``$1, $2, ...`` for asyncpg."""
    out = []
    i = 0
    in_str = False
    str_ch = None
    for ch in sql:
        if in_str:
            out.append(ch)
            if ch == str_ch:
                in_str = False
        else:
            if ch in ("'", '"'):
                in_str = True
                str_ch = ch
                out.append(ch)
            elif ch == "?":
                i += 1
                out.append(f"${i}")
            else:
                out.append(ch)
    return "".join(out)


def _qmarks_to_pyformat(sql: str) -> str:
    """Rewrite ``?`` placeholders to ``%s`` for psycopg2."""
    out = []
    in_str = False
    str_ch = None
    for ch in sql:
        if in_str:
            out.append(ch)
            if ch == str_ch:
                in_str = False
        else:
            if ch in ("'", '"'):
                in_str = True
                str_ch = ch
                out.append(ch)
            elif ch == "?":
                out.append("%s")
            else:
                out.append(ch)
    return "".join(out)


# ──────────────────────────────────────────────────────────────────────
# Database class
# ──────────────────────────────────────────────────────────────────────


class Database:
    """PostgreSQL storage shared between pipeline, optimizer, dashboards."""

    def __init__(self, db_path: str | None = None) -> None:
        # `db_path` kept for API compat; we ignore it and resolve a PG DSN.
        self.db_path = db_path or "pulse_bot"
        self.dsn = _resolve_dsn(db_path)

    # ── Schema ──────────────────────────────────────────────────────
    def init_schema(self) -> None:
        """Create all tables by executing the sidecar DDL."""
        ddl = _SCHEMA_SQL_PATH.read_text()
        with _sync_conn(self.dsn) as conn:
            conn.autocommit = True
            with conn.cursor() as cur:
                cur.execute(ddl)

    # ── Sync reads (dashboard / optimizer / pipeline scoring) ────────
    def _sync_query(
        self,
        sql: str,
        params: tuple | list | None = None,
        *,
        one: bool = False,
        source_db_path: str | None = None,
    ) -> Any:
        """Run a parameterised query and return dict rows (or one row if ``one``)."""
        dsn = _resolve_dsn(source_db_path) if source_db_path else self.dsn
        sql = _qmarks_to_pyformat(sql)
        with _sync_conn(dsn) as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(sql, tuple(params) if params else None)
                if one:
                    row = cur.fetchone()
                    return dict(row) if row else None
                return [dict(r) for r in cur.fetchall()]

    # ── Common dashboard reads ──────────────────────────────────────
    def get_recent_scores(self, limit: int = 200, source: str = "live") -> list[dict]:
        return self._sync_query(
            "SELECT * FROM token_scores WHERE source = ? "
            "ORDER BY scored_at DESC LIMIT ?",
            (source, limit),
        )

    def get_scores_last_hours(
        self, hours: int = 24, source: str = "live"
    ) -> list[dict]:
        return self._sync_query(
            "SELECT * FROM token_scores WHERE source = ? AND scored_at > ? "
            "ORDER BY scored_at DESC",
            (source, time.time() - hours * 3600),
        )

    def get_scores_by_date(self, date_str: str, source: str = "live") -> list[dict]:
        return self._sync_query(
            "SELECT * FROM token_scores WHERE source = ? "
            "AND to_char(to_timestamp(scored_at)::date, 'YYYY-MM-DD') = ? "
            "ORDER BY scored_at DESC",
            (source, date_str),
        )

    def get_stats(self, source: str = "live") -> dict:
        row = self._sync_query(
            """SELECT COUNT(*) AS total_seen,
                      SUM(CASE WHEN decision='BUY' THEN 1 ELSE 0 END) AS total_buy,
                      SUM(CASE WHEN decision='SKIP' THEN 1 ELSE 0 END) AS total_skip,
                      SUM(CASE WHEN decision='BORDERLINE' THEN 1 ELSE 0 END) AS total_borderline,
                      SUM(CASE WHEN fast_decision='FAST_BUY' THEN 1 ELSE 0 END) AS total_fast_buy
               FROM token_scores WHERE source = ?""",
            (source,),
            one=True,
        )
        return row or {
            "total_seen": 0,
            "total_buy": 0,
            "total_skip": 0,
            "total_borderline": 0,
            "total_fast_buy": 0,
        }

    def get_stats_by_date(self, date_str: str, source: str = "live") -> dict:
        row = self._sync_query(
            """SELECT COUNT(*) AS total_seen,
                      SUM(CASE WHEN decision='BUY' THEN 1 ELSE 0 END) AS total_buy,
                      SUM(CASE WHEN decision='SKIP' THEN 1 ELSE 0 END) AS total_skip,
                      SUM(CASE WHEN decision='BORDERLINE' THEN 1 ELSE 0 END) AS total_borderline,
                      SUM(CASE WHEN fast_decision='FAST_BUY' THEN 1 ELSE 0 END) AS total_fast_buy
               FROM token_scores WHERE source = ?
                 AND to_char(to_timestamp(scored_at)::date, 'YYYY-MM-DD') = ?""",
            (source, date_str),
            one=True,
        )
        return row or {
            "total_seen": 0,
            "total_buy": 0,
            "total_skip": 0,
            "total_borderline": 0,
            "total_fast_buy": 0,
        }

    def get_available_dates(self) -> list[str]:
        rows = self._sync_query(
            "SELECT DISTINCT to_char(to_timestamp(scored_at)::date, 'YYYY-MM-DD') AS d "
            "FROM token_scores ORDER BY d DESC LIMIT 30"
        )
        return [r["d"] for r in rows]

    # ── Creator lookups ─────────────────────────────────────────────
    def get_creator_stats_sync(
        self, wallet: str, source_db_path: str | None = None
    ) -> "CreatorStats | None":
        from pulse_bot.models import CreatorStats

        row = self._sync_query(
            "SELECT * FROM creators WHERE wallet = ?",
            (wallet,),
            one=True,
            source_db_path=source_db_path,
        )
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

    def get_creator_stats_as_of_sync(
        self,
        wallet: str,
        ref_mint: str,
        source_db_path: str | None = None,
    ) -> "CreatorStats | None":
        """As-of creator stats, anchored on insert_order (ex-ROWID)."""
        from pulse_bot.models import CreatorStats

        ref = self._sync_query(
            "SELECT insert_order AS rid, created_at AS cts "
            "FROM tokens WHERE mint = ? LIMIT 1",
            (ref_mint,),
            one=True,
            source_db_path=source_db_path,
        )
        if ref is None:
            return None
        row = self._sync_query(
            """SELECT COUNT(*) AS cnt,
                      MIN(created_at) AS first_ts,
                      MAX(created_at) AS last_ts
               FROM tokens
               WHERE creator = ? AND insert_order < ?""",
            (wallet, ref["rid"]),
            one=True,
            source_db_path=source_db_path,
        )
        total = int(row["cnt"] or 0)
        if total == 0:
            return None
        snap_row = None
        ref_ts = float(ref["cts"] or 0)
        if ref_ts > 0:
            snap_row = self._sync_query(
                """SELECT rug_count, graduated_count, total_prior_tokens,
                          median_peak_mc_sol, creator_age_days,
                          inter_token_interval_sec, creator_balance_sol
                   FROM creator_snapshots
                   WHERE creator = ? AND observed_at < ?
                   ORDER BY observed_at DESC, id DESC LIMIT 1""",
                (wallet, ref_ts),
                one=True,
                source_db_path=source_db_path,
            )
        rug_rate = graduation_rate = 0.0
        median_peak_mc_sol = creator_age_days = 0.0
        inter_token_interval_sec = creator_balance_sol = 0.0
        rug_count = graduated_count = snapshot_prior_tokens = 0
        if snap_row:
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

    def get_creator_tokens_today_sync(self, wallet: str) -> int:
        row = self._sync_query(
            "SELECT COUNT(*) AS cnt FROM tokens WHERE creator = ? "
            "AND to_char(to_timestamp(created_at)::date, 'YYYY-MM-DD') "
            "  = to_char(NOW()::date, 'YYYY-MM-DD')",
            (wallet,),
            one=True,
        )
        return int(row["cnt"]) if row else 0

    def get_creator_tokens_on_day_sync(
        self,
        wallet: str,
        ref_mint: str,
        source_db_path: str | None = None,
    ) -> int:
        ref = self._sync_query(
            "SELECT insert_order AS rid, created_at FROM tokens "
            "WHERE mint = ? LIMIT 1",
            (ref_mint,),
            one=True,
            source_db_path=source_db_path,
        )
        if not ref:
            return 0
        row = self._sync_query(
            """SELECT COUNT(*) AS cnt FROM tokens
               WHERE creator = ? AND insert_order < ?
                 AND to_char(to_timestamp(created_at)::date, 'YYYY-MM-DD')
                     = to_char(to_timestamp(?)::date, 'YYYY-MM-DD')""",
            (wallet, ref["rid"], ref["created_at"]),
            one=True,
            source_db_path=source_db_path,
        )
        return int(row["cnt"]) if row else 0

    # ── Token-count reads (market-context features) ─────────────────
    def get_tokens_last_5min_sync(self, ref_mint: str | None = None) -> int:
        if ref_mint is None:
            now = time.time()
            row = self._sync_query(
                "SELECT COUNT(*) AS cnt FROM tokens "
                "WHERE created_at > ? AND created_at < ?",
                (now - 300.0, now),
                one=True,
            )
            return int(row["cnt"]) if row else 0
        ref = self._sync_query(
            "SELECT insert_order AS rid, created_at FROM tokens "
            "WHERE mint = ? LIMIT 1",
            (ref_mint,),
            one=True,
        )
        if not ref:
            return 0
        row = self._sync_query(
            "SELECT COUNT(*) AS cnt FROM tokens "
            "WHERE insert_order < ? AND created_at > ? AND created_at < ?",
            (ref["rid"], ref["created_at"] - 300.0, ref["created_at"]),
            one=True,
        )
        return int(row["cnt"]) if row else 0

    def get_concurrent_observations_sync(
        self,
        ref_mint: str,
        observe_seconds: float,
    ) -> int:
        ref = self._sync_query(
            "SELECT insert_order AS rid, created_at FROM tokens "
            "WHERE mint = ? LIMIT 1",
            (ref_mint,),
            one=True,
        )
        if not ref:
            return 0
        row = self._sync_query(
            "SELECT COUNT(*) AS cnt FROM tokens "
            "WHERE insert_order < ? AND created_at >= ? AND created_at < ?",
            (ref["rid"], ref["created_at"] - observe_seconds, ref["created_at"]),
            one=True,
        )
        return int(row["cnt"]) if row else 0

    # ── Wallet analytics (Phase E) ──────────────────────────────────
    def get_wallet_prior_stats_sync(
        self,
        wallets: list[str],
        exclude_mint: str,
        cutoff_ts: float,
        source_db_path: str | None = None,
    ) -> dict[str, dict]:
        if not wallets:
            return {}
        placeholders = ",".join("?" * len(wallets))
        sql = f"""SELECT wallet,
                      COUNT(*) AS all_mint_count,
                      MIN(first_buy_ts) AS first_seen_ts,
                      SUM(CASE WHEN sell_volume_sol > 0 THEN 1 ELSE 0 END)
                          AS closed_mint_count,
                      SUM(CASE WHEN sell_volume_sol > 0
                               THEN sell_volume_sol - buy_volume_sol
                               ELSE 0 END) AS total_pnl_sol,
                      SUM(CASE WHEN sell_volume_sol > 0
                               AND sell_volume_sol - buy_volume_sol > 0
                               THEN 1 ELSE 0 END) AS win_count,
                      MAX(CASE WHEN sell_volume_sol > 0
                               THEN sell_volume_sol - buy_volume_sol
                               ELSE NULL END) AS max_pnl_sol
               FROM wallet_activity
               WHERE wallet IN ({placeholders})
                 AND mint != ?
                 AND last_trade_ts < ?
               GROUP BY wallet"""
        rows = self._sync_query(
            sql,
            (*wallets, exclude_mint, cutoff_ts),
            source_db_path=source_db_path,
        )
        out: dict[str, dict] = {}
        for r in rows:
            all_mc = int(r["all_mint_count"] or 0)
            closed_mc = int(r["closed_mint_count"] or 0)
            wc = int(r["win_count"] or 0)
            out[r["wallet"]] = {
                "all_mint_count": all_mc,
                "closed_mint_count": closed_mc,
                "wr": (wc / closed_mc) if closed_mc > 0 else float("nan"),
                "total_pnl_sol": float(r["total_pnl_sol"] or 0.0),
                "max_pnl_sol": (
                    float(r["max_pnl_sol"])
                    if r["max_pnl_sol"] is not None
                    else float("nan")
                ),
                "first_seen_ts": float(r["first_seen_ts"] or 0.0),
            }
        return out

    # ── Async writes (pipeline) ─────────────────────────────────────
    async def _exec(self, sql: str, *args) -> Any:
        pool = await _get_async_pool(self.dsn)
        sql = _qmarks_to_pgparams(sql)
        async with pool.acquire() as conn:
            return await conn.execute(sql, *args)

    async def _fetchval(self, sql: str, *args) -> Any:
        pool = await _get_async_pool(self.dsn)
        sql = _qmarks_to_pgparams(sql)
        async with pool.acquire() as conn:
            return await conn.fetchval(sql, *args)

    async def _fetchrow(self, sql: str, *args) -> Any:
        pool = await _get_async_pool(self.dsn)
        sql = _qmarks_to_pgparams(sql)
        async with pool.acquire() as conn:
            return await conn.fetchrow(sql, *args)

    async def _fetch(self, sql: str, *args) -> list:
        pool = await _get_async_pool(self.dsn)
        sql = _qmarks_to_pgparams(sql)
        async with pool.acquire() as conn:
            return await conn.fetch(sql, *args)

    async def insert_token(self, token: "Token") -> None:
        await self._exec(
            """INSERT INTO tokens (mint, name, symbol, creator, created_at, uri, launchpad)
               VALUES (?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT (mint) DO NOTHING""",
            token.mint,
            token.name,
            token.symbol,
            token.creator,
            token.created_at,
            token.uri,
            token.launchpad,
        )

    async def insert_trades_batch(self, trades: list["Trade"]) -> list[int]:
        if not trades:
            return []
        pool = await _get_async_pool(self.dsn)
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """INSERT INTO trades (mint, wallet, tx_type, sol_amount,
                                        token_amount, market_cap_sol,
                                        v_sol_in_bonding_curve, timestamp,
                                        is_creator)
                   SELECT * FROM UNNEST($1::text[], $2::text[], $3::text[],
                                         $4::double precision[], $5::double precision[],
                                         $6::double precision[], $7::double precision[],
                                         $8::double precision[], $9::smallint[])
                   RETURNING id""",
                [t.mint for t in trades],
                [t.wallet for t in trades],
                [t.tx_type for t in trades],
                [t.sol_amount for t in trades],
                [t.token_amount for t in trades],
                [t.market_cap_sol for t in trades],
                [t.v_sol_in_bonding_curve for t in trades],
                [t.timestamp for t in trades],
                [int(t.is_creator) for t in trades],
            )
            return [r["id"] for r in rows]

    async def upsert_wallet_activity_from_trades(
        self,
        trades: list["Trade"],
    ) -> None:
        if not trades:
            return
        agg: dict[tuple[str, str], dict] = {}
        for t in trades:
            key = (t.wallet, t.mint)
            if key not in agg:
                agg[key] = {
                    "buy_sol": 0.0,
                    "sell_sol": 0.0,
                    "first_ts": t.timestamp,
                    "last_ts": t.timestamp,
                }
            if t.tx_type == "buy":
                agg[key]["buy_sol"] += float(t.sol_amount or 0.0)
            elif t.tx_type == "sell":
                agg[key]["sell_sol"] += float(t.sol_amount or 0.0)
            ts = float(t.timestamp or 0.0)
            if ts < agg[key]["first_ts"]:
                agg[key]["first_ts"] = ts
            if ts > agg[key]["last_ts"]:
                agg[key]["last_ts"] = ts
        now = time.time()
        rows_data = [
            (
                w,
                m,
                a["first_ts"],
                a["last_ts"],
                a["buy_sol"],
                a["sell_sol"],
                a["sell_sol"] - a["buy_sol"],
                now,
            )
            for (w, m), a in agg.items()
        ]
        pool = await _get_async_pool(self.dsn)
        async with pool.acquire() as conn:
            await conn.executemany(
                """INSERT INTO wallet_activity (
                    wallet, mint, first_buy_ts, last_trade_ts,
                    buy_volume_sol, sell_volume_sol, realized_pnl_sol, updated_at
                ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8)
                ON CONFLICT (wallet, mint) DO UPDATE SET
                    first_buy_ts = LEAST(wallet_activity.first_buy_ts, EXCLUDED.first_buy_ts),
                    last_trade_ts = GREATEST(wallet_activity.last_trade_ts, EXCLUDED.last_trade_ts),
                    buy_volume_sol = wallet_activity.buy_volume_sol + EXCLUDED.buy_volume_sol,
                    sell_volume_sol = wallet_activity.sell_volume_sol + EXCLUDED.sell_volume_sol,
                    realized_pnl_sol = (wallet_activity.sell_volume_sol + EXCLUDED.sell_volume_sol)
                                     - (wallet_activity.buy_volume_sol + EXCLUDED.buy_volume_sol),
                    updated_at = EXCLUDED.updated_at""",
                rows_data,
            )

    @staticmethod
    def _get_score_value(result: "ScoringResult", col: str) -> Any:
        """Same semantics as the SQLite version: pull by attr, fall back to 0.0.

        Python ``bool`` values are cast to ``int`` so psycopg2 stores them
        in SMALLINT columns without a type-mismatch error.
        """
        v = getattr(result, col, None)
        if v is None:
            if col in (
                "mint",
                "symbol",
                "name",
                "creator",
                "decision",
                "fast_decision",
                "fast_reasons",
                "reasons",
                "fast_trade_ids",
                "full_trade_ids",
                "creator_reason",
                "source",
            ):
                return ""
            return 0.0
        if isinstance(v, bool):
            return int(v)
        return v

    async def upsert_scoring_result(self, result: "ScoringResult") -> None:
        cols = ", ".join(_SCORE_COLUMNS)
        placeholders = ", ".join(f"${i+1}" for i in range(len(_SCORE_COLUMNS)))
        updates = ", ".join(
            f"{c} = EXCLUDED.{c}" for c in _SCORE_COLUMNS if c not in ("mint", "source")
        )
        values = tuple(self._get_score_value(result, c) for c in _SCORE_COLUMNS)
        pool = await _get_async_pool(self.dsn)
        async with pool.acquire() as conn:
            await conn.execute(
                f"""INSERT INTO token_scores ({cols}) VALUES ({placeholders})
                   ON CONFLICT (mint, source) DO UPDATE SET {updates}""",
                *values,
            )

    async def upsert_creator(self, wallet: str, sold_early: bool) -> None:
        now = time.time()
        await self._exec(
            """INSERT INTO creators (wallet, total_tokens_created, times_seen,
                                     tokens_where_creator_sold_early,
                                     first_seen_at, last_seen_at)
               VALUES (?, 1, 1, ?, ?, ?)
               ON CONFLICT (wallet) DO UPDATE SET
                 total_tokens_created = creators.total_tokens_created + 1,
                 times_seen = creators.times_seen + 1,
                 tokens_where_creator_sold_early = creators.tokens_where_creator_sold_early + EXCLUDED.tokens_where_creator_sold_early,
                 last_seen_at = EXCLUDED.last_seen_at""",
            wallet,
            int(sold_early),
            now,
            now,
        )

    # ── Paper trade lifecycle ───────────────────────────────────────
    async def open_paper_trade(self, data: dict) -> int:
        cols_ordered = [
            "mint",
            "symbol",
            "entry_price",
            "entry_time",
            "entry_mcap_sol",
            "entry_buyer_number",
            "entry_type",
            "entry_score",
            "buy_amount_sol",
        ]
        values = [data.get(c) for c in cols_ordered]
        col_list = ", ".join(cols_ordered)
        placeholders = ", ".join(f"${i+1}" for i in range(len(cols_ordered)))
        pool = await _get_async_pool(self.dsn)
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                f"INSERT INTO paper_trades ({col_list}) VALUES ({placeholders}) RETURNING id",
                *values,
            )
            return int(row["id"])

    async def update_paper_trade(
        self,
        trade_id: int,
        current_price: float,
        entry_price: float,
        total_buys: int,
        total_sells: int,
        mcap_sol: float,
    ) -> None:
        """Live/replay progress update. ``mcap_sol`` matches old API."""
        pnl_pct = (
            (current_price - entry_price) / entry_price * 100.0
            if entry_price > 0
            else 0.0
        )
        await self._exec(
            """UPDATE paper_trades SET
                   current_price = ?, current_pnl_pct = ?,
                   current_mcap_sol = ?,
                   total_buys = ?, total_sells = ?,
                   price_updated_at = ?
               WHERE id = ?""",
            current_price,
            pnl_pct,
            mcap_sol,
            total_buys,
            total_sells,
            time.time(),
            trade_id,
        )

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
        """Mirror the SQLite API: entry_price + buy_amount_sol used to derive
        PnL if caller didn't precompute it; hold_seconds computed in SQL from
        the stored entry_time. Keeps caller sites unchanged from pre-migration."""
        close_ts = time.time() if exit_time is None else exit_time
        if pnl_pct is None:
            from pulse_bot.core import calc_pnl_pct

            pnl_pct = calc_pnl_pct(entry_price, exit_price, buy_amount_sol)
        pnl_sol = buy_amount_sol * (pnl_pct / 100.0)
        await self._exec(
            """UPDATE paper_trades SET
                   status='closed', exit_price=?, exit_time=?,
                   exit_reason=?, exit_buyer_number=?, exit_mcap_sol=?,
                   pnl_pct=?, pnl_sol=?,
                   hold_seconds=GREATEST(? - entry_time, 0),
                   current_price=?, current_pnl_pct=?
               WHERE id=?""",
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
        )

    async def update_live_price(
        self,
        mint: str,
        current_price: float,
        entry_price: float,
    ) -> None:
        """Match old API: accepts entry_price, computes pnl internally."""
        pnl = (
            ((current_price - entry_price) / entry_price) * 100.0
            if entry_price > 0
            else 0.0
        )
        await self._exec(
            "UPDATE token_scores SET current_price = ?, live_pnl_pct = ?, "
            "price_updated_at = ? WHERE mint = ? AND source = 'live'",
            current_price,
            pnl,
            time.time(),
            mint,
        )

    # ── Misc helpers preserved for compat ───────────────────────────
    async def save_sol_price(self, mint: str, price_usd: float) -> None:
        await self._exec(
            "UPDATE token_scores SET sol_price_usd = ? "
            "WHERE mint = ? AND source = 'live'",
            price_usd,
            mint,
        )

    async def save_ml_prediction(
        self,
        mint: str,
        proba: float,
        model_hash: str,
        feature_vector_json: str,
        schema_version: str,
    ) -> None:
        await self._exec(
            """UPDATE token_scores SET
                   ml_entry_proba = ?, ml_model_hash = ?,
                   ml_feature_vector = ?, ml_feature_schema = ?
               WHERE mint = ? AND source = 'live'""",
            proba,
            model_hash,
            feature_vector_json,
            schema_version,
            mint,
        )

    async def insert_scoring_backfill(self, result: "ScoringResult") -> None:
        """Same as upsert_scoring_result but with source='backfill'."""
        result.source = "backfill"
        await self.upsert_scoring_result(result)

    # Optimizer run persistence — preserved 1:1 from SQLite version.
    def save_optimization_run(self, run_data: dict) -> None:
        """Sync save (called from optimizer worker)."""
        cols = [
            "run_id",
            "optimizer_session",
            "params",
            "entry_mode",
            "total_trades",
            "wins",
            "losses",
            "win_rate",
            "total_pnl_sol",
            "gross_profit_sol",
            "gross_loss_sol",
            "profit_factor",
            "avg_win_pct",
            "avg_loss_pct",
            "avg_win_sol",
            "avg_loss_sol",
            "max_drawdown_pct",
            "initial_balance_sol",
            "final_balance_sol",
            "roi_pct",
            "avg_hold_seconds",
            "fast_buys",
            "full_buys",
            "exit_reasons",
            "trades_json",
            "created_at",
        ]
        placeholders = ", ".join("%s" for _ in cols)
        updates = ", ".join(f"{c} = EXCLUDED.{c}" for c in cols if c != "run_id")
        with _sync_conn(self.dsn) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"""INSERT INTO optimization_runs ({", ".join(cols)})
                       VALUES ({placeholders})
                       ON CONFLICT (run_id) DO UPDATE SET {updates}""",
                    tuple(run_data.get(c) for c in cols),
                )
                trades = run_data.get("trades", [])
                if trades:
                    tcols = [
                        "run_id",
                        "mint",
                        "symbol",
                        "entry_type",
                        "exit_reason",
                        "entry_price",
                        "exit_price",
                        "entry_time",
                        "exit_time",
                        "sol_invested",
                        "sol_received",
                        "pnl_sol",
                        "pnl_pct",
                        "hold_seconds",
                        "partial_sells",
                    ]
                    tp = ", ".join("%s" for _ in tcols)
                    cur.executemany(
                        f"INSERT INTO optimization_trades ({', '.join(tcols)}) VALUES ({tp})",
                        [
                            (run_data["run_id"], *(t.get(c) for c in tcols[1:]))
                            for t in trades
                        ],
                    )
            conn.commit()

    def get_optimization_runs(
        self,
        session: str | None = None,
        limit: int = 500,
        min_trades: int = 5,
    ) -> list[dict]:
        if session:
            return self._sync_query(
                "SELECT * FROM optimization_runs WHERE optimizer_session = ? "
                "AND total_trades >= ? ORDER BY profit_factor DESC LIMIT ?",
                (session, min_trades, limit),
            )
        return self._sync_query(
            "SELECT * FROM optimization_runs WHERE total_trades >= ? "
            "ORDER BY created_at DESC LIMIT ?",
            (min_trades, limit),
        )

    def get_optimization_sessions(self, min_trades: int = 5) -> list[dict]:
        return self._sync_query(
            """SELECT optimizer_session,
                      COUNT(*) AS run_count,
                      MAX(profit_factor) AS best_pf,
                      MAX(win_rate) AS best_wr,
                      MAX(roi_pct) AS best_roi,
                      MIN(created_at) AS started_at,
                      MAX(created_at) AS last_run_at
               FROM optimization_runs
               WHERE total_trades >= ?
               GROUP BY optimizer_session ORDER BY last_run_at DESC""",
            (min_trades,),
        )

    def get_optimization_trades(self, run_id: str) -> list[dict]:
        return self._sync_query(
            "SELECT * FROM optimization_trades WHERE run_id = ? "
            "ORDER BY entry_time ASC",
            (run_id,),
        )

    # Backfill scoring support
    def clear_backfill_scores(self) -> None:
        with _sync_conn(self.dsn) as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM token_scores WHERE source = 'backfill'")
            conn.commit()

    def clear_backtest_scores(self) -> None:
        """Purge backtest rows before a verify300 run."""
        with _sync_conn(self.dsn) as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM token_scores WHERE source = 'backtest'")
            conn.commit()

    def get_live_decisions(self) -> list[dict]:
        return self._sync_query("SELECT * FROM live_decisions ORDER BY created_at ASC")

    async def upsert_live_decision(self, data: dict) -> None:
        await self._exec(
            """INSERT INTO live_decisions (mint, symbol, fast_decision, fast_score,
                                           full_decision, full_score,
                                           buy_count, unique_buyers, buy_volume_sol,
                                           created_at, decided_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT (mint) DO UPDATE SET
                   fast_decision = EXCLUDED.fast_decision,
                   fast_score = EXCLUDED.fast_score,
                   full_decision = EXCLUDED.full_decision,
                   full_score = EXCLUDED.full_score,
                   buy_count = EXCLUDED.buy_count,
                   unique_buyers = EXCLUDED.unique_buyers,
                   buy_volume_sol = EXCLUDED.buy_volume_sol,
                   decided_at = EXCLUDED.decided_at""",
            data["mint"],
            data.get("symbol"),
            data.get("fast_decision"),
            data.get("fast_score"),
            data.get("full_decision"),
            data.get("full_score"),
            data.get("buy_count"),
            data.get("unique_buyers"),
            data.get("buy_volume_sol"),
            data.get("created_at"),
            data.get("decided_at"),
        )

    # Streamlit dashboard reads
    def get_open_paper_trades(self) -> list[dict]:
        return self._sync_query(
            "SELECT * FROM paper_trades WHERE status = 'open' "
            "ORDER BY entry_time DESC"
        )

    def get_closed_paper_trades(
        self,
        limit: int = 200,
        hours: int | None = None,
    ) -> list[dict]:
        if hours is not None:
            return self._sync_query(
                "SELECT * FROM paper_trades WHERE status = 'closed' "
                "AND exit_time > ? ORDER BY exit_time DESC LIMIT ?",
                (time.time() - hours * 3600, limit),
            )
        return self._sync_query(
            "SELECT * FROM paper_trades WHERE status = 'closed' "
            "ORDER BY exit_time DESC LIMIT ?",
            (limit,),
        )

    def get_paper_trade_for_resume(self, mint: str) -> dict | None:
        return self._sync_query(
            "SELECT * FROM paper_trades WHERE mint = ? AND status = 'open' "
            "ORDER BY id DESC LIMIT 1",
            (mint,),
            one=True,
        )

    def count_open_paper_trades(self) -> int:
        row = self._sync_query(
            "SELECT COUNT(*) AS cnt FROM paper_trades WHERE status = 'open'",
            one=True,
        )
        return int(row["cnt"]) if row else 0

    def get_paper_trades(self, status: str | None = None) -> list[dict]:
        """Generic paper_trades fetch. Dashboard compat — preserves old
        SQLite API signature."""
        if status:
            return self._sync_query(
                "SELECT * FROM paper_trades WHERE status = ? ORDER BY entry_time DESC",
                (status,),
            )
        return self._sync_query("SELECT * FROM paper_trades ORDER BY entry_time DESC")

    # ── Creator snapshots ───────────────────────────────────────────
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
        with _sync_conn(self.dsn) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """INSERT INTO creator_snapshots (
                        creator, observed_at, computed_through_ts, api_source,
                        feature_version, total_prior_tokens, rug_count,
                        graduated_count, rug_rate, graduation_rate,
                        median_peak_mc_sol, avg_ttl_sec,
                        inter_token_interval_sec, creator_age_days,
                        creator_balance_sol, data_json)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                       RETURNING id""",
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
                sid = cur.fetchone()[0]
            conn.commit()
            return int(sid)

    def get_creator_snapshot_as_of(self, creator: str, ref_ts: float) -> dict | None:
        return self._sync_query(
            "SELECT * FROM creator_snapshots WHERE creator = ? AND observed_at <= ? "
            "ORDER BY observed_at DESC, id DESC LIMIT 1",
            (creator, ref_ts),
            one=True,
        )

    def get_creator_snapshot_latest(self, creator: str) -> dict | None:
        return self._sync_query(
            "SELECT * FROM creator_snapshots WHERE creator = ? "
            "ORDER BY observed_at DESC, id DESC LIMIT 1",
            (creator,),
            one=True,
        )

    def add_creator_flag(
        self, creator: str, flag: str, reason: str | None, set_at: float
    ) -> None:
        with _sync_conn(self.dsn) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE creator_flag_history SET valid_to = %s "
                    "WHERE creator = %s AND valid_to IS NULL",
                    (set_at, creator),
                )
                cur.execute(
                    "INSERT INTO creator_flag_history "
                    "(creator, flag, reason, valid_from) VALUES (%s, %s, %s, %s)",
                    (creator, flag, reason, set_at),
                )
            conn.commit()

    def get_creator_flag_as_of(self, creator: str, ref_ts: float) -> dict | None:
        return self._sync_query(
            "SELECT * FROM creator_flag_history "
            "WHERE creator = ? AND valid_from <= ? "
            "AND (valid_to IS NULL OR valid_to > ?) "
            "ORDER BY valid_from DESC LIMIT 1",
            (creator, ref_ts, ref_ts),
            one=True,
        )

    def get_creator_flag_latest(self, creator: str) -> dict | None:
        return self._sync_query(
            "SELECT * FROM creator_flag_history "
            "WHERE creator = ? AND valid_to IS NULL "
            "ORDER BY valid_from DESC LIMIT 1",
            (creator,),
            one=True,
        )

    # ── Holder snapshots ────────────────────────────────────────────
    def save_holder_snapshot(self, snap: "HolderSnapshot") -> int:
        with _sync_conn(self.dsn) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """INSERT INTO token_holders_snapshots (
                        mint, observed_at, capture_at_age_sec,
                        total_supply_raw, top1_raw, top5_raw, top10_raw,
                        top1_pct, top5_pct, top10_pct,
                        holder_count, is_partial, is_negative_row, api_source)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                       RETURNING id""",
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
                sid = cur.fetchone()[0]
            conn.commit()
            return int(sid)

    def save_holder_capture_failure(
        self,
        mint: str,
        target_age_sec: float,
        error_type: str,
        error_detail: str | None = None,
    ) -> None:
        with _sync_conn(self.dsn) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """INSERT INTO holder_capture_failures
                       (mint, attempted_at, target_age_sec, error_type, error_detail)
                       VALUES (%s,%s,%s,%s,%s)""",
                    (mint, time.time(), target_age_sec, error_type, error_detail),
                )
            conn.commit()

    # ── Event log + live decisions (async) ──────────────────────────
    async def log_event(self, event_type: str, data: dict) -> None:
        await self._exec(
            "INSERT INTO event_log (event_type, data, timestamp) VALUES (?, ?, ?)",
            event_type,
            json.dumps(data),
            time.time(),
        )

    async def save_live_decision(self, data: dict) -> None:
        await self.upsert_live_decision(data)

    async def save_mint_onchain_state(
        self,
        mint: str,
        mint_authority_revoked: bool,
        freeze_authority_revoked: bool,
        checked_at: float,
    ) -> None:
        await self._exec(
            """UPDATE tokens SET
                   mint_authority_revoked = ?,
                   freeze_authority_revoked = ?,
                   onchain_checked_at = ?
               WHERE mint = ?""",
            int(mint_authority_revoked),
            int(freeze_authority_revoked),
            checked_at,
            mint,
        )
