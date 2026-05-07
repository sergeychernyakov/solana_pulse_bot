# scripts/exit_label_sweep.py
"""Fixed-entries sweep over (TP, SL, max_hold) for label-quality optimization.

Unlike pulse_bot.optimizer (which re-applies entry rules per combo and
ends up with ~12 trades when rules are tight), this script holds entries
fixed to "every token's first post-scoring buy" and only varies exit
parameters. Output: per-combo exit_reason histogram + avg_pnl_pct + WR.

Goal: pick (TP, SL, max_hold) where ``take_profit`` + ``hard_stop`` rates
are non-trivial (>~5% combined), so simulate_exit produces meaningful
labels for ML training. Currently TP=100%/SL=15%/max_hold=300s yields
take_profit ≈ 0.002% (effectively dead).

Usage:
    PYTHONPATH=. .venv/bin/python scripts/exit_label_sweep.py
"""

from __future__ import annotations

import json
import logging
import time
from itertools import product
from pathlib import Path

import pandas as pd
import psycopg2
import psycopg2.extras

from pulse_bot.config import get_config
from pulse_bot.db import _resolve_dsn
from pulse_bot.ml.simulate_exit import simulate_exit_batch
from pulse_bot.models import Trade

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

TP_GRID = [30, 50, 70, 100]
SL_GRID = [8, 12, 15, 20]
MH_GRID = [120, 180, 300, 600]

ENTRY_PARQUET = Path("data/ml/entry.parquet")
OUT_JSON = Path("data/ml/exit_label_sweep.json")


def load_entries_and_trades() -> tuple[
    dict[str, list[Trade]], dict[str, tuple[float, float]]
]:
    df = pd.read_parquet(ENTRY_PARQUET)
    logger.info("Loaded entry.parquet: %d rows", len(df))
    if "scored_at" not in df.columns or "mint" not in df.columns:
        raise SystemExit("entry.parquet missing required columns")
    df = df[df["scored_at"].notna()].copy()
    mints = df["mint"].astype(str).tolist()
    scored_map = {
        str(r["mint"]): float(r["scored_at"]) for _, r in df.iterrows()
    }
    cfg = get_config()
    window_sec = max(600.0, float(cfg.exit_max_hold_seconds) + 60.0, max(MH_GRID) + 60.0)

    conn = psycopg2.connect(_resolve_dsn("pulse_bot.db"))
    cur = conn.cursor()
    all_rows: dict[str, list[tuple]] = {m: [] for m in mints}
    chunk = 500
    t0 = time.time()
    fetched = 0
    for i in range(0, len(mints), chunk):
        ck = mints[i : i + chunk]
        cur.execute(
            """SELECT mint, timestamp, tx_type, sol_amount, token_amount,
                      market_cap_sol, v_sol_in_bonding_curve, wallet
               FROM trades
               WHERE mint = ANY(%s::text[]) AND market_cap_sol > 0
               ORDER BY mint, timestamp""",
            (ck,),
        )
        for r in cur:
            scored = scored_map.get(r[0])
            if scored is None:
                continue
            ts = float(r[1] or 0.0)
            if ts < scored or ts > scored + window_sec:
                continue
            all_rows[r[0]].append(r)
            fetched += 1
        if (i // chunk) % 10 == 0:
            logger.info(
                "  fetched %d trades / %d mints scanned (%.1fs)",
                fetched, i + len(ck), time.time() - t0
            )
    cur.close()
    conn.close()
    logger.info("Total fetched: %d trades across %d mints in %.1fs",
                fetched, len(mints), time.time() - t0)

    trades_by_mint: dict[str, list[Trade]] = {}
    entries_by_mint: dict[str, tuple[float, float]] = {}
    n_doa_no_trades = 0
    n_doa_no_entry = 0
    for mint in mints:
        rows = all_rows.get(mint) or []
        if not rows:
            n_doa_no_trades += 1
            continue
        entry_idx = None
        entry_price = None
        entry_ts = None
        for i, t in enumerate(rows):
            if t[2] == "buy" and t[4] and t[3]:
                entry_price = float(t[3]) / float(t[4])
                entry_ts = float(t[1])
                entry_idx = i
                break
        if entry_idx is None or not entry_price or entry_price <= 0:
            n_doa_no_entry += 1
            continue
        trades_by_mint[mint] = [
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
            for t in rows[entry_idx + 1 :]
        ]
        entries_by_mint[mint] = (entry_ts, entry_price)
    logger.info(
        "Built entries: %d viable, %d doa_no_trades, %d doa_no_entry",
        len(entries_by_mint), n_doa_no_trades, n_doa_no_entry,
    )
    return trades_by_mint, entries_by_mint


def run_combo(
    tp: float, sl: float, mh: float,
    trades_by_mint: dict[str, list[Trade]],
    entries_by_mint: dict[str, tuple[float, float]],
) -> dict:
    cfg = get_config()
    cfg.exit_take_profit_pct = float(tp)
    cfg.exit_hard_stop_loss_pct = float(sl)
    cfg.exit_max_hold_seconds = float(mh)

    out = simulate_exit_batch(cfg, trades_by_mint, entries_by_mint)
    reasons: dict[str, int] = {}
    pnls: list[float] = []
    for res in out.values():
        reasons[res.exit_reason] = reasons.get(res.exit_reason, 0) + 1
        pnls.append(float(res.pnl_pct or 0.0))
    n = len(out)
    if not n:
        return {"tp": tp, "sl": sl, "mh": mh, "n": 0}
    avg = sum(pnls) / n
    pos = sum(1 for p in pnls if p > 0)
    big_pos = sum(1 for p in pnls if p >= 30)
    big_neg = sum(1 for p in pnls if p <= -10)
    return {
        "tp": tp, "sl": sl, "mh": mh, "n": n,
        "avg_pnl_pct": round(avg, 3),
        "wr": round(pos / n, 4),
        "tp_rate": round(reasons.get("take_profit", 0) / n, 4),
        "sl_rate": round(reasons.get("hard_stop", 0) / n, 4),
        "timeout_rate": round(reasons.get("timeout", 0) / n, 4),
        "trail_rate": round(reasons.get("trailing_stop", 0) / n, 4),
        "big_pos_rate": round(big_pos / n, 4),
        "big_neg_rate": round(big_neg / n, 4),
        "reasons": reasons,
    }


def main() -> None:
    trades_by_mint, entries_by_mint = load_entries_and_trades()
    combos = list(product(TP_GRID, SL_GRID, MH_GRID))
    logger.info("Sweeping %d combos over %d entries...", len(combos), len(entries_by_mint))
    results: list[dict] = []
    t0 = time.time()
    for idx, (tp, sl, mh) in enumerate(combos, 1):
        r = run_combo(tp, sl, mh, trades_by_mint, entries_by_mint)
        results.append(r)
        logger.info(
            "[%2d/%d] TP=%3.0f SL=%2.0f MH=%4.0f n=%d avg_pnl=%+6.2f%% WR=%.2f%% "
            "TP_rate=%.2f%% SL_rate=%.2f%% timeout=%.2f%%",
            idx, len(combos), tp, sl, mh, r["n"], r["avg_pnl_pct"],
            r["wr"]*100, r["tp_rate"]*100, r["sl_rate"]*100, r["timeout_rate"]*100,
        )
    logger.info("Sweep done in %.1fs", time.time() - t0)

    OUT_JSON.write_text(json.dumps(results, indent=2))
    logger.info("Wrote results to %s", OUT_JSON)

    print("\n" + "=" * 100)
    print("TOP 10 by avg_pnl_pct (TP_rate ≥ 5% AND SL_rate ≥ 2%):")
    print("=" * 100)
    filtered = [r for r in results if r["tp_rate"] >= 0.05 and r["sl_rate"] >= 0.02]
    if not filtered:
        print("No combos meet the TP_rate ≥ 5% AND SL_rate ≥ 2% filter — relaxing.")
        filtered = results
    filtered.sort(key=lambda r: -r["avg_pnl_pct"])
    print(f"{'TP':>4} {'SL':>4} {'MH':>5} {'N':>6} {'avgPnL%':>8} {'WR%':>6} "
          f"{'TPr%':>6} {'SLr%':>6} {'TOr%':>6} {'+30%':>6} {'-10%':>6}")
    for r in filtered[:10]:
        print(f"{r['tp']:>4.0f} {r['sl']:>4.0f} {r['mh']:>5.0f} {r['n']:>6} "
              f"{r['avg_pnl_pct']:>+8.2f} {r['wr']*100:>6.2f} "
              f"{r['tp_rate']*100:>6.2f} {r['sl_rate']*100:>6.2f} "
              f"{r['timeout_rate']*100:>6.2f} "
              f"{r['big_pos_rate']*100:>6.2f} {r['big_neg_rate']*100:>6.2f}")


if __name__ == "__main__":
    main()
