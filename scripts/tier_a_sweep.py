#!/usr/bin/env python3
# scripts/tier_a_sweep.py
"""Tier-A optimization sweep: ``exit_inactivity_seconds`` and
``exit_trailing_stop_distance_pct``.

These two knobs were identified in the 2026-05-07 codex review as
the highest-EV / lowest-regression-risk levers:

* **inactivity** targets the 235 fee-bleed trades (~−1.6 SOL/25h).
  Faster exit on stagnation should preserve the same winners while
  reducing −0.007 SOL/trade per stagnant position.

* **trailing distance** targets the moonshot tail. Current 50 %
  distance gives back half of every peak above the +50 % activation
  threshold; tightening to 20-30 % could lift average winner by
  20-30 %.

Other knobs (TP, SL, max_hold) are already optimized via
:mod:`scripts.replay_exits_sweep`; we hold them at the live values
that produce the current paper PnL.

Output: ``data/ml/tier_a_sweep.json`` (full grid) + a Pareto-frontier
table to stdout.

Usage::

    PYTHONPATH=. .venv/bin/python scripts/tier_a_sweep.py
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
from itertools import product
from pathlib import Path

# Keep the noisy "no exit_model" line from PaperTradeRunner out of
# the sweep output — 16 combos × 1k+ positions = 16k log lines.
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logging.getLogger("pulse_bot.ml.policy._main").setLevel(logging.ERROR)
logging.getLogger("pulse_bot.ml.policy").setLevel(logging.ERROR)
logger = logging.getLogger(__name__)

import psycopg2
import psycopg2.extras

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pulse_bot.config import get_config
from pulse_bot.db import _resolve_dsn
from pulse_bot.ml.simulate_exit import simulate_exit_batch
from pulse_bot.models import Trade

# Keep TP / SL / max_hold pinned to the live values — they're our
# regression baseline. We're not searching here; we're targeting two
# specific knobs on top of "everything else as it is".
LIVE_TP_PCT = 30.0
LIVE_SL_PCT = 15.0
LIVE_MAX_HOLD_SEC = 120.0

INACTIVITY_GRID = [15.0, 30.0, 45.0, 60.0]
TRAILING_GRID = [20.0, 30.0, 40.0, 50.0]

DAYS = 14
LIMIT = 5000
HORIZON = float(LIVE_MAX_HOLD_SEC + 60.0)

OUT_JSON = Path(__file__).resolve().parents[1] / "data" / "ml" / "tier_a_sweep.json"


def main() -> int:
    dsn = _resolve_dsn(os.environ.get("PULSE_PG_DSN"))
    conn = psycopg2.connect(dsn)
    cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
    cur.execute(
        """SELECT id, mint, entry_time, entry_price, exit_time,
                  pnl_pct, pnl_sol, exit_reason
           FROM paper_trades
           WHERE exit_time IS NOT NULL
             AND entry_price > 0
             AND entry_time > extract(epoch FROM now()) - %s * 86400
           ORDER BY entry_time DESC
           LIMIT %s""",
        (DAYS, LIMIT),
    )
    rows = [dict(r) for r in cur.fetchall()]
    cur.close()
    logger.info("Loaded %d closed paper_trades from last %dd", len(rows), DAYS)
    if not rows:
        return 1

    mints = [r["mint"] for r in rows]
    entries = {r["mint"]: (float(r["entry_time"]), float(r["entry_price"])) for r in rows}
    actual_pnl_pct = {r["mint"]: float(r["pnl_pct"] or 0.0) for r in rows}

    trades_by_mint: dict[str, list[Trade]] = {m: [] for m in mints}
    cur2 = conn.cursor()
    chunk = 500
    fetched = 0
    t0 = time.time()
    for i in range(0, len(mints), chunk):
        ck = mints[i: i + chunk]
        cur2.execute(
            """SELECT mint, timestamp, tx_type, sol_amount, token_amount,
                      market_cap_sol, v_sol_in_bonding_curve, wallet
               FROM trades
               WHERE mint = ANY(%s::text[]) AND market_cap_sol > 0
               ORDER BY mint, timestamp""",
            (ck,),
        )
        for r in cur2:
            mint = r[0]
            ts = float(r[1] or 0.0)
            entry_ts = entries.get(mint, (0.0, 0.0))[0]
            if ts < entry_ts or ts > entry_ts + HORIZON:
                continue
            trades_by_mint[mint].append(
                Trade(
                    mint=mint, wallet=r[7] or "", tx_type=r[2],
                    sol_amount=float(r[3] or 0.0),
                    token_amount=float(r[4] or 0.0),
                    new_token_balance=0.0, bonding_curve_key="",
                    v_sol_in_bonding_curve=float(r[6] or 0.0),
                    v_tokens_in_bonding_curve=0.0,
                    market_cap_sol=float(r[5] or 0.0),
                    timestamp=ts,
                )
            )
            fetched += 1
    cur2.close()
    conn.close()
    logger.info("Fetched %d post-entry trades for %d mints in %.1fs",
                fetched, len(mints), time.time() - t0)

    mints_with_trades = [m for m, ts in trades_by_mint.items() if ts]
    logger.info("Mints with post-entry trades: %d / %d", len(mints_with_trades), len(mints))

    combos = list(product(INACTIVITY_GRID, TRAILING_GRID))
    logger.info("Sweeping %d (inactivity, trailing) combos × %d positions",
                len(combos), len(mints_with_trades))

    results: list[dict] = []
    t0 = time.time()
    for inactivity, trail_dist in combos:
        cfg = get_config()
        cfg.exit_take_profit_pct = LIVE_TP_PCT
        cfg.exit_hard_stop_loss_pct = LIVE_SL_PCT
        cfg.exit_max_hold_seconds = LIVE_MAX_HOLD_SEC
        cfg.exit_inactivity_seconds = float(inactivity)
        cfg.exit_trailing_stop_distance_pct = float(trail_dist)
        cfg.exit_trailing_stop_enabled = True
        out = simulate_exit_batch(
            cfg,
            {m: trades_by_mint[m] for m in mints_with_trades},
            {m: entries[m] for m in mints_with_trades},
        )
        if not out:
            continue
        reasons: dict[str, int] = {}
        pnls: list[float] = []
        for mint, res in out.items():
            reasons[res.exit_reason] = reasons.get(res.exit_reason, 0) + 1
            pnls.append(float(res.pnl_pct or 0.0))
        n = len(out)
        avg = sum(pnls) / n
        wr = sum(1 for p in pnls if p > 0) / n
        winners = [p for p in pnls if p > 0]
        avg_winner = sum(winners) / len(winners) if winners else 0.0
        # Position size assumed at 0.03 SOL — current live default.
        total_sol = sum(pnls) * 0.03 / 100.0
        # Tail capture: average of top 5 % winners.
        sorted_pnls = sorted(pnls, reverse=True)
        top_k = max(1, int(0.05 * n))
        tail_avg = sum(sorted_pnls[:top_k]) / top_k
        results.append({
            "inactivity_sec": inactivity,
            "trailing_pct": trail_dist,
            "n": n,
            "avg_pnl_pct": round(avg, 3),
            "wr_pct": round(wr * 100.0, 2),
            "avg_winner_pct": round(avg_winner, 3),
            "tail_5pct_avg": round(tail_avg, 3),
            "total_sol": round(total_sol, 4),
            "exit_reasons": reasons,
        })
        logger.info("inactivity=%.0fs trail=%.0f%% → avg=%+5.2f%% WR=%.1f%% tail5=%+6.1f%% total=%+5.3f SOL",
                    inactivity, trail_dist, avg, wr * 100, tail_avg, total_sol)
    logger.info("Sweep done in %.1fs", time.time() - t0)

    OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    OUT_JSON.write_text(json.dumps(results, indent=2))
    logger.info("Wrote results to %s", OUT_JSON)

    actual_pnls = [actual_pnl_pct[m] for m in mints_with_trades]
    actual_total_sol = sum(actual_pnls) * 0.03 / 100.0
    actual_wr = sum(1 for p in actual_pnls if p > 0) / len(actual_pnls)
    print(f"\n--- ACTUAL (live) baseline on same {len(mints_with_trades)} positions ---")
    print(f"  total_sol={actual_total_sol:+.4f}  avg_pnl={sum(actual_pnls) / len(actual_pnls):+.2f}%  WR={actual_wr * 100:.1f}%")

    print(f"\n--- TOP combos by total_sol (live config = inactivity=60, trail=50) ---")
    results.sort(key=lambda r: -r["total_sol"])
    print(f"{'inact':>6} {'trail':>6} {'avg%':>8} {'WR%':>6} {'avgWin%':>8} {'tail5%':>8} {'totSOL':>8}")
    for r in results:
        marker = "  ←LIVE" if (r["inactivity_sec"] == 60.0 and r["trailing_pct"] == 50.0) else ""
        print(f"{r['inactivity_sec']:>6.0f} {r['trailing_pct']:>6.0f} "
              f"{r['avg_pnl_pct']:>+8.3f} {r['wr_pct']:>6.2f} "
              f"{r['avg_winner_pct']:>+8.2f} {r['tail_5pct_avg']:>+8.2f} "
              f"{r['total_sol']:>+8.4f}{marker}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
