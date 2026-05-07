# scripts/backfill_creator_blacklist.py
"""Periodic re-scan: flip ``creators.blacklisted`` based on snapshot stats.

Definition (Tier-2, data-driven sweep 2026-05-01):
    total_prior_tokens >= 20
    AND graduation_rate < 0.02
    AND median_peak_mc_sol < 28

Idempotent: also UN-flips creators who no longer meet criteria (e.g., a
creator's `graduation_rate` rose above 2%).

Designed to run hourly via systemd timer or cron. Logs counts; safe to
re-run any time.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys

import psycopg2

logger = logging.getLogger("backfill_creator_blacklist")

# Tier-2 thresholds — env-tunable for future calibration without code change.
DEFAULT_MIN_TOKENS = int(os.environ.get("PULSE_SCAMMER_MIN_TOKENS", "20"))
DEFAULT_MAX_GRAD_RATE = float(os.environ.get("PULSE_SCAMMER_MAX_GRAD_RATE", "0.02"))
DEFAULT_MAX_MED_PEAK = float(os.environ.get("PULSE_SCAMMER_MAX_MED_PEAK", "28"))


def _resolve_dsn() -> str:
    dsn = os.environ.get("PULSE_PG_DSN")
    if not dsn:
        raise RuntimeError(
            "PULSE_PG_DSN must be set; source .env first."
        )
    return dsn


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--min-tokens", type=int, default=DEFAULT_MIN_TOKENS)
    parser.add_argument("--max-grad-rate", type=float, default=DEFAULT_MAX_GRAD_RATE)
    parser.add_argument("--max-med-peak", type=float, default=DEFAULT_MAX_MED_PEAK)
    parser.add_argument("--dry-run", action="store_true",
                        help="Report what would change but do not commit.")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    logger.info(
        "Tier-2 thresholds: min_tokens=%d, max_grad_rate=%.3f, max_med_peak=%.1f",
        args.min_tokens, args.max_grad_rate, args.max_med_peak,
    )

    conn = psycopg2.connect(_resolve_dsn())
    try:
        cur = conn.cursor()

        # Latest snapshot per creator.
        cur.execute(
            """
            SELECT COUNT(DISTINCT creator) FROM creator_snapshots
            """
        )
        n_snapshots = cur.fetchone()[0]

        cur.execute(
            """
            WITH latest_meta AS (
              SELECT DISTINCT ON (creator) creator,
                     total_prior_tokens, graduation_rate, median_peak_mc_sol
                FROM creator_snapshots ORDER BY creator, observed_at DESC
            ),
            tier2_creators AS (
              SELECT creator FROM latest_meta
               WHERE total_prior_tokens >= %s
                 AND graduation_rate < %s
                 AND median_peak_mc_sol < %s
            )
            SELECT
              (SELECT COUNT(*) FROM tier2_creators) AS n_should_flag,
              (SELECT COUNT(*) FROM creators WHERE blacklisted = 1) AS n_currently_flagged,
              (SELECT COUNT(*) FROM creators WHERE wallet IN (SELECT creator FROM tier2_creators)) AS n_in_creators_tbl
            """,
            (args.min_tokens, args.max_grad_rate, args.max_med_peak),
        )
        n_should, n_current, n_in_tbl = cur.fetchone()

        logger.info(
            "Snapshot creators=%d, Tier-2 candidates=%d, currently blacklisted=%d, "
            "matching rows in `creators`=%d",
            n_snapshots, n_should, n_current, n_in_tbl,
        )

        if args.dry_run:
            logger.info("DRY RUN — no changes made.")
            return 0

        # Flip ON: creators meeting criteria but not yet flagged.
        cur.execute(
            """
            WITH latest_meta AS (
              SELECT DISTINCT ON (creator) creator,
                     total_prior_tokens, graduation_rate, median_peak_mc_sol
                FROM creator_snapshots ORDER BY creator, observed_at DESC
            ),
            tier2 AS (
              SELECT creator FROM latest_meta
               WHERE total_prior_tokens >= %s
                 AND graduation_rate < %s
                 AND median_peak_mc_sol < %s
            )
            UPDATE creators
               SET blacklisted = 1
             WHERE wallet IN (SELECT creator FROM tier2)
               AND (blacklisted IS NULL OR blacklisted = 0)
            RETURNING wallet
            """,
            (args.min_tokens, args.max_grad_rate, args.max_med_peak),
        )
        flipped_on = len(cur.fetchall())

        # Flip OFF: creators currently flagged but no longer meeting criteria.
        cur.execute(
            """
            WITH latest_meta AS (
              SELECT DISTINCT ON (creator) creator,
                     total_prior_tokens, graduation_rate, median_peak_mc_sol
                FROM creator_snapshots ORDER BY creator, observed_at DESC
            ),
            tier2 AS (
              SELECT creator FROM latest_meta
               WHERE total_prior_tokens >= %s
                 AND graduation_rate < %s
                 AND median_peak_mc_sol < %s
            )
            UPDATE creators
               SET blacklisted = 0
             WHERE blacklisted = 1
               AND wallet NOT IN (SELECT creator FROM tier2)
            RETURNING wallet
            """,
            (args.min_tokens, args.max_grad_rate, args.max_med_peak),
        )
        flipped_off = len(cur.fetchall())

        conn.commit()
        logger.info(
            "Done: flipped ON=%d, flipped OFF=%d",
            flipped_on, flipped_off,
        )
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
