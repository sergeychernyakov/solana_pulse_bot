# scripts/honest_topn_compare.py
"""Honest top-N comparison of two entry_model artifacts on the same holdout.

Loads ``data/ml/entry_model.ubj`` (current) and ``data/ml/entry_model.ubj.prev``
(previous), scores both on the last-20% time-based holdout from
``data/ml/entry.parquet``, and reports top-N wins, AUC, and overlap.

This is the canonical tool referenced in ``docs/MODEL_TESTING.md`` for
deciding whether a retrain actually improved ranking quality. WR-at-fixed-
proba comparisons are misleading when calibration shifts; top-N is invariant.

Usage:
    PYTHONPATH=. .venv/bin/python -m scripts.honest_topn_compare
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import numpy as np
import pandas as pd
import xgboost as xgb
from sklearn.metrics import roc_auc_score

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parents[1]
DATASET = ROOT / "data/ml/entry.parquet"
NEW_MODEL = ROOT / "data/ml/entry_model.ubj"
PREV_MODEL = ROOT / "data/ml/entry_model.ubj.prev"
NEW_META = ROOT / "data/ml/entry_model.meta.json"
PREV_META = ROOT / "data/ml/entry_model.meta.json.prev"

TOP_N_GRID = [50, 100, 200, 500, 1000, 1500]


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    if not PREV_MODEL.exists():
        logger.error("No .prev artifact at %s — nothing to compare against.", PREV_MODEL)
        return 1

    new_meta = json.loads(NEW_META.read_text())
    prev_meta = json.loads(PREV_META.read_text())

    if new_meta["features"] != prev_meta["features"]:
        logger.error(
            "Feature lists differ between new (%d) and prev (%d) — schemas "
            "incompatible, top-N comparison meaningless.",
            len(new_meta["features"]),
            len(prev_meta["features"]),
        )
        return 1

    features = new_meta["features"]
    logger.info("Schema: %s, %d features", new_meta["schema_version"], len(features))

    df = pd.read_parquet(DATASET).sort_values("scored_at").reset_index(drop=True)
    test = df.iloc[int(len(df) * 0.8):].reset_index(drop=True)
    base_rate = test["label"].mean()
    logger.info(
        "Holdout: %d rows, %d positives (%.2f%% base rate)",
        len(test),
        int(test["label"].sum()),
        base_rate * 100,
    )

    X = test[features].astype(float).values
    y = test["label"].astype(int).values

    new_model = xgb.Booster()
    new_model.load_model(str(NEW_MODEL))
    prev_model = xgb.Booster()
    prev_model.load_model(str(PREV_MODEL))
    dtest = xgb.DMatrix(X, feature_names=features)
    new_proba = new_model.predict(dtest)
    prev_proba = prev_model.predict(dtest)

    logger.info("")
    logger.info("=== Top-N comparison (same holdout, same N) ===")
    logger.info(
        "%5s | %10s | %9s | %9s | %9s | %8s",
        "N",
        "PREV wins",
        "NEW wins",
        "PREV WR",
        "NEW WR",
        "Δ wins",
    )
    logger.info("-" * 70)
    for n in TOP_N_GRID:
        if n > len(y):
            continue
        prev_top = np.argsort(prev_proba)[::-1][:n]
        new_top = np.argsort(new_proba)[::-1][:n]
        prev_wins = int(y[prev_top].sum())
        new_wins = int(y[new_top].sum())
        delta = new_wins - prev_wins
        logger.info(
            "%5d | %10d | %9d | %8.2f%% | %8.2f%% | %+8d",
            n,
            prev_wins,
            new_wins,
            prev_wins / n * 100,
            new_wins / n * 100,
            delta,
        )

    logger.info("")
    for n in [100, 500]:
        prev_set = set(np.argsort(prev_proba)[::-1][:n].tolist())
        new_set = set(np.argsort(new_proba)[::-1][:n].tolist())
        overlap = len(prev_set & new_set)
        logger.info("Top-%d overlap: %d/%d = %.1f%%", n, overlap, n, overlap / n * 100)

    logger.info("")
    logger.info("AUC on shared holdout:")
    logger.info("  PREV: %.4f", roc_auc_score(y, prev_proba))
    logger.info("  NEW:  %.4f", roc_auc_score(y, new_proba))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
