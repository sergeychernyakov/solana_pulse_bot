# pulse_bot/ml/train.py
"""Train + evaluate XGBoost classifiers for entry and exit.

Usage:
    python -m pulse_bot.ml.train --dataset entry
    python -m pulse_bot.ml.train --dataset exit

Reads ``data/ml/{entry,exit}.parquet``, splits train/test chronologically
(last 20% = holdout), fits XGBoost, reports AUC / precision at top-10% /
feature importance, saves ``data/ml/{entry,exit}_model.ubj``.

Time-based split is critical — random shuffle would leak future regime
into training and overstate AUC. We want to know "can the model predict
tomorrow's tokens?" not "can it interpolate between adjacent tokens?".
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
from sklearn.metrics import roc_auc_score

logger = logging.getLogger(__name__)


def load_df(path: Path) -> pd.DataFrame:
    if path.suffix == ".parquet":
        return pd.read_parquet(path)
    return pd.read_csv(path)


def split_chronological(
    df: pd.DataFrame, time_col: str, train_frac: float = 0.8
) -> tuple[pd.DataFrame, pd.DataFrame]:
    df = df.sort_values(time_col).reset_index(drop=True)
    cut = int(len(df) * train_frac)
    return df.iloc[:cut], df.iloc[cut:]


def train_entry(data_path: Path, model_out: Path, split: str = "chrono") -> dict:
    df = load_df(data_path)
    logger.info(
        "Entry dataset: %d rows, label balance %.1f%%", len(df), df.label.mean() * 100
    )

    # Use canonical feature order from features.py — ensures loaded
    # model + live inference path see columns in identical positions.
    from pulse_bot.ml.features import (ENTRY_FEATURE_ORDER,
                                       FEATURE_SCHEMA_VERSION)

    missing = [c for c in ENTRY_FEATURE_ORDER if c not in df.columns]
    if missing:
        raise ValueError(
            f"entry.parquet missing canonical features {missing}. "
            "Rebuild via build_dataset before training."
        )
    feature_cols = list(ENTRY_FEATURE_ORDER)
    if split == "random":
        logger.warning(
            "RANDOM SPLIT — leaks regime/time correlation between train "
            "and test (codex v9: DEBUG ONLY, AUC is not a generalization "
            "measure). Use --split chrono for honest numbers."
        )
        import numpy as _np

        _np.random.seed(42)
        idx = _np.random.permutation(len(df))
        cut = int(len(df) * 0.8)
        train_df = df.iloc[idx[:cut]]
        test_df = df.iloc[idx[cut:]]
        val_df = test_df.iloc[: len(test_df) // 2]
        test_df = test_df.iloc[len(test_df) // 2 :]
    else:
        # 2026-04-23: 70/15/15 chrono split. Val is used to search
        # confidence-gating thresholds + fit Platt calibration without
        # leaking into the held-out test metric.
        df = df.sort_values("scored_at").reset_index(drop=True)
        n = len(df)
        train_end = int(n * 0.70)
        val_end = int(n * 0.85)
        train_df = df.iloc[:train_end]
        val_df = df.iloc[train_end:val_end]
        test_df = df.iloc[val_end:]
    logger.info(
        "Split (%s): train=%d val=%d test=%d",
        split,
        len(train_df),
        len(val_df),
        len(test_df),
    )

    X_train = train_df[feature_cols]
    y_train = train_df["label"]
    X_val = val_df[feature_cols]
    y_val = val_df["label"]
    X_test = test_df[feature_cols]
    y_test = test_df["label"]

    # Class imbalance weight
    pos = int(y_train.sum())
    neg = len(y_train) - pos
    spw = max(neg / max(pos, 1), 1.0)
    logger.info("scale_pos_weight = %.2f (pos=%d, neg=%d)", spw, pos, neg)

    # Codex v9 fix: reduce capacity — 80 positives × depth=5 × 500 trees
    # would massively overfit. With depth=3, min_child_weight=5 each leaf
    # needs ≥5 samples, so ~16 leaves across the tree max.
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
        random_state=42,
    )
    if y_test.sum() == 0 or y_train.sum() == 0:
        logger.warning(
            "Label imbalance on split: train_pos=%d val_pos=%d test_pos=%d — "
            "cannot train/evaluate reliably. Gather more data.",
            int(y_train.sum()),
            int(y_val.sum()),
            int(y_test.sum()),
        )
    # Early-stop on val, not test. Using test would leak.
    model.fit(
        X_train,
        y_train,
        eval_set=[(X_val, y_val)],
        verbose=False,
    )
    proba_val = model.predict_proba(X_val)[:, 1]
    proba_test = model.predict_proba(X_test)[:, 1]
    auc = roc_auc_score(y_test, proba_test) if y_test.sum() > 0 else float("nan")

    # Precision at top 10%
    thresh = (
        proba_test[proba_test.argsort()[::-1][len(proba_test) // 10]]
        if len(proba_test) > 10
        else 0.5
    )
    mask = proba_test >= thresh
    precision_top10 = y_test[mask].mean() if mask.sum() else 0.0

    # ── Confidence-gating: threshold search on val (not test) ──────
    # Returned thresholds feed EntryMLPolicy.decide_with_confidence()
    # at inference time. PROBA_FLOOR tuned so that the low-proba bucket
    # has the lowest achievable WR (saves us from false positives that
    # rules would otherwise buy). PROBA_CEILING tuned to maximise
    # precision on the high-proba bucket subject to min-count floor.
    thresholds = _search_confidence_thresholds(proba_val, y_val.values)
    logger.info(
        "Confidence-gating thresholds (val-tuned): FLOOR=%.3f CEILING=%.3f",
        thresholds["floor"],
        thresholds["ceiling"],
    )
    logger.info(
        "  below FLOOR: n=%d WR=%.2f%%",
        thresholds["floor_n"],
        thresholds["floor_wr"] * 100,
    )
    logger.info(
        "  above CEILING: n=%d WR=%.2f%%",
        thresholds["ceiling_n"],
        thresholds["ceiling_wr"] * 100,
    )

    # ── Platt calibration on val ────────────────────────────────────
    # Fit a single-feature logistic regression proba_raw → label on val
    # so that reported proba at inference matches empirical frequency.
    # XGBoost with scale_pos_weight is uncalibrated by construction.
    calib = _fit_platt(proba_val, y_val.values)

    # ── Evaluate on test (no leakage — test unused until now) ───────
    test_metrics = _evaluate_gated(
        proba_test,
        y_test.values,
        thresholds,
        calib,
    )
    logger.info(
        "Test gating: BUY=%d WR=%.2f%% | SKIP=%d WR=%.2f%% | RULES=%d WR=%.2f%%",
        test_metrics["buy_n"],
        test_metrics["buy_wr"] * 100,
        test_metrics["skip_n"],
        test_metrics["skip_wr"] * 100,
        test_metrics["rules_n"],
        test_metrics["rules_wr"] * 100,
    )

    # Codex v9: use gain importance (less biased by feature scale).
    booster = model.get_booster()
    raw = booster.get_score(importance_type="gain")
    importance = dict(sorted(raw.items(), key=lambda x: -x[1])[:15])

    logger.info("=" * 60)
    logger.info("ENTRY MODEL RESULTS")
    logger.info("  AUC (holdout):         %.4f", auc)
    logger.info("  Base rate (test):      %.2f%%", y_test.mean() * 100)
    logger.info("  Precision at top 10%%:  %.2f%%", precision_top10 * 100)
    logger.info("  Top-15 features:")
    for f, v in importance.items():
        logger.info("    %.4f  %s", v, f)
    logger.info("=" * 60)

    model.save_model(model_out)
    logger.info("Saved model to %s", model_out)
    # Save feature list + thresholds + calibration alongside model
    meta_out = model_out.with_suffix(".meta.json")
    meta_out.write_text(
        json.dumps(
            {
                "features": feature_cols,
                "schema_version": FEATURE_SCHEMA_VERSION,
                "auc": auc,
                "precision_top10": precision_top10,
                "base_rate": float(y_test.mean()),
                "train_rows": len(train_df),
                "val_rows": len(val_df),
                "test_rows": len(test_df),
                "confidence_thresholds": thresholds,
                "calibration": calib,
                "test_gated": test_metrics,
            },
            indent=2,
        )
    )
    return {
        "auc": auc,
        "precision_top10": precision_top10,
        "thresholds": thresholds,
        "test_gated": test_metrics,
    }


def _search_confidence_thresholds(
    proba: "np.ndarray",
    y: "np.ndarray",
) -> dict:
    """Grid-search PROBA_FLOOR and PROBA_CEILING on a validation set.

    FLOOR: choose proba value such that the ``below-floor`` bucket is at
    least ``min_bucket`` samples and has the lowest empirical WR.
    CEILING: choose proba value such that ``above-ceiling`` bucket is at
    least ``min_bucket`` samples and has the highest empirical WR.

    Hyperparameters intentionally coarse — val set will be tiny (15% of
    661 ≈ 99 rows, ~18 positives). Finer grids would overfit.
    """
    import numpy as np

    min_bucket = max(10, int(0.1 * len(proba)))
    grid = np.linspace(0.10, 0.90, 17)  # 0.05 steps
    base_wr = float(y.mean()) if len(y) else 0.0

    # FLOOR: seek low WR below threshold.
    best_floor = 0.30
    best_floor_wr = 1.0
    for t in grid:
        mask = proba < t
        if mask.sum() < min_bucket:
            continue
        wr = float(y[mask].mean())
        if wr < best_floor_wr:
            best_floor_wr = wr
            best_floor = float(t)
    # CEILING: seek high WR above threshold.
    best_ceiling = 0.70
    best_ceiling_wr = 0.0
    for t in grid:
        mask = proba >= t
        if mask.sum() < min_bucket:
            continue
        wr = float(y[mask].mean())
        if wr > best_ceiling_wr:
            best_ceiling_wr = wr
            best_ceiling = float(t)
    # Final recount with chosen thresholds
    below = proba < best_floor
    above = proba >= best_ceiling
    return {
        "floor": best_floor,
        "ceiling": best_ceiling,
        "floor_n": int(below.sum()),
        "floor_wr": float(y[below].mean()) if below.sum() else 0.0,
        "ceiling_n": int(above.sum()),
        "ceiling_wr": float(y[above].mean()) if above.sum() else 0.0,
        "val_base_rate": base_wr,
    }


def _fit_platt(proba: "np.ndarray", y: "np.ndarray") -> dict:
    """Platt scaling: fit 1-D logistic regression proba_raw → label.

    Returns {a, b} such that calibrated_proba = sigmoid(a*raw + b).
    Uses pure numpy to avoid a sklearn dependency at inference time.
    """
    import numpy as np

    x = np.asarray(proba, dtype=float).reshape(-1, 1)
    yv = np.asarray(y, dtype=float).reshape(-1)
    if yv.sum() == 0 or yv.sum() == len(yv):
        # Degenerate case — identity calibration.
        return {"a": 1.0, "b": 0.0, "note": "degenerate"}
    # Gradient descent. For such small N this is adequate.
    a, b = 1.0, 0.0
    lr, steps = 0.5, 500
    for _ in range(steps):
        z = a * x.ravel() + b
        p = 1.0 / (1.0 + np.exp(-z))
        err = p - yv
        ga = float((err * x.ravel()).mean())
        gb = float(err.mean())
        a -= lr * ga
        b -= lr * gb
    return {"a": float(a), "b": float(b)}


def _evaluate_gated(
    proba: "np.ndarray",
    y: "np.ndarray",
    thresholds: dict,
    calib: dict,
) -> dict:
    """Report BUY/SKIP/RULES bucket sizes + WRs on held-out test."""
    import numpy as np

    proba = np.asarray(proba, dtype=float)
    buy = proba >= thresholds["ceiling"]
    skip = proba < thresholds["floor"]
    rules = (~buy) & (~skip)
    return {
        "buy_n": int(buy.sum()),
        "buy_wr": float(y[buy].mean()) if buy.sum() else 0.0,
        "skip_n": int(skip.sum()),
        "skip_wr": float(y[skip].mean()) if skip.sum() else 0.0,
        "rules_n": int(rules.sum()),
        "rules_wr": float(y[rules].mean()) if rules.sum() else 0.0,
        "test_base_rate": float(y.mean()) if len(y) else 0.0,
    }


def train_exit(data_path: Path, model_out: Path) -> dict:
    df = load_df(data_path)
    logger.info(
        "Exit dataset: %d rows, label balance %.1f%%", len(df), df.label.mean() * 100
    )

    # Codex v9 fix: chrono split BY ENTRY_TS with mint-level grouping.
    # Old code split by row order — rows are per-mint-contiguous, so it
    # wasn't temporal at all. Now: find the timestamp whose cumulative
    # mint count hits 80%, and put all samples of every mint ≤ that time
    # in train (so no mint straddles the split).
    if "entry_ts" not in df.columns:
        raise ValueError(
            "Exit dataset missing 'entry_ts' column — rebuild via "
            "pulse_bot.ml.build_dataset (codex v9 added this)."
        )
    mints_order = df.drop_duplicates("mint").sort_values("entry_ts")["mint"].tolist()
    cut_mint = mints_order[int(len(mints_order) * 0.8)]
    cut_ts = df.loc[df.mint == cut_mint, "entry_ts"].iloc[0]
    train_mask = df.entry_ts < cut_ts
    # Canonical exit order from features.py — ensures the .ubj persisted
    # here matches what ExitMLPolicy + live ExitManager extractor use.
    from pulse_bot.ml.features import EXIT_FEATURE_ORDER

    missing = [c for c in EXIT_FEATURE_ORDER if c not in df.columns]
    if missing:
        raise ValueError(
            f"exit.parquet missing canonical features {missing}. "
            "Rebuild via build_dataset before training."
        )
    feature_cols = list(EXIT_FEATURE_ORDER)
    X_train = df.loc[train_mask, feature_cols]
    y_train = df.loc[train_mask, "label"]
    X_test = df.loc[~train_mask, feature_cols]
    y_test = df.loc[~train_mask, "label"]
    logger.info(
        "Chrono split (by entry_ts, mint-grouped): train=%d, test=%d, "
        "train_mints=%d, test_mints=%d",
        len(X_train),
        len(X_test),
        df.loc[train_mask, "mint"].nunique(),
        df.loc[~train_mask, "mint"].nunique(),
    )

    pos = int(y_train.sum())
    neg = len(y_train) - pos
    spw = max(neg / max(pos, 1), 1.0)

    model = xgb.XGBClassifier(
        n_estimators=200,
        max_depth=3,
        min_child_weight=5,
        learning_rate=0.05,
        subsample=0.8,
        colsample_bytree=0.8,
        scale_pos_weight=spw,
        objective="binary:logistic",
        eval_metric="auc",
        early_stopping_rounds=20,
        random_state=42,
    )
    if y_test.sum() == 0 or y_train.sum() == 0:
        logger.warning(
            "Exit split: train_pos=%d test_pos=%d — "
            "insufficient positives in at least one fold.",
            int(y_train.sum()),
            int(y_test.sum()),
        )
    model.fit(
        X_train,
        y_train,
        eval_set=[(X_test, y_test)],
        verbose=False,
    )
    proba = model.predict_proba(X_test)[:, 1]
    auc = roc_auc_score(y_test, proba) if y_test.sum() > 0 else float("nan")

    booster = model.get_booster()
    raw = booster.get_score(importance_type="gain")
    importance = dict(sorted(raw.items(), key=lambda x: -x[1])[:15])

    logger.info("=" * 60)
    logger.info("EXIT MODEL RESULTS")
    logger.info("  AUC (holdout): %.4f", auc)
    logger.info("  Base rate (test): %.2f%%", y_test.mean() * 100)
    logger.info("  Top features:")
    for f, v in importance.items():
        logger.info("    %.4f  %s", v, f)
    logger.info("=" * 60)

    model.save_model(model_out)
    meta_out = model_out.with_suffix(".meta.json")
    meta_out.write_text(
        json.dumps(
            {
                "features": feature_cols,
                "auc": auc,
                "base_rate": float(y_test.mean()),
            },
            indent=2,
        )
    )
    logger.info("Saved model to %s", model_out)
    return {"auc": auc}


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-dir", default="data/ml")
    ap.add_argument("--dataset", choices=["entry", "exit", "both"], default="both")
    ap.add_argument(
        "--split",
        choices=["chrono", "random"],
        default="chrono",
        help="chrono = honest (default). random = DEBUG ONLY, AUC leaks "
        "regime; codex v9 audit forbids citing it as generalization.",
    )
    args = ap.parse_args()

    data_dir = Path(args.data_dir)

    if args.dataset in ("entry", "both"):
        data = next(
            (
                data_dir / n
                for n in ["entry.parquet", "entry.csv"]
                if (data_dir / n).exists()
            ),
            None,
        )
        if data is None:
            logger.error("No entry.parquet or entry.csv in %s", data_dir)
            sys.exit(1)
        train_entry(data, data_dir / "entry_model.ubj", split=args.split)

    if args.dataset in ("exit", "both"):
        data = next(
            (
                data_dir / n
                for n in ["exit.parquet", "exit.csv"]
                if (data_dir / n).exists()
            ),
            None,
        )
        if data is None:
            logger.error("No exit.parquet or exit.csv in %s", data_dir)
            sys.exit(1)
        train_exit(data, data_dir / "exit_model.ubj")


if __name__ == "__main__":
    sys.exit(main())
