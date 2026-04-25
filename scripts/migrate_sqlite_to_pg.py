# scripts/migrate_sqlite_to_pg.py
"""One-shot data migration SQLite → PostgreSQL.

Moves all tables from pulse_bot.db (SQLite) to the `pulse_bot` Postgres
database. Pulse-bot tables only — optimizer.db / backtest_results.db can
be migrated later if needed.

Usage:
    python scripts/migrate_sqlite_to_pg.py \\
        --sqlite pulse_bot.db \\
        --pg-dsn "postgresql://sergeychernyakov@localhost/pulse_bot"

Strategy per table:
1. `pandas.read_sql_table` — load SQLite rows as DataFrame.
2. Cast types (SMALLINT bool flags, etc.).
3. `df.to_sql(..., if_exists='append', method='multi', chunksize=5000)`
   — batch INSERT via SQLAlchemy.
4. Validate row-count match before/after.

Leaves SQLite file intact for rollback. Writes a report to stdout.
"""

from __future__ import annotations

import argparse
import logging
import sqlite3
import sys
import time
from typing import Any

import pandas as pd
from sqlalchemy import create_engine, text

logger = logging.getLogger("migrate")

# Tables in dependency order (parents before children where possible —
# Postgres doesn't require this since we have no FKs, but it keeps
# progress logs readable).
TABLES = [
    "tokens",
    "creators",
    "trades",
    "token_scores",
    "paper_trades",
    "live_decisions",
    "creator_snapshots",
    "token_holders_snapshots",
    "holder_capture_failures",
    "creator_flag_history",
    "wallet_activity",
    "optimization_runs",
    "optimization_trades",
    "event_log",
]

# SMALLINT flag columns that SQLite stored as INTEGER but Postgres expects
# proper integer type; pandas sometimes coerces these to float64 (because
# of NaN). Cast to Int64 (nullable int) before writing.
INT_FLAG_COLS = {
    "tokens": [],
    "creators": ["total_tokens_created", "times_seen",
                 "tokens_where_creator_sold_early", "blacklisted"],
    "trades": ["is_creator"],
    "token_scores": [
        "total_score", "fast_score", "fast_buy_count", "fast_unique_buyers",
        "buy_count", "sell_count", "unique_buyers", "unique_sellers",
        "buy_diversity", "creator_sold", "repeat_buyer_count",
        "name_length", "symbol_length", "has_uri", "is_all_caps", "has_numbers",
        "hour_utc", "creator_tokens_today", "tokens_last_5min",
        "concurrent_observations", "fast_trade_count", "full_trade_count",
        "creator_score", "unique_buyers_first30s", "unique_buyers_last30s",
    ],
    "paper_trades": [
        "entry_buyer_number", "entry_score", "total_buys", "total_sells",
        "exit_buyer_number",
    ],
    "live_decisions": ["fast_score", "full_score", "buy_count", "unique_buyers"],
    "creator_snapshots": ["feature_version", "total_prior_tokens",
                          "rug_count", "graduated_count"],
    "token_holders_snapshots": ["holder_count", "is_partial", "is_negative_row"],
    "holder_capture_failures": [],
    "creator_flag_history": [],
    "wallet_activity": ["is_closed_at_ingest_time"],
    "optimization_runs": ["total_trades", "wins", "losses",
                          "fast_buys", "full_buys"],
    "optimization_trades": ["partial_sells"],
    "event_log": [],
}


def coerce_int_cols(df: pd.DataFrame, cols: list[str]) -> pd.DataFrame:
    """Cast float columns that should be ints to pandas nullable Int64."""
    for c in cols:
        if c in df.columns:
            # Some values may be NaN — use pandas Int64 (nullable int)
            try:
                df[c] = df[c].astype("Int64")
            except Exception as exc:  # noqa: BLE001
                logger.warning("coerce %s failed: %s", c, exc)
    return df


def migrate_table(sqlite_conn: sqlite3.Connection, pg_engine: Any, name: str) -> tuple[int, int]:
    """Migrate a single table. Returns (source_count, dest_count)."""
    t0 = time.time()
    # Source count (SQLite)
    src_cnt = sqlite_conn.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0]
    logger.info("table %s: %d rows in SQLite", name, src_cnt)
    if src_cnt == 0:
        logger.info("  (empty) — nothing to copy")
        return (0, 0)

    # Load DataFrame
    df = pd.read_sql_query(f"SELECT * FROM {name}", sqlite_conn)
    # Coerce int flag columns (pandas auto-promotes to float on NaN)
    df = coerce_int_cols(df, INT_FLAG_COLS.get(name, []))

    # Write in batches. method='multi' = one INSERT per chunk.
    df.to_sql(
        name,
        pg_engine,
        if_exists="append",
        index=False,
        method="multi",
        chunksize=2000,
    )

    # Dest count
    with pg_engine.connect() as c:
        dst_cnt = c.execute(text(f"SELECT COUNT(*) FROM {name}")).scalar()
    dt = time.time() - t0
    logger.info(
        "  wrote %d / %d rows in %.1fs (%.0f rows/sec)",
        dst_cnt, src_cnt, dt, src_cnt / max(dt, 0.001),
    )
    return (src_cnt, dst_cnt)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sqlite", default="pulse_bot.db", help="SQLite file")
    ap.add_argument(
        "--pg-dsn",
        default="postgresql+psycopg2://sergeychernyakov@localhost/pulse_bot",
        help="SQLAlchemy-style Postgres DSN",
    )
    ap.add_argument(
        "--truncate",
        action="store_true",
        help="TRUNCATE destination tables before insert (idempotent reruns)",
    )
    ap.add_argument(
        "--only",
        help="Comma-separated table list; if set, only these tables are migrated",
    )
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    sqlite_conn = sqlite3.connect(args.sqlite)
    sqlite_conn.row_factory = sqlite3.Row
    pg_engine = create_engine(args.pg_dsn, future=True)

    tables = TABLES
    if args.only:
        wanted = [t.strip() for t in args.only.split(",") if t.strip()]
        tables = [t for t in TABLES if t in wanted]
        missing = [t for t in wanted if t not in TABLES]
        if missing:
            logger.warning("unknown tables ignored: %s", missing)

    if args.truncate:
        logger.info("TRUNCATE ...")
        with pg_engine.begin() as c:
            for t in tables:
                c.execute(text(f"TRUNCATE TABLE {t} RESTART IDENTITY CASCADE"))

    results: list[tuple[str, int, int]] = []
    for t in tables:
        try:
            src, dst = migrate_table(sqlite_conn, pg_engine, t)
            results.append((t, src, dst))
        except Exception as exc:  # noqa: BLE001
            logger.exception("table %s FAILED: %s", t, exc)
            results.append((t, -1, -1))

    print("\n=== MIGRATION REPORT ===")
    print(f"{'table':<30} {'SQLite':>10} {'Postgres':>10}  {'status':>10}")
    all_ok = True
    for name, src, dst in results:
        status = "OK" if src == dst and src >= 0 else "MISMATCH" if src >= 0 else "ERROR"
        if status != "OK":
            all_ok = False
        print(f"{name:<30} {src:>10} {dst:>10}  {status:>10}")
    sqlite_conn.close()
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
