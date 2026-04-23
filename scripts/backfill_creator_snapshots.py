# scripts/backfill_creator_snapshots.py
"""One-shot backfill: produce point-in-time creator snapshots for every
token already in ``pulse_bot.db`` (#48 phase 2).

For each (creator, token) pair we record a snapshot at the token's
``created_at``, computed from earlier-only tokens via
``LocalSnapshotSource``. This is what ``get_creator_snapshot_as_of``
will serve during replay/backtest.

Idempotent: creator_snapshots is append-only, but each run adds a row
only if a snapshot at exactly (creator, observed_at) does not already
exist. Re-running is safe — duplicates just get skipped.

Usage:
    .venv/bin/python scripts/backfill_creator_snapshots.py [--db PATH] [--limit N]
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sqlite3
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from pulse_bot.db import Database  # noqa: E402
from pulse_bot.helius_creator import LocalSnapshotSource  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("backfill")


def _existing_snapshots(db_path: str) -> set[tuple[str, float]]:
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(
            "SELECT creator, observed_at FROM creator_snapshots "
            "WHERE api_source = 'local'"
        ).fetchall()
    finally:
        conn.close()
    return {(r[0], float(r[1])) for r in rows}


async def _backfill(db_path: str, limit: int | None) -> None:
    db = Database(db_path)
    db.init_schema()

    conn = sqlite3.connect(db_path)
    try:
        sql = (
            "SELECT mint, creator, created_at FROM tokens "
            "WHERE creator IS NOT NULL AND created_at IS NOT NULL "
            "ORDER BY created_at ASC"
        )
        if limit:
            sql += f" LIMIT {int(limit)}"
        token_rows = conn.execute(sql).fetchall()
    finally:
        conn.close()

    total = len(token_rows)
    logger.info("Found %d tokens to backfill from %s", total, db_path)
    if not total:
        return

    existing = _existing_snapshots(db_path)
    logger.info("Skipping %d already-present snapshots", len(existing))

    source = LocalSnapshotSource(db_path)
    saved = 0
    skipped_existing = 0
    skipped_empty = 0
    t0 = time.monotonic()

    for i, (mint, creator, created_at) in enumerate(token_rows, start=1):
        created_at = float(created_at)
        if (creator, created_at) in existing:
            skipped_existing += 1
            continue

        snap = await source.compute(creator, created_at)
        if snap is None:
            skipped_empty += 1
            continue

        db.save_creator_snapshot(
            creator=snap.creator,
            observed_at=snap.observed_at,
            computed_through_ts=snap.computed_through_ts,
            api_source=snap.api_source,
            total_prior_tokens=snap.total_prior_tokens,
            rug_count=snap.rug_count,
            graduated_count=snap.graduated_count,
            median_peak_mc_sol=snap.median_peak_mc_sol,
            avg_ttl_sec=snap.avg_ttl_sec,
            inter_token_interval_sec=snap.inter_token_interval_sec,
            creator_age_days=snap.creator_age_days,
            creator_balance_sol=snap.creator_balance_sol,
            feature_version=snap.feature_version,
        )
        saved += 1
        existing.add((creator, created_at))

        if i % 200 == 0:
            elapsed = time.monotonic() - t0
            rate = i / elapsed if elapsed > 0 else 0.0
            logger.info(
                "[%d/%d] saved=%d skipped_existing=%d skipped_empty=%d rate=%.1f/s",
                i,
                total,
                saved,
                skipped_existing,
                skipped_empty,
                rate,
            )

    elapsed = time.monotonic() - t0
    logger.info(
        "Backfill done in %.1fs: saved=%d, skipped_existing=%d, skipped_empty_firsttoken=%d",
        elapsed,
        saved,
        skipped_existing,
        skipped_empty,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default="pulse_bot.db")
    parser.add_argument("--limit", type=int, default=None)
    args = parser.parse_args()
    asyncio.run(_backfill(args.db, args.limit))


if __name__ == "__main__":
    main()
