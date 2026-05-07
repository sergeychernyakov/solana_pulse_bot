# scripts/live_audit.py
"""Live paper-trades audit.

Reports realized PnL, WR, exit-reason distribution and (where the
regression head's encoded forecast is available) Spearman ρ between
predicted PnL and realized PnL on LIVE data.

The rho-on-test from train.py was 0.146 — we want to know whether the
model behaves the same on production traffic, or whether the live
distribution shifted enough that the ranking signal collapsed.

Encoding scheme (decision_service.apply_ml_override 2026-04-28):
    entry_score = round(reg_pnl_pct * 10) + 500
    decoded_pred = (entry_score - 500) / 10

Usage:
    PYTHONPATH=. .venv/bin/python scripts/live_audit.py [--hours 48]
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from typing import Any

import psycopg2
import psycopg2.extras

from pulse_bot.db import _resolve_dsn

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
logger = logging.getLogger(__name__)


def _spearman(xs: list[float], ys: list[float]) -> float:
    """Pure-python Spearman rho. Falls back to NaN on empty input."""
    n = len(xs)
    if n < 3:
        return float("nan")
    rx = _ranks(xs)
    ry = _ranks(ys)
    mx = sum(rx) / n
    my = sum(ry) / n
    num = sum((rx[i] - mx) * (ry[i] - my) for i in range(n))
    dx = sum((r - mx) ** 2 for r in rx) ** 0.5
    dy = sum((r - my) ** 2 for r in ry) ** 0.5
    if dx == 0 or dy == 0:
        return float("nan")
    return num / (dx * dy)


def _ranks(xs: list[float]) -> list[float]:
    indexed = sorted(range(len(xs)), key=lambda i: xs[i])
    ranks = [0.0] * len(xs)
    i = 0
    while i < len(indexed):
        j = i
        while j + 1 < len(indexed) and xs[indexed[j + 1]] == xs[indexed[i]]:
            j += 1
        avg = (i + j) / 2 + 1
        for k in range(i, j + 1):
            ranks[indexed[k]] = avg
        i = j + 1
    return ranks


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=int, default=48)
    args = ap.parse_args()

    cutoff_ts = time.time() - args.hours * 3600

    conn = psycopg2.connect(_resolve_dsn(os.environ.get("PULSE_PG_DSN")))
    cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
    cur.execute(
        """SELECT id, mint, symbol, entry_time, exit_time, entry_price,
                  exit_price, entry_mcap_sol, exit_mcap_sol,
                  pnl_sol, pnl_pct, exit_reason,
                  entry_type, entry_score, entry_buyer_number,
                  buy_amount_sol
           FROM paper_trades
           WHERE exit_time IS NOT NULL AND entry_time >= %s
           ORDER BY entry_time""",
        (cutoff_ts,),
    )
    rows = cur.fetchall()
    cur.close()
    conn.close()

    if not rows:
        print(f"No closed paper_trades in last {args.hours}h.")
        return

    print(f"\n=== LIVE AUDIT — last {args.hours}h ({len(rows)} closed trades) ===\n")

    total_pnl_sol = sum(float(r["pnl_sol"] or 0.0) for r in rows)
    pnls_pct = [float(r["pnl_pct"] or 0.0) for r in rows]
    winners = [p for p in pnls_pct if p > 0]
    losers = [p for p in pnls_pct if p <= 0]
    wr = len(winners) / len(rows)

    print(f"Total realized PnL:    {total_pnl_sol:+.4f} SOL")
    print(f"Win rate:              {wr*100:5.2f}% ({len(winners)} winners / {len(losers)} losers)")
    print(f"Avg PnL per trade:     {sum(pnls_pct)/len(pnls_pct):+.2f}%")
    if winners:
        print(f"Avg winner:            {sum(winners)/len(winners):+.2f}%")
    if losers:
        print(f"Avg loser:             {sum(losers)/len(losers):+.2f}%")
    if winners and losers:
        ev = wr * (sum(winners)/len(winners)) + (1-wr) * (sum(losers)/len(losers))
        print(f"EV per trade:          {ev:+.2f}%")

    # Exit reason breakdown
    print(f"\n--- Exit reasons ---")
    reason_pnl: dict[str, list[float]] = {}
    for r in rows:
        reason = str(r["exit_reason"] or "unknown")
        reason_pnl.setdefault(reason, []).append(float(r["pnl_pct"] or 0.0))
    for reason, pnls in sorted(reason_pnl.items(), key=lambda kv: -len(kv[1])):
        avg = sum(pnls) / len(pnls)
        wr_r = sum(1 for p in pnls if p > 0) / len(pnls)
        print(f"  {reason:<20} n={len(pnls):>4} ({len(pnls)/len(rows)*100:>5.1f}%)  "
              f"avg={avg:+7.2f}%  WR={wr_r*100:5.1f}%")

    # Entry type breakdown
    print(f"\n--- Entry types ---")
    type_stats: dict[str, list[float]] = {}
    for r in rows:
        t = str(r["entry_type"] or "?")
        type_stats.setdefault(t, []).append(float(r["pnl_pct"] or 0.0))
    for t, pnls in sorted(type_stats.items(), key=lambda kv: -len(kv[1])):
        avg = sum(pnls) / len(pnls)
        wr_t = sum(1 for p in pnls if p > 0) / len(pnls)
        print(f"  {t:<20} n={len(pnls):>4} ({len(pnls)/len(rows)*100:>5.1f}%)  "
              f"avg={avg:+7.2f}%  WR={wr_t*100:5.1f}%")

    # Reg-PnL forecast vs realized — only for ml_override entries
    # opened AFTER 2026-04-28 21:41:57 (when reg encoding shipped)
    encoded_cutoff_ts = 1761777717  # rich UTC restart time
    ml_rows = [
        r for r in rows
        if r["entry_type"] == "ml_override"
        and float(r["entry_time"] or 0) >= encoded_cutoff_ts
        and r["entry_score"] is not None
    ]
    if ml_rows:
        print(f"\n--- entry_model_reg LIVE accuracy ({len(ml_rows)} ml_override BUYs since reg-encoding) ---")
        preds = []
        reals = []
        for r in ml_rows:
            score = int(r["entry_score"])
            # Filter out legacy int(ml_cal*100) entries (range 1-100, not in [400,600] band typical for reg).
            if 1 <= score <= 999 and score != 0:
                pred = (score - 500) / 10.0
                preds.append(pred)
                reals.append(float(r["pnl_pct"] or 0.0))
        if preds and len(preds) >= 5:
            rho = _spearman(preds, reals)
            print(f"  Spearman ρ:           {rho:+.4f} (test was 0.146)")
            print(f"  Predicted PnL range:  [{min(preds):+.2f}%, {max(preds):+.2f}%]  median={sorted(preds)[len(preds)//2]:+.2f}%")
            print(f"  Realized PnL range:   [{min(reals):+.2f}%, {max(reals):+.2f}%]  median={sorted(reals)[len(reals)//2]:+.2f}%")
            # Predicted top quartile vs bottom quartile realized PnL
            paired = sorted(zip(preds, reals))
            q = max(1, len(paired) // 4)
            top_real = [p[1] for p in paired[-q:]]
            bot_real = [p[1] for p in paired[:q]]
            print(f"  Top-25% predicted → realized avg: {sum(top_real)/len(top_real):+.2f}% ({len(top_real)} trades)")
            print(f"  Bot-25% predicted → realized avg: {sum(bot_real)/len(bot_real):+.2f}% ({len(bot_real)} trades)")
        else:
            print(f"  Not enough reg-encoded trades yet (have {len(preds)}, need ≥5).")

    # Recent 24h vs prior 24h split
    if args.hours >= 48:
        cut24 = time.time() - 24 * 3600
        recent = [r for r in rows if float(r["entry_time"] or 0) >= cut24]
        prior = [r for r in rows if float(r["entry_time"] or 0) < cut24]
        if recent and prior:
            print(f"\n--- 24h split ---")
            for label, batch in [("LAST 24h", recent), ("PRIOR 24h", prior)]:
                pn = [float(r["pnl_pct"] or 0.0) for r in batch]
                tp = sum(float(r["pnl_sol"] or 0.0) for r in batch)
                wr_b = sum(1 for p in pn if p > 0) / len(pn) if pn else 0
                print(f"  {label}: n={len(batch):>4}  total={tp:+.4f} SOL  "
                      f"avg={sum(pn)/len(pn):+.2f}%  WR={wr_b*100:.1f}%")

    print()


if __name__ == "__main__":
    main()
