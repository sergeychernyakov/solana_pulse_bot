# scripts/retrain_entry_timing.py
"""Retrain entry_timing 3-class model on recent paper-trade data.

Pulls the last N closed mints + their full trade history from PG, runs
``EntryTimingLabelBuilder.build_for_corpus``, then ``train_entry_timing``.
Writes ``data/ml/entry_timing_model.ubj`` + matching meta.json.

The 2026-04-28 retrain is meant to test the inverse-frequency
sample-weighting fix added to ``train_entry_timing`` (without it the
model collapses to ``p_skip ≈ 1.0`` on every live token because SKIP
dominates the prior 75-80%).

Usage::

    python -m scripts.retrain_entry_timing
    python -m scripts.retrain_entry_timing --max-mints 5000
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

logger = logging.getLogger(__name__)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--max-mints", type=int, default=10000,
                        help="Cap on how many mints to label (most recent first)")
    parser.add_argument("--out", default="data/ml/entry_timing_model.ubj")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    # Lazy-import: keeps the module importable on machines without
    # xgboost (e.g. Mac dev shells).
    import psycopg2

    from pulse_bot.db import _resolve_dsn
    from pulse_bot.ml.entry_timing import (
        EntryTimingLabelBuilder,
        train_entry_timing,
    )
    from pulse_bot.models import Trade

    dsn = _resolve_dsn(os.environ.get("PULSE_PG_DSN", "pulse_bot"))
    conn = psycopg2.connect(dsn)
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT t.mint, t.created_at
                FROM tokens t
                JOIN paper_trades p ON p.mint = t.mint
                WHERE p.status = 'closed'
                  AND t.created_at IS NOT NULL
                ORDER BY t.created_at DESC
                LIMIT %s
                """,
                (args.max_mints,),
            )
            mint_rows = cur.fetchall()
        if not mint_rows:
            logger.error("No closed paper trades found — nothing to label.")
            return 1
        logger.info("Pulling trades for %d mints...", len(mint_rows))

        corpus: list[tuple[str, list[Trade], float]] = []
        with conn.cursor() as cur:
            for mint, created_at in mint_rows:
                cur.execute(
                    """SELECT mint, wallet, tx_type, sol_amount, token_amount,
                              market_cap_sol, v_sol_in_bonding_curve, timestamp
                       FROM trades WHERE mint = %s ORDER BY id ASC""",
                    (mint,),
                )
                trade_rows = cur.fetchall()
                if not trade_rows:
                    continue
                trades = [
                    Trade(
                        mint=r[0],
                        wallet=r[1],
                        tx_type=r[2],
                        sol_amount=float(r[3] or 0.0),
                        token_amount=float(r[4] or 0.0),
                        new_token_balance=0.0,
                        bonding_curve_key="",
                        v_sol_in_bonding_curve=float(r[6] or 0.0),
                        v_tokens_in_bonding_curve=0.0,
                        market_cap_sol=float(r[5] or 0.0),
                        timestamp=float(r[7] or 0.0),
                    )
                    for r in trade_rows
                ]
                corpus.append((mint, trades, float(created_at)))
        logger.info("Built corpus of %d mints with trades", len(corpus))
    finally:
        conn.close()

    builder = EntryTimingLabelBuilder()
    snaps = builder.build_for_corpus(corpus)
    logger.info("Generated %d (mint, snapshot) timing labels", len(snaps))
    if len(snaps) < 500:
        logger.error(
            "Insufficient snapshots (%d) — need >500 for stable training",
            len(snaps),
        )
        return 1

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    meta = train_entry_timing(snaps, out_path)
    logger.info("Saved entry_timing model: %s", meta)
    return 0


if __name__ == "__main__":
    sys.exit(main())
