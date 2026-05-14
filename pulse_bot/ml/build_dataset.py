# pulse_bot/ml/build_dataset.py
"""Build training datasets for entry + exit XGBoost classifiers.

**Entry dataset**: one row per token scored at T+full_obs_seconds. Features
= scorer metrics + creator_snapshot + holder_snapshot (all available by
45-90s of token life). Label = token went up by ≥X% within Y minutes
after scoring (binary classification).

**Exit dataset**: one row per (mint, sampled timepoint during hold).
Features = evolving pulse state (buy_rate, sell_rate, top_holder_shift,
peak_pnl, drawdown). Label = "price N seconds from now will be lower
than now by ≥X%" (→ should sell now). Trained on the trades stream
replayed against any closed paper_trade.

Both datasets are ground-truth-based (peak MC / curve progress / death
time from the ``trades`` table), not the bot's own PnL — so we're
learning token dynamics independent of any filter that produced the
paper trades.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

import pandas as pd
import psycopg2
import psycopg2.extras

from pulse_bot.config import get_config
from pulse_bot.db import _resolve_dsn
from pulse_bot.ml.features import SCORER_FEATURES
from pulse_bot.ml.simulate_exit import simulate_exit
from pulse_bot.models import Trade

_CACHED_CONFIG = None


def _get_cached_config():
    """Cache the pulse config — called per-row, building it from env each time
    would dominate runtime."""
    global _CACHED_CONFIG
    if _CACHED_CONFIG is None:
        _CACHED_CONFIG = get_config()
    return _CACHED_CONFIG


def _df_rows_to_trades(
    df: pd.DataFrame, mint: str, creator_wallet: str | None = None
) -> list[Trade]:
    """Construct Trade objects from a trades-table row slice for simulate_exit.

    2026-05-01 (codex M3): ``is_creator`` is honored when ``creator_wallet``
    is supplied — otherwise the simulated exit never sees ``creator_dump``
    even on tokens where the creator actually sold, biasing forward-hold
    labels longer than reality.
    """
    out: list[Trade] = []
    for r in df.itertuples(index=False):
        wallet = str(getattr(r, "wallet", "") or "")
        out.append(
            Trade(
                mint=mint,
                wallet=wallet,
                tx_type=str(r.tx_type),
                sol_amount=float(r.sol_amount or 0.0),
                token_amount=float(r.token_amount or 0.0),
                new_token_balance=0.0,
                bonding_curve_key="",
                v_sol_in_bonding_curve=float(
                    getattr(r, "v_sol_in_bonding_curve", 0.0) or 0.0
                ),
                v_tokens_in_bonding_curve=0.0,
                market_cap_sol=float(r.market_cap_sol or 0.0),
                timestamp=float(r.timestamp),
                is_creator=bool(creator_wallet) and wallet == creator_wallet,
            )
        )
    return out


def _simulate_forward_hold_seconds(
    trades: pd.DataFrame,
    t: float,
    current_price: float,
    mint: str,
    creator_wallet: str | None = None,
) -> float:
    """Simulate live exit policy over trades after ``t`` and return time-to-exit
    in seconds. Right-censored at the end of the available trade stream.

    Used as the regression target for the ``exit_quantile_max_hold`` head —
    matches the question the model must answer at inference: "if I entered
    now at current state, how long would the bot hold this position?"

    2026-05-01 (codex M2): wrapped in broad exception handler. One bad row
    must not crash a 60k-row dataset rebuild — return 0.0 (right-censored
    immediate-exit) and continue.
    """
    fwd_df = trades[trades.timestamp > t]
    if fwd_df.empty:
        return 0.0
    try:
        config = _get_cached_config()
        fwd_trades = _df_rows_to_trades(fwd_df, mint, creator_wallet)
        result = simulate_exit(config, fwd_trades, t, current_price)
        return float(result.hold_seconds)
    except Exception:  # nosec B110
        # Reason: dataset rebuild is a long-running batch; one malformed
        # mint must not abort the whole run. Treat as immediate-exit.
        logger.exception("simulate_exit failed for %s @ %.0f — censoring", mint, t)
        return 0.0


def _connect_pg(db_path: str):
    """Compat wrapper: accepts legacy ``db_path`` (ignored) and returns a
    raw psycopg2 connection to the Postgres instance. pandas read_sql
    works with it (emits a harmless warning). Direct ``?``-placeholder
    execute calls must use :func:`_pg_exec` instead."""
    return psycopg2.connect(_resolve_dsn(db_path))


def _pg_exec(conn, sql: str, params: tuple | list | None = None):
    """Execute ``sql`` (with ``?`` placeholders) and return the cursor.
    Translates ``?`` → ``%s`` so SQLite-style code migrates verbatim."""
    cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
    cur.execute(sql.replace("?", "%s"), tuple(params) if params else None)
    return cur


logger = logging.getLogger(__name__)


def _compute_time_aware_features(
    trade_rows: list[tuple], created_at: float
) -> dict[str, float]:
    """Phase 2.5 — re-aggregate buy stream at T+30/60/90 from raw trade rows.

    Mirrors :meth:`MetricsCalculator._stats_up_to` (live path) so train and
    serve see identical values. Each ``trade_row`` is the tuple selected by
    the bulk query in :func:`build_entry_dataset`:
    ``(mint, timestamp, tx_type, sol_amount, wallet)``.

    Buy-rate denominator is the snapshot age (30/60/90), not
    observation_seconds — represents "buys/sec since launch up to T+N",
    invariant across rows with different observation windows.
    """
    out: dict[str, float] = {
        "unique_buyers_at_30": 0.0,
        "unique_buyers_at_60": 0.0,
        "unique_buyers_at_90": 0.0,
        "buy_rate_at_30": 0.0,
        "buy_rate_at_60": 0.0,
        "buy_rate_at_90": 0.0,
        "buy_volume_sol_at_30": 0.0,
        "buy_volume_sol_at_60": 0.0,
        "buy_volume_sol_at_90": 0.0,
    }
    if not trade_rows or created_at <= 0:
        return out
    buys = [
        (float(t[1] or 0.0), t[4] or "", float(t[3] or 0.0))
        for t in trade_rows
        if t[2] == "buy"
    ]
    if not buys:
        return out
    for age in (30.0, 60.0, 90.0):
        cutoff = created_at + age
        sub = [b for b in buys if b[0] <= cutoff]
        if not sub:
            continue
        uniq = len({b[1] for b in sub})
        vol = sum(b[2] for b in sub)
        rate = len(sub) / age
        suffix = f"_at_{int(age)}"
        out[f"unique_buyers{suffix}"] = float(uniq)
        out[f"buy_rate{suffix}"] = float(rate)
        out[f"buy_volume_sol{suffix}"] = float(vol)
    return out


# ── Entry classifier ────────────────────────────────────────────────

# Scorer metrics to SELECT from ``token_scores``. Canonical list lives in
# ``pulse_bot.ml.features.SCORER_FEATURES`` — see that module for the
# rationale behind excluding MC/graduation/curve from features (codex v9
# label leakage fix) and the cyclical hour encoding.
ENTRY_FEATURES = SCORER_FEATURES


def build_entry_dataset(
    db_path: str,
    label_horizon_sec: float = 300.0,
    label_tp_pct: float = 50.0,
    label_sl_pct: float = 30.0,
    require_helius: bool = False,
    now_buffer_sec: float | None = None,
    label_buy_amount_sol: float = 0.1,
    label_buy_slip: float = 0.02,
    label_sell_slip: float = 0.03,
) -> pd.DataFrame:
    """One row per live-scored token with features + binary label.

    **Label = 1 iff a fixed paper-trade strategy (TP=50%, SL=30%, max
    hold = label_horizon_sec) would have been profitable**, applied
    starting at scored_at with entry at first buy price thereafter.
    Price trajectory reconstructed from ``v_sol_in_bonding_curve`` /
    ``v_tokens_in_bonding_curve`` so SELL transitions update price
    (codex v9 audit: prior ffill-only-on-buys label was blind to dumps).

    Label is scale-invariant — a token near graduation can still trigger
    a TP or SL on small absolute moves, so no label leakage from raw MC.
    """
    import time as _time

    conn = _connect_pg(db_path)
    # Exclude tokens too young to have their labeling horizon complete.
    # Default buffer = horizon + small safety margin.
    if now_buffer_sec is None:
        now_buffer_sec = label_horizon_sec + 60.0
    cutoff_ts = _time.time() - now_buffer_sec
    base = pd.read_sql_query(
        f"""
        SELECT
            s.mint, s.scored_at, s.market_cap_sol AS mc_at_scoring,
            s.creator, s.hour_utc,
            {", ".join("s." + f for f in ENTRY_FEATURES)},
            t.created_at
        FROM token_scores s
        JOIN tokens t ON t.mint = s.mint
        WHERE s.source IN ('live', 'backfill') AND s.scored_at IS NOT NULL
          AND s.scored_at <= %s
        """,
        conn,
        params=(cutoff_ts,),
    )
    logger.info(
        "Loaded %d scored tokens (scored_at <= %.0f; buffer %.0fs)",
        len(base),
        cutoff_ts,
        now_buffer_sec,
    )

    # Helius features
    h30 = pd.read_sql_query(
        """
        SELECT mint,
               top1_pct AS top1_30,
               top5_pct AS top5_30,
               top10_pct AS top10_30,
               holder_count AS hc_30
        FROM token_holders_snapshots
        WHERE capture_at_age_sec = 30.0
          AND is_negative_row = 0
          AND top1_pct IS NOT NULL
        """,
        conn,
    )
    h60 = pd.read_sql_query(
        """
        SELECT mint,
               top1_pct AS top1_60,
               top5_pct AS top5_60,
               top10_pct AS top10_60,
               holder_count AS hc_60
        FROM token_holders_snapshots
        WHERE capture_at_age_sec = 60.0
          AND is_negative_row = 0
          AND top1_pct IS NOT NULL
        """,
        conn,
    )
    h120 = pd.read_sql_query(
        """
        SELECT mint,
               top1_pct AS top1_120,
               top5_pct AS top5_120,
               top10_pct AS top10_120,
               holder_count AS hc_120
        FROM token_holders_snapshots
        WHERE capture_at_age_sec = 120.0
          AND is_negative_row = 0
          AND top1_pct IS NOT NULL
        """,
        conn,
    )
    base = base.merge(h30, on="mint", how="left")
    base = base.merge(h60, on="mint", how="left")
    base = base.merge(h120, on="mint", how="left")

    # Train/serve parity fix (2026-05-05): Pipeline._fetch_holder_snapshot_all
    # extrapolates T+120 from (T+30, T+60) when the scheduled T+120 capture
    # hasn't landed yet at predict time (race: scoring at T+90 vs capture at
    # T+120). Mirror that here for the ~1.2% of historical rows that have
    # T+30 + T+60 but no T+120, so the live distribution matches training.
    # Linear projection: f(120) ≈ 2*f(60) - f(30). Pump.fun bonding-curve
    # concentration is mostly monotonic in the first 2 minutes — this is a
    # defensible proxy. holder_count clamped to >= max(hc_30, hc_60).
    needs_extrap = (
        base["top1_120"].isna() & ~base["top1_60"].isna() & ~base["top1_30"].isna()
    )
    if needs_extrap.any():
        n_extrap = int(needs_extrap.sum())
        logger.info(
            "Extrapolating T+120 holder snapshot for %d / %d rows", n_extrap, len(base)
        )
        for col_30, col_60, col_120 in [
            ("top1_30", "top1_60", "top1_120"),
            ("top5_30", "top5_60", "top5_120"),
            ("top10_30", "top10_60", "top10_120"),
        ]:
            base.loc[needs_extrap, col_120] = (
                2.0 * base.loc[needs_extrap, col_60] - base.loc[needs_extrap, col_30]
            ).clip(0.0, 100.0)
        # hc_120: linear extrap, but holders only ever grow, so floor by
        # max(hc_30, hc_60).
        hc_extrap = (
            2.0 * base.loc[needs_extrap, "hc_60"] - base.loc[needs_extrap, "hc_30"]
        )
        hc_floor = base.loc[needs_extrap, ["hc_30", "hc_60"]].max(axis=1)
        base.loc[needs_extrap, "hc_120"] = pd.concat([hc_extrap, hc_floor], axis=1).max(
            axis=1
        )

    base["top1_delta"] = base["top1_120"] - base["top1_30"]
    base["top5_delta"] = base["top5_120"] - base["top5_30"]
    base["top10_delta"] = base["top10_120"] - base["top10_30"]
    # hc_velocity = holders added per second between T+30 and T+120
    # (90-second window). Positive = growing, negative = exiting.
    base["hc_velocity"] = (base["hc_120"] - base["hc_30"]) / 90.0
    # Phase B derived features — mirror extract_entry_features logic so
    # train and serve see identical values.
    base["top5_minus_top1_120"] = base["top5_120"] - base["top1_120"]
    base["top10_minus_top5_120"] = base["top10_120"] - base["top5_120"]
    base["buy_vol_to_sell_vol_ratio"] = base["buy_volume_sol"] / (
        base["sell_volume_sol"] + 0.01
    )
    base["buy_count_to_sell_count_ratio"] = base["buy_count"] / (
        base["sell_count"] + 1.0
    )
    base["hc_growth_ratio"] = base["hc_120"] / (base["hc_30"] + 1.0)
    # Phase C derived — mirror extract_entry_features.

    base["buy_size_growth"] = base["max_buy_sol"] / (base["avg_buy_sol"] + 0.001)
    base["fast_to_full_volume_ratio"] = base["fast_volume_sol"] / (
        base["buy_volume_sol"] + 0.01
    )
    # log_market_cap removed 2026-04-25 (v16): market_cap_sol no longer
    # in SCORER_FEATURES; derivative dropped with it.
    full_rate = base["buy_count"] / 90.0
    base["fast_buy_rate_to_full"] = base["fast_buy_rate"] / (full_rate + 0.001)

    # TODO(Phase A2): market-context features. Implement when scorer.py
    # grows a MarketSnapshot helper that captures same values at live
    # scoring time — otherwise train/serve skew.
    # Per codex 2026-04-22: explicit completeness indicator distinguishes
    # "fetched and concentration is 0" from "fetch failed". Must match
    # live extractor logic in pulse_bot.ml.features.extract_entry_features.
    base["helius_snapshot_complete"] = (~base["top1_30"].isna()).astype(float)

    # Creator snapshots — as-of join by (creator, observed_at ≤ scored_at)
    # to avoid leakage. Picks the most recent snapshot computed before
    # each token was scored. Creator without any snapshot → zero-filled.
    # PROVISIONAL per codex 2026-04-22: naïve ΔAUC was within noise;
    # re-evaluating at higher N via k-fold.
    creator_snaps = pd.read_sql_query(
        """
        SELECT creator, observed_at,
               creator_age_days,
               median_peak_mc_sol AS creator_median_peak_mc_sol,
               inter_token_interval_sec AS creator_inter_token_interval_sec,
               total_prior_tokens AS creator_total_prior_tokens,
               creator_balance_sol,
               graduated_count AS creator_graduated_count
        FROM creator_snapshots
        """,
        conn,
    )
    if not creator_snaps.empty:
        creator_snaps = creator_snaps.sort_values("observed_at")
        base = base.sort_values("scored_at")
        base = pd.merge_asof(
            base,
            creator_snaps,
            left_on="scored_at",
            right_on="observed_at",
            by="creator",
            direction="backward",
        )
        base = base.drop(columns=["observed_at"], errors="ignore")

    if require_helius:
        before = len(base)
        base = base.dropna(subset=["top1_30"])
        logger.info("Filtered to Helius-complete rows: %d → %d", before, len(base))
    else:
        # Codex 2026-04-22: training/serving parity fix. Live extractor
        # zero-fills missing Helius; training previously kept NaN. Tree
        # still splits correctly on NaN OR 0.0, but if train sees NaN
        # while live sees 0.0 the distributions diverge → skew. Zero-fill
        # both sides identically.
        helius_cols = [
            "top1_30",
            "top5_30",
            "top10_30",
            "hc_30",
            "top1_120",
            "top5_120",
            "top10_120",
            "hc_120",
            "top1_delta",
            "top5_delta",
            "top10_delta",
            "hc_velocity",
        ]
        for c in helius_cols:
            if c in base.columns:
                base[c] = base[c].fillna(0.0)
        # helius_snapshot_complete is already 0/1 from the expression
        # above; no fillna needed.
        #
        # Creator features — train/serve parity (2026-05-05): align with
        # ``_get_creator_feat`` in features/_main.py:
        #   * Rows with no creator snapshot at all (merge_asof miss) → NaN
        #     (matches serve when ``creator_snapshot=None``).
        #   * Rows with snapshot but creator_total_prior_tokens < 2 →
        #     ``creator_median_peak_mc_sol`` and ``creator_inter_token_interval_sec``
        #     are mathematically undefined (median of empty / interval of one)
        #     → NaN. The other creator features (age_days, balance_sol) stay as-is.
        # XGBoost handles NaN natively via missingness splits; this restores
        # the distribution that the model needs to see in serve, where ~64%
        # of live creators are solo (single-token wallets).
        creator_zero_fill = {
            "creator_total_prior_tokens",  # legitimate count; 0 for solo
            "creator_graduated_count",  # legitimate count
        }
        creator_nan_for_solo = {
            "creator_median_peak_mc_sol",
            "creator_inter_token_interval_sec",
        }
        if "creator_total_prior_tokens" in base.columns:
            solo_mask = base["creator_total_prior_tokens"].fillna(0).lt(2)
            for c in creator_nan_for_solo:
                if c in base.columns:
                    import numpy as _np

                    base.loc[solo_mask, c] = _np.nan
        # Counts → 0-fill (no snapshot ≡ no observed priors).
        for c in creator_zero_fill:
            if c in base.columns:
                base[c] = base[c].fillna(0.0)
        # ``creator_age_days`` and ``creator_balance_sol`` stay NaN when
        # no snapshot — XGBoost will route them via the missingness branch
        # rather than treat them as "fresh wallet, zero balance".

    # Phase E — top-3 buyer wallet prior features. Same helper used
    # by live pipeline (``compute_top3_buyer_wallets``) + same SQL
    # semantics as ``Database.get_wallet_prior_stats_sync``. Rows with
    # fewer than 3 distinct buyers get <3 top-N; rows whose top buyers
    # have no prior history get NaN across the 5 features. Requires
    # ``wallet_activity`` table to be populated — run
    # ``python -m pulse_bot.ml.wallet_indexer --db <path>`` first.
    from pulse_bot.ml.features import (
        WALLET_FEATURES,
        _extract_wallet_prior_features,
        compute_n_buyers_first_5s,
        compute_topN_buyer_wallets,
    )

    wa_row_count = _pg_exec(conn, "SELECT COUNT(*) FROM wallet_activity").fetchone()[0]
    if wa_row_count == 0:
        logger.warning(
            "wallet_activity table is empty — all %d WALLET_FEATURES "
            "will be NaN. Run `python -m pulse_bot.ml.wallet_indexer "
            "--db %s` first to populate.",
            len(WALLET_FEATURES),
            db_path,
        )
    else:
        logger.info(
            "wallet_activity has %d (wallet,mint) pairs; computing "
            "top-3 buyer prior-stats per row...",
            wa_row_count,
        )

    wallet_feat_rows: list[dict[str, float]] = []
    nz_wallet_feat_count = 0
    # Pre-load mint→created_at map for n_buyers_first_5s computation.
    mint_created_map: dict[str, float] = {}
    for mrow in _pg_exec(conn, "SELECT mint, created_at FROM tokens").fetchall():
        mint_created_map[mrow[0]] = float(mrow[1] or 0.0)

    # 2026-04-28 perf fix: replace ~88k per-mint trade queries with a
    # single bulk SELECT + pandas groupby. Old loop took ~30 min on
    # rich (88k roundtrips × ~20 ms each). New path fetches all
    # ts<=max(scored_at) trades for the mints we care about in one
    # pass and indexes into a dict — runtime drops to ~2 min.
    logger.info(
        "Bulk-fetching trade rows for %d mints (replaces per-row " "loop)...", len(base)
    )
    base_mints = base["mint"].tolist()
    max_scored = float(base["scored_at"].max()) if len(base) else 0.0
    trades_by_mint: dict[str, list[dict]] = {m: [] for m in base_mints}
    chunk = 5000
    fetched = 0
    for i in range(0, len(base_mints), chunk):
        chunk_mints = base_mints[i : i + chunk]
        placeholders = ",".join("?" * len(chunk_mints))
        cur = _pg_exec(
            conn,
            f"""SELECT mint, tx_type, wallet, sol_amount, timestamp
                FROM trades
                WHERE mint IN ({placeholders}) AND timestamp <= ?
                ORDER BY id ASC""",
            (*chunk_mints, max_scored),
        )
        for tr in cur.fetchall():
            mint = tr[0]
            if mint in trades_by_mint:
                trades_by_mint[mint].append(
                    {
                        "tx_type": tr[1],
                        "wallet": tr[2],
                        "sol_amount": tr[3],
                        "timestamp": tr[4],
                    }
                )
                fetched += 1
    logger.info(
        "Bulk-fetched %d trade rows (avg %.1f per mint)",
        fetched,
        fetched / max(len(base_mints), 1),
    )

    # Pre-collect all top-10 buyer wallets across mints and bulk-query
    # wallet_activity once (instead of per-mint subqueries). Filter by
    # last_trade_ts < per-mint scored_at applied client-side.
    per_mint_top10: dict[str, list[str]] = {}
    all_wallets: set[str] = set()
    for _, row in base.iterrows():
        mint = row["mint"]
        scored_at = float(row["scored_at"])
        trades_for_mint = trades_by_mint.get(mint, [])
        # Filter to trades ≤ scored_at (bulk fetched at max_scored above
        # so a per-mint trim is needed for the leakage-safe semantics).
        trades_clipped = [t for t in trades_for_mint if t["timestamp"] <= scored_at]
        top10 = compute_topN_buyer_wallets(trades_clipped, n=10)
        per_mint_top10[mint] = top10
        all_wallets.update(top10)

    # Bulk pull wallet_activity for the union of all top10 wallets,
    # one query for everyone. We collapse the per-mint filter
    # (mint != X AND last_trade_ts < scored_at) client-side because
    # the wallet→mint set is large.
    wallet_activity_rows: dict[str, list[dict]] = {w: [] for w in all_wallets}
    if all_wallets and wa_row_count > 0:
        wallets_list = list(all_wallets)
        for i in range(0, len(wallets_list), chunk):
            chunk_w = wallets_list[i : i + chunk]
            placeholders = ",".join("?" * len(chunk_w))
            cur = _pg_exec(
                conn,
                f"""SELECT wallet, mint, first_buy_ts, last_trade_ts,
                           buy_volume_sol, sell_volume_sol
                    FROM wallet_activity
                    WHERE wallet IN ({placeholders})""",
                tuple(chunk_w),
            )
            for r in cur.fetchall():
                wallet_activity_rows[r[0]].append(
                    {
                        "mint": r[1],
                        "first_buy_ts": float(r[2] or 0.0),
                        "last_trade_ts": float(r[3] or 0.0),
                        "buy_volume_sol": float(r[4] or 0.0),
                        "sell_volume_sol": float(r[5] or 0.0),
                    }
                )
    logger.info(
        "Bulk-pulled wallet_activity for %d distinct top-10 wallets", len(all_wallets)
    )

    # v21 (2026-04-28): bulk-pull wallet_classifications for sniper /
    # smart_money / bot / cluster flags. One query for the whole top-10
    # union; ~150k rows in the table so this is millisecond cheap.
    classifications: dict[str, dict] = {}
    if all_wallets:
        try:
            wallets_list = list(all_wallets)
            for i in range(0, len(wallets_list), chunk):
                chunk_w = wallets_list[i : i + chunk]
                placeholders = ",".join("?" * len(chunk_w))
                cur = _pg_exec(
                    conn,
                    f"""SELECT wallet, is_sniper, is_smart_money, is_bot,
                               cluster_size
                        FROM wallet_classifications
                        WHERE wallet IN ({placeholders})""",
                    tuple(chunk_w),
                )
                for r in cur.fetchall():
                    classifications[r[0]] = {
                        "is_sniper": bool(r[1] or 0),
                        "is_smart_money": bool(r[2] or 0),
                        "is_bot": bool(r[3] or 0),
                        "cluster_size": r[4],
                    }
            logger.info(
                "Bulk-pulled wallet_classifications for %d wallets",
                len(classifications),
            )
        except Exception as exc:
            logger.warning(
                "wallet_classifications JOIN failed (continuing w/o v21 "
                "features): %s",
                exc,
            )

    for _, row in base.iterrows():
        mint = row["mint"]
        scored_at = float(row["scored_at"])
        trades_for_mint = trades_by_mint.get(mint, [])
        trades_clipped = [t for t in trades_for_mint if t["timestamp"] <= scored_at]
        top10 = per_mint_top10.get(mint, [])

        # Compute per-wallet stats client-side (replaces the per-mint
        # GROUP BY query on wallet_activity).
        stats_map: dict[str, dict] = {}
        for w in top10:
            rows_w = [
                r
                for r in wallet_activity_rows.get(w, [])
                if r["mint"] != mint and r["last_trade_ts"] < scored_at
            ]
            if not rows_w:
                continue
            all_mc = len(rows_w)
            closed_rows = [r for r in rows_w if r["sell_volume_sol"] > 0]
            closed_mc = len(closed_rows)
            total_pnl = sum(
                r["sell_volume_sol"] - r["buy_volume_sol"] for r in closed_rows
            )
            wins = sum(
                1
                for r in closed_rows
                if (r["sell_volume_sol"] - r["buy_volume_sol"]) > 0
            )
            max_pnl = max(
                (r["sell_volume_sol"] - r["buy_volume_sol"] for r in closed_rows),
                default=None,
            )
            first_seen = min((r["first_buy_ts"] for r in rows_w), default=0.0)
            stats_map[w] = {
                "all_mint_count": all_mc,
                "closed_mint_count": closed_mc,
                "wr": (wins / closed_mc) if closed_mc > 0 else float("nan"),
                "total_pnl_sol": float(total_pnl),
                "max_pnl_sol": (
                    float(max_pnl) if max_pnl is not None else float("nan")
                ),
                "first_seen_ts": float(first_seen),
            }

        # v21 — pass classification subset for top10 to extractor.
        cls_subset = {w: classifications[w] for w in top10 if w in classifications}
        feats = _extract_wallet_prior_features(
            stats_map,
            top10,
            scored_at,
            wallet_classifications=cls_subset,
        )
        feats["n_buyers_first_5s"] = compute_n_buyers_first_5s(
            trades_clipped, mint_created_map.get(mint, 0.0)
        )
        wallet_feat_rows.append(feats)
        import math as _m

        if not _m.isnan(feats["top3_buyer_prior_mint_count_sum"]):
            nz_wallet_feat_count += 1

    wallet_df = pd.DataFrame(wallet_feat_rows)
    for col in WALLET_FEATURES:
        base[col] = wallet_df[col].values
    logger.info(
        "WALLET_FEATURES: %d/%d rows have prior-stats (%.1f%% non-NaN)",
        nz_wallet_feat_count,
        len(base),
        (nz_wallet_feat_count / max(len(base), 1)) * 100.0,
    )

    # ── Phase 2.5 (2026-04-25): time-aware snapshot features ─────────────
    # Re-aggregate the BUY stream truncated at created_at + 30/60/90 to
    # populate unique_buyers / buy_rate / buy_volume_sol @ each age.
    # token_scores does not store these columns directly (legacy rows
    # predate v18 schema), so we always recompute from trades to get a
    # consistent value. Live path (Scorer) already computes the same
    # values via MetricsCalculator._stats_up_to — see parity test.
    from pulse_bot.ml.features import TIME_AWARE_DERIVED_FEATURES, TIME_AWARE_FEATURES

    ta_mints: list[str] = base["mint"].tolist()
    obs_trades_by_mint: dict[str, list[tuple]] = {m: [] for m in ta_mints}
    if ta_mints:
        cur = conn.cursor()
        chunk_size = 500
        ta_rows_fetched = 0
        for chunk_start in range(0, len(ta_mints), chunk_size):
            chunk = ta_mints[chunk_start : chunk_start + chunk_size]
            cur.execute(
                """SELECT mint, timestamp, tx_type, sol_amount, wallet
                   FROM trades
                   WHERE mint = ANY(%s::text[])
                   ORDER BY mint ASC, timestamp ASC""",
                (chunk,),
            )
            for tr in cur:
                obs_trades_by_mint[tr[0]].append(tr)
                ta_rows_fetched += 1
        cur.close()
        logger.info(
            "Phase 2.5: bulk-fetched %d trades for time-aware aggregation",
            ta_rows_fetched,
        )

    created_map = {
        str(row["mint"]): float(row["created_at"]) for _, row in base.iterrows()
    }
    ta_feat_rows: list[dict[str, float]] = []
    for _, row in base.iterrows():
        mint = str(row["mint"])
        created_at = created_map.get(mint, 0.0)
        trade_rows = obs_trades_by_mint.get(mint) or []
        feats = _compute_time_aware_features(trade_rows, created_at)
        ta_feat_rows.append(feats)
    ta_df = pd.DataFrame(ta_feat_rows)
    for col in TIME_AWARE_FEATURES:
        base[col] = ta_df[col].values

    # Derived deltas — mirror extract_entry_features so train/serve parity
    # is bit-identical. top1_at_60 uses Helius @30/@120 (already merged
    # earlier in this function) with linear interpolation.
    base["top1_at_60"] = base["top1_30"] + (base["top1_120"] - base["top1_30"]) * (
        60.0 - 30.0
    ) / (120.0 - 30.0)
    base["delta_top1_30_to_60"] = base["top1_at_60"] - base["top1_30"]
    base["delta_buy_rate_60_to_90"] = base["buy_rate_at_90"] - base["buy_rate_at_60"]
    base["delta_unique_buyers_30_to_60"] = (
        base["unique_buyers_at_60"] - base["unique_buyers_at_30"]
    )
    # Sanity: TIME_AWARE_DERIVED_FEATURES must all be present on base.
    for col in TIME_AWARE_DERIVED_FEATURES:
        assert col in base.columns, f"Phase 2.5: derived column {col!r} missing"

    # Label v2 (codex 2026-04-22): realized PnL % applying the SAME
    # fee+slippage math as live ``PaperTradeRunner._calc_leg_pnl``.
    # Entry = market_cap_sol at first post-scoring buy. Exit = MC at
    # first trade where MC crosses TP/SL threshold (approximates the
    # price we'd actually sell at ~400ms after crossover, then apply
    # sell-side slippage on top). If neither threshold hits before the
    # horizon, exit at the final MC in the window. This replaces the
    # previous "+tp_pct / -sl_pct flat" label — old label flipped sign
    # on ~10-20% of rows near zero because fees/slippage weren't baked
    # into the threshold calculation, producing noisy labels.
    # Option B (2026-04-24): label via simulate_exit() — the same
    # ExitManager + PulseMonitor + PaperTradeRunner code the LIVE bot
    # uses. Previously labels used a fixed TP=+50%/SL=-30%/horizon=300s
    # policy that diverged from the live config's TP=+100%/SL=-15%/
    # max_hold=90s + trailing + inactivity rules — classic train/serve
    # skew. Using simulate_exit means any config change automatically
    # invalidates this dataset (meta.json stores config hash).
    from pulse_bot.config import get_config
    from pulse_bot.ml.simulate_exit import simulate_exit, simulate_exit_batch
    from pulse_bot.models import Trade

    live_config = get_config()
    # Dataset-build horizon: widen the trade query window so simulate_exit
    # has the full stream available. Live bot caps via exit_max_hold_seconds +
    # inactivity — simulate_exit enforces both internally.
    trade_window_sec = max(
        label_horizon_sec,
        float(live_config.exit_max_hold_seconds) + 60.0,
    )
    logger.info(
        "Computing labels via simulate_exit (live config: TP=%.0f%% SL=%.0f%% "
        "max_hold=%.0fs inactivity=%.0fs trailing=%s)",
        live_config.exit_take_profit_pct,
        live_config.exit_hard_stop_loss_pct,
        live_config.exit_max_hold_seconds,
        live_config.exit_inactivity_seconds,
        live_config.exit_trailing_stop_enabled,
    )

    # ── Bulk-fetch trades for ALL mints in one round-trip ──────────────
    # Previous implementation issued one SELECT per row (~60k mints →
    # ~60k network round-trips). Roadmap parallel-infra item: pull every
    # relevant trade once, group by mint in pandas, then loop in pure
    # Python with zero DB I/O. The per-token simulate_exit_batch path is
    # bit-identical to simulate_exit (see test_simulate_exit_parity).
    mints = base["mint"].tolist()
    scored_map = {
        str(row["mint"]): float(row["scored_at"]) for _, row in base.iterrows()
    }
    all_trades_by_mint: dict[str, list[tuple]] = {m: [] for m in mints}
    if mints:
        # Chunked ``ANY(%s::text[])`` (500 mints/chunk). Benchmarked vs
        # both per-mint point queries and a single giant ANY(60k) — the
        # giant ANY drops to a Seq Scan on ``trades`` (~1.2s/query),
        # while chunks of ~500 stay on the (mint, timestamp) BTree index
        # and finish ~7x faster. 500 was chosen by sweep on a 5000-mint
        # slice; 100 also works but adds round-trips.
        cur = conn.cursor()
        rows_fetched = 0
        chunk_size = 500
        for chunk_start in range(0, len(mints), chunk_size):
            chunk = mints[chunk_start : chunk_start + chunk_size]
            cur.execute(
                """SELECT mint, timestamp, tx_type, sol_amount, token_amount,
                          market_cap_sol, v_sol_in_bonding_curve, wallet
                   FROM trades
                   WHERE mint = ANY(%s::text[])
                     AND market_cap_sol > 0
                   ORDER BY mint ASC, timestamp ASC""",
                (chunk,),
            )
            for tr in cur:
                mint = tr[0]
                scored = scored_map.get(mint)
                if scored is None:
                    continue
                ts = float(tr[1] or 0.0)
                # Same window filter as the per-token query (BETWEEN
                # scored AND scored + window). Done client-side: one
                # bulk query per chunk, each mint has its own ts cutoff.
                if ts < scored or ts > scored + trade_window_sec:
                    continue
                all_trades_by_mint[mint].append(tr)
                rows_fetched += 1
        cur.close()
        logger.info(
            "Bulk-fetched %d trades across %d mints (chunked %d-mint queries)",
            rows_fetched,
            len(mints),
            chunk_size,
        )

    # ── Build entry points + post-entry trade lists (no DB I/O) ────────
    labels: list[int | None] = []
    realized_pnls: list[float | None] = []
    exit_reasons: list[str | None] = []
    # Distinct from the earlier raw-row map of the same name — that one is
    # fully consumed before this point; this is the Trade-object map.
    trades_by_mint: dict[str, list[Trade]] = {}  # type: ignore[no-redef]
    entries_by_mint: dict[str, tuple[float, float]] = {}
    # Pre-flagged DOA mints: skip the simulator entirely (label=0 / 0.0 PnL,
    # exit_reason set explicitly so the simulator's "timeout" doesn't mask
    # them in diagnostics).
    doa_reasons: dict[str, str] = {}

    for _, row in base.iterrows():
        mint = str(row["mint"])
        trade_rows = all_trades_by_mint.get(mint) or []
        if not trade_rows:
            doa_reasons[mint] = "doa_no_trades"
            continue
        entry_idx = None
        entry_price = None
        entry_ts = None
        for i, t in enumerate(trade_rows):
            tx = t[2]
            sol_amt = t[3]
            tok_amt = t[4]
            if tx == "buy" and tok_amt and sol_amt:
                entry_price = float(sol_amt) / float(tok_amt)
                entry_ts = float(t[1])
                entry_idx = i
                break
        if entry_idx is None or not entry_price or entry_price <= 0:
            doa_reasons[mint] = "doa_no_entry"
            continue
        post_entry_trades = [
            Trade(
                mint=mint,
                wallet=t[7] or "",
                tx_type=t[2],
                sol_amount=float(t[3] or 0.0),
                token_amount=float(t[4] or 0.0),
                new_token_balance=0.0,
                bonding_curve_key="",
                v_sol_in_bonding_curve=float(t[6] or 0.0),
                v_tokens_in_bonding_curve=0.0,
                market_cap_sol=float(t[5] or 0.0),
                timestamp=float(t[1]),
            )
            for t in trade_rows[entry_idx + 1 :]
        ]
        trades_by_mint[mint] = post_entry_trades
        entries_by_mint[mint] = (entry_ts, entry_price)

    # ── Batch simulate (still loops per mint inside; PaperTradeRunner is
    #    stateful and serial — but no I/O, no cursor reopens) ──────────
    sim_results: dict[str, "object"] = {}
    if entries_by_mint:
        try:
            sim_results = simulate_exit_batch(
                live_config, trades_by_mint, entries_by_mint
            )
        except Exception as exc:
            # Fall back to per-token path so a single bad row doesn't
            # nuke the whole dataset. We still avoid PG round-trips here.
            logger.warning(
                "simulate_exit_batch raised %s — falling back to per-token loop",
                exc,
            )
            for mint, (e_ts, e_px) in entries_by_mint.items():
                try:
                    sim_results[mint] = simulate_exit(
                        live_config, trades_by_mint[mint], e_ts, e_px
                    )
                except Exception as inner:
                    logger.warning(
                        "simulate_exit failed for %s (entry_ts=%.0f): %s — drop",
                        mint,
                        e_ts,
                        inner,
                    )
                    sim_results[mint] = None  # type: ignore[assignment]

    # ── Stitch results back in base-row order ─────────────────────────
    for _, row in base.iterrows():
        mint = str(row["mint"])
        if mint in doa_reasons:
            labels.append(0)
            realized_pnls.append(0.0)
            exit_reasons.append(doa_reasons[mint])
            continue
        mr = sim_results.get(mint)
        if mr is None:
            labels.append(None)
            realized_pnls.append(None)
            exit_reasons.append(None)
            continue
        realized = float(mr.pnl_pct)
        labels.append(1 if realized > 0 else 0)
        realized_pnls.append(realized)
        exit_reasons.append(str(mr.exit_reason))
    conn.close()

    base["label"] = labels
    # realized_pnl_pct is a SIDE column for honest economic backtest.
    # Must be excluded from feature list in train.py — it is the label
    # magnitude and would be perfect label leakage if trained on.
    base["realized_pnl_pct"] = realized_pnls
    base["exit_reason"] = exit_reasons
    base = base.dropna(subset=["label"])
    base["label"] = base["label"].astype(int)
    base["realized_pnl_pct"] = base["realized_pnl_pct"].astype(float)
    # Log the exit-reason distribution so we can eyeball whether the
    # simulator is producing realistic mixes (e.g. too many `timeout`
    # means most trades never resolve in-window → widen trade_window_sec).
    if not base.empty:
        reason_counts = base["exit_reason"].value_counts()
        logger.info("Label exit_reason distribution: %s", dict(reason_counts))
    # Cyclical hour encoding replaces raw hour_utc (codex v9 P2 fix).
    import math as _math

    base["hour_sin"] = base["hour_utc"].apply(
        lambda h: _math.sin(2 * _math.pi * (h or 0) / 24)
    )
    base["hour_cos"] = base["hour_utc"].apply(
        lambda h: _math.cos(2 * _math.pi * (h or 0) / 24)
    )
    base = base.drop(columns=["hour_utc"])
    pos = int(base["label"].sum())
    logger.info(
        "Entry dataset: %d rows, %d positives (%.1f%%)",
        len(base),
        pos,
        pos * 100 / max(len(base), 1),
    )
    return base


# ── Exit classifier ─────────────────────────────────────────────────

EXIT_FEATURES = [
    # State at decision time
    "hold_seconds",
    "current_pnl_pct",
    "peak_pnl_pct",  # max pnl observed so far
    "drawdown_from_peak",  # (peak - current)
    "buy_rate_recent",  # buys/sec in last 10s
    "sell_rate_recent",
    "unique_buyers_recent",
    "curve_progress_pct",
    "curve_velocity_recent",
]


def build_exit_dataset(
    db_path: str,
    lookahead_sec: float = 30.0,
    label_threshold_pct: float = 5.0,
    sample_every_sec: float = 5.0,
) -> pd.DataFrame:
    """Row per (mint, time point during token life after entry).

    At each sample, the *state* = buy/sell activity in last 10s, current
    price vs entry, peak pnl so far. Label = 1 iff price drops by
    ``label_threshold_pct`` within ``lookahead_sec`` (→ should sell
    now). Features computed purely from ``trades`` table (ground truth).

    Only processes tokens where we have at least one FAST_BUY decision
    (proxy for "we would have considered buying"). For each such token,
    synthesise a held-position timeline every ``sample_every_sec``.
    """
    conn = _connect_pg(db_path)
    candidates = pd.read_sql_query(
        """
        SELECT DISTINCT s.mint, s.scored_at AS entry_ts,
               t.created_at, t.creator AS creator_wallet
        FROM token_scores s
        JOIN tokens t ON t.mint = s.mint
        WHERE s.source = 'live'
          AND (s.fast_decision = 'FAST_BUY' OR s.decision = 'BUY')
        """,
        conn,
    )
    logger.info("Exit candidate tokens: %d", len(candidates))

    # TODO(cross-model signal): exit_v2 (2026-04-23) injected
    # entry_ml_proba as the #1-gain feature via a helper that replayed
    # the live entry inference path on each candidate. Removed in v3 —
    # restore _precompute_entry_probas + the feature + the hash gate
    # when re-enabling (see EXIT_FEATURE_ORDER comment in features.py).
    rows: list[dict] = []
    for _, c in candidates.iterrows():
        mint, entry_ts = c["mint"], c["entry_ts"]
        creator_wallet = (
            c.get("creator_wallet") if hasattr(c, "get") else c["creator_wallet"]
        )
        trades = pd.read_sql_query(
            """SELECT timestamp, tx_type, sol_amount, token_amount,
                      market_cap_sol, v_sol_in_bonding_curve, wallet
               FROM trades WHERE mint = %s AND timestamp >= %s
                 AND market_cap_sol > 0
               ORDER BY timestamp""",
            conn,
            params=(mint, entry_ts),
        )
        if len(trades) < 2:
            continue
        entry_buys = trades[(trades.tx_type == "buy") & (trades.token_amount > 0)]
        if entry_buys.empty:
            continue
        entry = entry_buys.iloc[0]
        entry_price = entry.market_cap_sol  # MC proxy, total-supply constant
        entry_t = float(entry.timestamp)
        # Codex v9 fix: use market_cap_sol as the price series. MC reflects
        # bonding-curve state on BOTH buys and sells. Old code ffilled
        # sol/token only on buys so sell dumps were invisible.
        trades["price"] = trades["market_cap_sol"]
        trades = trades.dropna(subset=["price"])
        if trades.empty:
            continue
        peak_pnl = 0.0
        t = entry_t + sample_every_sec
        t_end = float(trades.timestamp.iloc[-1])
        while t < t_end:
            # State at time t
            window_start = t - 10.0
            in_window = trades[
                (trades.timestamp >= window_start) & (trades.timestamp <= t)
            ]
            if in_window.empty:
                t += sample_every_sec
                continue
            current_price = in_window.price.iloc[-1]
            pnl = (current_price - entry_price) / entry_price * 100
            peak_pnl = max(peak_pnl, pnl)
            # Label: price in next lookahead_sec drops by threshold
            future = trades[
                (trades.timestamp > t) & (trades.timestamp <= t + lookahead_sec)
            ]
            if future.empty:
                t += sample_every_sec
                continue
            min_future_price = future.price.min()
            drop_pct = (current_price - min_future_price) / current_price * 100
            label = 1 if drop_pct >= label_threshold_pct else 0

            buys = in_window[in_window.tx_type == "buy"]
            sells = in_window[in_window.tx_type == "sell"]
            # Forward 60s PnL (E3): real regression target for quantile
            # heads. Uses the actual mid-future window, not drawdown
            # proxy. Looks 60 seconds ahead; if the trade stream is
            # exhausted sooner, use the final available price (no
            # lookahead leak since by then the trade stream has ended).
            fwd_window = trades[(trades.timestamp > t) & (trades.timestamp <= t + 60.0)]
            if fwd_window.empty:
                # fall back to the final price if nothing in 60s window
                fwd_price = trades.price.iloc[-1]
            else:
                fwd_price = fwd_window.price.iloc[-1]
            forward_pnl_60s = (fwd_price - current_price) / current_price * 100
            # Time-to-exit target for max_hold quantile head (v2 2026-04-30).
            # Was: ``forward_seconds_to_peak`` had survivor bias (ρ=-0.196,
            # anti_correlated). DOA tokens collapsed to sec=0; pumpers to
            # sec=600. Model learned the inverse of what we need.
            # Now: simulate live exit policy on future trades; label is the
            # time the bot would actually hold this position. This matches
            # the inference-time question — "given current state, when
            # would max_hold/SL/TP fire?"
            forward_seconds_to_exit = _simulate_forward_hold_seconds(
                trades, t, current_price, mint, creator_wallet
            )

            rows.append(
                {
                    "mint": mint,
                    "entry_ts": entry_t,  # Codex v9: needed for chrono split
                    "sample_ts": t,
                    "hold_seconds": t - entry_t,
                    "current_pnl_pct": pnl,
                    "peak_pnl_pct": peak_pnl,
                    "drawdown_from_peak": peak_pnl - pnl,
                    "buy_rate_recent": len(buys) / 10.0,
                    "sell_rate_recent": len(sells) / 10.0,
                    "unique_buyers_recent": int(buys.wallet.nunique()),
                    "curve_progress_pct": (
                        in_window.v_sol_in_bonding_curve.iloc[-1] / 85.0 * 100.0
                        if not in_window.empty
                        else 0.0
                    ),
                    "curve_velocity_recent": (
                        (
                            in_window.v_sol_in_bonding_curve.iloc[-1]
                            - in_window.v_sol_in_bonding_curve.iloc[0]
                        )
                        / 10.0
                        if len(in_window) > 1
                        else 0.0
                    ),
                    "forward_pnl_60s": float(forward_pnl_60s),
                    "forward_seconds_to_exit": float(forward_seconds_to_exit),
                    "label": label,
                }
            )
            t += sample_every_sec
    conn.close()

    df = pd.DataFrame(rows)
    if df.empty:
        logger.warning("Exit dataset is empty")
        return df
    pos = int(df["label"].sum())
    logger.info(
        "Exit dataset: %d rows, %d positives (%.1f%%)",
        len(df),
        pos,
        pos * 100 / max(len(df), 1),
    )
    return df


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="pulse_bot.db")
    ap.add_argument("--out-dir", default="data/ml")
    ap.add_argument("--dataset", choices=["entry", "exit", "both"], default="both")
    ap.add_argument("--entry-horizon-sec", type=float, default=300.0)
    ap.add_argument("--entry-tp-pct", type=float, default=50.0)
    ap.add_argument("--entry-sl-pct", type=float, default=30.0)
    ap.add_argument(
        "--require-helius",
        action="store_true",
        help="Drop entry rows missing Helius holder data.",
    )
    args = ap.parse_args()

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    if args.dataset in ("entry", "both"):
        df = build_entry_dataset(
            args.db,
            label_horizon_sec=args.entry_horizon_sec,
            label_tp_pct=args.entry_tp_pct,
            label_sl_pct=args.entry_sl_pct,
            require_helius=args.require_helius,
        )
        out = out_dir / "entry.parquet"
        try:
            df.to_parquet(out, index=False)
            logger.info("Wrote %s", out)
        except Exception:
            out = out_dir / "entry.csv"
            df.to_csv(out, index=False)
            logger.info("Wrote %s (parquet unavailable)", out)

    if args.dataset in ("exit", "both"):
        df = build_exit_dataset(args.db)
        out = out_dir / "exit.parquet"
        try:
            df.to_parquet(out, index=False)
            logger.info("Wrote %s", out)
        except Exception:
            out = out_dir / "exit.csv"
            df.to_csv(out, index=False)
            logger.info("Wrote %s (parquet unavailable)", out)


if __name__ == "__main__":
    sys.exit(main())
