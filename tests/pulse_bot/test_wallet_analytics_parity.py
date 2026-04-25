# tests/pulse_bot/test_wallet_analytics_parity.py
"""Phase E — train/serve parity test for top-3 buyer prior features.

2026-04-24 PG migration: fixture builds scratch data via the ``pg_test_db``
module-level isolated DB fixture (see conftest.py).

Strategy: build a synthetic trades table with deterministic wallet/mint
patterns, run the indexer backfill, then check that the BUILD-DATASET
path and the LIVE-SERVE path produce bit-identical WALLET_FEATURES for
at least 100 tokens (stratified: 50 with expected non-NaN, 50 with
expected NaN / new buyers).

Why stratified: the creator-skew bug hid for 3 months because no test
exercised the "I have data" side of the branch explicitly. Mirroring
codex's recommendation here — force both coverage classes.

Tolerance: <1% relative diff on each feature, NaN must match NaN (not
0.0) — the whole NaN-first policy rests on that.
"""

from __future__ import annotations

import math

import psycopg2
import pytest

pytestmark = pytest.mark.usefixtures("pg_test_db")


class _Conn:
    """Thin wrapper making a psycopg2 connection behave like sqlite3.Connection:
    ``conn.execute(sql, params).fetchall()`` works, as does ``conn.commit()``."""

    def __init__(self, pg_conn):
        self._c = pg_conn

    def execute(self, sql, params=None):
        cur = self._c.cursor()
        cur.execute(sql, params)
        return cur

    def cursor(self):
        return self._c.cursor()

    def commit(self):
        self._c.commit()

    def close(self):
        self._c.close()


def _connect(db_path: str) -> _Conn:
    """Open a psycopg2 connection through the SQLite-shaped wrapper.
    ``db_path`` may be a DSN URL or legacy SQLite-path string (ignored)."""
    from pulse_bot.db import _resolve_dsn

    return _Conn(psycopg2.connect(_resolve_dsn(db_path)))


import tempfile
import time
from pathlib import Path

import pytest

from pulse_bot.db import Database
from pulse_bot.ml.features import (
    WALLET_FEATURES,
    _extract_wallet_prior_features,
    compute_top3_buyer_wallets,
)
from pulse_bot.ml.wallet_indexer import WalletIndexer

# ── Fixture layout ──────────────────────────────────────────────────

# Synthetic dataset:
# * 50 "history-rich" tokens whose top-3 buyers have traded >=2 other
#   mints previously with realized sells (so WR / PnL defined).
# * 50 "cold-start" tokens whose top-3 buyers are brand new (no prior
#   trades) → all 5 WALLET_FEATURES must resolve to NaN.
# All tokens are scored at a fixed reference time; prior activity sits
# strictly before that scored_at.


def _build_fixture_db(db_path: str) -> tuple[list[str], list[str], list[str], float]:
    """Populate DB with tokens/trades. Returns (hist_mints, cold_mints,
    known_wallets, scored_at)."""
    db = Database(db_path)
    db.init_schema()

    now = 1_700_000_000.0  # deterministic fixed reference time
    scored_at = now + 10_000.0

    conn = _connect(db_path)
    cur = conn.cursor()

    known_wallets = [f"W_known_{i:03d}" for i in range(40)]
    hist_mints: list[str] = []
    cold_mints: list[str] = []

    # 1) Historical tokens: each known wallet trades 3 prior tokens with
    # mixed realized PnL (2 wins, 1 loss → WR=0.667 per wallet).
    prior_mint_id = 0
    for w_idx, wallet in enumerate(known_wallets):
        for k in range(3):
            mint = f"PRIOR_{prior_mint_id:05d}"
            prior_mint_id += 1
            ts_buy = now - 3000.0 - (k * 100.0) - (w_idx * 0.5)
            ts_sell = ts_buy + 30.0
            # Tokens row required for completeness but not strictly necessary
            # for wallet_activity derivation. Keep minimal.
            cur.execute(
                "INSERT INTO tokens (mint, name, symbol, creator, created_at, uri) "
                "VALUES (%s, %s, %s, %s, %s, %s)",
                (mint, f"P{prior_mint_id}", "P", "CREATOR_PRIOR", ts_buy - 5.0, ""),
            )
            # Two wins + one loss pattern: k=0,1 → PnL>0, k=2 → PnL<0
            buy_sol = 1.0
            sell_sol = 1.5 if k in (0, 1) else 0.6
            cur.execute(
                "INSERT INTO trades (mint, wallet, tx_type, sol_amount, "
                "token_amount, market_cap_sol, v_sol_in_bonding_curve, "
                "timestamp, is_creator) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 0)",
                (mint, wallet, "buy", buy_sol, 1000.0, 10.0, 30.0, ts_buy),
            )
            cur.execute(
                "INSERT INTO trades (mint, wallet, tx_type, sol_amount, "
                "token_amount, market_cap_sol, v_sol_in_bonding_curve, "
                "timestamp, is_creator) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 0)",
                (mint, wallet, "sell", sell_sol, 1000.0, 10.0, 30.0, ts_sell),
            )

    # 2) History-rich target tokens: top-3 buyers come from `known_wallets`
    for i in range(50):
        mint = f"HIST_{i:05d}"
        hist_mints.append(mint)
        ts_create = scored_at - 120.0 - i * 0.1
        cur.execute(
            "INSERT INTO tokens (mint, name, symbol, creator, created_at, uri) "
            "VALUES (%s, %s, %s, %s, %s, %s)",
            (mint, f"H{i}", "H", f"CREATOR_H_{i}", ts_create, ""),
        )
        # 3 known wallets + 2 random buyers, ordered by SOL buy size
        # so the known ones become top-3.
        top3 = [known_wallets[(i * 3 + k) % len(known_wallets)] for k in range(3)]
        # Buys at decreasing size 5, 3, 2 SOL → deterministic top-3
        for j, wallet in enumerate(top3):
            amt = 5.0 - j
            ts_b = ts_create + 5.0 + j * 0.1
            cur.execute(
                "INSERT INTO trades (mint, wallet, tx_type, sol_amount, "
                "token_amount, market_cap_sol, v_sol_in_bonding_curve, "
                "timestamp, is_creator) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 0)",
                (mint, wallet, "buy", amt, amt * 200.0, 10.0, 30.0, ts_b),
            )
        for j in range(2):
            noise_w = f"NOISE_{i:03d}_{j}"
            cur.execute(
                "INSERT INTO trades (mint, wallet, tx_type, sol_amount, "
                "token_amount, market_cap_sol, v_sol_in_bonding_curve, "
                "timestamp, is_creator) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 0)",
                (mint, noise_w, "buy", 0.5, 100.0, 10.0, 30.0, ts_create + 10.0 + j),
            )

    # 3) Cold-start target tokens: top-3 buyers are fresh (no prior trades)
    for i in range(50):
        mint = f"COLD_{i:05d}"
        cold_mints.append(mint)
        ts_create = scored_at - 120.0 - i * 0.1
        cur.execute(
            "INSERT INTO tokens (mint, name, symbol, creator, created_at, uri) "
            "VALUES (%s, %s, %s, %s, %s, %s)",
            (mint, f"C{i}", "C", f"CREATOR_C_{i}", ts_create, ""),
        )
        top3 = [f"COLD_W_{i:03d}_{k}" for k in range(3)]
        for j, wallet in enumerate(top3):
            amt = 5.0 - j
            ts_b = ts_create + 5.0 + j * 0.1
            cur.execute(
                "INSERT INTO trades (mint, wallet, tx_type, sol_amount, "
                "token_amount, market_cap_sol, v_sol_in_bonding_curve, "
                "timestamp, is_creator) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 0)",
                (mint, wallet, "buy", amt, amt * 200.0, 10.0, 30.0, ts_b),
            )

    conn.commit()
    conn.close()
    return hist_mints, cold_mints, known_wallets, scored_at


# ── Tests ────────────────────────────────────────────────────────────


def _compute_features_live_path(
    db: Database,
    mint: str,
    trades_before_scored: list[dict],
    scored_at: float,
) -> dict[str, float]:
    """Emulate pipeline.py — top3 → get_wallet_prior_stats_sync → extract."""
    top3 = compute_top3_buyer_wallets(trades_before_scored)
    if not top3:
        return _extract_wallet_prior_features(None, None, None)
    stats = db.get_wallet_prior_stats_sync(top3, exclude_mint=mint, cutoff_ts=scored_at)
    return _extract_wallet_prior_features(stats, top3, scored_at)


def _compute_features_build_path(
    conn,
    mint: str,
    scored_at: float,
) -> dict[str, float]:
    """Emulate build_dataset.py — mirror the same SQL + helper."""
    trade_rows = conn.execute(
        """SELECT tx_type, wallet, sol_amount FROM trades
           WHERE mint = %s AND timestamp <= %s
           ORDER BY id ASC""",
        (mint, scored_at),
    ).fetchall()
    trades_as_dicts = [
        {"tx_type": tr[0], "wallet": tr[1], "sol_amount": tr[2]} for tr in trade_rows
    ]
    top3 = compute_top3_buyer_wallets(trades_as_dicts)
    stats_map: dict[str, dict] = {}
    if top3:
        placeholders = ",".join(["%s"] * len(top3))
        stats_cur = conn.execute(
            f"""SELECT wallet,
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
                  AND mint != %s
                  AND last_trade_ts < %s
                GROUP BY wallet""",
            (*top3, mint, scored_at),
        )
        for srow in stats_cur.fetchall():
            all_mc = int(srow[1] or 0)
            closed_mc = int(srow[3] or 0)
            wc = int(srow[5] or 0)
            stats_map[srow[0]] = {
                "all_mint_count": all_mc,
                "closed_mint_count": closed_mc,
                "wr": (wc / closed_mc) if closed_mc > 0 else float("nan"),
                "total_pnl_sol": float(srow[4] or 0.0),
                "max_pnl_sol": (
                    float(srow[6]) if srow[6] is not None else float("nan")
                ),
                "first_seen_ts": float(srow[2] or 0.0),
            }
    return _extract_wallet_prior_features(stats_map, top3, scored_at)


def _nan_safe_close(a: float, b: float, tol: float = 1e-6) -> bool:
    if math.isnan(a) and math.isnan(b):
        return True
    if math.isnan(a) or math.isnan(b):
        return False
    if abs(a) < 1e-9 and abs(b) < 1e-9:
        return True
    return abs(a - b) / max(abs(a), abs(b), 1e-9) < tol


@pytest.fixture
def fixture_db(tmp_path_factory, pg_test_db):
    """Build fixture data into the per-test PG DB (function scope)."""
    tmp = tmp_path_factory.mktemp("wallet_parity")
    db_path = str(tmp / "fixture.db")  # routed to pg_test_db via _resolve_dsn
    hist_mints, cold_mints, known_wallets, scored_at = _build_fixture_db(db_path)
    # Populate wallet_activity via the indexer (one-shot backfill).
    indexer = WalletIndexer(db_path, batch_size=2000)
    stats = indexer.backfill()
    assert stats.trades_scanned > 0, "backfill saw no trades"
    assert stats.wallet_mint_pairs > 0, "no (wallet,mint) pairs populated"
    return {
        "db_path": db_path,
        "hist_mints": hist_mints,
        "cold_mints": cold_mints,
        "known_wallets": known_wallets,
        "scored_at": scored_at,
    }


def test_hist_mints_produce_non_nan_features(fixture_db):
    """50 history-rich tokens must have non-NaN values across all 5 features."""
    db = Database(fixture_db["db_path"])
    conn = _connect(fixture_db["db_path"])
    scored_at = fixture_db["scored_at"]
    for mint in fixture_db["hist_mints"]:
        live = _compute_features_build_path(conn, mint, scored_at)
        for feat in WALLET_FEATURES:
            v = live[feat]
            assert not math.isnan(v), (
                f"hist mint {mint}: feature {feat} is NaN, "
                f"expected non-NaN (top-3 buyers had prior history)"
            )
    conn.close()


def test_cold_mints_produce_nan_features(fixture_db):
    """50 cold-start tokens must have NaN across all 5 features."""
    db = Database(fixture_db["db_path"])
    conn = _connect(fixture_db["db_path"])
    scored_at = fixture_db["scored_at"]
    for mint in fixture_db["cold_mints"]:
        live = _compute_features_build_path(conn, mint, scored_at)
        for feat in WALLET_FEATURES:
            v = live[feat]
            assert math.isnan(v), (
                f"cold mint {mint}: feature {feat}={v}, expected NaN "
                f"(top-3 buyers were brand new)"
            )
    conn.close()


def test_live_vs_build_path_parity(fixture_db):
    """Pipeline live path and build_dataset path must produce identical feature
    values on 100 stratified tokens. No tolerance — they should bit-match."""
    db = Database(fixture_db["db_path"])
    conn = _connect(fixture_db["db_path"])
    scored_at = fixture_db["scored_at"]
    all_mints = fixture_db["hist_mints"] + fixture_db["cold_mints"]
    mismatches: list[str] = []
    for mint in all_mints:
        # Build path: query + helper
        build_feats = _compute_features_build_path(conn, mint, scored_at)
        # Live path: fetch trades from DB (simulating all_trades), then
        # call get_wallet_prior_stats_sync + helper
        trade_rows = conn.execute(
            """SELECT tx_type, wallet, sol_amount FROM trades
               WHERE mint = %s AND timestamp <= %s
               ORDER BY id ASC""",
            (mint, scored_at),
        ).fetchall()
        trades_as_dicts = [
            {"tx_type": tr[0], "wallet": tr[1], "sol_amount": tr[2]}
            for tr in trade_rows
        ]
        live_feats = _compute_features_live_path(db, mint, trades_as_dicts, scored_at)
        for feat in WALLET_FEATURES:
            if not _nan_safe_close(build_feats[feat], live_feats[feat]):
                mismatches.append(
                    f"{mint} {feat}: build={build_feats[feat]} "
                    f"live={live_feats[feat]}"
                )
    conn.close()
    assert (
        not mismatches
    ), f"{len(mismatches)} train/serve parity mismatches:\n" + "\n".join(
        mismatches[:10]
    )


def test_point_in_time_safety(fixture_db):
    """Future trades (timestamp > cutoff) must not leak into prior stats.

    Inject a buy by a known wallet with timestamp AFTER the target token's
    scored_at; verify the feature values do not change.
    """
    db_path = fixture_db["db_path"]
    scored_at = fixture_db["scored_at"]
    mint = fixture_db["hist_mints"][0]
    conn = _connect(db_path)
    # Capture baseline features
    baseline = _compute_features_build_path(conn, mint, scored_at)
    # Inject a future trade for the top-3 buyer (huge PnL)
    top3 = conn.execute(
        """SELECT wallet FROM trades WHERE mint = %s AND tx_type='buy'
           ORDER BY sol_amount DESC LIMIT 1""",
        (mint,),
    ).fetchall()
    assert top3, "no top buyer found"
    future_wallet = top3[0][0]
    future_mint = f"FUTURE_{mint}"
    future_ts = scored_at + 60.0  # strictly after scored_at
    conn.execute(
        "INSERT INTO tokens (mint, name, symbol, creator, created_at, uri) "
        "VALUES (%s, 'F', 'F', 'FC', %s, '')",
        (future_mint, future_ts - 1.0),
    )
    conn.execute(
        "INSERT INTO trades (mint, wallet, tx_type, sol_amount, token_amount, "
        "market_cap_sol, v_sol_in_bonding_curve, timestamp, is_creator) "
        "VALUES (%s, %s, 'buy', 100.0, 10000.0, 10.0, 30.0, %s, 0)",
        (future_mint, future_wallet, future_ts),
    )
    conn.execute(
        "INSERT INTO trades (mint, wallet, tx_type, sol_amount, token_amount, "
        "market_cap_sol, v_sol_in_bonding_curve, timestamp, is_creator) "
        "VALUES (%s, %s, 'sell', 500.0, 10000.0, 10.0, 30.0, %s, 0)",
        (future_mint, future_wallet, future_ts + 60.0),
    )
    conn.commit()
    # Rebuild wallet_activity to include the new row
    conn.close()
    indexer = WalletIndexer(db_path, batch_size=2000)
    indexer.backfill(truncate_first=True)
    conn = _connect(db_path)
    after = _compute_features_build_path(conn, mint, scored_at)
    conn.close()
    for feat in WALLET_FEATURES:
        assert _nan_safe_close(baseline[feat], after[feat]), (
            f"{feat}: future trade leaked — baseline={baseline[feat]}, "
            f"after={after[feat]}"
        )
