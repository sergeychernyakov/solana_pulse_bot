# scripts/sync_pg_schema.py
"""Compare SQLite and Postgres schemas, add missing columns to PG.

Migration 2026-04-24: initial PG schema was hand-ported from
pulse_bot/db.py `_SCHEMA_SQL`, but a number of ALTER TABLE ADD COLUMN
migrations lived only in `_ensure_token_columns` / `_ensure_token_score_columns`
helpers. This script inspects `PRAGMA table_info` for every SQLite table,
compares to `information_schema.columns` in Postgres, and issues ALTER
TABLE ADD COLUMN for anything missing on the PG side.

Type inference: SQLite TEXT → PG TEXT, INTEGER → INTEGER (or SMALLINT for
flag columns), REAL → DOUBLE PRECISION. Defaults preserved where obvious.

Idempotent: safe to re-run.
"""

from __future__ import annotations

import argparse
import logging
import sqlite3
import sys

import psycopg2

logger = logging.getLogger("sync_schema")

TYPE_MAP = {
    "TEXT": "TEXT",
    "INTEGER": "INTEGER",
    "REAL": "DOUBLE PRECISION",
    "BLOB": "BYTEA",
    "NUMERIC": "DOUBLE PRECISION",
    "": "TEXT",  # untyped
}

# Columns known to be bool-ish flags → use SMALLINT in PG for compactness.
FLAG_COLS = {
    "mint_authority_revoked",
    "freeze_authority_revoked",
    "is_creator",
    "blacklisted",
    "creator_sold",
    "has_uri",
    "is_all_caps",
    "has_numbers",
    "is_partial",
    "is_negative_row",
    "is_closed_at_ingest_time",
}


def pg_type_for(col_name: str, sqlite_type: str, dflt: str | None) -> str:
    """Return PG column type expression with default."""
    sqlite_type = (sqlite_type or "").upper().split("(")[0]
    if col_name in FLAG_COLS:
        pg_type = "SMALLINT"
    else:
        pg_type = TYPE_MAP.get(sqlite_type, "TEXT")
    clause = pg_type
    if dflt is not None:
        # SQLite defaults are literal strings; need light cleanup
        d = dflt.strip()
        if d.upper() == "NULL":
            pass  # leave no DEFAULT
        else:
            clause += f" DEFAULT {d}"
    return clause


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sqlite", default="pulse_bot.db")
    ap.add_argument("--pg-dsn", default="dbname=pulse_bot user=sergeychernyakov")
    ap.add_argument("--apply", action="store_true", help="Execute ALTER statements (else dry-run)")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    slite = sqlite3.connect(args.sqlite)
    slite.row_factory = sqlite3.Row
    pg = psycopg2.connect(args.pg_dsn)
    pg.autocommit = True
    pgcur = pg.cursor()

    # List SQLite tables
    tables = [r[0] for r in slite.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
    ).fetchall()]
    logger.info("tables: %s", tables)

    missing_total = 0
    for t in tables:
        sqlite_cols = {r[1]: r for r in slite.execute(f"PRAGMA table_info({t})").fetchall()}
        pgcur.execute(
            """SELECT column_name FROM information_schema.columns
               WHERE table_name=%s""",
            (t,),
        )
        pg_cols = {r[0] for r in pgcur.fetchall()}
        if not pg_cols:
            logger.warning("table %s not in PG — skipping (create schema first)", t)
            continue
        missing = [c for c in sqlite_cols if c not in pg_cols]
        if not missing:
            logger.info("✓ %s: all %d cols present", t, len(sqlite_cols))
            continue
        logger.info("⚠ %s: missing %d cols in PG: %s", t, len(missing), missing)
        missing_total += len(missing)
        for col in missing:
            row = sqlite_cols[col]  # (cid, name, type, notnull, dflt_value, pk)
            col_type = pg_type_for(col, row[2], row[4])
            sql = f'ALTER TABLE {t} ADD COLUMN IF NOT EXISTS {col} {col_type}'
            if args.apply:
                pgcur.execute(sql)
                logger.info("  APPLIED: %s", sql)
            else:
                logger.info("  DRY-RUN: %s", sql)

    slite.close()
    pg.close()
    logger.info(
        "%d total missing columns across %d tables. Rerun with --apply to commit.",
        missing_total, len(tables),
    )
    return 0 if missing_total == 0 or args.apply else 1


if __name__ == "__main__":
    sys.exit(main())
