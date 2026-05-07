# pulse_bot/ml/wallet_indexer.py
"""Phase E — backfill and maintain the ``wallet_activity`` materialized view.

Aggregates per (wallet, mint): sum of buy/sell SOL volume, first/last
trade timestamps, realized PnL. The live pipeline calls
``Database.upsert_wallet_activity_from_trades`` after each trade batch to
keep the view current; this module provides the one-shot historical
backfill for any trade rows that existed before Phase E shipped.

Usage:
    python -m pulse_bot.ml.wallet_indexer --db pulse_bot

Migrated 2026-04-24 from sqlite3 to Postgres (psycopg2). The SQLite
READER-LOCK-contention retry loop is no longer needed — PG handles
concurrent readers/writers natively.
"""

from __future__ import annotations

import argparse
import logging
import time
from dataclasses import dataclass

import psycopg2
import psycopg2.extras

logger = logging.getLogger(__name__)


@dataclass
class BackfillStats:
    """Summary of a backfill pass for logging + tests."""

    trades_scanned: int = 0
    wallet_mint_pairs: int = 0
    batches_committed: int = 0
    elapsed_sec: float = 0.0


class WalletIndexer:
    """Synchronous backfill over the ``trades`` table (Postgres).

    Streams trades in timestamp order, aggregates per (wallet, mint),
    and UPSERTs into ``wallet_activity`` in batches. Uses a server-side
    cursor so 3M+ trades stream without loading everything into RAM.
    """

    def __init__(self, db_path: str = "pulse_bot", batch_size: int = 5000) -> None:
        # ``db_path`` is legacy; resolve to a PG DSN.
        from pulse_bot.db import _resolve_dsn

        self.dsn = _resolve_dsn(db_path)
        self.batch_size = batch_size

    def backfill(self, truncate_first: bool = False) -> BackfillStats:
        """Stream all rows in ``trades`` and populate ``wallet_activity``.

        ``truncate_first``: wipe the table first (safe — it's derived).
        Default False so repeated runs are idempotent (UPSERT handles it).
        """
        t0 = time.time()
        stats = BackfillStats()
        # Two connections: streaming reader + batched writer.
        read_conn = psycopg2.connect(self.dsn)
        write_conn = psycopg2.connect(self.dsn)
        try:
            if truncate_first:
                with write_conn.cursor() as cur:
                    cur.execute("TRUNCATE TABLE wallet_activity")
                write_conn.commit()
                logger.info("wallet_activity truncated")

            # Server-side cursor: streams rows from PG without buffering
            # the full 3M+ result set in memory.
            read_cur = read_conn.cursor(
                name="wallet_indexer_stream",
                cursor_factory=psycopg2.extras.DictCursor,
            )
            read_cur.itersize = 10_000
            read_cur.execute(
                """SELECT mint, wallet, tx_type, sol_amount, timestamp
                   FROM trades
                   WHERE timestamp IS NOT NULL
                   ORDER BY timestamp ASC"""
            )

            agg: dict[tuple[str, str], dict] = {}
            now = time.time()
            last_commit = 0
            seen_pairs: set[tuple[str, str]] = set()

            for row in read_cur:
                stats.trades_scanned += 1
                key = (row["wallet"], row["mint"])
                seen_pairs.add(key)
                if key not in agg:
                    agg[key] = {
                        "buy_sol": 0.0,
                        "sell_sol": 0.0,
                        "first_ts": row["timestamp"],
                        "last_ts": row["timestamp"],
                    }
                if row["tx_type"] == "buy":
                    agg[key]["buy_sol"] += float(row["sol_amount"] or 0.0)
                elif row["tx_type"] == "sell":
                    agg[key]["sell_sol"] += float(row["sol_amount"] or 0.0)
                ts = float(row["timestamp"] or 0.0)
                if ts < agg[key]["first_ts"]:
                    agg[key]["first_ts"] = ts
                if ts > agg[key]["last_ts"]:
                    agg[key]["last_ts"] = ts

                if stats.trades_scanned - last_commit >= self.batch_size:
                    self._flush(write_conn, agg, now)
                    stats.batches_committed += 1
                    agg.clear()
                    last_commit = stats.trades_scanned
                    logger.info(
                        "Backfill progress: %d trades scanned, %d pairs so far",
                        stats.trades_scanned,
                        len(seen_pairs),
                    )

            if agg:
                self._flush(write_conn, agg, now)
                stats.batches_committed += 1

            read_cur.close()
            stats.wallet_mint_pairs = len(seen_pairs)
            stats.elapsed_sec = time.time() - t0
            logger.info(
                "Backfill complete: %d trades → %d (wallet,mint) pairs "
                "in %d batches (%.1fs)",
                stats.trades_scanned,
                stats.wallet_mint_pairs,
                stats.batches_committed,
                stats.elapsed_sec,
            )
            return stats
        finally:
            read_conn.close()
            write_conn.close()

    @staticmethod
    def _flush(
        conn: "psycopg2.extensions.connection",
        agg: dict[tuple[str, str], dict],
        now: float,
    ) -> None:
        """UPSERT one batch of aggregates."""
        if not agg:
            return
        rows = [
            (
                wallet,
                mint,
                a["first_ts"],
                a["last_ts"],
                a["buy_sol"],
                a["sell_sol"],
                a["sell_sol"] - a["buy_sol"],
                1 if a["sell_sol"] > 0 else 0,
                now,
            )
            for (wallet, mint), a in agg.items()
        ]
        with conn.cursor() as cur:
            psycopg2.extras.execute_values(
                cur,
                """INSERT INTO wallet_activity (
                    wallet, mint, first_buy_ts, last_trade_ts,
                    buy_volume_sol, sell_volume_sol, realized_pnl_sol,
                    is_closed_at_ingest_time, updated_at
                ) VALUES %s
                ON CONFLICT (wallet, mint) DO UPDATE SET
                    first_buy_ts = LEAST(wallet_activity.first_buy_ts, EXCLUDED.first_buy_ts),
                    last_trade_ts = GREATEST(wallet_activity.last_trade_ts, EXCLUDED.last_trade_ts),
                    buy_volume_sol = wallet_activity.buy_volume_sol + EXCLUDED.buy_volume_sol,
                    sell_volume_sol = wallet_activity.sell_volume_sol + EXCLUDED.sell_volume_sol,
                    realized_pnl_sol = (wallet_activity.sell_volume_sol + EXCLUDED.sell_volume_sol)
                                     - (wallet_activity.buy_volume_sol + EXCLUDED.buy_volume_sol),
                    updated_at = EXCLUDED.updated_at""",
                rows,
                page_size=1000,
            )
        conn.commit()


def main() -> None:
    parser = argparse.ArgumentParser(description="Backfill wallet_activity from trades")
    parser.add_argument(
        "--db",
        default="pulse_bot",
        help="DB path (legacy; PG DSN resolved automatically)",
    )
    parser.add_argument("--batch-size", type=int, default=5000)
    parser.add_argument("--truncate", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    indexer = WalletIndexer(args.db, batch_size=args.batch_size)
    stats = indexer.backfill(truncate_first=args.truncate)
    print(
        f"Scanned {stats.trades_scanned} trades, "
        f"populated {stats.wallet_mint_pairs} (wallet,mint) pairs "
        f"across {stats.batches_committed} batches in {stats.elapsed_sec:.1f}s"
    )


if __name__ == "__main__":
    main()
