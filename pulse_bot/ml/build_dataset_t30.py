# pulse_bot/ml/build_dataset_t30.py
"""Build the @T+30 training dataset for the Phase 3 dual-snapshot entry model.

This is a STRICT subset-sibling of ``build_entry_dataset`` in
``build_dataset.py``: it produces one row per scored token, with features
restricted to what is physically observable by 30 seconds of token life.
Labels are reused unchanged — outcome (TP/SL/timeout via simulate_exit)
is fixed and does not depend on the moment we score; only the FEATURE
snapshot moves earlier.

Two key constraints that protect us from train/serve skew vs the live
``EntryT30Policy``:

1. **Trade window clipped at scored_at + 30s.** Top-3 buyer wallets are
   computed only from buys that occurred up to that cutoff. Live pipeline
   must do the same when the T+30 model is wired in.
2. **Helius @T+30 columns only.** ``top1_30``, ``top5_30``, ``top10_30``,
   ``hc_30`` come from the existing 30s capture row. T+120 columns are
   never read here.

Schema lives in ``pulse_bot.ml.features.ENTRY_T30_FEATURE_ORDER``; bump
``FEATURE_SCHEMA_VERSION_T30`` when feature names/order change.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

import pandas as pd

from pulse_bot.ml.build_dataset import _connect_pg, _pg_exec, build_entry_dataset
from pulse_bot.ml.features import (
    SCORER_FEATURES_T30,
    WALLET_FEATURES,
    _extract_wallet_prior_features,
    compute_n_buyers_first_5s,
    compute_topN_buyer_wallets,
)

logger = logging.getLogger(__name__)


# Helper kept module-level so unit tests can patch it. The clipping
# window matches the live decision moment (scored_at == T+30 in the
# Phase 3 deployment).
T30_WINDOW_SEC: float = 30.0


def build_entry_dataset_t30(
    db_path: str,
    label_horizon_sec: float = 300.0,
    require_helius: bool = False,
    now_buffer_sec: float | None = None,
    base_parquet_path: Path | None = None,
) -> pd.DataFrame:
    """Build the @T+30 entry dataset.

    Implementation strategy: reuse ``build_entry_dataset`` to obtain the
    canonical labels + scorer columns + creator snapshot join, then:

    * RECOMPUTE wallet features against a trade window CLIPPED at
      ``scored_at + T30_WINDOW_SEC``. The base builder uses the full
      pre-scoring window — at T+30 we cannot see those buys yet.
    * RECOMPUTE the T+30-only derived features (``hc_velocity_to_30``)
      and align ``buy_count / 30`` denominators in the rate ratios.
    * DROP T+120-derived columns from the returned DataFrame so only
      ``ENTRY_T30_FEATURE_ORDER`` features survive (plus label /
      bookkeeping).

    Returns a DataFrame whose feature columns match
    ``ENTRY_T30_FEATURE_ORDER`` exactly, plus ``mint``, ``scored_at``,
    ``label``, ``realized_pnl_pct``, ``exit_reason``.

    2026-04-28 perf fix: if ``base_parquet_path`` is provided AND the
    file is fresh (mtime within 24h), reuse the pre-built dataframe
    instead of calling ``build_entry_dataset`` from scratch. This was
    the second-biggest cost in the retrain pipeline — the inner build
    duplicated ~25min of work that ``train --dataset entry`` had just
    done. CLI auto-passes ``data/ml/entry.parquet``.
    """
    base: pd.DataFrame | None = None
    import time as _time

    if base_parquet_path is not None and base_parquet_path.exists():
        age_sec = _time.time() - base_parquet_path.stat().st_mtime
        if age_sec < 24 * 3600:
            try:
                base = pd.read_parquet(base_parquet_path)
                logger.info(
                    "Reusing freshly-built %s (%.0f min old, %d rows) — "
                    "skipping build_entry_dataset (saves ~25 min)",
                    base_parquet_path,
                    age_sec / 60,
                    len(base),
                )
            except Exception as exc:
                logger.warning(
                    "Failed to load %s, falling back to full build: %s",
                    base_parquet_path,
                    exc,
                )
                base = None
        else:
            logger.info(
                "%s is %.0f min old (>24h) — forcing full rebuild",
                base_parquet_path,
                age_sec / 60,
            )
    if base is None:
        base = build_entry_dataset(
            db_path,
            label_horizon_sec=label_horizon_sec,
            require_helius=require_helius,
            now_buffer_sec=now_buffer_sec,
        )
    if base.empty:
        logger.warning("base entry dataset empty — returning empty T30 frame")
        return base

    # ── Recompute wallet features with the T+30 trade-window cutoff ────
    conn = _connect_pg(db_path)
    try:
        wa_row_count = _pg_exec(
            conn, "SELECT COUNT(*) FROM wallet_activity"
        ).fetchone()[0]
        wallet_feat_rows: list[dict[str, float]] = []
        for _, row in base.iterrows():
            mint = str(row["mint"])
            scored_at = float(row["scored_at"])
            t30_cutoff = scored_at - (90.0 - T30_WINDOW_SEC)
            # ``scored_at`` in build_entry_dataset is the T+90 anchor
            # (live scoring fires at observe_seconds=90s after creation).
            # The T+30 snapshot equivalent would be 60 seconds earlier.
            # We pull buys with timestamp <= t30_cutoff so the top-3
            # ranking matches what the live T+30 inference would see.
            trade_rows = _pg_exec(
                conn,
                """SELECT tx_type, wallet, sol_amount, timestamp FROM trades
                   WHERE mint = ? AND timestamp <= ?
                   ORDER BY id ASC""",
                (mint, t30_cutoff),
            ).fetchall()
            trades_as_dicts = [
                {
                    "tx_type": tr[0],
                    "wallet": tr[1],
                    "sol_amount": tr[2],
                    "timestamp": tr[3],
                }
                for tr in trade_rows
            ]
            top3 = compute_topN_buyer_wallets(trades_as_dicts, n=10)
            stats_map: dict[str, dict] = {}
            if top3 and wa_row_count > 0:
                placeholders = ",".join("?" * len(top3))
                stats_cur = _pg_exec(
                    conn,
                    f"""SELECT wallet,
                               COUNT(*) AS all_mint_count,
                               MIN(first_buy_ts) AS first_seen_ts,
                               SUM(CASE WHEN sell_volume_sol > 0
                                        THEN 1 ELSE 0 END)
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
                        GROUP BY wallet""",
                    (*top3, mint, t30_cutoff),
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
            # v21 — wallet_classifications JOIN for top3 wallets.
            cls_subset: dict = {}
            if top3:
                try:
                    placeholders = ",".join("?" * len(top3))
                    cls_cur = _pg_exec(
                        conn,
                        f"""SELECT wallet, is_sniper, is_smart_money, is_bot,
                                   cluster_size
                            FROM wallet_classifications
                            WHERE wallet IN ({placeholders})""",
                        tuple(top3),
                    )
                    for r in cls_cur.fetchall():
                        cls_subset[r[0]] = {
                            "is_sniper": bool(r[1] or 0),
                            "is_smart_money": bool(r[2] or 0),
                            "is_bot": bool(r[3] or 0),
                            "cluster_size": r[4],
                        }
                # v21 wallet-prior features default to NaN on lookup failure.
                except Exception:  # nosec B110
                    pass
            feats = _extract_wallet_prior_features(
                stats_map,
                top3,
                t30_cutoff,
                wallet_classifications=cls_subset,
            )
            # v20 sniper proxy at T+30. Need mint creation time for age.
            created_at_row = _pg_exec(
                conn, "SELECT created_at FROM tokens WHERE mint = ?", (mint,)
            ).fetchone()
            mint_created_at = float(created_at_row[0] or 0.0) if created_at_row else 0.0
            feats["n_buyers_first_5s"] = compute_n_buyers_first_5s(
                trades_as_dicts, mint_created_at
            )
            wallet_feat_rows.append(feats)
        wallet_df = pd.DataFrame(wallet_feat_rows)
        for col in WALLET_FEATURES:
            base[col] = wallet_df[col].values
    finally:
        conn.close()

    # ── T+30-only derived features ────────────────────────────────────
    # Override the buy-rate denominator (T+90 builder uses /90; at T+30
    # we observed only 30 seconds of activity).
    full_rate_30 = base["buy_count"] / 30.0
    base["fast_buy_rate_to_full"] = base["fast_buy_rate"] / (full_rate_30 + 0.001)
    base["hc_velocity_to_30"] = base["hc_30"].fillna(0.0) / 30.0

    # ── Drop T+120-derived columns to enforce schema parity ──────────
    drop_cols = [
        "top1_120",
        "top5_120",
        "top10_120",
        "hc_120",
        "top1_delta",
        "top5_delta",
        "top10_delta",
        "hc_velocity",
        "top5_minus_top1_120",
        "top10_minus_top5_120",
        "hc_growth_ratio",
        "helius_snapshot_complete",
    ]
    base = base.drop(
        columns=[c for c in drop_cols if c in base.columns], errors="ignore"
    )

    # Sanity: every T+30 scorer feature must already be present from the
    # base builder (they are stored on token_scores and selected there).
    missing_scorer = [c for c in SCORER_FEATURES_T30 if c not in base.columns]
    if missing_scorer:
        raise RuntimeError(
            "build_entry_dataset_t30: base frame is missing scorer columns "
            f"required by ENTRY_T30_FEATURE_ORDER: {missing_scorer}. Check "
            "that build_entry_dataset still SELECTs them via SCORER_FEATURES."
        )

    pos = int(base["label"].sum()) if "label" in base.columns else 0
    logger.info(
        "T30 entry dataset: %d rows, %d positives (%.1f%%)",
        len(base),
        pos,
        pos * 100 / max(len(base), 1),
    )
    return base


def main() -> None:
    """CLI mirror of build_dataset.py for the @T+30 dataset."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="pulse_bot.db")
    ap.add_argument("--out-dir", default="data/ml")
    ap.add_argument("--entry-horizon-sec", type=float, default=300.0)
    ap.add_argument(
        "--require-helius",
        action="store_true",
        help="Drop entry rows missing Helius @T+30 holder data.",
    )
    args = ap.parse_args()

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    # 2026-04-28: auto-reuse the just-built entry.parquet when
    # invoked right after `train --dataset entry`. Saves ~25 min by
    # skipping the duplicate build_entry_dataset call inside.
    base_path = out_dir / "entry.parquet"
    df = build_entry_dataset_t30(
        args.db,
        label_horizon_sec=args.entry_horizon_sec,
        require_helius=args.require_helius,
        base_parquet_path=base_path if base_path.exists() else None,
    )
    out = out_dir / "entry_t30.parquet"
    try:
        df.to_parquet(out, index=False)
        logger.info("Wrote %s", out)
    except Exception:
        out = out_dir / "entry_t30.csv"
        df.to_csv(out, index=False)
        logger.info("Wrote %s (parquet unavailable)", out)


if __name__ == "__main__":
    sys.exit(main())
