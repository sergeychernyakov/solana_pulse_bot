# pulse_bot/ml/backfill_scoring.py
"""Retroactive scoring of tokens present in DB but not yet in token_scores.

The live pipeline missed many tokens (died between T+90 collection window
or hit rate limits). The `trades` table still has their tx stream, so we
can reconstruct the same ScoringResult by running MetricsCalculator and
Scorer on their historical trades as of T+observe_seconds.

Marked with ``source='backfill'`` to keep it separable from live rows.
Build_dataset already aggregates both live and backfill if queried
without ``source='live'`` constraint — for this script we extend coverage
by ALSO including source='backfill' when labels would exist.

Usage:
    python -m pulse_bot.ml.backfill_scoring [--limit N] [--min-trades K]
"""

from __future__ import annotations

import argparse
import logging
import sqlite3
import sys
import time

from pulse_bot.config import get_config
from pulse_bot.db import Database
from pulse_bot.filters.metrics import MetricsCalculator
from pulse_bot.filters.scorer import Scorer
from pulse_bot.models import Token, Trade

logger = logging.getLogger(__name__)


def _row_to_token(row: sqlite3.Row) -> Token:
    return Token(
        mint=row["mint"],
        name=row["name"] or "",
        symbol=row["symbol"] or "",
        creator=row["creator"] or "",
        created_at=float(row["created_at"] or 0),
        uri=row["uri"] or "",
        launchpad=row["launchpad"] or "pumpfun",
    )


def _row_to_trade(row: sqlite3.Row) -> Trade:
    # The DB schema omits some live-stream fields (new_token_balance,
    # bonding_curve_key, v_tokens_in_bonding_curve) — fill defaults.
    # Scorer/MetricsCalculator only use the fields present in DB, so the
    # defaults are safe. See pulse_bot/filters/metrics.py usage.
    return Trade(
        mint=row["mint"],
        wallet=row["wallet"],
        tx_type=row["tx_type"],
        sol_amount=float(row["sol_amount"] or 0),
        token_amount=float(row["token_amount"] or 0),
        new_token_balance=0.0,
        bonding_curve_key="",
        v_sol_in_bonding_curve=float(row["v_sol_in_bonding_curve"] or 0),
        v_tokens_in_bonding_curve=0.0,
        market_cap_sol=float(row["market_cap_sol"] or 0),
        timestamp=float(row["timestamp"] or 0),
        is_creator=bool(row["is_creator"] or 0),
    )


def _save_backfill_score(
    conn: sqlite3.Connection,
    result,
    fast_trade_count: int,
    full_trade_count: int,
) -> None:
    """Insert minimal ScoringResult into token_scores as source='backfill'.

    We reuse the same ``INSERT OR REPLACE`` path as upsert_scoring_result
    — without duplicating that method's 50+ columns — by writing directly
    with a subset that covers the ML feature list. Remaining columns
    default to 0.0/0 (all have DEFAULT clauses).
    """
    result.source = "backfill"
    result.fast_trade_count = fast_trade_count
    result.full_trade_count = full_trade_count
    # Re-use Database.upsert_scoring_result via a synchronous path — the
    # async version uses aiosqlite but we're in sync context. Build
    # equivalent INSERT OR REPLACE directly.
    from pulse_bot.db import _SCORE_COLUMNS
    from pulse_bot.db import Database as _DB

    cols = ", ".join(_SCORE_COLUMNS)
    placeholders = ", ".join(["?"] * len(_SCORE_COLUMNS))
    values = tuple(_DB._get_score_value(result, col) for col in _SCORE_COLUMNS)
    conn.execute(
        f"INSERT OR REPLACE INTO token_scores ({cols}) VALUES ({placeholders})",
        values,
    )


def run_backfill(
    db_path: str,
    limit: int | None = None,
    min_trades: int = 5,
    skip_existing: bool = True,
) -> dict:
    """Retroactively score tokens not yet in token_scores.

    Args:
        db_path: sqlite file.
        limit: only process first N matching tokens (debug mode).
        min_trades: require at least this many trades in observe window.
        skip_existing: skip tokens already present in token_scores.

    Returns stats dict.
    """
    config = get_config()
    db = Database(db_path)
    _metrics = MetricsCalculator(graduation_sol=85.0)  # noqa: F841 reserved for future replay use
    scorer = Scorer(config, db)

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout = 5000")

    # Candidate mints: in tokens but not in (live) token_scores.
    if skip_existing:
        rows = conn.execute(
            """
            SELECT mint, name, symbol, creator, created_at, uri, launchpad
            FROM tokens
            WHERE mint NOT IN (
                SELECT mint FROM token_scores WHERE source='live'
            )
            ORDER BY created_at ASC
            """
        ).fetchall()
    else:
        rows = conn.execute(
            """SELECT mint, name, symbol, creator, created_at, uri, launchpad
               FROM tokens ORDER BY created_at ASC"""
        ).fetchall()
    logger.info("Candidate tokens: %d", len(rows))

    if limit is not None:
        rows = rows[:limit]
        logger.info("Processing first %d", len(rows))

    stats = {
        "candidates": len(rows),
        "processed": 0,
        "skipped_few_trades": 0,
        "scored": 0,
        "error": 0,
    }
    start = time.time()

    for i, row in enumerate(rows):
        token = _row_to_token(row)
        fast_end = token.created_at + config.fast_observe_seconds
        full_end = token.created_at + config.observe_seconds

        # Load trades in the observation window (same as live path).
        trade_rows = conn.execute(
            """
            SELECT id, mint, wallet, tx_type, sol_amount, token_amount,
                   market_cap_sol, v_sol_in_bonding_curve, timestamp, is_creator
            FROM trades
            WHERE mint = ? AND timestamp <= ?
            ORDER BY timestamp ASC
            """,
            (token.mint, full_end),
        ).fetchall()
        if len(trade_rows) < min_trades:
            stats["skipped_few_trades"] += 1
            stats["processed"] += 1
            continue
        # Match live-pipeline inclusion bar: only score tokens that had
        # meaningful fast-phase activity. Raw "had 5 trades total" lets
        # in tokens that pumped out in 1 minute and died — live would
        # never have looked at those. Require ≥15 trades in fast window
        # AND ≥1 SOL buy volume in fast window (matches fast filter's
        # implicit threshold). Without this, backfill floods training
        # with DOA rows that crash base rate.
        fast_trade_count_check = sum(
            1 for r in trade_rows if r["timestamp"] <= fast_end
        )
        fast_buy_volume_check = sum(
            float(r["sol_amount"] or 0)
            for r in trade_rows
            if r["timestamp"] <= fast_end and r["tx_type"] == "buy"
        )
        if fast_trade_count_check < 15 or fast_buy_volume_check < 1.0:
            stats["skipped_few_trades"] += 1
            stats["processed"] += 1
            continue
        all_trades = [_row_to_trade(r) for r in trade_rows]
        fast_trades = [t for t in all_trades if t.timestamp <= fast_end]

        # Context features as-of token creation (same as live).
        tokens_5min = db.get_tokens_last_5min_sync(ref_mint=token.mint)
        concurrent = db.get_concurrent_observations_sync(
            ref_mint=token.mint,
            observe_seconds=config.observe_seconds,
        )
        creator_tokens_today = db.get_creator_tokens_on_day_sync(
            token.creator,
            ref_mint=token.mint,
        )
        try:
            creator_snapshot = db.get_creator_stats_as_of_sync(
                token.creator,
                ref_mint=token.mint,
            )
        except Exception:
            creator_snapshot = None

        try:
            result = scorer.score(
                token,
                all_trades,
                tokens_last_5min=tokens_5min,
                concurrent_observations=concurrent,
                creator_snapshot=creator_snapshot,
                creator_tokens_today=creator_tokens_today,
            )
        except Exception as e:
            logger.warning("Score failed %s: %s", token.mint[:10], e)
            stats["error"] += 1
            stats["processed"] += 1
            continue

        result.scored_at = full_end  # pretend we scored at T+observe
        _save_backfill_score(
            conn,
            result,
            fast_trade_count=len(fast_trades),
            full_trade_count=len(all_trades),
        )
        stats["scored"] += 1
        stats["processed"] += 1

        if (i + 1) % 500 == 0:
            conn.commit()
            elapsed = time.time() - start
            rate = stats["processed"] / max(elapsed, 0.01)
            remaining = (len(rows) - stats["processed"]) / max(rate, 0.01)
            logger.info(
                "Progress: %d/%d  scored=%d  skipped=%d  err=%d  " "%.1f/s  ETA %.0fs",
                stats["processed"],
                len(rows),
                stats["scored"],
                stats["skipped_few_trades"],
                stats["error"],
                rate,
                remaining,
            )
    conn.commit()
    conn.close()
    stats["elapsed_sec"] = round(time.time() - start, 1)
    return stats


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", default="pulse_bot.db")
    ap.add_argument(
        "--limit", type=int, default=None, help="Process only first N tokens (debug)."
    )
    ap.add_argument("--min-trades", type=int, default=5)
    ap.add_argument(
        "--no-skip-existing",
        action="store_true",
        help="Re-score all tokens, not just missing ones.",
    )
    args = ap.parse_args()
    stats = run_backfill(
        args.db,
        limit=args.limit,
        min_trades=args.min_trades,
        skip_existing=not args.no_skip_existing,
    )
    print("\n=== Backfill stats ===")
    for k, v in stats.items():
        print(f"  {k}: {v}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
