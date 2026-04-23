# pulse_bot/ml/label_noise_floor.py
"""One-shot label noise estimator via k-NN disagreement.

For each row, find its k nearest neighbors in standardized feature space
and measure how often neighbor labels disagree with the row's own label.
Average disagreement rate sets a rough upper bound on achievable
classifier accuracy — if ~20% of near-identical feature vectors carry
opposite labels, the learnable signal is capped near 80%.

Runs with weekly retrain, not daily. Saves ``data/ml/noise_floor.json``.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from sklearn.neighbors import NearestNeighbors
from sklearn.preprocessing import StandardScaler

from pulse_bot.ml.build_dataset import build_entry_dataset, build_exit_dataset

logger = logging.getLogger(__name__)


def compute_noise_floor(
    X: pd.DataFrame,
    y: pd.Series,
    k: int = 5,
) -> dict[str, Any]:
    """Fraction of k-NN pairs whose labels disagree."""
    n = len(y)
    if n < k + 2:
        return {
            "skipped": True,
            "reason": f"need ≥{k + 2} rows, have {n}",
        }
    # Median-impute NaNs, then standardize — k-NN is distance-based so
    # unscaled features dominate the metric.
    X_filled = X.fillna(X.median(numeric_only=True))
    X_filled = X_filled.fillna(0)
    scaler = StandardScaler()
    Xs = scaler.fit_transform(X_filled.astype(float).values)
    nn = NearestNeighbors(n_neighbors=k + 1).fit(Xs)
    _, idxs = nn.kneighbors(Xs)
    y_arr = y.values
    # First column is self; drop it
    neighbor_labels = y_arr[idxs[:, 1:]]
    own = y_arr[:, None]
    disagreement = (neighbor_labels != own).mean(axis=1)
    mean_noise = float(disagreement.mean())
    median_noise = float(np.median(disagreement))
    # Split by class — are positives or negatives noisier?
    pos_mask = y_arr == 1
    neg_mask = y_arr == 0
    pos_noise = float(disagreement[pos_mask].mean()) if pos_mask.sum() else None
    neg_noise = float(disagreement[neg_mask].mean()) if neg_mask.sum() else None
    return {
        "k": k,
        "n_rows": int(n),
        "n_positives": int(pos_mask.sum()),
        "mean_disagreement": mean_noise,
        "median_disagreement": median_noise,
        "positives_noise": pos_noise,
        "negatives_noise": neg_noise,
        "upper_bound_accuracy": 1.0 - mean_noise,
        "interpretation": (
            "Lower is better. >15% suggests label is fundamentally noisy "
            "relative to features — model AUC ceiling is capped."
        ),
    }


def run(
    db_path: str,
    out_path: Path,
    kinds: list[str],
    k: int = 5,
) -> dict[str, Any]:
    report: dict[str, Any] = {
        "generated_at": datetime.now(UTC).isoformat(),
        "k": k,
        "kinds": {},
    }
    for kind in kinds:
        logger.info("Computing noise floor for %s...", kind)
        if kind == "entry":
            df = build_entry_dataset(db_path)
            exclude = {
                "mint",
                "scored_at",
                "created_at",
                "creator",
                "mc_at_scoring",
                "top1_120",
                "top5_120",
                "label",
            }
        elif kind == "exit":
            df = build_exit_dataset(db_path)
            exclude = {"mint", "entry_ts", "sample_ts", "label"}
        else:
            raise ValueError(f"Unknown kind: {kind}")
        if df.empty:
            report["kinds"][kind] = {"error": "empty dataset"}
            continue
        feature_cols = [c for c in df.columns if c not in exclude]
        X = df[feature_cols]
        y = df["label"]
        res = compute_noise_floor(X, y, k=k)
        res["features_used"] = feature_cols
        report["kinds"][kind] = res
        logger.info(
            "%s noise floor: mean disagreement = %.1f%% (n=%d)",
            kind,
            (res.get("mean_disagreement") or 0) * 100,
            res.get("n_rows", 0),
        )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(report, indent=2))
    logger.info("Wrote %s", out_path)
    return report


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", default="pulse_bot.db")
    ap.add_argument("--out", default="data/ml/noise_floor.json")
    ap.add_argument("--kind", choices=["entry", "exit", "both"], default="both")
    ap.add_argument("--k", type=int, default=5)
    args = ap.parse_args()

    kinds = ["entry", "exit"] if args.kind == "both" else [args.kind]
    run(args.db, Path(args.out), kinds, k=args.k)
    return 0


if __name__ == "__main__":
    sys.exit(main())
