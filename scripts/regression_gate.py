#!/usr/bin/env python3
# scripts/regression_gate.py
"""Hard regression gate: replay historical paper_trades through the
*current* exit config and refuse to ship if WR / PnL would degrade
versus the frozen baseline.

How it works
------------
1. Pull last N closed paper_trades that the bot actually opened
   (real entries, real post-entry trade streams in the ``trades``
   table).
2. For each, replay :func:`simulate_exit_batch` with the *current*
   :class:`PulseBotConfig` — this answers the counterfactual "what
   would TP / SL / max_hold / dynamic-max-hold do today on yesterday's
   entries?".
3. Aggregate WR, PnL/trade, profit factor.
4. Compare to ``data/ml/baseline_metrics.json``.
   * **PASS** — every metric ≥ baseline − 1·SE.
   * **WARN** — any metric in (baseline − 2·SE, baseline − 1·SE).
   * **FAIL** — any metric < baseline − 2·SE.
5. ``--freeze`` saves current run as the new baseline (manual gate
   for known-good points; do not freeze automatically on PASS — that
   would let slow drift erode the bar over time).

Exit codes are CI-friendly: 0 PASS, 1 FAIL, 2 WARN.

Usage
-----
.. code-block:: bash

   # First time: snapshot the current ledger as the baseline.
   PYTHONPATH=. .venv/bin/python scripts/regression_gate.py --freeze

   # Subsequent runs (CI / pre-push hook): just compare.
   PYTHONPATH=. .venv/bin/python scripts/regression_gate.py

   # Tune sample window:
   PYTHONPATH=. .venv/bin/python scripts/regression_gate.py --days 14 --limit 3000
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import psycopg2
import psycopg2.extras

# Suppress noisy startup logs from the trade-supervisor instantiation.
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logging.getLogger("pulse_bot.ml.policy._main").setLevel(logging.ERROR)
logging.getLogger("pulse_bot.ml.policy").setLevel(logging.ERROR)

# Make the package importable when running the script directly.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pulse_bot.config import get_config  # noqa: E402
from pulse_bot.db import _resolve_dsn  # noqa: E402
from pulse_bot.ml.simulate_exit import simulate_exit_batch  # noqa: E402
from pulse_bot.models import Trade  # noqa: E402

logger = logging.getLogger("regression_gate")

REPO_ROOT = Path(__file__).resolve().parents[1]
# Tracked location (data/ is in .gitignore — baseline must be in repo
# so the gate is reproducible across machines and CI).
BASELINE_PATH = REPO_ROOT / "pulse_bot" / "ml" / "regression_baseline.json"

# Tolerances. baseline − k·SE on a metric. PASS_K is generous so daily
# noise doesn't trip the gate; FAIL_K is the "definitely worse"
# threshold.
PASS_K = 1.0
FAIL_K = 2.0


def _git_hash() -> str:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"], cwd=REPO_ROOT
        ).decode().strip()
    except Exception:
        return "unknown"


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%MZ")


def _load_paper_trades(conn, days: int, limit: int) -> list[dict]:
    cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
    cur.execute(
        """SELECT id, mint, entry_time, entry_price, entry_mcap_sol,
                  exit_time, pnl_pct, pnl_sol, exit_reason, entry_type
           FROM paper_trades
           WHERE exit_time IS NOT NULL
             AND entry_price > 0
             AND entry_time > extract(epoch FROM now()) - %s * 86400
           ORDER BY entry_time DESC
           LIMIT %s""",
        (int(days), int(limit)),
    )
    rows = [dict(r) for r in cur.fetchall()]
    cur.close()
    return rows


def _load_post_entry_trades(
    conn,
    rows: list[dict],
    horizon_seconds: float,
) -> dict[str, list[Trade]]:
    """Bulk-fetch trades from ``trades`` for each mint, clipped to the
    [entry_ts, entry_ts + horizon] window."""
    mints = [r["mint"] for r in rows]
    entries = {
        r["mint"]: float(r["entry_time"] or 0.0) for r in rows
    }
    out: dict[str, list[Trade]] = {m: [] for m in mints}
    cur = conn.cursor()
    chunk = 500
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
            mint = r[0]
            ts = float(r[1] or 0.0)
            entry_ts = entries.get(mint, 0.0)
            if ts < entry_ts or ts > entry_ts + horizon_seconds:
                continue
            out[mint].append(
                Trade(
                    mint=mint,
                    wallet=r[7] or "",
                    tx_type=r[2],
                    sol_amount=float(r[3] or 0.0),
                    token_amount=float(r[4] or 0.0),
                    new_token_balance=0.0,
                    bonding_curve_key="",
                    v_sol_in_bonding_curve=float(r[6] or 0.0),
                    v_tokens_in_bonding_curve=0.0,
                    market_cap_sol=float(r[5] or 0.0),
                    timestamp=ts,
                )
            )
            fetched += 1
    cur.close()
    return out


def _aggregate(pnls_pct: list[float], pnls_sol: list[float]) -> dict[str, Any]:
    n = len(pnls_pct)
    if n == 0:
        return {"n": 0}
    wins = [p for p in pnls_pct if p > 0]
    losses = [p for p in pnls_pct if p <= 0]
    sum_wins_sol = sum(s for s, p in zip(pnls_sol, pnls_pct) if p > 0)
    sum_losses_sol = sum(-s for s, p in zip(pnls_sol, pnls_pct) if p <= 0)
    profit_factor = sum_wins_sol / sum_losses_sol if sum_losses_sol > 0 else float("inf")

    mean_pnl_sol = sum(pnls_sol) / n
    var_pnl_sol = sum((p - mean_pnl_sol) ** 2 for p in pnls_sol) / max(1, n - 1)
    se_pnl_sol = math.sqrt(var_pnl_sol / n)
    wr_pct = 100.0 * len(wins) / n
    se_wr_pct = math.sqrt(wr_pct * (100.0 - wr_pct) / n)

    return {
        "n": n,
        "wr_pct": round(wr_pct, 2),
        "wr_pct_se": round(se_wr_pct, 2),
        "avg_pnl_sol": round(mean_pnl_sol, 5),
        "avg_pnl_sol_se": round(se_pnl_sol, 5),
        "sum_pnl_sol": round(sum(pnls_sol), 4),
        "profit_factor": (
            round(profit_factor, 3) if profit_factor != float("inf") else None
        ),
        "n_wins": len(wins),
        "n_losses": len(losses),
    }


def _replay_with_current_config(
    rows: list[dict], trades_by_mint: dict[str, list[Trade]]
) -> dict[str, Any]:
    cfg = get_config()
    entries = {
        r["mint"]: (float(r["entry_time"]), float(r["entry_price"]))
        for r in rows
    }
    mints_with_trades = [m for m, ts in trades_by_mint.items() if ts]
    out = simulate_exit_batch(
        cfg,
        {m: trades_by_mint[m] for m in mints_with_trades},
        {m: entries[m] for m in mints_with_trades},
    )
    pnls_pct: list[float] = []
    pnls_sol: list[float] = []
    reasons: dict[str, int] = {}
    # Fall back to original entry buy_amount_sol for SOL conversion;
    # config default is 0.03 SOL / position.
    buy_size = float(get_config().__dict__.get("buy_amount_sol", 0.03))
    for mint, res in out.items():
        pct = float(res.pnl_pct or 0.0)
        pnls_pct.append(pct)
        pnls_sol.append(buy_size * pct / 100.0)
        reasons[res.exit_reason] = reasons.get(res.exit_reason, 0) + 1
    return {
        **_aggregate(pnls_pct, pnls_sol),
        "exit_reasons": reasons,
    }


def _verdict_for_metric(
    name: str, current: float, baseline: float, baseline_se: float
) -> tuple[str, str]:
    """Return (level, message). level ∈ {'pass', 'warn', 'fail'}."""
    delta = current - baseline
    pass_threshold = -PASS_K * baseline_se
    fail_threshold = -FAIL_K * baseline_se
    if delta >= pass_threshold:
        return "pass", f"{name}={current:.4f} (Δ={delta:+.4f}, baseline={baseline:.4f}±{baseline_se:.4f})"
    if delta >= fail_threshold:
        return "warn", f"⚠️  {name}={current:.4f} below baseline by {-delta:.4f} ({-delta / baseline_se:.1f}·SE)"
    return "fail", f"❌ {name}={current:.4f} REGRESSED by {-delta:.4f} ({-delta / baseline_se:.1f}·SE) vs baseline {baseline:.4f}"


def _compare(current: dict[str, Any], baseline: dict[str, Any]) -> int:
    """Return exit code: 0 pass / 1 fail / 2 warn."""
    base_metrics = baseline["metrics"]
    has_warn = False
    has_fail = False
    print(f"\nBaseline frozen at {baseline.get('frozen_at')} on git={baseline.get('git_hash')}")
    print(f"  baseline n={base_metrics['n']}  current n={current['n']}\n")

    for metric, se_key in (
        ("wr_pct", "wr_pct_se"),
        ("avg_pnl_sol", "avg_pnl_sol_se"),
    ):
        cur = float(current.get(metric) or 0.0)
        base = float(base_metrics.get(metric) or 0.0)
        # Use the *baseline* SE — frozen at known-good point — so a
        # noisier current sample can't widen the tolerance and pass.
        base_se = float(base_metrics.get(se_key) or 0.0) or 1e-9
        level, msg = _verdict_for_metric(metric, cur, base, base_se)
        prefix = {"pass": "✓", "warn": "⚠", "fail": "✗"}[level]
        print(f"  {prefix} {msg}")
        has_warn = has_warn or level == "warn"
        has_fail = has_fail or level == "fail"

    # profit_factor is a one-sided "must not drop below baseline" check
    # (no formal SE since it's a ratio of sums). Tolerate −10 %.
    pf_cur = current.get("profit_factor")
    pf_base = base_metrics.get("profit_factor")
    if pf_cur is not None and pf_base is not None:
        if pf_cur < pf_base * 0.9:
            print(f"  ✗ profit_factor={pf_cur:.3f} below 90% of baseline {pf_base:.3f}")
            has_fail = True
        elif pf_cur < pf_base:
            print(f"  ⚠ profit_factor={pf_cur:.3f} below baseline {pf_base:.3f}")
            has_warn = True
        else:
            print(f"  ✓ profit_factor={pf_cur:.3f} ≥ baseline {pf_base:.3f}")

    print()
    if has_fail:
        print("RESULT: FAIL — refusing to ship")
        return 1
    if has_warn:
        print("RESULT: WARN — within 1-2·SE of regression, review before deploying")
        return 2
    print("RESULT: PASS")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=14,
                        help="Lookback window for paper_trades (default 14)")
    parser.add_argument("--limit", type=int, default=2000,
                        help="Max paper_trades to replay (default 2000)")
    parser.add_argument("--horizon", type=float, default=600.0,
                        help="Post-entry trade horizon in seconds (default 600)")
    parser.add_argument("--freeze", action="store_true",
                        help="Save current run as the new baseline. Use only "
                             "when you're confident the current config is "
                             "known-good.")
    args = parser.parse_args()

    dsn = _resolve_dsn(os.environ.get("PULSE_PG_DSN"))
    conn = psycopg2.connect(dsn)
    try:
        rows = _load_paper_trades(conn, args.days, args.limit)
        if not rows:
            print("No closed paper_trades found in the lookback window — "
                  "nothing to gate.")
            return 0
        logger.info("Loaded %d closed paper_trades", len(rows))

        t0 = time.time()
        trades_by_mint = _load_post_entry_trades(conn, rows, args.horizon)
        n_with_trades = sum(1 for v in trades_by_mint.values() if v)
        logger.info(
            "Fetched post-entry trades for %d / %d mints in %.1fs",
            n_with_trades, len(rows), time.time() - t0,
        )

        current = _replay_with_current_config(rows, trades_by_mint)
        print(f"\nCurrent-config replay: {json.dumps(current, indent=2)}")
    finally:
        conn.close()

    if args.freeze:
        BASELINE_PATH.parent.mkdir(parents=True, exist_ok=True)
        snapshot = {
            "frozen_at": _now_iso(),
            "git_hash": _git_hash(),
            "lookback_days": args.days,
            "limit": args.limit,
            "horizon_seconds": args.horizon,
            "metrics": current,
        }
        BASELINE_PATH.write_text(json.dumps(snapshot, indent=2))
        print(f"\nFroze baseline → {BASELINE_PATH}")
        return 0

    if not BASELINE_PATH.exists():
        print(f"\n⚠ no baseline at {BASELINE_PATH}; run with --freeze once "
              "to establish one. Treating as PASS for now.")
        return 0

    baseline = json.loads(BASELINE_PATH.read_text())
    return _compare(current, baseline)


if __name__ == "__main__":
    sys.exit(main())
