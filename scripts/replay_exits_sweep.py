# scripts/replay_exits_sweep.py
"""Replay exits on actually-entered paper trades.

For each closed paper_trade (where the bot actually opened a position),
re-run simulate_exit on the post-entry trade stream with a grid of
(TP, SL, max_hold). Reports total PnL and exit-reason distribution per
combo. Unlike exit_label_sweep.py (which simulates entries everywhere),
this replays only positions the bot really held — the realistic
counterfactual: "had we used these exit knobs, what would we have made?"

Output: data/ml/replay_exits_sweep.json + sorted leaderboard.

Usage:
    PYTHONPATH=. .venv/bin/python scripts/replay_exits_sweep.py
"""

from __future__ import annotations

import json
import logging
import os
import time
from itertools import product
from pathlib import Path

import psycopg2
import psycopg2.extras

from pulse_bot.config import get_config
from pulse_bot.db import _resolve_dsn
from pulse_bot.ml.simulate_exit import simulate_exit_batch
from pulse_bot.models import Trade

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
# Suppress per-instantiation "No exit model" spam from PaperTradeRunner —
# 175 combos × 700 positions = 122k log lines that drown the real output.
logging.getLogger("pulse_bot.ml.policy._main").setLevel(logging.ERROR)
logging.getLogger("pulse_bot.ml.policy").setLevel(logging.ERROR)
logger = logging.getLogger(__name__)

# Tighter grid centered on what live data suggests:
#  - winners are near +50-65% (sell_pressure + near_graduation rows)
#  - losers don't hit SL=30% (avg loser -5.58%) so SL isn't gating
#  - dead_token=92.9% means most positions just rot — shorter max_hold
#    might cut the rot before it deepens.
TP_GRID = [20, 30, 50, 70]
SL_GRID = [8, 12, 20, 30]
MH_GRID = [90, 120, 180, 300]

OUT_JSON = Path("data/ml/replay_exits_sweep.json")


def main() -> None:
    conn = psycopg2.connect(_resolve_dsn(os.environ.get("PULSE_PG_DSN")))
    cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)

    # Load closed paper_trades that actually entered.
    cur.execute(
        """SELECT id, mint, entry_time, entry_price, entry_mcap_sol,
                  exit_time, pnl_pct, pnl_sol, exit_reason, entry_type
           FROM paper_trades
           WHERE exit_time IS NOT NULL
             AND entry_price > 0
             AND entry_time > extract(epoch FROM now()) - 7*86400
           ORDER BY entry_time DESC
           LIMIT 2000"""
    )
    rows = cur.fetchall()
    logger.info("Loaded %d closed paper_trades from last 7d", len(rows))
    if not rows:
        return

    # Bulk-fetch post-entry trades for these mints.
    mints = [r["mint"] for r in rows]
    entries: dict[str, tuple[float, float]] = {
        r["mint"]: (float(r["entry_time"]), float(r["entry_price"])) for r in rows
    }
    actual_pnl: dict[str, float] = {
        r["mint"]: float(r["pnl_pct"] or 0.0) for r in rows
    }

    trades_by_mint: dict[str, list[Trade]] = {m: [] for m in mints}
    chunk = 500
    fetched = 0
    t0 = time.time()
    cur2 = conn.cursor()
    for i in range(0, len(mints), chunk):
        ck = mints[i : i + chunk]
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
            # Only post-entry trades with reasonable horizon.
            if ts < entry_ts or ts > entry_ts + max(MH_GRID) + 60:
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
    cur.close()
    conn.close()
    logger.info("Fetched %d post-entry trades for %d mints in %.1fs",
                fetched, len(mints), time.time() - t0)

    # Filter to mints with at least 1 post-entry trade
    mints_with_trades = [m for m, ts in trades_by_mint.items() if ts]
    logger.info("Mints with post-entry trades: %d / %d", len(mints_with_trades), len(mints))

    # Run sweep
    combos = list(product(TP_GRID, SL_GRID, MH_GRID))
    logger.info("Sweeping %d combos × %d positions...", len(combos), len(mints_with_trades))

    results: list[dict] = []
    t0 = time.time()
    for idx, (tp, sl, mh) in enumerate(combos, 1):
        cfg = get_config()
        cfg.exit_take_profit_pct = float(tp)
        cfg.exit_hard_stop_loss_pct = float(sl)
        cfg.exit_max_hold_seconds = float(mh)
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
        losers = [p for p in pnls if p <= 0]
        avg_w = sum(winners) / len(winners) if winners else 0.0
        avg_l = sum(losers) / len(losers) if losers else 0.0
        # Total simulated PnL in SOL (assuming 0.01 SOL per trade — same as live)
        total_sol = sum(pnls) * 0.01 / 100.0  # pnls are in %, convert
        r = {
            "tp": tp, "sl": sl, "mh": mh, "n": n,
            "avg_pnl_pct": round(avg, 3),
            "wr": round(wr, 4),
            "tp_rate": round(reasons.get("take_profit", 0) / n, 4),
            "sl_rate": round(reasons.get("hard_stop", 0) / n, 4),
            "timeout_rate": round(reasons.get("timeout", 0) / n, 4),
            "trail_rate": round(reasons.get("trailing_stop", 0) / n, 4),
            "sell_pressure_rate": round(reasons.get("sell_pressure", 0) / n, 4),
            "avg_winner": round(avg_w, 3),
            "avg_loser": round(avg_l, 3),
            "total_sol_at_001": round(total_sol, 4),
            "reasons": reasons,
        }
        results.append(r)
        if idx % 20 == 0:
            logger.info("[%d/%d] last: TP=%d SL=%d MH=%d avg=%+5.2f%% WR=%.1f%% total=%+5.3f SOL",
                        idx, len(combos), tp, sl, mh, avg, wr*100, total_sol)
    logger.info("Sweep done in %.1fs", time.time() - t0)

    OUT_JSON.write_text(json.dumps(results, indent=2))
    logger.info("Wrote results to %s", OUT_JSON)

    # Compare to actual realized PnL
    actual_pnls = [actual_pnl[m] for m in mints_with_trades]
    actual_total_sol = sum(actual_pnls) * 0.01 / 100.0
    actual_wr = sum(1 for p in actual_pnls if p > 0) / len(actual_pnls)
    print(f"\n--- ACTUAL (live) baseline on same {len(mints_with_trades)} positions ---")
    print(f"  total: {actual_total_sol:+.4f} SOL  avg: {sum(actual_pnls)/len(actual_pnls):+.2f}%  WR: {actual_wr*100:.1f}%")

    print(f"\n--- TOP 15 by total simulated PnL ---")
    results.sort(key=lambda r: -r["total_sol_at_001"])
    print(f"{'TP':>4} {'SL':>4} {'MH':>5} {'avgPnL%':>8} {'WR%':>6} {'TPr%':>6} {'SLr%':>6} {'TOr%':>6} {'avgW%':>7} {'avgL%':>7} {'totSOL':>8}")
    for r in results[:15]:
        print(f"{r['tp']:>4.0f} {r['sl']:>4.0f} {r['mh']:>5.0f} "
              f"{r['avg_pnl_pct']:>+8.2f} {r['wr']*100:>6.2f} "
              f"{r['tp_rate']*100:>6.2f} {r['sl_rate']*100:>6.2f} "
              f"{r['timeout_rate']*100:>6.2f} "
              f"{r['avg_winner']:>+7.2f} {r['avg_loser']:>+7.2f} "
              f"{r['total_sol_at_001']:>+8.4f}")


if __name__ == "__main__":
    main()
