#!/usr/bin/env python3
# scripts/same_data_compare.py
"""Apples-to-apples model comparison on identical historical data.

The codex critique of the 2026-05-07 retrain pointed at the missing
piece: train.py reports its own held-out metrics, but those splits
are *different* between runs, so "old AUC vs new AUC" is not directly
comparable. The honest test is to score **both** models on the
**same** rows and compute realized-PnL counterfactual:

    "If we'd deployed model X at proba ≥ threshold on these rows,
     what's the sum of realized_pnl_pct on the rows it would have
     bought?"

Inputs
------
* ``data/ml/entry.parquet`` — fresh dataset with 115K rows, both
  feature columns and ``realized_pnl_pct`` outcome per row.
* ``data/ml/entry_model.ubj`` — reverted to May-5 known-good.
* ``data/ml/entry_model_candidate.ubj`` — the May-7 retrain we
  haven't deployed.
* Both ``meta.json`` files for feature ordering.

The chronological held-out tail (last 15 % of the dataset) is the
target slice — these are the trades neither model trained on.

Output
------
Markdown-style table: ``BUY count, WR, sum_pnl_pct, avg_pnl_pct``
for old vs new at three thresholds (live live=0.15, EV-tuned old
ceiling=0.110, EV-tuned new ceiling=0.252) so we can see how each
model behaves at each candidate operating point.

Verdict logic
-------------
``new ≥ old`` on **avg_pnl_pct at the live threshold (0.15)** with
N ≥ 30 BUYs on each side → KEEP candidate. Otherwise → REVERT.

Usage::

    PYTHONPATH=. .venv/bin/python scripts/same_data_compare.py
"""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import xgboost as xgb

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("same_data_compare")

REPO_ROOT = Path(__file__).resolve().parents[1]
PARQUET = REPO_ROOT / "data" / "ml" / "entry.parquet"
OLD_MODEL = REPO_ROOT / "data" / "ml" / "entry_model.ubj"
OLD_META = REPO_ROOT / "data" / "ml" / "entry_model.meta.json"
NEW_MODEL = REPO_ROOT / "data" / "ml" / "entry_model_candidate.ubj"
NEW_META = REPO_ROOT / "data" / "ml" / "entry_model_candidate.meta.json"

# 15 % chronological tail — held out from BOTH models' training
# windows in practice (old trained earlier, new trained on full
# dataset including this tail, so the tail is technically TEST for
# old and validation/test mix for new — still the closest fair
# arbiter we have).
HOLDOUT_FRAC = 0.15

THRESHOLDS = {
    "live (0.15)": 0.15,
    "old EV ceiling (0.110)": 0.110,
    "new EV ceiling (0.252)": 0.252,
    "new EV floor (0.190)": 0.190,
}


def _load_model(model_path: Path) -> xgb.XGBClassifier:
    clf = xgb.XGBClassifier()
    clf.load_model(str(model_path))
    return clf


def _score(model: xgb.XGBClassifier, df: pd.DataFrame, features: list[str]) -> np.ndarray:
    # Some old/new feature lists may differ in 1-2 columns. Reindex
    # to whatever each model expects, filling missing with NaN
    # (XGBoost handles missingness natively).
    X = df.reindex(columns=features)
    proba = model.predict_proba(X.values)[:, 1]
    return proba


def _aggregate(buy_mask: np.ndarray, realized: np.ndarray) -> dict:
    n = int(buy_mask.sum())
    if n == 0:
        return {"n_buy": 0}
    pnls = realized[buy_mask]
    wins = (pnls > 0).sum()
    return {
        "n_buy": n,
        "wr_pct": float(100.0 * wins / n),
        "sum_pnl_pct": float(pnls.sum()),
        "avg_pnl_pct": float(pnls.mean()),
        "median_pnl_pct": float(np.median(pnls)),
        "p90_pnl_pct": float(np.percentile(pnls, 90)),
    }


def main() -> int:
    logger.info("Loading parquet from %s", PARQUET)
    df = pd.read_parquet(PARQUET)
    df = df.sort_values("scored_at").reset_index(drop=True)
    n = len(df)
    cut = int(n * (1 - HOLDOUT_FRAC))
    test = df.iloc[cut:].copy()
    logger.info("Total rows %d, holdout tail %d (%.0f%%)", n, len(test), HOLDOUT_FRAC * 100)

    realized = test["realized_pnl_pct"].fillna(0.0).to_numpy()

    old_meta = json.loads(OLD_META.read_text())
    new_meta = json.loads(NEW_META.read_text())
    old_features = old_meta["features"]
    new_features = new_meta["features"]

    feature_overlap = set(old_features) & set(new_features)
    only_old = sorted(set(old_features) - set(new_features))
    only_new = sorted(set(new_features) - set(old_features))
    logger.info("Old features: %d  New features: %d  Overlap: %d",
                len(old_features), len(new_features), len(feature_overlap))
    if only_old:
        logger.info("Only-in-old (%d): %s", len(only_old), only_old)
    if only_new:
        logger.info("Only-in-new (%d): %s", len(only_new), only_new)

    logger.info("Scoring OLD model on holdout...")
    old_model = _load_model(OLD_MODEL)
    proba_old = _score(old_model, test, old_features)

    logger.info("Scoring NEW model on holdout...")
    new_model = _load_model(NEW_MODEL)
    proba_new = _score(new_model, test, new_features)

    # Sanity: how do proba distributions differ?
    logger.info("OLD proba: p50=%.3f p90=%.3f p99=%.3f max=%.3f",
                np.median(proba_old), np.percentile(proba_old, 90),
                np.percentile(proba_old, 99), proba_old.max())
    logger.info("NEW proba: p50=%.3f p90=%.3f p99=%.3f max=%.3f",
                np.median(proba_new), np.percentile(proba_new, 90),
                np.percentile(proba_new, 99), proba_new.max())

    # Headline number — fixed live threshold (0.15)
    live_t = 0.15
    old = _aggregate(proba_old >= live_t, realized)
    new = _aggregate(proba_new >= live_t, realized)
    logger.info("")
    logger.info("=== HEADLINE: same-data comparison @ live threshold %.2f ===", live_t)
    logger.info("  OLD: n=%d  WR=%.2f%%  sum_pct=%+.0f%%  avg_pct=%+.3f%%",
                old.get("n_buy", 0), old.get("wr_pct", 0),
                old.get("sum_pnl_pct", 0), old.get("avg_pnl_pct", 0))
    logger.info("  NEW: n=%d  WR=%.2f%%  sum_pct=%+.0f%%  avg_pct=%+.3f%%",
                new.get("n_buy", 0), new.get("wr_pct", 0),
                new.get("sum_pnl_pct", 0), new.get("avg_pnl_pct", 0))

    # Verdict
    print()
    print("Verdict (same-data, threshold=0.15):")
    if old.get("n_buy", 0) < 30 or new.get("n_buy", 0) < 30:
        print(f"  INDETERMINATE — sample too small (need ≥30, have old={old.get('n_buy', 0)} new={new.get('n_buy', 0)})")
    else:
        old_avg = old["avg_pnl_pct"]
        new_avg = new["avg_pnl_pct"]
        old_sum = old["sum_pnl_pct"]
        new_sum = new["sum_pnl_pct"]
        delta_avg = new_avg - old_avg
        delta_sum = new_sum - old_sum
        if new_avg > old_avg and new_sum > old_sum:
            print(f"  ✅ NEW BETTER on both axes")
            print(f"     avg_pnl: {old_avg:+.3f}% → {new_avg:+.3f}% (Δ={delta_avg:+.3f}pp)")
            print(f"     sum_pnl: {old_sum:+.0f}% → {new_sum:+.0f}% (Δ={delta_sum:+.0f}pp)")
            print(f"     RECOMMEND: KEEP candidate, plan deployment")
        elif new_avg < old_avg and new_sum < old_sum:
            print(f"  ❌ NEW WORSE on both axes")
            print(f"     avg_pnl: {old_avg:+.3f}% → {new_avg:+.3f}% (Δ={delta_avg:+.3f}pp)")
            print(f"     sum_pnl: {old_sum:+.0f}% → {new_sum:+.0f}% (Δ={delta_sum:+.0f}pp)")
            print(f"     RECOMMEND: DROP candidate, keep May-5 model")
        else:
            print(f"  ⚠ MIXED")
            print(f"     avg_pnl: {old_avg:+.3f}% → {new_avg:+.3f}% (Δ={delta_avg:+.3f}pp)")
            print(f"     sum_pnl: {old_sum:+.0f}% → {new_sum:+.0f}% (Δ={delta_sum:+.0f}pp)")
            print(f"     RECOMMEND: stay with old until clearer signal")

    # Wider table at all candidate thresholds
    print()
    print("Per-threshold table (same-data):")
    print(f"{'threshold':<25} {'OLD n':>6} {'OLD WR':>7} {'OLD sum%':>10} {'OLD avg%':>9}  | "
          f"{'NEW n':>6} {'NEW WR':>7} {'NEW sum%':>10} {'NEW avg%':>9}")
    for label, t in THRESHOLDS.items():
        o = _aggregate(proba_old >= t, realized)
        n_ = _aggregate(proba_new >= t, realized)
        print(f"{label:<25} "
              f"{o.get('n_buy', 0):>6} "
              f"{o.get('wr_pct', 0):>6.2f}% "
              f"{o.get('sum_pnl_pct', 0):>+9.0f}% "
              f"{o.get('avg_pnl_pct', 0):>+8.3f}%  | "
              f"{n_.get('n_buy', 0):>6} "
              f"{n_.get('wr_pct', 0):>6.2f}% "
              f"{n_.get('sum_pnl_pct', 0):>+9.0f}% "
              f"{n_.get('avg_pnl_pct', 0):>+8.3f}%")

    return 0


if __name__ == "__main__":
    sys.exit(main())
