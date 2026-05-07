# pulse_bot/ml/wallet_classifier.py
"""Wallet behavior classifier — sniper / smart-money / bot / wash-cluster.

Computes per-wallet flags from `trades` + `wallet_activity` + `tokens`,
writes them to `wallet_classifications`. The codex review of 2026-04-27
threw out a single 4-output regression model in favor of rule-based
detectors that feed into the existing entry XGBoost.

Detectors (all evaluated independently):

1. **is_sniper** — 2-of-4 of:
   * fastest_buy_age_sec < 5      (physically impossible for human)
   * buy_amount_cv < 0.15 AND n_buys ≥ 5  (fixed-size bot config)
   * n_buys_30d ≥ 300             (~10/day, way above human pace)
   * median_hold_sec < 60         (scalper bot pattern)

2. **is_smart_money** — wallet WR > 40% on graduated mints,
   with at least 3 graduated trades. "Graduated" = peak market cap
   ≥ 35 SOL (proxy for "made it past the rug threshold"; pump.fun
   formal graduation is ~84 SOL but the sweet spot is broader).

3. **is_bot** — strict sniper subset:
   is_sniper AND fastest_buy_age_sec < 2 AND n_buys_30d ≥ 500.
   Used to mark "definitely automated" without false positives.

4. **cluster_id** — wash-cluster detector. Wallets that co-bought
   ≥ 3 of the same mints within 30s of each other. Uses Union-Find
   on the co-occurrence graph.

Usage::

    python -m pulse_bot.ml.wallet_classifier              # full rebuild
    python -m pulse_bot.ml.wallet_classifier --no-clusters  # skip wash
    python -m pulse_bot.ml.wallet_classifier --stats        # show counts only

Designed to be safe to re-run (TRUNCATE+INSERT each pass). Total scan
on ~3.5M trades / 1.5M wallet_activity rows takes ~5-10 min on rich.
"""

from __future__ import annotations

import argparse
import logging
import time
from typing import Optional

import psycopg2
import psycopg2.extras

logger = logging.getLogger(__name__)


DDL = """
CREATE TABLE IF NOT EXISTS wallet_classifications (
    wallet TEXT PRIMARY KEY,
    fastest_buy_age_sec DOUBLE PRECISION,
    buy_amount_cv DOUBLE PRECISION,
    n_buys_total INTEGER,
    n_buys_30d INTEGER,
    median_hold_sec DOUBLE PRECISION,
    n_graduated_traded INTEGER,
    graduated_winrate DOUBLE PRECISION,
    is_sniper SMALLINT NOT NULL DEFAULT 0,
    is_smart_money SMALLINT NOT NULL DEFAULT 0,
    is_bot SMALLINT NOT NULL DEFAULT 0,
    cluster_id INTEGER,
    cluster_size INTEGER,
    updated_at DOUBLE PRECISION NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_wc_sniper ON wallet_classifications(is_sniper) WHERE is_sniper = 1;
CREATE INDEX IF NOT EXISTS idx_wc_smart ON wallet_classifications(is_smart_money) WHERE is_smart_money = 1;
CREATE INDEX IF NOT EXISTS idx_wc_bot ON wallet_classifications(is_bot) WHERE is_bot = 1;
CREATE INDEX IF NOT EXISTS idx_wc_cluster ON wallet_classifications(cluster_id) WHERE cluster_id IS NOT NULL;
"""


# Codex-reviewed thresholds (see module docstring).
SNIPER_FASTEST_BUY_AGE = 5.0
SNIPER_CV_MAX = 0.15
SNIPER_CV_MIN_BUYS = 5
SNIPER_N_30D = 300
SNIPER_MEDIAN_HOLD = 60.0

BOT_FASTEST_BUY_AGE = 2.0
BOT_N_30D = 500

SMART_MONEY_WR_MIN = 0.40
SMART_MONEY_GRAD_MIN = 3
GRADUATED_MC_THRESHOLD = 35.0  # SOL — proxy for "promising"

CLUSTER_SHARED_MINTS_MIN = 3
CLUSTER_TIME_WINDOW_SEC = 30.0


def _open_conn(dsn: str) -> "psycopg2.extensions.connection":
    return psycopg2.connect(dsn)


def ensure_table(conn: "psycopg2.extensions.connection") -> None:
    """Create the table + indexes if they don't already exist."""
    with conn.cursor() as cur:
        cur.execute(DDL)
    conn.commit()
    logger.info("wallet_classifications table ready")


def compute_per_wallet_stats(conn: "psycopg2.extensions.connection") -> dict[str, dict]:
    """Single-pass aggregation of per-wallet behavioral stats from trades.

    Returns {wallet: {fastest_buy_age, buy_amount_cv, n_buys_total,
                     n_buys_30d, median_hold_sec}}.
    """
    logger.info("Computing per-wallet stats from trades...")
    t0 = time.time()
    out: dict[str, dict] = {}
    with conn.cursor(name="wallet_stats_stream") as cur:
        cur.itersize = 50_000
        # First aggregation: trade-level stats. Median hold computed
        # separately from wallet_activity (cheaper than per-row pairing).
        cur.execute(
            """
            SELECT t.wallet,
                   MIN(t.timestamp - tk.created_at) FILTER (
                       WHERE t.tx_type = 'buy' AND tk.created_at > 0
                   ) AS fastest_buy_age,
                   STDDEV_POP(t.sol_amount) FILTER (
                       WHERE t.tx_type = 'buy' AND t.sol_amount > 0
                   ) AS buy_std,
                   AVG(t.sol_amount) FILTER (
                       WHERE t.tx_type = 'buy' AND t.sol_amount > 0
                   ) AS buy_mean,
                   COUNT(*) FILTER (WHERE t.tx_type = 'buy') AS n_buys_total,
                   COUNT(*) FILTER (
                       WHERE t.tx_type = 'buy'
                         AND t.timestamp > EXTRACT(EPOCH FROM NOW() - INTERVAL '30 days')
                   ) AS n_buys_30d
            FROM trades t
            LEFT JOIN tokens tk ON tk.mint = t.mint
            WHERE t.wallet IS NOT NULL
            GROUP BY t.wallet
            """
        )
        for row in cur:
            wallet = row[0]
            buy_mean = float(row[3] or 0.0)
            buy_std = float(row[2] or 0.0)
            cv = (buy_std / buy_mean) if buy_mean > 0 else None
            out[wallet] = {
                "fastest_buy_age": float(row[1]) if row[1] is not None else None,
                "buy_amount_cv": cv,
                "n_buys_total": int(row[4] or 0),
                "n_buys_30d": int(row[5] or 0),
                "median_hold_sec": None,  # filled in next pass
            }
    logger.info(
        "Per-wallet trade stats: %d wallets in %.1fs",
        len(out),
        time.time() - t0,
    )

    # Median hold time per wallet from wallet_activity (closed positions
    # only — open positions don't have a hold duration yet).
    logger.info("Computing median hold from wallet_activity...")
    t1 = time.time()
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT wallet,
                   PERCENTILE_CONT(0.5) WITHIN GROUP (
                       ORDER BY (last_trade_ts - first_buy_ts)
                   ) AS median_hold
            FROM wallet_activity
            WHERE sell_volume_sol > 0
              AND last_trade_ts > first_buy_ts
            GROUP BY wallet
            """
        )
        n_with_hold = 0
        for row in cur:
            wallet = row[0]
            if wallet in out:
                out[wallet]["median_hold_sec"] = float(row[1]) if row[1] is not None else None
                n_with_hold += 1
    logger.info(
        "Median hold: %d wallets w/ closed positions in %.1fs",
        n_with_hold,
        time.time() - t1,
    )
    return out


def compute_smart_money(conn: "psycopg2.extensions.connection") -> dict[str, dict]:
    """Wallet WR on graduated mints (peak MC ≥ 35 SOL).

    Returns {wallet: {n_graduated_traded, graduated_winrate}}.
    """
    logger.info("Computing smart-money stats (graduated WR)...")
    t0 = time.time()
    out: dict[str, dict] = {}
    with conn.cursor() as cur:
        # Compute peak MC per mint from trades, mark graduated ones,
        # then aggregate per-wallet WR via wallet_activity.
        cur.execute(
            f"""
            WITH peak AS (
                SELECT mint, MAX(market_cap_sol) AS peak_mc
                FROM trades
                WHERE market_cap_sol IS NOT NULL
                GROUP BY mint
                HAVING MAX(market_cap_sol) >= {GRADUATED_MC_THRESHOLD}
            )
            SELECT wa.wallet,
                   COUNT(*) AS n_graduated_traded,
                   SUM(CASE WHEN wa.realized_pnl_sol > 0 THEN 1 ELSE 0 END)::float
                       / GREATEST(COUNT(*), 1) AS graduated_wr
            FROM wallet_activity wa
            JOIN peak ON peak.mint = wa.mint
            WHERE wa.sell_volume_sol > 0
            GROUP BY wa.wallet
            HAVING COUNT(*) >= {SMART_MONEY_GRAD_MIN}
            """
        )
        for row in cur:
            out[row[0]] = {
                "n_graduated_traded": int(row[1]),
                "graduated_winrate": float(row[2] or 0.0),
            }
    logger.info(
        "Smart-money candidates (≥%d graduated trades): %d wallets in %.1fs",
        SMART_MONEY_GRAD_MIN,
        len(out),
        time.time() - t0,
    )
    return out


def compute_clusters(conn: "psycopg2.extensions.connection") -> dict[str, tuple[int, int]]:
    """Wash-cluster detector via co-occurrence graph + Union-Find.

    Two wallets are linked if they bought the same 3+ mints within 30s
    of each other. Connected components in this graph form clusters.

    Returns {wallet: (cluster_id, cluster_size)}.

    Performance: dominated by self-join on `trades.first_buy_ts` per
    mint. With 3.5M trades and ~600k unique (wallet, mint) buy
    pairs this is the slowest detector — ~3-5 min on rich.
    """
    logger.info("Computing wash-cluster co-occurrence graph...")
    t0 = time.time()

    # Step 1: per (mint, wallet) min buy timestamp.
    # Step 2: pair up wallets with |Δt| ≤ 30s on same mint.
    # Step 3: count shared mints per pair, keep ≥3.
    pairs: list[tuple[str, str]] = []
    with conn.cursor() as cur:
        cur.execute(
            f"""
            WITH wallet_buys AS (
                SELECT mint, wallet, MIN(timestamp) AS buy_ts
                FROM trades
                WHERE tx_type = 'buy' AND wallet IS NOT NULL
                GROUP BY mint, wallet
            ),
            co AS (
                SELECT a.wallet AS w1, b.wallet AS w2, a.mint
                FROM wallet_buys a
                JOIN wallet_buys b
                  ON a.mint = b.mint
                 AND a.wallet < b.wallet
                 AND ABS(a.buy_ts - b.buy_ts) <= {CLUSTER_TIME_WINDOW_SEC}
            )
            SELECT w1, w2
            FROM co
            GROUP BY w1, w2
            HAVING COUNT(DISTINCT mint) >= {CLUSTER_SHARED_MINTS_MIN}
            """
        )
        pairs = [(r[0], r[1]) for r in cur.fetchall()]
    logger.info(
        "Found %d wallet pairs co-buying ≥%d mints within %ds in %.1fs",
        len(pairs),
        CLUSTER_SHARED_MINTS_MIN,
        int(CLUSTER_TIME_WINDOW_SEC),
        time.time() - t0,
    )

    # Union-Find to group transitively.
    parent: dict[str, str] = {}

    def find(w: str) -> str:
        while parent.setdefault(w, w) != w:
            parent[w] = parent[parent[w]]  # path compression
            w = parent[w]
        return w

    def union(a: str, b: str) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    for w1, w2 in pairs:
        union(w1, w2)

    # Assign integer cluster IDs and compute sizes.
    root_to_id: dict[str, int] = {}
    cluster_size: dict[int, int] = {}
    out: dict[str, tuple[int, int]] = {}
    next_id = 1
    for w in parent:
        root = find(w)
        if root not in root_to_id:
            root_to_id[root] = next_id
            next_id += 1
        cid = root_to_id[root]
        cluster_size[cid] = cluster_size.get(cid, 0) + 1
        out[w] = (cid, 0)  # size patched below
    # Patch sizes.
    for w, (cid, _) in list(out.items()):
        out[w] = (cid, cluster_size[cid])
    logger.info(
        "Wash-cluster grouping: %d wallets in %d clusters (max size %d)",
        len(out),
        len(cluster_size),
        max(cluster_size.values()) if cluster_size else 0,
    )
    return out


def classify_and_write(
    conn: "psycopg2.extensions.connection",
    do_clusters: bool = True,
) -> dict[str, int]:
    """Compute all detectors, write results, return population counts."""
    ensure_table(conn)
    stats = compute_per_wallet_stats(conn)
    smart = compute_smart_money(conn)
    clusters = compute_clusters(conn) if do_clusters else {}

    logger.info("Applying detector rules...")
    now = time.time()
    rows: list[tuple] = []
    counts = {
        "total": 0,
        "is_sniper": 0,
        "is_smart_money": 0,
        "is_bot": 0,
        "in_cluster": 0,
    }
    for wallet, s in stats.items():
        # Sniper: 2-of-4
        crit = 0
        if s["fastest_buy_age"] is not None and s["fastest_buy_age"] < SNIPER_FASTEST_BUY_AGE:
            crit += 1
        cv = s.get("buy_amount_cv")
        if cv is not None and cv < SNIPER_CV_MAX and s["n_buys_total"] >= SNIPER_CV_MIN_BUYS:
            crit += 1
        if s["n_buys_30d"] >= SNIPER_N_30D:
            crit += 1
        mh = s.get("median_hold_sec")
        if mh is not None and mh < SNIPER_MEDIAN_HOLD:
            crit += 1
        is_sniper = 1 if crit >= 2 else 0

        # Bot: strict sniper subset
        is_bot = 0
        if (
            is_sniper
            and s["fastest_buy_age"] is not None
            and s["fastest_buy_age"] < BOT_FASTEST_BUY_AGE
            and s["n_buys_30d"] >= BOT_N_30D
        ):
            is_bot = 1

        # Smart money
        sm = smart.get(wallet)
        is_smart = 0
        n_grad = 0
        grad_wr = None
        if sm is not None:
            n_grad = sm["n_graduated_traded"]
            grad_wr = sm["graduated_winrate"]
            if grad_wr >= SMART_MONEY_WR_MIN and n_grad >= SMART_MONEY_GRAD_MIN:
                is_smart = 1

        # Cluster
        cid_size = clusters.get(wallet)
        cluster_id = cid_size[0] if cid_size else None
        cluster_size = cid_size[1] if cid_size else None

        counts["total"] += 1
        counts["is_sniper"] += is_sniper
        counts["is_smart_money"] += is_smart
        counts["is_bot"] += is_bot
        if cluster_id is not None:
            counts["in_cluster"] += 1

        rows.append(
            (
                wallet,
                s["fastest_buy_age"],
                cv,
                s["n_buys_total"],
                s["n_buys_30d"],
                mh,
                n_grad,
                grad_wr,
                is_sniper,
                is_smart,
                is_bot,
                cluster_id,
                cluster_size,
                now,
            )
        )

    logger.info("Writing %d wallet_classifications rows...", len(rows))
    t0 = time.time()
    with conn.cursor() as cur:
        cur.execute("TRUNCATE TABLE wallet_classifications")
        psycopg2.extras.execute_values(
            cur,
            """INSERT INTO wallet_classifications (
                wallet, fastest_buy_age_sec, buy_amount_cv, n_buys_total,
                n_buys_30d, median_hold_sec, n_graduated_traded,
                graduated_winrate, is_sniper, is_smart_money, is_bot,
                cluster_id, cluster_size, updated_at
            ) VALUES %s""",
            rows,
            page_size=2000,
        )
    conn.commit()
    logger.info("Wrote in %.1fs", time.time() - t0)

    logger.info("=" * 60)
    logger.info("WALLET CLASSIFICATION COUNTS")
    for k, v in counts.items():
        pct = (v / counts["total"] * 100) if counts["total"] else 0
        logger.info("  %-15s %d (%.2f%%)", k, v, pct)
    logger.info("=" * 60)
    return counts


def main() -> None:
    parser = argparse.ArgumentParser(description="Wallet behavior classifier")
    parser.add_argument("--db", default="pulse_bot")
    parser.add_argument(
        "--no-clusters",
        action="store_true",
        help="Skip the slow co-occurrence cluster pass.",
    )
    parser.add_argument(
        "--stats",
        action="store_true",
        help="Show current population counts only; don't recompute.",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    from pulse_bot.db import _resolve_dsn

    dsn = _resolve_dsn(args.db)
    conn = _open_conn(dsn)
    try:
        if args.stats:
            ensure_table(conn)
            with conn.cursor() as cur:
                cur.execute(
                    """SELECT
                         COUNT(*),
                         SUM(is_sniper), SUM(is_smart_money), SUM(is_bot),
                         COUNT(cluster_id)
                       FROM wallet_classifications"""
                )
                r = cur.fetchone()
            print(f"total={r[0]} sniper={r[1]} smart={r[2]} bot={r[3]} clustered={r[4]}")
        else:
            classify_and_write(conn, do_clusters=not args.no_clusters)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
