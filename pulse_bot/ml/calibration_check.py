# pulse_bot/ml/calibration_check.py
"""Calibration reliability report for entry / exit XGBoost models.

AUC measures *ranking quality* — whether positives score higher than
negatives. Calibration measures *probability accuracy* — whether
``predict_proba = 0.8`` really corresponds to an ~80% hit rate. They
are independent: a model can rank perfectly (AUC=1) and still produce
garbage probabilities, and vice versa.

For any threshold-based decision (``sell if ml_exit_proba >= 0.85``)
calibration matters more than AUC. This script prints a reliability
table: predicted-proba bin → empirical hit rate. If numbers diverge
wildly, the model's confidence cannot be trusted as literal
probability and any proba threshold is fiction.

Usage:
    python -m pulse_bot.ml.calibration_check --kind entry
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import xgboost as xgb
from sklearn.metrics import brier_score_loss

logger = logging.getLogger(__name__)


def _load_model(path: Path) -> tuple[xgb.XGBClassifier, list[str]]:
    meta = json.loads(path.with_suffix(".meta.json").read_text())
    model = xgb.XGBClassifier()
    model.load_model(path)
    return model, meta["features"]


def _chrono_split(df: pd.DataFrame, time_col: str) -> tuple[pd.DataFrame, pd.DataFrame]:
    df = df.sort_values(time_col).reset_index(drop=True)
    cut = int(len(df) * 0.8)
    return df.iloc[:cut], df.iloc[cut:]


def calibration_table(
    y_true: np.ndarray,
    y_proba: np.ndarray,
    n_bins: int = 5,
) -> list[dict]:
    """Equal-width bins in [0, 1]. Returns list of {bin, n, mean_proba, hit_rate, deviation}."""
    edges = np.linspace(0.0, 1.0, n_bins + 1)
    rows: list[dict] = []
    for i, (lo, hi) in enumerate(zip(edges[:-1], edges[1:])):
        mask = (y_proba >= lo) & ((y_proba < hi) if i < n_bins - 1 else (y_proba <= hi))
        n = int(mask.sum())
        if n == 0:
            rows.append(
                {
                    "bin": f"[{lo:.2f},{hi:.2f})",
                    "n": 0,
                    "mean_proba": None,
                    "hit_rate": None,
                    "deviation": None,
                }
            )
            continue
        mean_proba = float(y_proba[mask].mean())
        hit_rate = float(y_true[mask].mean())
        rows.append(
            {
                "bin": f"[{lo:.2f},{hi:.2f}{'' if i < n_bins - 1 else ']'})",
                "n": n,
                "mean_proba": round(mean_proba, 3),
                "hit_rate": round(hit_rate, 3),
                "deviation": round(mean_proba - hit_rate, 3),
            }
        )
    return rows


def verdict(rows: list[dict]) -> str:
    """Summarize calibration quality across bins."""
    deviations = [r["deviation"] for r in rows if r["deviation"] is not None]
    if not deviations:
        return "NO DATA — not enough samples for reliability check"
    mean_abs = np.mean(np.abs(deviations))
    max_abs = np.max(np.abs(deviations))
    if max_abs <= 0.10 and mean_abs <= 0.05:
        return "✓ WELL CALIBRATED — proba threshold decisions reliable"
    if max_abs <= 0.20:
        return "~ MARGINAL — avoid sharp proba thresholds (0.85+)"
    return "✗ POORLY CALIBRATED — proba is noise, rank only"


def run(kind: str, data_dir: Path) -> dict:
    model_path = data_dir / f"{kind}_model.ubj"
    model, features = _load_model(model_path)
    parquet = data_dir / f"{kind}.parquet"
    df = pd.read_parquet(parquet)
    time_col = "scored_at" if kind == "entry" else "entry_ts"
    if kind == "exit":
        mints = df.drop_duplicates("mint").sort_values(time_col)["mint"].tolist()
        cut_idx = max(1, int(len(mints) * 0.8))
        cut_mint = mints[min(cut_idx, len(mints) - 1)]
        cut_ts = df.loc[df.mint == cut_mint, time_col].iloc[0]
        test = df.loc[df[time_col] >= cut_ts]
    else:
        _, test = _chrono_split(df, time_col)
    X_test = test[features]
    y_test = test["label"].values
    proba = model.predict_proba(X_test)[:, 1]

    report = {
        "kind": kind,
        "n_test": int(len(y_test)),
        "positives_test": int(y_test.sum()),
        "base_rate_test": round(float(y_test.mean()), 3),
        "brier_score": round(float(brier_score_loss(y_test, proba)), 4),
        "proba_distribution": {
            "min": round(float(proba.min()), 3),
            "p25": round(float(np.percentile(proba, 25)), 3),
            "p50": round(float(np.percentile(proba, 50)), 3),
            "p75": round(float(np.percentile(proba, 75)), 3),
            "max": round(float(proba.max()), 3),
        },
        "calibration_bins_5": calibration_table(y_test, proba, n_bins=5),
        "calibration_bins_10": calibration_table(y_test, proba, n_bins=10),
    }
    report["verdict"] = verdict(report["calibration_bins_5"])
    return report


def print_report(report: dict) -> None:
    print(f"\n=== Calibration — {report['kind']} model ===")
    print(
        f"Test rows: {report['n_test']}, positives: {report['positives_test']} "
        f"(base rate {report['base_rate_test']:.1%})"
    )
    print(f"Brier score: {report['brier_score']} (lower = better, 0.25 = random)")
    dist = report["proba_distribution"]
    print(
        f"Proba range: [{dist['min']} … {dist['p25']} … {dist['p50']} "
        f"… {dist['p75']} … {dist['max']}]"
    )
    print()
    print(f"{'Bin':<15} {'N':>5} {'Predicted':>10} {'Actual':>8} {'Deviation':>10}")
    print("-" * 50)
    for row in report["calibration_bins_5"]:
        mp = f"{row['mean_proba']:.3f}" if row["mean_proba"] is not None else "—"
        hr = f"{row['hit_rate']:.3f}" if row["hit_rate"] is not None else "—"
        dv = f"{row['deviation']:+.3f}" if row["deviation"] is not None else "—"
        print(f"{row['bin']:<15} {row['n']:>5} {mp:>10} {hr:>8} {dv:>10}")
    print()
    print(f"Verdict: {report['verdict']}")


def main() -> int:
    logging.basicConfig(level=logging.WARNING)
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--kind", choices=["entry", "exit", "both"], default="both")
    ap.add_argument("--data-dir", default="data/ml")
    args = ap.parse_args()
    data_dir = Path(args.data_dir)
    kinds = ["entry", "exit"] if args.kind == "both" else [args.kind]
    for k in kinds:
        try:
            report = run(k, data_dir)
            print_report(report)
        except Exception as e:
            logger.exception("Failed for %s: %s", k, e)
    return 0


if __name__ == "__main__":
    sys.exit(main())
