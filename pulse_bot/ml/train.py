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


# Codex Q4 #1 recommendation (2026-04-23): regression head trains against
# realized_pnl_pct (continuous) rather than binary label (sign only). Every
# labeled row now carries magnitude information, so gradients use 5-10×
# more signal per example. Label column is ``realized_pnl_pct`` — already
# computed by build_entry_dataset alongside binary label, so no dataset
# rebuild required. Guardrail: values clipped to [-100, +300] to avoid
# rare extreme rows dominating MSE.
PNL_CLIP_LO = -100.0
PNL_CLIP_HI = 300.0


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
    from pulse_bot.ml.features import ENTRY_FEATURE_ORDER, FEATURE_SCHEMA_VERSION

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
    #
    # 2026-04-24: moved to config so optimizer can sweep them. Defaults
    # preserve the old values. Override via PULSE_ENTRY_* env vars or
    # `set_config_for_tests` in unit tests.
    from pulse_bot.config import get_config

    _cfg = get_config()
    model = xgb.XGBClassifier(
        n_estimators=_cfg.entry_train_n_estimators,
        max_depth=_cfg.entry_train_max_depth,
        min_child_weight=_cfg.entry_train_min_child_weight,
        learning_rate=_cfg.entry_train_learning_rate,
        subsample=_cfg.entry_train_subsample,
        colsample_bytree=_cfg.entry_train_colsample_bytree,
        scale_pos_weight=spw,
        objective="binary:logistic",
        eval_metric="auc",
        early_stopping_rounds=20,
        random_state=42,
    )
    logger.info(
        "XGBoost hparams: n_est=%d depth=%d lr=%.3f min_child=%d "
        "subsample=%.2f colsample=%.2f",
        _cfg.entry_train_n_estimators,
        _cfg.entry_train_max_depth,
        _cfg.entry_train_learning_rate,
        _cfg.entry_train_min_child_weight,
        _cfg.entry_train_subsample,
        _cfg.entry_train_colsample_bytree,
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
    # 2026-04-27: switched WR-based search to EV-based (codex review).
    # Now optimizes mean realized_pnl_pct per bucket — i.e. money, not
    # accuracy. Falls back to WR if pnl column missing.
    pnl_val = (
        val_df["realized_pnl_pct"].values
        if "realized_pnl_pct" in val_df.columns
        else None
    )
    thresholds = _search_confidence_thresholds(proba_val, y_val.values, pnl=pnl_val)
    logger.info(
        "Confidence-gating thresholds (val-tuned, objective=%s): "
        "FLOOR=%.3f CEILING=%.3f",
        thresholds.get("objective", "wr"),
        thresholds["floor"],
        thresholds["ceiling"],
    )
    if "floor_ev" in thresholds:
        logger.info(
            "  below FLOOR: n=%d WR=%.2f%% EV=%+.2f%% (val_base=%+.2f%%)",
            thresholds["floor_n"],
            thresholds["floor_wr"] * 100,
            thresholds["floor_ev"],
            thresholds["val_base_ev"],
        )
        logger.info(
            "  above CEILING: n=%d WR=%.2f%% EV=%+.2f%%",
            thresholds["ceiling_n"],
            thresholds["ceiling_wr"] * 100,
            thresholds["ceiling_ev"],
        )
    else:
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
    importance = dict(sorted(raw.items(), key=lambda x: -float(x[1]))[:15])  # type: ignore[arg-type]

    # Bootstrap 95% CIs (codex 2026-04-23) — point estimates at N_test=100
    # are ±10pp noise; CI is the only honest way to report them.
    auc_lo, auc_hi = _bootstrap_ci(proba_test, y_test.values, "auc")
    prec_lo, prec_hi = _bootstrap_ci(proba_test, y_test.values, "precision_top10")

    logger.info("=" * 60)
    logger.info("ENTRY MODEL RESULTS")
    logger.info(
        "  AUC (holdout):         %.4f   95%% CI [%.3f, %.3f]",
        auc,
        auc_lo,
        auc_hi,
    )
    logger.info("  Base rate (test):      %.2f%%", y_test.mean() * 100)
    logger.info(
        "  Precision at top 10%%:  %.2f%%   95%% CI [%.0f%%, %.0f%%]",
        precision_top10 * 100,
        prec_lo * 100,
        prec_hi * 100,
    )
    logger.info("  Top-15 features:")
    for f, v in importance.items():
        logger.info("    %.4f  %s", v, f)
    logger.info("=" * 60)

    # Codex 2026-04-28 critical fixes (d), (f): pre-save sanity gates.
    # Compute health flags BEFORE overwriting model artifact, so the live
    # policy can refuse to act on a broken retrain.
    health = _entry_model_health_check(
        model_out=model_out,
        proba_val=proba_val,
        new_auc=auc,
        thresholds=thresholds,
    )
    model.save_model(model_out)
    logger.info("Saved model to %s (health=%s)", model_out, health["status"])
    # Save feature list + thresholds + calibration alongside model.
    # config_hash + config_values pin the training-time PulseBotConfig
    # subset that affects labels/features/hparams. Live policy compares
    # against runtime config and WARNs on drift (protects against silent
    # Option-B style label mismatches; see config_hash.py docstring).
    from pulse_bot.ml.config_hash import (
        TRAIN_RELEVANT_FIELDS,
        compute_config_hash,
        extract_relevant_fields,
    )

    cfg_for_hash = _cfg
    config_hash = compute_config_hash(cfg_for_hash)
    config_values = extract_relevant_fields(cfg_for_hash)
    meta_out = model_out.with_suffix(".meta.json")
    meta_out.write_text(
        json.dumps(
            {
                "features": feature_cols,
                "schema_version": FEATURE_SCHEMA_VERSION,
                "auc": auc,
                "auc_ci95": [auc_lo, auc_hi],
                "precision_top10": precision_top10,
                "precision_top10_ci95": [prec_lo, prec_hi],
                "base_rate": float(y_test.mean()),
                "train_rows": len(train_df),
                "val_rows": len(val_df),
                "test_rows": len(test_df),
                "confidence_thresholds": thresholds,
                "calibration": calib,
                "test_gated": test_metrics,
                "config_hash": config_hash,
                "config_fields_version": 1,
                "config_field_names": list(TRAIN_RELEVANT_FIELDS),
                "config_values": config_values,
                "model_health": health,
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


def train_entry_t30(data_path: Path, model_out: Path, split: str = "chrono") -> dict:
    """Train the @T+30 dual-snapshot entry classifier (Phase 3).

    Mirrors ``train_entry`` but uses the T+30 feature schema from
    ``ENTRY_T30_FEATURE_ORDER``. Saves to ``data/ml/entry_model_t30.ubj``
    + matching meta.json. Bumps ``schema_version`` to
    ``FEATURE_SCHEMA_VERSION_T30`` so the live ``EntryT30Policy`` will
    refuse to load a stale T+30 file.
    """
    df = load_df(data_path)
    logger.info(
        "T30 entry dataset: %d rows, label balance %.1f%%",
        len(df),
        df.label.mean() * 100,
    )

    from pulse_bot.ml.features import (
        ENTRY_T30_FEATURE_ORDER,
        FEATURE_SCHEMA_VERSION_T30,
    )

    missing = [c for c in ENTRY_T30_FEATURE_ORDER if c not in df.columns]
    if missing:
        raise ValueError(
            f"entry_t30.parquet missing canonical features {missing}. "
            "Rebuild via pulse_bot.ml.build_dataset_t30 before training."
        )
    feature_cols = list(ENTRY_T30_FEATURE_ORDER)

    if split == "random":
        logger.warning(
            "RANDOM SPLIT — leaks regime/time correlation. Use chrono "
            "for honest numbers."
        )
        np.random.seed(42)
        idx = np.random.permutation(len(df))
        cut = int(len(df) * 0.8)
        train_df = df.iloc[idx[:cut]]
        test_df = df.iloc[idx[cut:]]
        val_df = test_df.iloc[: len(test_df) // 2]
        test_df = test_df.iloc[len(test_df) // 2 :]
    else:
        df = df.sort_values("scored_at").reset_index(drop=True)
        n = len(df)
        train_end = int(n * 0.70)
        val_end = int(n * 0.85)
        train_df = df.iloc[:train_end]
        val_df = df.iloc[train_end:val_end]
        test_df = df.iloc[val_end:]
    logger.info(
        "T30 split (%s): train=%d val=%d test=%d",
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

    pos = int(y_train.sum())
    neg = len(y_train) - pos
    spw = max(neg / max(pos, 1), 1.0)

    from pulse_bot.config import get_config

    _cfg = get_config()
    model = xgb.XGBClassifier(
        n_estimators=_cfg.entry_train_n_estimators,
        max_depth=_cfg.entry_train_max_depth,
        min_child_weight=_cfg.entry_train_min_child_weight,
        learning_rate=_cfg.entry_train_learning_rate,
        subsample=_cfg.entry_train_subsample,
        colsample_bytree=_cfg.entry_train_colsample_bytree,
        scale_pos_weight=spw,
        objective="binary:logistic",
        eval_metric="auc",
        early_stopping_rounds=20,
        random_state=42,
    )
    if y_test.sum() == 0 or y_train.sum() == 0:
        logger.warning(
            "T30 label imbalance: train_pos=%d val_pos=%d test_pos=%d — "
            "cannot train/evaluate reliably.",
            int(y_train.sum()),
            int(y_val.sum()),
            int(y_test.sum()),
        )
    model.fit(X_train, y_train, eval_set=[(X_val, y_val)], verbose=False)

    proba_val = model.predict_proba(X_val)[:, 1]
    proba_test = model.predict_proba(X_test)[:, 1]
    auc = roc_auc_score(y_test, proba_test) if y_test.sum() > 0 else float("nan")

    thresh = (
        proba_test[proba_test.argsort()[::-1][len(proba_test) // 10]]
        if len(proba_test) > 10
        else 0.5
    )
    mask = proba_test >= thresh
    precision_top10 = y_test[mask].mean() if mask.sum() else 0.0

    pnl_val_t30 = (
        val_df["realized_pnl_pct"].values
        if "realized_pnl_pct" in val_df.columns
        else None
    )
    thresholds = _search_confidence_thresholds(proba_val, y_val.values, pnl=pnl_val_t30)
    calib = _fit_platt(proba_val, y_val.values)
    test_metrics = _evaluate_gated(proba_test, y_test.values, thresholds, calib)

    booster = model.get_booster()
    raw = booster.get_score(importance_type="gain")
    importance = dict(sorted(raw.items(), key=lambda x: -float(x[1]))[:15])  # type: ignore[arg-type]

    logger.info("=" * 60)
    logger.info("ENTRY @T+30 MODEL RESULTS")
    logger.info("  AUC (holdout):         %.4f", auc)
    logger.info("  Base rate (test):      %.2f%%", y_test.mean() * 100)
    logger.info("  Precision at top 10%%:  %.2f%%", precision_top10 * 100)
    logger.info(
        "  Gating: BUY=%d WR=%.2f%% | SKIP=%d WR=%.2f%% | RULES=%d WR=%.2f%%",
        test_metrics["buy_n"],
        test_metrics["buy_wr"] * 100,
        test_metrics["skip_n"],
        test_metrics["skip_wr"] * 100,
        test_metrics["rules_n"],
        test_metrics["rules_wr"] * 100,
    )
    logger.info("  Top-15 features:")
    for f, v in importance.items():
        logger.info("    %.4f  %s", v, f)
    logger.info("=" * 60)

    model.save_model(model_out)
    from pulse_bot.ml.config_hash import (
        TRAIN_RELEVANT_FIELDS,
        compute_config_hash,
        extract_relevant_fields,
    )

    config_hash = compute_config_hash(_cfg)
    config_values = extract_relevant_fields(_cfg)
    meta_out = model_out.with_suffix(".meta.json")
    meta_out.write_text(
        json.dumps(
            {
                "features": feature_cols,
                "schema_version": FEATURE_SCHEMA_VERSION_T30,
                "auc": auc,
                "precision_top10": float(precision_top10),
                "base_rate": float(y_test.mean()),
                "train_rows": len(train_df),
                "val_rows": len(val_df),
                "test_rows": len(test_df),
                "confidence_thresholds": thresholds,
                "calibration": calib,
                "test_gated": test_metrics,
                "config_hash": config_hash,
                "config_fields_version": 1,
                "config_field_names": list(TRAIN_RELEVANT_FIELDS),
                "config_values": config_values,
                "snapshot_age_sec": 30.0,
            },
            indent=2,
        )
    )
    logger.info("Saved T30 model to %s", model_out)
    return {
        "auc": auc,
        "precision_top10": float(precision_top10),
        "thresholds": thresholds,
        "test_gated": test_metrics,
    }


def train_entry_regression(
    data_path: Path, model_out: Path, split: str = "chrono"
) -> dict:
    """Train XGBoost regressor on realized_pnl_pct target.

    Codex Q4 #1 recommendation: use magnitude of realized PnL as target
    instead of sign-only binary label. Each example now contributes
    ordered signal (how much up, how much down), so the gradient carries
    5-10× more information per row without enlarging the dataset.

    Confidence gating maps naturally onto PnL thresholds:
        predicted_pnl >= PNL_CEILING → BUY (high-EV)
        predicted_pnl <  PNL_FLOOR   → SKIP (high negative-EV)
        else                         → RULES (defer to rule scorer)

    Target clipped to [-100%, +300%] to protect MSE from rare moonshots.
    """
    df = load_df(data_path)
    if "realized_pnl_pct" not in df.columns:
        raise ValueError(
            "entry.parquet missing 'realized_pnl_pct' — rebuild via "
            "pulse_bot.ml.build_dataset (codex Q4 #1 added this column)."
        )
    # Drop rows without realized PnL (DOA tokens) — build_dataset already
    # drops them via dropna(subset=['label']) but we defend here anyway.
    df = df.dropna(subset=["realized_pnl_pct"])
    logger.info(
        "Entry regression dataset: %d rows, PnL mean=%.2f%% median=%.2f%%",
        len(df),
        df.realized_pnl_pct.mean(),
        df.realized_pnl_pct.median(),
    )

    from pulse_bot.ml.features import ENTRY_FEATURE_ORDER, FEATURE_SCHEMA_VERSION

    missing = [c for c in ENTRY_FEATURE_ORDER if c not in df.columns]
    if missing:
        raise ValueError(
            f"entry.parquet missing canonical features {missing}. "
            "Rebuild via build_dataset before training."
        )
    feature_cols = list(ENTRY_FEATURE_ORDER)

    if split == "random":
        logger.warning(
            "RANDOM SPLIT — regression AUC analogue leaks regime. "
            "Use --split chrono for honest numbers."
        )
        np.random.seed(42)
        idx = np.random.permutation(len(df))
        cut = int(len(df) * 0.8)
        train_df = df.iloc[idx[:cut]]
        test_df = df.iloc[idx[cut:]]
        val_df = test_df.iloc[: len(test_df) // 2]
        test_df = test_df.iloc[len(test_df) // 2 :]
    else:
        df = df.sort_values("scored_at").reset_index(drop=True)
        n = len(df)
        train_end = int(n * 0.70)
        val_end = int(n * 0.85)
        train_df = df.iloc[:train_end]
        val_df = df.iloc[train_end:val_end]
        test_df = df.iloc[val_end:]
    logger.info(
        "Regression split (%s): train=%d val=%d test=%d",
        split,
        len(train_df),
        len(val_df),
        len(test_df),
    )

    # Clip target to stabilize MSE against long-tail outliers.
    y_train = train_df["realized_pnl_pct"].clip(PNL_CLIP_LO, PNL_CLIP_HI).values
    y_val = val_df["realized_pnl_pct"].clip(PNL_CLIP_LO, PNL_CLIP_HI).values
    y_test = test_df["realized_pnl_pct"].clip(PNL_CLIP_LO, PNL_CLIP_HI).values
    X_train = train_df[feature_cols]
    X_val = val_df[feature_cols]
    X_test = test_df[feature_cols]

    model = xgb.XGBRegressor(
        n_estimators=300,
        max_depth=3,
        min_child_weight=5,
        learning_rate=0.05,
        subsample=0.8,
        colsample_bytree=0.8,
        objective="reg:squarederror",
        eval_metric="rmse",
        early_stopping_rounds=30,
        random_state=42,
    )
    model.fit(
        X_train,
        y_train,
        eval_set=[(X_val, y_val)],
        verbose=False,
    )

    pred_val = model.predict(X_val)
    pred_test = model.predict(X_test)

    # Regression metrics
    from scipy.stats import spearmanr
    from sklearn.metrics import mean_absolute_error, mean_squared_error

    rmse = float(np.sqrt(mean_squared_error(y_test, pred_test)))
    mae = float(mean_absolute_error(y_test, pred_test))
    spearman_rho, _ = spearmanr(pred_test, y_test)
    spearman_rho = float(spearman_rho) if not np.isnan(spearman_rho) else 0.0

    # AUC-equivalent: can the model rank winners above losers?
    # Treat realized_pnl > 0 as positive for ranking metric.
    y_test_binary = (y_test > 0).astype(int)
    auc = (
        roc_auc_score(y_test_binary, pred_test)
        if y_test_binary.sum() not in (0, len(y_test_binary))
        else float("nan")
    )

    # Precision at top 10% by predicted PnL
    k = max(1, len(pred_test) // 10)
    top_idx = pred_test.argsort()[::-1][:k]
    precision_top10 = float(y_test_binary[top_idx].mean())
    avg_pnl_top10 = float(y_test[top_idx].mean())

    # PnL thresholds tuned on val
    thresholds = _search_pnl_thresholds(pred_val, y_val)
    logger.info(
        "PnL-gating thresholds (val-tuned): FLOOR=%.2f%% CEILING=%.2f%%",
        thresholds["floor"],
        thresholds["ceiling"],
    )
    logger.info(
        "  below FLOOR: n=%d avg_pnl=%.2f%% WR=%.1f%%",
        thresholds["floor_n"],
        thresholds["floor_avg_pnl"],
        thresholds["floor_wr"] * 100,
    )
    logger.info(
        "  above CEILING: n=%d avg_pnl=%.2f%% WR=%.1f%%",
        thresholds["ceiling_n"],
        thresholds["ceiling_avg_pnl"],
        thresholds["ceiling_wr"] * 100,
    )

    test_metrics = _evaluate_gated_regression(pred_test, y_test, thresholds)
    logger.info(
        "Test gating: BUY n=%d avg_pnl=%.2f%% WR=%.1f%% | "
        "SKIP n=%d avg_pnl=%.2f%% | RULES n=%d avg_pnl=%.2f%%",
        test_metrics["buy_n"],
        test_metrics["buy_avg_pnl"],
        test_metrics["buy_wr"] * 100,
        test_metrics["skip_n"],
        test_metrics["skip_avg_pnl"],
        test_metrics["rules_n"],
        test_metrics["rules_avg_pnl"],
    )

    booster = model.get_booster()
    raw_imp = booster.get_score(importance_type="gain")
    importance = dict(sorted(raw_imp.items(), key=lambda x: -float(x[1]))[:15])  # type: ignore[arg-type]

    logger.info("=" * 60)
    logger.info("ENTRY REGRESSION MODEL RESULTS")
    logger.info("  RMSE (holdout):        %.3f%%", rmse)
    logger.info("  MAE  (holdout):        %.3f%%", mae)
    logger.info("  Spearman ρ:            %.4f", spearman_rho)
    logger.info("  AUC (sign ranking):    %.4f", auc)
    logger.info("  Precision @ top 10%%:   %.2f%%", precision_top10 * 100)
    logger.info("  Avg PnL @ top 10%%:     %.2f%%", avg_pnl_top10)
    logger.info("  Top-15 features:")
    for f, v in importance.items():
        logger.info("    %.4f  %s", v, f)
    logger.info("=" * 60)

    model.save_model(model_out)
    logger.info("Saved regression model to %s", model_out)
    # config_hash pins training-time PulseBotConfig subset. See
    # ``pulse_bot.ml.config_hash`` and the classification head above.
    from pulse_bot.config import get_config as _get_cfg
    from pulse_bot.ml.config_hash import (
        TRAIN_RELEVANT_FIELDS,
        compute_config_hash,
        extract_relevant_fields,
    )

    cfg_for_hash = _get_cfg()
    config_hash = compute_config_hash(cfg_for_hash)
    config_values = extract_relevant_fields(cfg_for_hash)
    meta_out = model_out.with_suffix(".meta.json")
    _meta = {
        "objective": "reg:squarederror",
        "features": feature_cols,
        "schema_version": FEATURE_SCHEMA_VERSION,
        "rmse": rmse,
        "mae": mae,
        "spearman_rho": spearman_rho,
        "auc_sign": auc,
        "precision_top10": precision_top10,
        "avg_pnl_top10": avg_pnl_top10,
        "base_rate": float(y_test_binary.mean()),
        "pnl_clip_lo": PNL_CLIP_LO,
        "pnl_clip_hi": PNL_CLIP_HI,
        "train_rows": len(train_df),
        "val_rows": len(val_df),
        "test_rows": len(test_df),
        "confidence_thresholds": thresholds,
        "test_gated": test_metrics,
        "config_hash": config_hash,
        "config_fields_version": 1,
        "config_field_names": list(TRAIN_RELEVANT_FIELDS),
        "config_values": config_values,
    }
    # Universal skill gate (task #96): emit an authoritative model_health
    # block so the runtime judge (model_registry.assess_skill) does not
    # have to re-derive it. The reg head is gated on rank + sign skill —
    # a head with rho/auc_sign below the bar must not gate live trades.
    from pulse_bot.ml.model_registry import assess_skill as _assess_skill

    _sk, _st, _rs = _assess_skill(_meta)
    _meta["model_health"] = {"status": _st, "skilled": _sk, "reason": _rs}
    logger.info("  model_health:          %s — %s", _st, _rs)
    meta_out.write_text(json.dumps(_meta, indent=2))
    return {
        "rmse": rmse,
        "mae": mae,
        "spearman_rho": spearman_rho,
        "precision_top10": precision_top10,
        "thresholds": thresholds,
        "test_gated": test_metrics,
    }


def _search_pnl_thresholds(
    pred: "np.ndarray",
    realized_pnl: "np.ndarray",
    min_bucket: int = 30,
) -> dict:
    """Grid-search PNL_FLOOR / PNL_CEILING on a validation set.

    FLOOR: predicted-PnL threshold below which the observed WR is lowest
    (filter out high-confidence losers). CEILING: threshold above which
    the observed avg PnL + WR peaks.

    Grid covers [-20%, +30%] in 5pp steps — wide enough to capture the
    natural FAST_BUY base-rate window (~27% WR, avg ~-2% PnL).
    """
    grid = np.arange(-20.0, 30.01, 5.0)
    realized_binary = (realized_pnl > 0).astype(int)

    best_floor = 0.0
    best_floor_wr = 1.0
    for t in grid:
        mask = pred < t
        if mask.sum() < min_bucket:
            continue
        wr = float(realized_binary[mask].mean())
        if wr < best_floor_wr:
            best_floor_wr = wr
            best_floor = float(t)

    best_ceiling = 10.0
    best_ceiling_score = -1e9  # combined WR + avg_pnl
    for t in grid:
        mask = pred >= t
        if mask.sum() < min_bucket:
            continue
        wr = float(realized_binary[mask].mean())
        avg_pnl = float(realized_pnl[mask].mean())
        score = wr + avg_pnl / 100.0  # WR weighted + small magnitude bias
        if score > best_ceiling_score:
            best_ceiling_score = score
            best_ceiling = float(t)

    # Sanity: on fat-tailed small val sets the grid search can pick
    # ceiling ≤ floor, which makes the live gate (decide_with_confidence)
    # degenerate — every predicted score either ≥ ceiling or < floor,
    # producing empty RULES bucket. Override to sensible priors when
    # that happens. These priors come from the regression training
    # result: dataset mean PnL ≈ -6%, so +5% is "positive EV" and -10%
    # is "confident loser".
    if best_ceiling <= best_floor:
        logger.warning(
            "Threshold search gave degenerate (floor=%.2f ≥ ceiling=%.2f). "
            "Falling back to priors floor=-10%% ceiling=+5%%.",
            best_floor,
            best_ceiling,
        )
        best_floor = -10.0
        best_ceiling = 5.0

    below = pred < best_floor
    above = pred >= best_ceiling
    return {
        "floor": best_floor,
        "ceiling": best_ceiling,
        "floor_n": int(below.sum()),
        "floor_wr": float(realized_binary[below].mean()) if below.sum() else 0.0,
        "floor_avg_pnl": (float(realized_pnl[below].mean()) if below.sum() else 0.0),
        "ceiling_n": int(above.sum()),
        "ceiling_wr": float(realized_binary[above].mean()) if above.sum() else 0.0,
        "ceiling_avg_pnl": (float(realized_pnl[above].mean()) if above.sum() else 0.0),
        "min_bucket": min_bucket,
        "grid_step": 5.0,
    }


def _evaluate_gated_regression(
    pred: "np.ndarray",
    realized_pnl: "np.ndarray",
    thresholds: dict,
) -> dict:
    """Report BUY/SKIP/RULES bucket sizes + avg PnL + WR on held-out test."""
    pred = np.asarray(pred, dtype=float)
    realized_pnl = np.asarray(realized_pnl, dtype=float)
    realized_binary = (realized_pnl > 0).astype(int)
    buy = pred >= thresholds["ceiling"]
    skip = pred < thresholds["floor"]
    rules = (~buy) & (~skip)
    return {
        "buy_n": int(buy.sum()),
        "buy_avg_pnl": float(realized_pnl[buy].mean()) if buy.sum() else 0.0,
        "buy_wr": float(realized_binary[buy].mean()) if buy.sum() else 0.0,
        "skip_n": int(skip.sum()),
        "skip_avg_pnl": float(realized_pnl[skip].mean()) if skip.sum() else 0.0,
        "skip_wr": float(realized_binary[skip].mean()) if skip.sum() else 0.0,
        "rules_n": int(rules.sum()),
        "rules_avg_pnl": float(realized_pnl[rules].mean()) if rules.sum() else 0.0,
        "rules_wr": float(realized_binary[rules].mean()) if rules.sum() else 0.0,
        "test_avg_pnl": float(realized_pnl.mean()) if len(realized_pnl) else 0.0,
    }


def _entry_model_health_check(
    *,
    model_out: "Path",
    proba_val: "np.ndarray",
    new_auc: float,
    thresholds: dict,
) -> dict:
    """Codex review 2026-04-28: pre-save sanity gates on a freshly-trained
    entry model. Four signals:

    - **proba_spread** (p99 − p1 on val): if < 0.30 the model outputs a
      narrow band → no ranking power regardless of holdout AUC. Today's
      live model: spread = 0.61 − 0.37 = 0.24, AUC=0.825 — passed AUC
      check yet was completely useless for thresholding.
    - **threshold_status**: surfaces the floor>=ceiling collapse that
      ``_search_confidence_thresholds`` now detects + flags.
    - **auc_regression**: compare against previous meta.json AUC. If the
      new run dropped by >2pp, mark for rollback (caller decides). Today
      we silently overwrote 0.905 → 0.891 → 0.825.
    - **calibration_drift** (added 2026-05-08): if proba_p50 shifts > 0.05
      vs previous meta, the threshold semantics break — same env-var
      ceiling now catches a different fraction of tokens. May-7 retrain
      missed every other check (auc Δ −0.011, spread 0.42 — both within
      tolerance) but had p50 shift 0.020 → 0.190 = entire decision boundary
      moved past the live ceiling=0.15. Documented post-mortem in
      ``docs/CHANGELOG.md`` 2026-05-08.

    Returns ``{status, proba_spread, prev_auc, auc_delta, threshold_status,
    notes}``. ``status`` ∈ {"ok", "degenerate", "narrow_proba_spread",
    "auc_regression", "calibration_drift"}. The live policy reads this
    and disables ML override when status != "ok".
    """
    import numpy as _np

    out: dict = {}
    # 1. proba spread on val
    p_lo = float(_np.quantile(proba_val, 0.01))
    p50 = float(_np.quantile(proba_val, 0.50))
    p_hi = float(_np.quantile(proba_val, 0.99))
    spread = p_hi - p_lo
    out["proba_p1"] = p_lo
    out["proba_p50"] = p50
    out["proba_p99"] = p_hi
    out["proba_spread"] = spread

    # 2. threshold-search status (set by _search_confidence_thresholds)
    out["threshold_status"] = thresholds.get("status", "unknown")

    # 3. AUC regression vs prior meta
    meta_prev = model_out.with_suffix(".meta.json")
    prev_auc = None
    prev_p50 = None
    if meta_prev.exists():
        try:
            prev = json.loads(meta_prev.read_text())
            prev_auc = float(prev.get("auc") or 0.0)
            # prev model_health may not exist on legacy artifacts —
            # treat missing as "no comparison available" (skip the
            # calibration-drift check rather than fail the build).
            prev_health = prev.get("model_health") or {}
            prev_p50_raw = prev_health.get("proba_p50")
            prev_p50 = float(prev_p50_raw) if prev_p50_raw is not None else None
        except Exception:
            prev_auc = None
            prev_p50 = None
    out["prev_auc"] = prev_auc
    out["auc_delta"] = (new_auc - prev_auc) if prev_auc is not None else None
    out["prev_proba_p50"] = prev_p50
    out["proba_p50_delta"] = (p50 - prev_p50) if prev_p50 is not None else None

    # Decide overall status (most-severe wins).
    notes: list[str] = []
    status = "ok"
    # ok_percentile_fallback is acceptable: EV search collapsed but
    # ranking enrichment verified, gates are usable. Anything else
    # (degenerate_flat, unknown, ...) is treated as degenerate.
    if out["threshold_status"] not in ("ok", "ok_percentile_fallback"):
        status = "degenerate"
        notes.append(f"threshold search returned {out['threshold_status']}")
    if spread < 0.30:
        if status == "ok":
            status = "narrow_proba_spread"
        notes.append(f"val proba spread {spread:.3f} < 0.30 — no ranking power")
    if prev_auc is not None and (new_auc - prev_auc) < -0.02:
        if status == "ok":
            status = "auc_regression"
        notes.append(
            f"AUC dropped {prev_auc:.4f} → {new_auc:.4f} (Δ={new_auc - prev_auc:+.4f}) "
            "— consider rollback to .prev artifact"
        )
    # 4. Calibration drift (codex 2026-05-08). Threshold 0.05 catches
    # subtle shifts before they become critical; the May-7 incident
    # had Δp50 = +0.17 — far above 0.05 but missed by every other
    # check.
    if prev_p50 is not None and abs(p50 - prev_p50) > 0.05:
        if status == "ok":
            status = "calibration_drift"
        notes.append(
            f"calibration drift: proba_p50 {prev_p50:.3f} → {p50:.3f} "
            f"(Δ={p50 - prev_p50:+.3f}, max=±0.05) — live env-var "
            "thresholds (PULSE_ENTRY_PROBA_CEILING etc) calibrated "
            "against the OLD distribution will catch a different "
            "fraction of tokens with this model. Re-tune env vars "
            "BEFORE deploying, or rollback to .prev."
        )
    out["status"] = status
    out["notes"] = notes

    # Snapshot the previous model BEFORE the caller overwrites it. Lets
    # operations roll back via:
    #   mv data/ml/entry_model.ubj.prev data/ml/entry_model.ubj
    #   mv data/ml/entry_model.meta.json.prev data/ml/entry_model.meta.json
    if model_out.exists():
        try:
            import shutil

            shutil.copy2(model_out, model_out.with_suffix(model_out.suffix + ".prev"))
            if meta_prev.exists():
                shutil.copy2(
                    meta_prev,
                    meta_prev.with_suffix(meta_prev.suffix + ".prev"),
                )
        except Exception as e:
            logger.warning("Failed to snapshot prev model: %s", e)
            notes.append(f"snapshot failed: {e}")

    if status != "ok":
        logger.warning("=" * 60)
        logger.warning("MODEL HEALTH ALERT — status=%s", status)
        for n in notes:
            logger.warning("  - %s", n)
        logger.warning(
            "Live policy will refuse ML override on this model. To force "
            "anyway, set PULSE_ALLOW_DEGENERATE_MODEL=1 (unsafe)."
        )
        logger.warning("=" * 60)
    else:
        delta_p50_str = (
            f", Δp50={out['proba_p50_delta']:+.3f}"
            if out.get("proba_p50_delta") is not None
            else ""
        )
        logger.info(
            "Model health: OK (spread=%.3f, threshold=%s, ΔAUC=%s%s)",
            spread,
            out["threshold_status"],
            f"{out['auc_delta']:+.4f}" if out["auc_delta"] is not None else "n/a",
            delta_p50_str,
        )
    return out


def _search_confidence_thresholds(
    proba: "np.ndarray",
    y: "np.ndarray",
    pnl: "np.ndarray | None" = None,
) -> dict:
    """Grid-search PROBA_FLOOR and PROBA_CEILING on a validation set.

    Codex 2026-04-27 review: WR-only search optimizes binary classification
    accuracy — but we trade with money, not labels. WR=22% with avg_W=22.9%
    avg_L=-10.2% is loss-making (EV=-2.92%/trade). Switched to EV-based
    search when ``pnl`` (realized_pnl_pct) is provided:

      * CEILING: maximise mean(pnl) above threshold (highest-EV bucket
        the model can identify; this is where we want to BUY).
      * FLOOR:   minimise mean(pnl) below threshold (lowest-EV bucket;
        this is where we want to SKIP).

    When ``pnl`` is None, falls back to legacy WR-based search (callers
    that don't have realized_pnl_pct hand still work).

    min_bucket=30 + 0.1 grid (codex 2026-04-23 tightening) preserved to
    reduce selection bias on small val sets — same N-floor logic, EV
    instead of WR as the optimization objective.
    """
    import numpy as np

    min_bucket = 30
    grid = np.arange(0.10, 0.91, 0.1)  # 0.1 steps, 9 points
    base_wr = float(y.mean()) if len(y) else 0.0
    use_ev = pnl is not None and len(pnl) == len(proba)

    if use_ev:
        # Defensive clip — extreme PnL outliers (e.g. unbounded "moonshot"
        # entries with bad price feeds) would otherwise dominate the
        # bucket mean and lock CEILING to a ~5-row spike. Clip to the
        # economically meaningful range our exit logic enforces.
        pnl_c = np.clip(pnl, -100.0, 200.0)
    base_ev = float(pnl_c.mean()) if use_ev else 0.0

    # FLOOR: seek lowest EV (or WR fallback) below threshold.
    best_floor = 0.30
    best_floor_score = float("inf")
    for t in grid:
        mask = proba < t
        if mask.sum() < min_bucket:
            continue
        if use_ev:
            score = float(pnl_c[mask].mean())
        else:
            score = float(y[mask].mean())
        if score < best_floor_score:
            best_floor_score = score
            best_floor = float(t)
    # CEILING: seek highest EV (or WR fallback) above threshold.
    best_ceiling = 0.70
    best_ceiling_score = -float("inf")
    for t in grid:
        mask = proba >= t
        if mask.sum() < min_bucket:
            continue
        if use_ev:
            score = float(pnl_c[mask].mean())
        else:
            score = float(y[mask].mean())
        if score > best_ceiling_score:
            best_ceiling_score = score
            best_ceiling = float(t)
    # Codex 2026-04-28: degeneracy guard with percentile fallback.
    # When EV is monotonically flat / negative across all proba slices
    # (typical on memecoin datasets where avg_pnl is heavily skewed),
    # EV-search returns floor >= ceiling. Previously we marked the
    # whole model "degenerate" — but if the classifier still RANKS
    # (top-quintile WR >> bottom-quintile WR), percentile-based gates
    # are perfectly valid even without positive-EV buckets. Split:
    #
    #   ok                       — EV-based gates found a profitable bucket
    #   ok_percentile_fallback   — EV flat but ranking enrichment present
    #   degenerate_flat          — no ranking power; do not act
    status = "ok"
    if best_floor >= best_ceiling:
        # EV-based search collapsed. Fall back to proba quintiles and
        # verify the model still has classification power.
        try:
            best_floor = float(np.quantile(proba, 0.20))
            best_ceiling = float(np.quantile(proba, 0.80))
        except Exception:
            best_floor, best_ceiling = 0.30, 0.70
        above_mask = proba >= best_ceiling
        below_mask = proba < best_floor
        above_wr_chk = float(y[above_mask].mean()) if above_mask.sum() else 0.0
        below_wr_chk = float(y[below_mask].mean()) if below_mask.sum() else 1.0
        # Ranking enrichment: top quintile must show ≥1.3× base WR
        # AND bottom quintile must show ≤0.7× base WR. Both directions
        # required so a single-tail model (e.g. only confident-skip) is
        # not mistaken for a working ranker.
        if (
            base_wr > 0
            and above_wr_chk >= base_wr * 1.3
            and below_wr_chk <= base_wr * 0.7
        ):
            status = "ok_percentile_fallback"
        else:
            status = "degenerate_flat"
    below = proba < best_floor
    above = proba >= best_ceiling
    out = {
        "floor": best_floor,
        "ceiling": best_ceiling,
        "floor_n": int(below.sum()),
        "floor_wr": float(y[below].mean()) if below.sum() else 0.0,
        "ceiling_n": int(above.sum()),
        "ceiling_wr": float(y[above].mean()) if above.sum() else 0.0,
        "val_base_rate": base_wr,
        "min_bucket": min_bucket,
        "grid_step": 0.1,
        "objective": "ev" if use_ev else "wr",
        "status": status,
    }
    if use_ev:
        out["floor_ev"] = float(pnl_c[below].mean()) if below.sum() else 0.0
        out["ceiling_ev"] = float(pnl_c[above].mean()) if above.sum() else 0.0
        out["val_base_ev"] = base_ev
    return out


def _fit_platt(proba: "np.ndarray", y: "np.ndarray") -> dict:
    """Platt scaling: fit 1-D logistic regression proba_raw → label.

    Returns {a, b} such that calibrated_proba = sigmoid(a*raw + b).

    Codex 2026-04-23: switched from handrolled gradient descent to
    sklearn.LogisticRegression(C=1.0). Previous implementation had no
    convergence check, no regularization, and could diverge on the ~18-
    positive val sample — producing extreme a/b that made sigmoid output
    unreliable. sklearn is already imported elsewhere in train.py for
    roc_auc_score, so no new dependency surface.
    """
    import numpy as np
    from sklearn.linear_model import LogisticRegression

    x = np.asarray(proba, dtype=float).reshape(-1, 1)
    yv = np.asarray(y, dtype=int).reshape(-1)
    if yv.sum() == 0 or yv.sum() == len(yv):
        return {"a": 1.0, "b": 0.0, "note": "degenerate"}
    # C=1.0 is standard sklearn default (moderate L2). For N~99 and
    # 1 feature, the risk is over-regularized coefficients — acceptable
    # trade for a well-conditioned fit. solver='lbfgs' is deterministic.
    clf = LogisticRegression(C=1.0, solver="lbfgs", max_iter=200)
    clf.fit(x, yv)
    return {
        "a": float(clf.coef_[0, 0]),
        "b": float(clf.intercept_[0]),
        "source": "sklearn.LogisticRegression(C=1.0)",
    }


def _bootstrap_ci(
    arr: "np.ndarray",
    y: "np.ndarray",
    metric: str,
    n_boot: int = 500,
    alpha: float = 0.05,
    seed: int = 42,
) -> tuple[float, float]:
    """Percentile bootstrap CI for a metric computed over paired arrays.

    Codex 2026-04-23 requirement: stop reporting point estimates without
    uncertainty bars at N_test=100. Returns ``(lo, hi)`` 95% CI.
    Supported ``metric``: "auc", "precision_top10", "base_rate".
    """
    import numpy as np
    from sklearn.metrics import roc_auc_score

    rng = np.random.default_rng(seed)
    n = len(arr)
    if n == 0:
        return (float("nan"), float("nan"))
    vals: list[float] = []
    for _ in range(n_boot):
        idx = rng.integers(0, n, size=n)
        a = arr[idx]
        yb = y[idx]
        if yb.sum() == 0 or yb.sum() == n:
            continue
        if metric == "auc":
            vals.append(float(roc_auc_score(yb, a)))
        elif metric == "precision_top10":
            k = max(1, n // 10)
            top_idx = a.argsort()[::-1][:k]
            vals.append(float(yb[top_idx].mean()))
        elif metric == "base_rate":
            vals.append(float(yb.mean()))
        else:
            raise ValueError(f"Unknown metric: {metric}")
    if not vals:
        return (float("nan"), float("nan"))
    lo = float(np.quantile(vals, alpha / 2))
    hi = float(np.quantile(vals, 1 - alpha / 2))
    return lo, hi


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


def train_exit_quantile(
    data_path: Path, model_out: Path, quantile: float = 0.25
) -> dict:
    """Quantile regression head for dynamic SL/TP (codex E3, shadow-only).

    Instead of MSE regression (dominated by fat-tailed outliers on
    pump.fun), fit XGBoost quantile regression against the 60s-forward
    return. Train two heads:
        * q=0.25 → lower-tail PnL prediction (SL tightening candidate)
        * q=0.75 → upper-tail PnL prediction (TP loosening candidate)

    Activation gate (NOT applied here — lives in ExitManager, disabled
    by default via ``PULSE_EXIT_REGRESSION_ACTIVE=0``):
        After 2 weeks of shadow logging, compare realized PnL in buckets
        predicted by this head vs fixed +100%/-15% thresholds on held-out
        test data. Paired bootstrap (500 resamples). Activate only if
        bucket beats the fixed threshold by ≥ 1σ.

    Target is 60s forward realized return, computed at build-time from
    ``trades`` table. Clipped to [-100, +300] like train_entry_regression.
    """
    df = load_df(data_path)
    if "entry_ts" not in df.columns:
        raise ValueError("Exit dataset missing 'entry_ts' — rebuild via build_dataset.")
    # Target: 60s forward realized return, computed at build-time from
    # the trades table (build_dataset.py:866). The legacy proxy fallback
    # using `-drawdown_from_peak` was removed 2026-04-27 (codex review):
    # column is reliably present, fallback was training quantile heads
    # on the wrong target and dragging Spearman down to 0.11/0.17.
    if "forward_pnl_60s" not in df.columns:
        raise ValueError(
            "exit.parquet missing 'forward_pnl_60s' — rebuild via "
            "`python -m pulse_bot.ml.build_dataset --dataset exit`."
        )

    mints_order = df.drop_duplicates("mint").sort_values("entry_ts")["mint"].tolist()
    cut_mint = mints_order[int(len(mints_order) * 0.8)]
    cut_ts = df.loc[df.mint == cut_mint, "entry_ts"].iloc[0]
    train_mask = df.entry_ts < cut_ts
    from pulse_bot.ml.features import EXIT_FEATURE_ORDER

    missing = [c for c in EXIT_FEATURE_ORDER if c not in df.columns]
    if missing:
        raise ValueError(
            f"exit.parquet missing canonical features {missing}. "
            "Rebuild via build_dataset before training."
        )
    feature_cols = list(EXIT_FEATURE_ORDER)
    X_train = df.loc[train_mask, feature_cols]
    y_train = df.loc[train_mask, "forward_pnl_60s"].clip(-100, 300).values
    X_test = df.loc[~train_mask, feature_cols]
    y_test = df.loc[~train_mask, "forward_pnl_60s"].clip(-100, 300).values

    # XGBoost quantile objective (requires xgboost >= 1.7)
    model = xgb.XGBRegressor(
        objective="reg:quantileerror",
        quantile_alpha=quantile,
        n_estimators=200,
        max_depth=3,
        min_child_weight=5,
        learning_rate=0.05,
        subsample=0.8,
        colsample_bytree=0.8,
        early_stopping_rounds=20,
        random_state=42,
    )
    model.fit(X_train, y_train, eval_set=[(X_test, y_test)], verbose=False)
    pred_test = model.predict(X_test)

    # Residual quantile coverage: fraction of actual <= predicted should
    # approximate ``quantile`` in-sample.
    coverage = float((y_test <= pred_test).mean())
    from scipy.stats import spearmanr

    rho, _ = spearmanr(pred_test, y_test)
    rho = float(rho) if not np.isnan(rho) else 0.0
    logger.info("=" * 60)
    logger.info("EXIT QUANTILE (q=%.2f) MODEL RESULTS", quantile)
    logger.info("  Coverage (should ≈ %.2f): %.3f", quantile, coverage)
    logger.info("  Spearman rho: %.4f", rho)
    logger.info("  Train rows: %d, Test rows: %d", len(X_train), len(X_test))
    logger.info("=" * 60)

    model.save_model(model_out)
    meta_out = model_out.with_suffix(".meta.json")
    _meta = {
        "objective": "reg:quantileerror",
        "quantile": quantile,
        "features": feature_cols,
        "coverage": coverage,
        "spearman_rho": rho,
        "train_rows": int(len(X_train)),
        "test_rows": int(len(X_test)),
        "note": (
            "Shadow-only until paired-bootstrap gate passes. Do "
            "not wire into live exit decisions via "
            "PULSE_EXIT_REGRESSION_ACTIVE=1 without 2-week shadow "
            "validation."
        ),
    }
    # Universal skill gate (task #96): a quantile head that is not
    # calibrated (achieved coverage far from the target quantile) must
    # not influence live exit decisions.
    from pulse_bot.ml.model_registry import assess_skill as _assess_skill

    _sk, _st, _rs = _assess_skill(_meta)
    _meta["model_health"] = {"status": _st, "skilled": _sk, "reason": _rs}
    logger.info("  model_health: %s — %s", _st, _rs)
    meta_out.write_text(json.dumps(_meta, indent=2))
    logger.info("Saved quantile model to %s", model_out)
    return {"quantile": quantile, "coverage": coverage, "spearman_rho": rho}


def train_exit_quantile_max_hold(
    data_path: Path, model_out: Path, quantile: float = 0.75
) -> dict:
    """Quantile regression for time-to-exit under live policy.

    Target: ``forward_seconds_to_exit`` — simulated time the live exit
    policy (TP/SL/max_hold + monitor signals) would hold a position
    entered at the current state. q=0.75 by default: predict a hold time
    long enough that 75% of similar states stay in until the simulated
    exit. Used to set per-token max_hold dynamically.

    History:
        v1 (2026-04-27): target was ``forward_seconds_to_peak`` — produced
            ρ=-0.196 anti_correlated due to survivor bias (DOA→sec=0,
            pumpers→sec=600, model learned the inverse).
        v2 (2026-04-30): replaced with simulate_exit-driven label. Now
            matches the live policy semantics — what the model is asked
            to predict at inference equals how the label was generated.
    """
    df = load_df(data_path)
    if "entry_ts" not in df.columns:
        raise ValueError("Exit dataset missing 'entry_ts' — rebuild via build_dataset.")
    if "forward_seconds_to_exit" not in df.columns:
        raise ValueError(
            "exit.parquet missing 'forward_seconds_to_exit' — rebuild "
            "via `python -m pulse_bot.ml.build_dataset --dataset exit`."
        )

    # 2026-04-28 fix: drop right-censored rows where peak wasn't observed
    # within the 600s horizon. Previously >90% of rows had y=600 (no peak
    # found) and the regressor learned a near-constant — Spearman -0.21.
    # Training only on observed-peak rows + emitting a coverage health
    # flag is the right structural fix until we have post-T+600 data.
    HORIZON = 600.0
    censored_mask = df["forward_seconds_to_exit"] >= HORIZON - 0.001
    n_censored = int(censored_mask.sum())
    n_observed = int(len(df) - n_censored)
    coverage = n_observed / max(len(df), 1)
    logger.info(
        "exit_quantile_max_hold: %d/%d rows (%.1f%%) had observable peak "
        "within %.0fs horizon (rest are right-censored, dropped from "
        "training)",
        n_observed,
        len(df),
        coverage * 100,
        HORIZON,
    )
    if n_observed < 200:
        raise ValueError(
            f"exit_quantile_max_hold: only {n_observed} non-censored rows "
            "— insufficient signal to train. Wait until T+180+ data "
            "accumulates (>1k observed peaks expected after 1-2 weeks)."
        )
    df_obs = df[~censored_mask].copy()

    mints_order = (
        df_obs.drop_duplicates("mint").sort_values("entry_ts")["mint"].tolist()
    )
    if len(mints_order) < 5:
        raise ValueError(
            "exit_quantile_max_hold: too few unique mints with observed "
            "peaks for chrono split. Wait for more data."
        )
    cut_mint = mints_order[int(len(mints_order) * 0.8)]
    cut_ts = df_obs.loc[df_obs.mint == cut_mint, "entry_ts"].iloc[0]
    train_mask = df_obs.entry_ts < cut_ts

    from pulse_bot.ml.features import EXIT_FEATURE_ORDER

    missing = [c for c in EXIT_FEATURE_ORDER if c not in df_obs.columns]
    if missing:
        raise ValueError(
            f"exit.parquet missing canonical features {missing}. "
            "Rebuild via build_dataset before training."
        )
    feature_cols = list(EXIT_FEATURE_ORDER)
    X_train = df_obs.loc[train_mask, feature_cols]
    y_train = df_obs.loc[train_mask, "forward_seconds_to_exit"].clip(0, HORIZON).values
    X_test = df_obs.loc[~train_mask, feature_cols]
    y_test = df_obs.loc[~train_mask, "forward_seconds_to_exit"].clip(0, HORIZON).values

    model = xgb.XGBRegressor(
        objective="reg:quantileerror",
        quantile_alpha=quantile,
        n_estimators=200,
        max_depth=3,
        min_child_weight=5,
        learning_rate=0.05,
        subsample=0.8,
        colsample_bytree=0.8,
        early_stopping_rounds=20,
        random_state=42,
    )
    model.fit(X_train, y_train, eval_set=[(X_test, y_test)], verbose=False)
    pred_test = model.predict(X_test)

    coverage_q = float((y_test <= pred_test).mean())
    from scipy.stats import spearmanr

    rho, _ = spearmanr(pred_test, y_test)
    rho = float(rho) if not np.isnan(rho) else 0.0
    # Health gate: refuse to deploy if Spearman is anti-correlated or
    # near-zero. Old artifact had rho=-0.21 (model predicted opposite
    # of reality). New training on non-censored rows must beat that
    # by a wide margin.
    status = "ok" if rho >= 0.10 else ("anti_correlated" if rho < 0 else "weak_signal")
    logger.info("=" * 60)
    logger.info("EXIT QUANTILE max_hold (q=%.2f) RESULTS", quantile)
    logger.info("  Coverage (target ≈ %.2f): %.3f", quantile, coverage_q)
    logger.info("  Spearman rho: %.4f (status=%s)", rho, status)
    logger.info("  Train rows: %d, Test rows: %d", len(X_train), len(X_test))
    logger.info(
        "  Observed-peak coverage: %.1f%% (rest right-censored, dropped)",
        coverage * 100,
    )
    logger.info("  Pred range: [%.1f, %.1f]s", pred_test.min(), pred_test.max())
    logger.info("=" * 60)

    model.save_model(model_out)
    meta_out = model_out.with_suffix(".meta.json")
    meta_out.write_text(
        json.dumps(
            {
                "objective": "reg:quantileerror",
                "quantile": quantile,
                "target": "forward_seconds_to_exit",
                "horizon_sec": 600.0,
                "features": feature_cols,
                "coverage": coverage_q,
                "non_censored_coverage": coverage,
                "spearman_rho": rho,
                "train_rows": int(len(X_train)),
                "test_rows": int(len(X_test)),
                "model_health": {"status": status, "spearman": rho},
                "note": (
                    "Shadow-only until paired-bootstrap gate passes vs "
                    "static max_hold default. Wire through "
                    "PULSE_EXIT_MAX_HOLD_DYNAMIC=1 only after validation."
                ),
            },
            indent=2,
        )
    )
    logger.info("Saved max_hold quantile model to %s", model_out)
    return {"quantile": quantile, "coverage": coverage, "spearman_rho": rho}


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
    importance = dict(sorted(raw.items(), key=lambda x: -float(x[1]))[:15])  # type: ignore[arg-type]

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
    from pulse_bot.ml.features import EXIT_FEATURE_SCHEMA_VERSION

    meta_out.write_text(
        json.dumps(
            {
                "features": feature_cols,
                "schema_version": EXIT_FEATURE_SCHEMA_VERSION,
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
    ap.add_argument(
        "--objective",
        choices=["classification", "regression"],
        default="classification",
        help="classification = binary label (legacy). regression = "
        "realized_pnl_pct target (codex Q4 #1; uses magnitude for 5-10x "
        "more gradient information per row). Writes a separate model "
        "file (entry_model_reg.ubj) so both can coexist.",
    )
    ap.add_argument(
        "--train-exit-quantile",
        action="store_true",
        help="Additionally train quantile regression heads (q=0.25 for SL "
        "tightening + q=0.75 for TP loosening). Shadow-only — activation "
        "behind PULSE_EXIT_REGRESSION_ACTIVE=1 after paired-bootstrap gate.",
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
        if args.objective == "regression":
            train_entry_regression(
                data, data_dir / "entry_model_reg.ubj", split=args.split
            )
        else:
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
        # 2026-04-27: dropped train_exit (binary classifier exit_v3).
        # Model contributed ~0% to live decisions (proba≥0.80 threshold
        # rarely fired, overlapped with hard rules). Quantile heads
        # below replace it with regression-based per-token thresholds.
        if args.train_exit_quantile:
            train_exit_quantile(data, data_dir / "exit_quantile_sl.ubj", quantile=0.25)
            train_exit_quantile(data, data_dir / "exit_quantile_tp.ubj", quantile=0.75)
            train_exit_quantile_max_hold(
                data, data_dir / "exit_quantile_max_hold.ubj", quantile=0.75
            )


if __name__ == "__main__":
    sys.exit(main())
