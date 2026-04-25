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

from pulse_bot.db import _resolve_dsn
from pulse_bot.ml.features import SCORER_FEATURES


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
    base = base.merge(h120, on="mint", how="left")
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
        creator_cols = [
            "creator_age_days",
            "creator_median_peak_mc_sol",
            "creator_inter_token_interval_sec",
            "creator_total_prior_tokens",
            "creator_balance_sol",
            "creator_graduated_count",
        ]
        for c in creator_cols:
            if c in base.columns:
                base[c] = base[c].fillna(0.0)

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
        compute_top3_buyer_wallets,
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
    for _, row in base.iterrows():
        mint = row["mint"]
        scored_at = float(row["scored_at"])
        trade_rows = _pg_exec(
            conn,
            """SELECT tx_type, wallet, sol_amount FROM trades
               WHERE mint = ? AND timestamp <= ?
               ORDER BY id ASC""",
            (mint, scored_at),
        ).fetchall()
        trades_as_dicts = [
            {"tx_type": tr[0], "wallet": tr[1], "sol_amount": tr[2]}
            for tr in trade_rows
        ]
        top3 = compute_top3_buyer_wallets(trades_as_dicts)
        stats_map: dict[str, dict] = {}
        if top3 and wa_row_count > 0:
            placeholders = ",".join("?" * len(top3))
            stats_cur = _pg_exec(
                conn,
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
                      AND mint != ?
                      AND last_trade_ts < ?
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
        feats = _extract_wallet_prior_features(stats_map, top3, scored_at)
        wallet_feat_rows.append(feats)
        # Track how many rows got non-NaN features for diagnostics.
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
    from pulse_bot.ml.simulate_exit import simulate_exit
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

    labels: list[int | None] = []
    realized_pnls: list[float | None] = []
    exit_reasons: list[str | None] = []
    for _, row in base.iterrows():
        scored_at = float(row["scored_at"])
        cutoff = scored_at + trade_window_sec
        trade_rows = _pg_exec(
            conn,
            """SELECT timestamp, tx_type, sol_amount, token_amount,
                      market_cap_sol, v_sol_in_bonding_curve, wallet
               FROM trades
               WHERE mint = ? AND timestamp BETWEEN ? AND ?
                 AND market_cap_sol > 0
               ORDER BY timestamp ASC""",
            (row["mint"], scored_at, cutoff),
        ).fetchall()
        if not trade_rows:
            # DOA — no post-scoring trades. 2026-04-24 codex diagnosis:
            # dropping these caused survivor bias (model trained on 1292
            # survivors, deployed on 96% DOA population → manifolds don't
            # overlap → live WR collapses + anti-correlation at high
            # proba). Fix: include DOA with label=0, realized_pnl=0 so
            # the model learns the DOA feature signature and decision
            # boundary at full population. Balance handled downstream via
            # scale_pos_weight (increases naturally) + optional stratified
            # undersample (controlled by env).
            labels.append(0)
            realized_pnls.append(0.0)
            exit_reasons.append("doa_no_trades")
            continue
        # First buy after scored_at defines the entry point (same
        # semantics the live pipeline uses: PaperTradeRunner opens on
        # first post-scoring buy with market_cap_sol > 0).
        entry_idx = None
        entry_price = None
        entry_ts = None
        for i, t in enumerate(trade_rows):
            ts, tx, sol_amt, tok_amt, mc, _vsol, _w = t
            if tx == "buy" and tok_amt and sol_amt:
                entry_price = float(sol_amt) / float(tok_amt)
                entry_ts = float(ts)
                entry_idx = i
                break
        if entry_idx is None or not entry_price or entry_price <= 0:
            # Had trades but no usable buy → functionally DOA for our
            # strategy (can't enter). Treat as label=0 same as DOA.
            labels.append(0)
            realized_pnls.append(0.0)
            exit_reasons.append("doa_no_entry")
            continue
        # Post-entry trades replayed through the exact exit logic.
        post_entry_trades = [
            Trade(
                mint=row["mint"],
                wallet=t[6] or "",
                tx_type=t[1],
                sol_amount=float(t[2] or 0.0),
                token_amount=float(t[3] or 0.0),
                new_token_balance=0.0,
                bonding_curve_key="",
                v_sol_in_bonding_curve=float(t[5] or 0.0),
                v_tokens_in_bonding_curve=0.0,
                market_cap_sol=float(t[4] or 0.0),
                timestamp=float(t[0]),
            )
            for t in trade_rows[entry_idx + 1 :]
        ]
        try:
            mr = simulate_exit(live_config, post_entry_trades, entry_ts, entry_price)
        except Exception as exc:
            logger.warning(
                "simulate_exit failed for %s (entry_ts=%.0f): %s — dropping row",
                row["mint"],
                entry_ts,
                exc,
            )
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
               t.created_at
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
