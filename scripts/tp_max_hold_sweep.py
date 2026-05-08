#!/usr/bin/env python3
# scripts/tp_max_hold_sweep.py
"""TP × max_hold sweep — the only exit knob still untested.

Tier-A sweeps (inactivity, trailing distance/activation) all said
"live config is local optimum." But none of them varied TP — the
take-profit threshold that closes positions at +N % above entry.
With moonshots reaching +100..+780 % in live data, TP=30 caps the
upside. Trailing is dead code (proven in trailing_activation_sweep)
because TP fires first. So the actual lever is **TP itself**.

Hypothesis: raising TP gives moonshots room to develop. Trade-off:
positions that briefly touched +30 % and then crashed lose more.
Sweep tells us which side wins on aggregate.

Grid: TP × max_hold, 5 × 3 = 15 combos.
* TP: 30 (live), 50, 75, 100, 150
* max_hold: 90, 120 (live), 180

Other knobs pinned to live values. Dynamic max_hold quantile head
disabled in the simulator (same reason as tier_a_sweep — it clamps
exits to ~30 s).

Output: ``data/ml/tp_max_hold_sweep.json`` + Pareto-frontier table.

Verdict logic: candidate (TP, max_hold) is "deploy-worthy" if
``total_sol >= live_total_sol`` AND ``wr_pct >= live_wr_pct - 1.0``.
Both clauses must hold. Anything else: stay with live.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
from itertools import product
from pathlib import Path

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

LIVE_TP_PCT = 30.0
LIVE_SL_PCT = 15.0
LIVE_MAX_HOLD_SEC = 120.0
LIVE_INACTIVITY_SEC = 60.0
LIVE_TRAIL_ACTIVATION = 50.0
LIVE_TRAIL_DISTANCE = 50.0

TP_GRID = [30.0, 50.0, 75.0, 100.0, 150.0]
MH_GRID = [90.0, 120.0, 180.0]

DAYS = 14
LIMIT = 5000
HORIZON = 600.0   # 10 min — gives moonshots room to develop
SIZE_SOL = 0.10

OUT_JSON = Path(__file__).resolve().parents[1] / "data" / "ml" / "tp_max_hold_sweep.json"


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
    logger.info("Fetched %d post-entry trades (horizon=%.0fs) for %d mints in %.1fs",
                fetched, HORIZON, len(mints), time.time() - t0)

    mints_with_trades = [m for m, ts in trades_by_mint.items() if ts]
    logger.info("Mints with post-entry trades: %d / %d", len(mints_with_trades), len(mints))

    combos = list(product(TP_GRID, MH_GRID))
    logger.info("Sweeping %d (TP, max_hold) combos × %d positions",
                len(combos), len(mints_with_trades))

    results: list[dict] = []
    t0 = time.time()
    for tp, mh in combos:
        cfg = get_config()
        cfg.exit_take_profit_pct = float(tp)
        cfg.exit_take_profit_enabled = True
        cfg.exit_hard_stop_loss_pct = LIVE_SL_PCT
        cfg.exit_max_hold_seconds = float(mh)
        cfg.exit_max_hold_dynamic = False  # disable simulator artifact
        cfg.exit_inactivity_seconds = LIVE_INACTIVITY_SEC
        cfg.exit_trailing_stop_enabled = True
        cfg.exit_trailing_stop_activation_pct = LIVE_TRAIL_ACTIVATION
        cfg.exit_trailing_stop_distance_pct = LIVE_TRAIL_DISTANCE
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
        sorted_pnls = sorted(pnls, reverse=True)
        top_k = max(1, int(0.05 * n))
        tail_avg = sum(sorted_pnls[:top_k]) / top_k
        total_sol = sum(pnls) * SIZE_SOL / 100.0
        tp_rate = reasons.get("take_profit", 0) / n
        trail_rate = reasons.get("trailing_stop", 0) / n
        results.append({
            "tp_pct": tp,
            "max_hold_sec": mh,
            "n": n,
            "avg_pnl_pct": round(avg, 3),
            "wr_pct": round(wr * 100.0, 2),
            "avg_winner_pct": round(avg_winner, 3),
            "tail_5pct_avg": round(tail_avg, 3),
            "total_sol": round(total_sol, 4),
            "tp_fire_rate": round(tp_rate * 100.0, 2),
            "trail_fire_rate": round(trail_rate * 100.0, 2),
            "exit_reasons": reasons,
        })
        logger.info("TP=%3.0f MH=%3.0f → avg=%+5.2f%% WR=%4.1f%% tail5=%+6.1f%% tp_fire=%4.1f%% total=%+5.3f SOL",
                    tp, mh, avg, wr * 100, tail_avg, tp_rate * 100, total_sol)
    logger.info("Sweep done in %.1fs", time.time() - t0)

    OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    OUT_JSON.write_text(json.dumps(results, indent=2))
    logger.info("Wrote results to %s", OUT_JSON)

    actual_pnls = [actual_pnl_pct[m] for m in mints_with_trades]
    actual_total_sol = sum(actual_pnls) * SIZE_SOL / 100.0
    actual_wr = sum(1 for p in actual_pnls if p > 0) / len(actual_pnls)
    print(f"\n--- ACTUAL (live) baseline on same {len(mints_with_trades)} positions ---")
    print(f"  total_sol={actual_total_sol:+.4f}  avg_pnl={sum(actual_pnls) / len(actual_pnls):+.2f}%  WR={actual_wr * 100:.1f}%")

    # Sort by total_sol; mark live combo and any candidate that beats it
    live_combo = next((r for r in results if r["tp_pct"] == LIVE_TP_PCT and r["max_hold_sec"] == LIVE_MAX_HOLD_SEC), None)
    print(f"\n--- TOP combos (live = TP=30, MH=120) ---")
    results.sort(key=lambda r: -r["total_sol"])
    print(f"{'TP':>4} {'MH':>5} {'avg%':>8} {'WR%':>6} {'avgW%':>8} {'tail5%':>8} {'TP%':>6} {'totSOL':>9}")
    for r in results:
        marker = ""
        if r["tp_pct"] == LIVE_TP_PCT and r["max_hold_sec"] == LIVE_MAX_HOLD_SEC:
            marker = "  ←LIVE"
        elif live_combo and (r["total_sol"] > live_combo["total_sol"] and r["wr_pct"] >= live_combo["wr_pct"] - 1.0):
            marker = "  ★ CANDIDATE"
        print(f"{r['tp_pct']:>4.0f} {r['max_hold_sec']:>5.0f} "
              f"{r['avg_pnl_pct']:>+8.3f} {r['wr_pct']:>6.2f} "
              f"{r['avg_winner_pct']:>+8.2f} {r['tail_5pct_avg']:>+8.2f} "
              f"{r['tp_fire_rate']:>6.2f} "
              f"{r['total_sol']:>+9.4f}{marker}")

    # Verdict
    print()
    if live_combo:
        better = [r for r in results
                  if r["total_sol"] > live_combo["total_sol"]
                  and r["wr_pct"] >= live_combo["wr_pct"] - 1.0
                  and not (r["tp_pct"] == LIVE_TP_PCT and r["max_hold_sec"] == LIVE_MAX_HOLD_SEC)]
        if better:
            best = better[0]
            print(f"VERDICT: {len(better)} candidates beat live on PnL with WR ≥ live − 1pp.")
            print(f"  BEST: TP={best['tp_pct']:.0f} MH={best['max_hold_sec']:.0f} → "
                  f"total_sol={best['total_sol']:+.4f} (Δ={best['total_sol'] - live_combo['total_sol']:+.4f}) "
                  f"WR={best['wr_pct']:.2f}% (live={live_combo['wr_pct']:.2f}%)")
            print(f"  Recommendation: validate via regression_gate, then deploy.")
        else:
            print(f"VERDICT: No combo beats live (TP=30 MH=120, total_sol={live_combo['total_sol']:+.4f}).")
            print(f"  Recommendation: KEEP CURRENT CONFIG. Don't change exit knobs.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
