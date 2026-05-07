#!/usr/bin/env python3
# scripts/slippage_analysis.py
"""Slippage-aware re-evaluation of paper / sweep PnL.

Paper PnL assumes perfect fills at the observed market price. Real
pump.fun execution pays a curve-impact toll on every entry and every
exit. For 0.03 SOL positions on typical curves the toll is 1-3 % per
side; bigger sizes scale ~ √(size_sol) (Uniswap-style invariant).

Two modes:

* ``--mode=paper`` — pull live ``paper_trades`` since the
  confidence-gate fix and recompute PnL with slippage applied.
  Tells you how the *real* live PnL would differ from the *paper*
  PnL on the trades the bot has actually opened.

* ``--mode=sweep`` — load ``data/ml/tier_a_sweep.json`` (or another
  sweep) and recompute every combo's total PnL with slippage. Lets
  you pick configs that survive realistic execution cost.

The slippage model (per side):

.. math::

    s = s_{base} \\cdot \\sqrt{size / size_{ref}}

where ``s_base = 1.5 %`` and ``size_ref = 0.03 SOL``. Calibration is
deliberately conservative — until we have a real-money fill log,
this is a model, not a measurement.

Usage::

    PYTHONPATH=. .venv/bin/python scripts/slippage_analysis.py --mode=paper
    PYTHONPATH=. .venv/bin/python scripts/slippage_analysis.py --mode=sweep --slippage 2.5
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import os
import sys
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("slippage")

REPO_ROOT = Path(__file__).resolve().parents[1]
SWEEP_PATH = REPO_ROOT / "data" / "ml" / "tier_a_sweep.json"

# Live sizing (one rung of the ladder; the bot is fixed at 0.03 today).
SIZE_REF_SOL = 0.03


def slip_factor(slippage_base_pct: float, size_sol: float) -> float:
    """Per-side slippage factor as a fraction (e.g. 0.02 = 2%).

    Sub-linear in size: doubling size only raises slippage by √2.
    """
    return (slippage_base_pct / 100.0) * math.sqrt(size_sol / SIZE_REF_SOL)


def adjust_pnl_pct(pnl_pct: float, slippage_pct_per_side: float) -> float:
    """Apply per-side slippage to a paper PnL percent.

    Entry pays ``+s`` (buy higher than mid), exit pays ``-s`` (sell
    lower). Mathematically::

        new_pnl = (1 + pnl/100) * (1 - s) / (1 + s) - 1

    For small ``s`` this is ≈ ``pnl − 2·s`` in pp.
    """
    s = slippage_pct_per_side / 100.0
    new_factor = (1.0 + pnl_pct / 100.0) * (1.0 - s) / (1.0 + s)
    return (new_factor - 1.0) * 100.0


def _aggregate_pct(pnls_pct: list[float], size_sol: float) -> dict:
    n = len(pnls_pct)
    if n == 0:
        return {"n": 0}
    pnls_sol = [size_sol * p / 100.0 for p in pnls_pct]
    wins = [p for p in pnls_pct if p > 0]
    return {
        "n": n,
        "wr_pct": round(100.0 * len(wins) / n, 2),
        "avg_pnl_pct": round(sum(pnls_pct) / n, 3),
        "sum_pnl_sol": round(sum(pnls_sol), 4),
        "avg_pnl_sol": round(sum(pnls_sol) / n, 5),
        "median_pnl_pct": round(sorted(pnls_pct)[n // 2], 3),
    }


def _paper_mode(slippage_base_pct: float) -> int:
    import psycopg2
    import psycopg2.extras
    sys.path.insert(0, str(REPO_ROOT))
    from pulse_bot.db import _resolve_dsn  # noqa: E402

    dsn = _resolve_dsn(os.environ.get("PULSE_PG_DSN"))
    conn = psycopg2.connect(dsn)
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
        cur.execute(
            """SELECT pnl_pct, buy_amount_sol
               FROM paper_trades
               WHERE pnl_sol IS NOT NULL
                 AND entry_time > 1778078400
               ORDER BY entry_time"""
        )
        rows = [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()

    if not rows:
        print("No closed trades since 2026-05-06 14:30 UTC.")
        return 1

    pnls_pct_paper: list[float] = []
    pnls_pct_real: list[float] = []
    sizes: list[float] = []
    for r in rows:
        size = float(r.get("buy_amount_sol") or SIZE_REF_SOL)
        slip = slip_factor(slippage_base_pct, size) * 100.0  # back to pct
        p = float(r["pnl_pct"] or 0.0)
        pnls_pct_paper.append(p)
        pnls_pct_real.append(adjust_pnl_pct(p, slip))
        sizes.append(size)

    avg_size = sum(sizes) / len(sizes)
    paper = _aggregate_pct(pnls_pct_paper, avg_size)
    real = _aggregate_pct(pnls_pct_real, avg_size)

    print(f"\nN={len(rows)} closed paper_trades since 2026-05-06 14:30 UTC, "
          f"avg position size {avg_size:.4f} SOL")
    print(f"  per-side slippage at avg size: ~{slip_factor(slippage_base_pct, avg_size) * 100:.2f}%")
    print()
    print(f"{'metric':<20} {'PAPER':>14} {'POST-SLIP':>14} {'Δ':>10}")
    for k in ("n", "wr_pct", "avg_pnl_pct", "sum_pnl_sol", "avg_pnl_sol", "median_pnl_pct"):
        a = paper[k]
        b = real[k]
        if isinstance(a, int) and isinstance(b, int):
            print(f"{k:<20} {a:>14} {b:>14} {b - a:>+10}")
        else:
            print(f"{k:<20} {float(a):>14.4f} {float(b):>14.4f} {float(b) - float(a):>+10.4f}")

    # Slippage hits per bucket — where the edge erodes most.
    print(f"\nSlippage damage by paper-PnL bucket:")
    buckets = [
        ("<-10%", lambda p: p < -10),
        ("-10..0%", lambda p: -10 <= p < 0),
        ("0..10%", lambda p: 0 <= p < 10),
        ("10..50%", lambda p: 10 <= p < 50),
        ("50..100%", lambda p: 50 <= p < 100),
        ("100+%", lambda p: p >= 100),
    ]
    for name, pred in buckets:
        idxs = [i for i, p in enumerate(pnls_pct_paper) if pred(p)]
        if not idxs:
            continue
        p_total_sol = sum(sizes[i] * pnls_pct_paper[i] / 100.0 for i in idxs)
        r_total_sol = sum(sizes[i] * pnls_pct_real[i] / 100.0 for i in idxs)
        print(f"  {name:>9}: n={len(idxs):>3}  paper={p_total_sol:>+8.4f}  "
              f"real={r_total_sol:>+8.4f}  Δ={r_total_sol - p_total_sol:>+8.4f} SOL")
    return 0


def _sweep_mode(slippage_base_pct: float) -> int:
    if not SWEEP_PATH.exists():
        print(f"Sweep file missing: {SWEEP_PATH}\nRun scripts/tier_a_sweep.py first.")
        return 1

    sweep = json.loads(SWEEP_PATH.read_text())
    slip = slip_factor(slippage_base_pct, SIZE_REF_SOL) * 100.0  # pct, since size pinned to 0.03

    rescored: list[dict] = []
    for combo in sweep:
        # We don't have per-trade pnls in the sweep summary, only
        # aggregates. Use avg_pnl_pct as a coarse proxy: shift it by
        # 2·slip (≈ subtraction works for small slippage), then
        # re-derive total_sol on the same trade count.
        new_avg = adjust_pnl_pct(combo["avg_pnl_pct"], slip)
        new_total_sol = SIZE_REF_SOL * new_avg * combo["n"] / 100.0
        rescored.append({
            **combo,
            "avg_pnl_pct_real": round(new_avg, 3),
            "total_sol_real": round(new_total_sol, 4),
            "edge_lost_sol": round(combo["total_sol"] - new_total_sol, 4),
        })

    print(f"Slippage applied: ±{slippage_base_pct:.2f}% per side at "
          f"{SIZE_REF_SOL} SOL (≈{slip:.2f}% per side here, "
          f"~{2 * slip:.2f}% round-trip)")
    print()
    print(f"{'inact':>6} {'trail':>6} {'paper_SOL':>10} {'real_SOL':>10} {'Δ_SOL':>10} {'paper%':>8} {'real%':>8}")
    rescored.sort(key=lambda r: -r["total_sol_real"])
    for r in rescored:
        marker = "  ←LIVE" if (r["inactivity_sec"] == 60 and r["trailing_pct"] == 50) else ""
        print(f"{r['inactivity_sec']:>6.0f} {r['trailing_pct']:>6.0f} "
              f"{r['total_sol']:>+10.4f} {r['total_sol_real']:>+10.4f} "
              f"{-r['edge_lost_sol']:>+10.4f} "
              f"{r['avg_pnl_pct']:>+8.3f} {r['avg_pnl_pct_real']:>+8.3f}{marker}")
    return 0


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--mode", choices=["paper", "sweep"], default="paper")
    p.add_argument("--slippage", type=float, default=1.5,
                   help="Base per-side slippage in %% at 0.03 SOL position. "
                        "Default 1.5 %% — conservative-but-realistic for "
                        "pump.fun bonding curves at the typical 30-40 SOL "
                        "mcap entries.")
    args = p.parse_args()
    if args.mode == "paper":
        return _paper_mode(args.slippage)
    return _sweep_mode(args.slippage)


if __name__ == "__main__":
    sys.exit(main())
