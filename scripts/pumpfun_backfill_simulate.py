#!/usr/bin/env python3
# scripts/pumpfun_backfill_simulate.py
"""Backfill on-chain simulation against historical paper_trades.

For each closed paper_trade entry in the chosen window, builds a
fresh-blockhash buy transaction at the configured slippage cap and
runs it through Solana's ``simulateTransaction`` RPC. **No SOL is
spent.** The output answers a single question that's been
unanswered until now:

    "If we'd actually submitted these trades on-chain at 1 % slippage,
     what fraction would have filled?"

That number determines real-money viability:
  * fill_rate ≥ 80 %  → 1 % cap is realistic, ready for tiny live
  * fill_rate 50-80 % → loosen to 1.5-2 % then re-measure
  * fill_rate <  50 % → cap is too tight; pump.fun volatility
                        + tx-confirm latency exceeds 1 % most of the
                        time. Loosen further OR drop project.

Usage::

    ssh rich "cd ~/www/gg && set -a && source .env && set +a && \\
              PYTHONPATH=. .venv/bin/python scripts/pumpfun_backfill_simulate.py \\
                  --slippage-bps 100 \\
                  --sol-amount 0.01 \\
                  --limit 100"

Output:
  data/ml/pumpfun_simulate_backfill.json   per-mint result rows
  Console: aggregate fill_rate, error breakdown, slippage histogram

Cost: 0 SOL. Reads-only on-chain (simulate). The wallet's balance
is unaffected; the keypair only signs the dry-run transactions.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import time
from collections import Counter
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("pumpfun_backfill_sim")

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

import psycopg2  # noqa: E402
import psycopg2.extras  # noqa: E402

from pulse_bot.db import _resolve_dsn  # noqa: E402
from pulse_bot.execution_pumpfun import PumpFunExecution  # noqa: E402

OUT_JSON = REPO_ROOT / "data" / "ml" / "pumpfun_simulate_backfill.json"


def _load_paper_trade_entries(conn, days: int, limit: int) -> list[dict]:
    """Pull the last N closed paper_trade entries we'd want to
    counterfactually simulate. Filter to entries that actually opened
    a position (entry_price > 0)."""
    cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
    cur.execute(
        """SELECT id, mint, symbol, entry_time, entry_price,
                  pnl_pct, pnl_sol, exit_reason
           FROM paper_trades
           WHERE entry_price > 0
             AND entry_time > extract(epoch FROM now()) - %s * 86400
             AND pnl_sol IS NOT NULL
           ORDER BY entry_time DESC
           LIMIT %s""",
        (int(days), int(limit)),
    )
    rows = [dict(r) for r in cur.fetchall()]
    cur.close()
    return rows


async def _run(args: argparse.Namespace) -> int:
    dsn = _resolve_dsn(os.environ.get("PULSE_PG_DSN"))
    conn = psycopg2.connect(dsn)
    rows = _load_paper_trade_entries(conn, args.days, args.limit)
    conn.close()
    logger.info("Loaded %d paper_trade entries from last %dd", len(rows), args.days)
    if not rows:
        return 1

    sol_lamports = int(args.sol_amount * 1e9)

    # Build PumpFunExecution from env. allow_live_submit=False is the
    # default — this script intentionally only simulates.
    try:
        ex = PumpFunExecution.from_env(allow_live_submit=False)
    except RuntimeError as exc:
        logger.error("env not configured: %s", exc)
        return 2

    logger.info(
        "Wallet: %s  position size: %.4f SOL  slippage: %d bps",
        ex.wallet_pubkey, args.sol_amount, args.slippage_bps,
    )

    results: list[dict] = []
    err_counter: Counter[str] = Counter()
    n_success = 0
    cu_consumed: list[int] = []
    t0 = time.time()
    try:
        for idx, row in enumerate(rows, 1):
            mint = row["mint"]
            try:
                res = await ex.simulate_buy(
                    mint=mint,
                    sol_amount_lamports=sol_lamports,
                    slippage_bps=args.slippage_bps,
                )
            except Exception as exc:
                logger.warning("[%d/%d] %s: exception %s", idx, len(rows), mint[:12], exc)
                err_counter["exception:%s" % type(exc).__name__] += 1
                results.append({
                    "mint": mint,
                    "success": False,
                    "err": f"exception:{exc}",
                })
                continue
            if res.success:
                n_success += 1
                if res.units_consumed is not None:
                    cu_consumed.append(int(res.units_consumed))
            else:
                err_label = (
                    res.err
                    if isinstance(res.err, str)
                    else json.dumps(res.err) if res.err is not None
                    else "unknown_err"
                )
                err_counter[err_label[:80]] += 1
            results.append({
                "mint": mint,
                "symbol": row.get("symbol"),
                "success": bool(res.success),
                "expected_tokens": int(res.expected_tokens),
                "slippage_bps_cap": int(res.slippage_bps_cap),
                "units_consumed": res.units_consumed,
                "err": (
                    res.err if isinstance(res.err, (str, type(None)))
                    else json.dumps(res.err)
                ),
                "real_paper_pnl_pct": row.get("pnl_pct"),
            })
            if idx % 10 == 0:
                logger.info(
                    "[%d/%d] success_rate so far: %.1f %%  elapsed: %.0fs",
                    idx, len(rows), 100.0 * n_success / idx, time.time() - t0,
                )
    finally:
        await ex.close()

    n = len(results)
    fill_rate = (n_success / n) if n else 0.0
    avg_cu = sum(cu_consumed) / len(cu_consumed) if cu_consumed else 0

    print()
    print("=" * 70)
    print(f"BACKFILL SIMULATION DONE — {n} mints, {time.time() - t0:.0f}s elapsed")
    print(f"  position size:  {args.sol_amount} SOL  ({sol_lamports} lamports)")
    print(f"  slippage cap:   {args.slippage_bps} bps  ({args.slippage_bps / 100:.2f} %)")
    print(f"  successes:      {n_success} / {n}  ({fill_rate * 100:.1f} %)")
    print(f"  avg CU on succ: {avg_cu:.0f}")
    print()
    print("Error breakdown:")
    for err, count in err_counter.most_common(15):
        print(f"  {count:>5}  {err}")
    print("=" * 70)
    print()
    print("Decision band on fill_rate:")
    if fill_rate >= 0.80:
        print(f"  ✅ {fill_rate * 100:.1f} % ≥ 80 % — slippage cap is realistic")
        print(f"     READY for tiny live test at 0.01 SOL × this slippage.")
    elif fill_rate >= 0.50:
        print(f"  ⚠ {fill_rate * 100:.1f} % in [50, 80) — borderline")
        print(f"     Consider loosening slippage to 150-200 bps and re-measuring.")
    else:
        print(f"  ❌ {fill_rate * 100:.1f} % < 50 % — slippage cap too tight for pump.fun")
        print(f"     Need 200-300 bps OR project not viable at this position size.")

    OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    OUT_JSON.write_text(json.dumps({
        "summary": {
            "n": n,
            "n_success": n_success,
            "fill_rate": fill_rate,
            "avg_cu_on_success": avg_cu,
            "sol_amount": args.sol_amount,
            "slippage_bps": args.slippage_bps,
        },
        "errors": dict(err_counter.most_common()),
        "rows": results,
    }, indent=2))
    print(f"\nResults written to {OUT_JSON}")
    return 0


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--slippage-bps", type=int, default=100,
                   help="On-chain slippage cap in basis points (default 100 = 1 %).")
    p.add_argument("--sol-amount", type=float, default=0.01,
                   help="Position size in SOL (default 0.01).")
    p.add_argument("--days", type=int, default=14,
                   help="Lookback window for paper_trades (default 14).")
    p.add_argument("--limit", type=int, default=100,
                   help="Max paper_trade entries to simulate (default 100).")
    args = p.parse_args()
    return asyncio.run(_run(args))


if __name__ == "__main__":
    sys.exit(main())
