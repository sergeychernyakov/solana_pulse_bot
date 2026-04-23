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
import sqlite3
import sys
from pathlib import Path

import pandas as pd

from pulse_bot.ml.features import SCORER_FEATURES

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

    conn = sqlite3.connect(db_path)
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
          AND s.scored_at <= ?
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
               rug_count AS creator_rug_count,
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
            "hc_30",
            "top1_120",
            "top5_120",
            "hc_120",
            "top1_delta",
            "top5_delta",
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
            "creator_rug_count",
            "creator_graduated_count",
        ]
        for c in creator_cols:
            if c in base.columns:
                base[c] = base[c].fillna(0.0)

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
    from pulse_bot.core import calc_realized_pnl_pct

    logger.info("Computing realized-PnL labels (fees+slippage baked in)...")
    labels: list[int | None] = []
    realized_pnls: list[float | None] = []
    for _, row in base.iterrows():
        scored_at = row["scored_at"]
        cutoff = scored_at + label_horizon_sec
        trades = conn.execute(
            """SELECT timestamp, tx_type, sol_amount, token_amount,
                      market_cap_sol
               FROM trades
               WHERE mint = ? AND timestamp BETWEEN ? AND ?
                 AND market_cap_sol > 0
               ORDER BY timestamp ASC""",
            (row["mint"], scored_at, cutoff),
        ).fetchall()
        if not trades:
            # DOA — no post-scoring trades. Drop (was tried with synthetic
            # label=0 but flooded training with 99%+ losers; model learned
            # DOA-pattern instead of winner-pattern). Keep original drop.
            labels.append(None)
            realized_pnls.append(None)
            continue
        entry_mc = None
        for t in trades:
            if t[1] == "buy" and t[3] and t[2]:
                entry_mc = t[4]
                break
        if not entry_mc or entry_mc <= 0:
            labels.append(None)
            realized_pnls.append(None)
            continue
        tp_mc = entry_mc * (1.0 + label_tp_pct / 100.0)
        sl_mc = entry_mc * (1.0 - label_sl_pct / 100.0)
        exit_mc: float | None = None
        for t in trades:
            mc = t[4]
            if not mc:
                continue
            if mc >= tp_mc or mc <= sl_mc:
                exit_mc = mc
                break
        if exit_mc is None:
            exit_mc = trades[-1][4] or entry_mc
        realized_pnl = calc_realized_pnl_pct(
            entry_price=entry_mc,
            exit_price=exit_mc,
            buy_amount_sol=label_buy_amount_sol,
            buy_slip_pct=label_buy_slip,
            sell_slip_pct=label_sell_slip,
            num_sell_legs=1,
        )
        labels.append(1 if realized_pnl > 0 else 0)
        realized_pnls.append(float(realized_pnl))
    conn.close()

    base["label"] = labels
    # realized_pnl_pct is a SIDE column for honest economic backtest.
    # Must be excluded from feature list in train.py — it is the label
    # magnitude and would be perfect label leakage if trained on.
    base["realized_pnl_pct"] = realized_pnls
    base = base.dropna(subset=["label"])
    base["label"] = base["label"].astype(int)
    base["realized_pnl_pct"] = base["realized_pnl_pct"].astype(float)
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
    conn = sqlite3.connect(db_path)
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

    rows: list[dict] = []
    for _, c in candidates.iterrows():
        mint, entry_ts = c["mint"], c["entry_ts"]
        trades = pd.read_sql_query(
            """SELECT timestamp, tx_type, sol_amount, token_amount,
                      market_cap_sol, v_sol_in_bonding_curve, wallet
               FROM trades WHERE mint = ? AND timestamp >= ?
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
