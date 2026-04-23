# pulse_bot/ml/feature_stability.py
"""Feature-stability analysis for the entry model.

Runs N independent trainings with different ``random_state`` seeds on the
existing chronological split, then aggregates gain importance per feature
to classify each as:

* ``stable_dead``   — gain == 0 in every seed. Safe to remove.
* ``stable_active`` — gain > 0 in every seed. Safe to keep.
* ``unstable``      — gain > 0 in some seeds but not all. Keep for now.

Purpose: XGBoost is stochastic (subsample + colsample_bytree) so a
single-run gain=0 does not prove a feature is useless. Running five
seeds is the minimum to separate stable signal from seed-variance noise.

Usage: ``python -m pulse_bot.ml.feature_stability [--n-seeds 5]``
"""

from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path
from statistics import mean, stdev

import pandas as pd
import xgboost as xgb
from sklearn.metrics import roc_auc_score

from pulse_bot.ml.features import ENTRY_FEATURE_ORDER

logger = logging.getLogger(__name__)


def _gains_from_model(
    model: xgb.XGBClassifier,
    feature_cols: list[str],
) -> dict[str, float]:
    raw = model.get_booster().get_score(importance_type="gain")
    out = {f: 0.0 for f in feature_cols}
    for k, v in raw.items():
        if k.startswith("f") and k[1:].isdigit():
            out[feature_cols[int(k[1:])]] = float(v)
        elif k in out:
            out[k] = float(v)
    return out


def run_stability(
    data_path: Path = Path("data/ml/entry.parquet"),
    n_seeds: int = 5,
) -> dict:
    df = pd.read_parquet(data_path).sort_values("scored_at").reset_index(drop=True)
    feature_cols = list(ENTRY_FEATURE_ORDER)
    missing = [c for c in feature_cols if c not in df.columns]
    if missing:
        raise ValueError(
            f"Dataset missing canonical features {missing}. "
            "Rebuild via build_dataset."
        )
    n = len(df)
    train_end = int(n * 0.70)
    val_end = int(n * 0.85)
    X_train = df.iloc[:train_end][feature_cols]
    y_train = df.iloc[:train_end]["label"]
    X_val = df.iloc[train_end:val_end][feature_cols]
    y_val = df.iloc[train_end:val_end]["label"]
    X_test = df.iloc[val_end:][feature_cols]
    y_test = df.iloc[val_end:]["label"]

    pos = int(y_train.sum())
    neg = len(y_train) - pos
    spw = max(neg / max(pos, 1), 1.0)

    per_seed: dict[int, dict] = {}
    for seed in range(1, n_seeds + 1):
        model = xgb.XGBClassifier(
            n_estimators=150,
            max_depth=3,
            min_child_weight=5,
            learning_rate=0.05,
            subsample=0.8,
            colsample_bytree=0.8,
            scale_pos_weight=spw,
            objective="binary:logistic",
            eval_metric="auc",
            early_stopping_rounds=20,
            random_state=seed,
        )
        model.fit(X_train, y_train, eval_set=[(X_val, y_val)], verbose=False)
        proba = model.predict_proba(X_test)[:, 1]
        auc = roc_auc_score(y_test, proba) if y_test.sum() > 0 else float("nan")
        per_seed[seed] = {
            "auc": float(auc),
            "gains": _gains_from_model(model, feature_cols),
        }
        logger.info("seed=%d AUC=%.4f", seed, auc)

    # Aggregate
    stable_dead: list[str] = []
    stable_active: list[str] = []
    unstable: list[str] = []
    summary: dict[str, dict] = {}
    for feat in feature_cols:
        vals = [per_seed[s]["gains"][feat] for s in range(1, n_seeds + 1)]
        nonzero = sum(1 for v in vals if v > 0)
        mn = mean(vals) if vals else 0.0
        sd = stdev(vals) if len(vals) > 1 else 0.0
        summary[feat] = {
            "nonzero_seeds": nonzero,
            "n_seeds": n_seeds,
            "mean_gain": mn,
            "stdev_gain": sd,
            "per_seed": vals,
        }
        if nonzero == 0:
            stable_dead.append(feat)
        elif nonzero == n_seeds:
            stable_active.append(feat)
        else:
            unstable.append(feat)

    aucs = [per_seed[s]["auc"] for s in range(1, n_seeds + 1)]
    return {
        "n_seeds": n_seeds,
        "auc_mean": mean(aucs),
        "auc_stdev": stdev(aucs) if len(aucs) > 1 else 0.0,
        "auc_per_seed": aucs,
        "stable_dead": stable_dead,
        "stable_active": stable_active,
        "unstable": unstable,
        "per_feature": summary,
    }


def _print_report(result: dict) -> None:
    print(
        f"\nAUC across {result['n_seeds']} seeds: mean={result['auc_mean']:.4f} "
        f"stdev={result['auc_stdev']:.4f}  per-seed={result['auc_per_seed']}"
    )
    print(
        f"\nClassification ({len(result['stable_dead'])} dead | "
        f"{len(result['unstable'])} unstable | "
        f"{len(result['stable_active'])} active):\n"
    )
    print("=== STABLE DEAD (gain=0 in all seeds → safe to remove) ===")
    for f in result["stable_dead"]:
        print(f"  · {f}")
    print("\n=== UNSTABLE (gain>0 in some seeds → keep, risky to remove) ===")
    for f in result["unstable"]:
        info = result["per_feature"][f]
        print(
            f"  · {f:42s}  nz={info['nonzero_seeds']}/{info['n_seeds']}  "
            f"mean={info['mean_gain']:.2f}  ±{info['stdev_gain']:.2f}"
        )
    print("\n=== STABLE ACTIVE top-15 by mean gain ===")
    active_sorted = sorted(
        result["stable_active"],
        key=lambda f: -result["per_feature"][f]["mean_gain"],
    )[:15]
    for f in active_sorted:
        info = result["per_feature"][f]
        print(f"  {info['mean_gain']:7.2f}  ±{info['stdev_gain']:5.2f}  {f}")


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    ap = argparse.ArgumentParser()
    ap.add_argument("--n-seeds", type=int, default=5)
    ap.add_argument("--data", default="data/ml/entry.parquet")
    ap.add_argument("--out", default="data/ml/feature_stability.json")
    args = ap.parse_args()
    result = run_stability(Path(args.data), n_seeds=args.n_seeds)
    Path(args.out).write_text(json.dumps(result, indent=2))
    _print_report(result)
    print(f"\nFull JSON report saved to {args.out}")


if __name__ == "__main__":
    main()
