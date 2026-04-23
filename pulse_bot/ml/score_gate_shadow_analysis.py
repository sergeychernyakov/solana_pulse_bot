# pulse_bot/ml/score_gate_shadow_analysis.py
"""Decision B shadow experiment — does ML want to buy BORDERLINE tokens?

Runs after live bot has accumulated token_scores rows with ml_entry_proba
populated (requires a restart with shadow logging active). Reports:

1. Agreement rate between rule gate (score ≥ 30) and ML (proba ≥ 0.5)
2. For BORDERLINE tokens (10 ≤ score < 30) with HIGH ml_proba, their
   realized PnL — the "missed alpha" hypothesis test.
3. For BUY tokens (score ≥ 30) with LOW ml_proba, their realized PnL —
   the "false-positive rule" hypothesis test.
4. Recommendation: lower gate, keep gate, or more data needed.

Codex 2026-04-22: this is the proper way to test Decision B (hard gate
at 30) — shadow data instead of armchair comparison.
"""

from __future__ import annotations

import argparse
import logging
import sqlite3
import sys
from pathlib import Path

import pandas as pd

from pulse_bot.config import PUMPFUN_PRIORITY_FEE

logger = logging.getLogger(__name__)

# Round-trip cost matches daily_validation.py.
REALISTIC_ROUND_TRIP_COST_PCT = 4.0  # 2% pump.fun round-trip + 2% slippage
PRIORITY_COST_SOL = 2 * PUMPFUN_PRIORITY_FEE
POSITION_SIZE_SOL = 0.1


def analyze(db_path: Path, proba_threshold: float = 0.5) -> dict:
    conn = sqlite3.connect(db_path)
    # Realized PnL for each token — reconstruct from trades table.
    # Uses same realized-PnL label shape as build_dataset (TP=50/SL=30/
    # max_hold=300), but stored as a percentage so backtest is honest.
    df = pd.read_sql_query(
        """
        SELECT s.mint, s.decision, s.total_score, s.scored_at,
               s.ml_entry_proba, t.created_at
        FROM token_scores s
        JOIN tokens t ON t.mint = s.mint
        WHERE s.source = 'live'
          AND s.ml_entry_proba IS NOT NULL
          AND s.scored_at IS NOT NULL
        """,
        conn,
    )
    if df.empty:
        conn.close()
        return {"error": "no ml_entry_proba rows yet — bot needs restart + data"}

    # Join realized PnL from entry.parquet (build_dataset computed it).
    pq = Path("data/ml/entry.parquet")
    if pq.exists():
        pnl_df = pd.read_parquet(pq, columns=["mint", "realized_pnl_pct"])
        df = df.merge(pnl_df, on="mint", how="left")
    conn.close()

    df["ml_would_trade"] = df["ml_entry_proba"] >= proba_threshold
    df["rules_would_trade"] = df["decision"] == "BUY"

    # Agreement matrix
    matrix = pd.crosstab(
        df["rules_would_trade"],
        df["ml_would_trade"],
        rownames=["rules BUY"],
        colnames=["ml BUY"],
    )

    report: dict = {
        "proba_threshold": proba_threshold,
        "n_scored_with_proba": len(df),
        "agreement_matrix": matrix.to_dict(),
    }

    def pnl_summary(subset: pd.DataFrame, label: str) -> dict:
        sub = subset.dropna(subset=["realized_pnl_pct"])
        if sub.empty:
            return {"n": 0, "note": "no realized PnL available"}
        n = len(sub)
        wr = float((sub.realized_pnl_pct > 0).mean())
        mean_pct = float(sub.realized_pnl_pct.mean())
        net_mean_pct = mean_pct - REALISTIC_ROUND_TRIP_COST_PCT
        total_sol = float(
            (sub.realized_pnl_pct - REALISTIC_ROUND_TRIP_COST_PCT).sum()
            / 100.0
            * POSITION_SIZE_SOL
            - n * PRIORITY_COST_SOL
        )
        return {
            "n": n,
            "win_rate": round(wr, 3),
            "mean_raw_pnl_pct": round(mean_pct, 2),
            "mean_net_pnl_pct": round(net_mean_pct, 2),
            "total_net_sol": round(total_sol, 4),
        }

    # Four buckets
    report["rules_BUY_and_ml_BUY"] = pnl_summary(
        df[df.rules_would_trade & df.ml_would_trade],
        "rules ∧ ML",
    )
    report["rules_BUY_only"] = pnl_summary(
        df[df.rules_would_trade & ~df.ml_would_trade],
        "rules only (ML would skip)",
    )
    report["ml_BUY_only_borderline"] = pnl_summary(
        df[~df.rules_would_trade & df.ml_would_trade & (df.decision == "BORDERLINE")],
        "ML only — BORDERLINE alpha claim",
    )
    report["ml_BUY_only_skip"] = pnl_summary(
        df[~df.rules_would_trade & df.ml_would_trade & (df.decision == "SKIP")],
        "ML only — from SKIP tier",
    )

    # Decision heuristic
    borderline_pnl = report["ml_BUY_only_borderline"].get("total_net_sol")
    rules_pnl = report["rules_BUY_and_ml_BUY"].get("total_net_sol")
    if borderline_pnl is None or rules_pnl is None:
        report["recommendation"] = "insufficient data"
    elif borderline_pnl > 0 and borderline_pnl > rules_pnl * 0.3:
        report["recommendation"] = (
            "LOWER GATE — ML-picks from BORDERLINE are profitable at "
            "≥30% of rules-BUY volume. Worth reducing score_threshold_buy."
        )
    elif borderline_pnl < 0:
        report["recommendation"] = (
            "KEEP GATE — ML's BORDERLINE picks lose money net of fees. "
            "Rules gate correctly filters this tier."
        )
    else:
        report["recommendation"] = (
            "MORE DATA — signal is marginal. Wait for N ≥ 500 BORDERLINE "
            "tokens with both ml_proba and realized PnL."
        )

    return report


def main() -> int:
    logging.basicConfig(level=logging.INFO)
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="pulse_bot.db")
    ap.add_argument("--threshold", type=float, default=0.5)
    args = ap.parse_args()
    import json

    report = analyze(Path(args.db), proba_threshold=args.threshold)
    print(json.dumps(report, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
