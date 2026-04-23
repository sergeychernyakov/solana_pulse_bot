# pulse_bot/ml/daily_validation.py
"""Daily honesty tests for entry + exit XGBoost models."""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import sys
from dataclasses import asdict, dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

import numpy as np
import pandas as pd
import xgboost as xgb
from scipy import stats
from sklearn.metrics import brier_score_loss, roc_auc_score

from pulse_bot.config import PUMPFUN_FEE_PCT, PUMPFUN_PRIORITY_FEE
from pulse_bot.ml.build_dataset import build_entry_dataset, build_exit_dataset

logger = logging.getLogger(__name__)

ModelKind = Literal["entry", "exit"]

# Known-leak features. Any of these appearing in top-10 gain importance
# signals a regression of a codex v9 fix.
KNOWN_LEAK_FEATURES: set[str] = {
    "market_cap_sol",
    "mc_at_scoring",
    "sol_to_graduation",
    "v_sol_in_bonding_curve",
    "v_tokens_in_bonding_curve",
}

# Alert thresholds (tuned for N~80 positives per codex review 2026-04-22).
SHUFFLED_LABELS_THRESHOLD = 0.55
SHUFFLED_LABELS_N_RUNS = 5
ADVERSARIAL_ABS_THRESHOLD = 0.85
ADVERSARIAL_WOW_DELTA_THRESHOLD = 0.10
PRIOR_DRIFT_THRESHOLD = 0.30
KS_P_VALUE_THRESHOLD = 0.01
CALIBRATION_N_BINS = 5
ROLLING_MIN_POSITIVES = 500

# Economic backtest cost model (realistic variant).
REALISTIC_SLIPPAGE_PCT = 2.0
REALISTIC_FAILED_TX_RATE = 0.05
REALISTIC_PER_TRADE_FIXED_SOL = 0.0015  # ~$0.30 priority fee ballpark
# Pump.fun-native costs from pulse_bot.config. Round-trip = buy + sell, so
# the 1%-each-side trading fee costs 2% per closed position, and the
# priority fee is paid twice.
PUMPFUN_ROUND_TRIP_FEE_PCT = PUMPFUN_FEE_PCT * 100.0 * 2.0
PUMPFUN_ROUND_TRIP_PRIORITY_SOL = PUMPFUN_PRIORITY_FEE * 2.0

DEFAULT_ENTRY_THRESHOLD = 0.5


@dataclass
class ValidationResult:
    """One test's outcome."""

    name: str
    passed: bool
    severity: Literal["info", "warn", "alert"] = "info"
    metric: float | None = None
    details: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        d = asdict(self)
        # Numpy scalars are not JSON-serializable — coerce.
        if self.metric is not None:
            d["metric"] = float(self.metric)
        d["details"] = _json_safe(self.details)
        return d


def _json_safe(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {k: _json_safe(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_json_safe(x) for x in obj]
    if isinstance(obj, (np.floating, np.integer)):
        return obj.item()
    if isinstance(obj, np.ndarray):
        return obj.tolist()
    return obj


# ── Individual tests ────────────────────────────────────────────────


def check_shuffled_labels(
    X: pd.DataFrame,
    y: pd.Series,
    model_params: dict[str, Any],
    n_runs: int = SHUFFLED_LABELS_N_RUNS,
    seed: int = 42,
) -> ValidationResult:
    """Permute y, retrain, average AUC — should be ~0.5 under null."""
    if len(y) < 20 or y.sum() < 5:
        return ValidationResult(
            name="shuffled_labels",
            passed=True,
            severity="info",
            details={"skipped": "insufficient sample"},
        )
    rng = np.random.default_rng(seed)
    aucs: list[float] = []
    # Drop training-only params that break raw retrain
    params = {
        k: v
        for k, v in model_params.items()
        if k
        not in {
            "early_stopping_rounds",
            "eval_metric",
            "callbacks",
        }
    }
    # Loaded xgb models return n_estimators=None (inference-only meta).
    n_est = params.get("n_estimators")
    params["n_estimators"] = min(int(n_est) if n_est else 150, 150)
    for i in range(n_runs):
        shuffled = rng.permutation(y.values)
        # Chronological 80/20 split on shuffled labels
        cut = int(len(X) * 0.8)
        X_tr, X_te = X.iloc[:cut], X.iloc[cut:]
        y_tr, y_te = shuffled[:cut], shuffled[cut:]
        if y_tr.sum() < 2 or y_te.sum() < 2:
            continue
        m = xgb.XGBClassifier(**params)
        m.fit(X_tr, y_tr, verbose=False)
        proba = m.predict_proba(X_te)[:, 1]
        aucs.append(roc_auc_score(y_te, proba))
    if not aucs:
        return ValidationResult(
            name="shuffled_labels",
            passed=True,
            severity="info",
            details={"skipped": "all shuffles had degenerate splits"},
        )
    mean_auc = float(np.mean(aucs))
    passed = mean_auc <= SHUFFLED_LABELS_THRESHOLD
    return ValidationResult(
        name="shuffled_labels",
        passed=passed,
        severity="alert" if not passed else "info",
        metric=mean_auc,
        details={
            "threshold": SHUFFLED_LABELS_THRESHOLD,
            "n_runs": n_runs,
            "individual_aucs": [float(a) for a in aucs],
            "interpretation": (
                "AUC should be ~0.5 under null. Elevated mean = feature→label leak."
            ),
        },
    )


def check_adversarial_validation(
    X_train: pd.DataFrame,
    X_test: pd.DataFrame,
    previous_auc: float | None = None,
) -> ValidationResult:
    """Classify train-vs-test rows. High AUC → distribution shifted."""
    if len(X_train) < 20 or len(X_test) < 20:
        return ValidationResult(
            name="adversarial_validation",
            passed=True,
            severity="info",
            details={"skipped": "insufficient sample"},
        )
    X_combined = pd.concat(
        [X_train.assign(_is_test=0), X_test.assign(_is_test=1)],
        ignore_index=True,
    )
    labels = X_combined.pop("_is_test").values
    features = X_combined.values
    # Shuffled 80/20 for this meta-classifier (NOT chrono — we want to
    # detect whether train/test are distinguishable at all).
    rng = np.random.default_rng(2026)
    idx = rng.permutation(len(labels))
    cut = int(len(idx) * 0.8)
    m = xgb.XGBClassifier(
        n_estimators=100,
        max_depth=3,
        learning_rate=0.1,
        objective="binary:logistic",
        eval_metric="auc",
        random_state=42,
    )
    m.fit(features[idx[:cut]], labels[idx[:cut]], verbose=False)
    proba = m.predict_proba(features[idx[cut:]])[:, 1]
    if labels[idx[cut:]].sum() in (0, len(idx) - cut):
        return ValidationResult(
            name="adversarial_validation",
            passed=True,
            severity="info",
            details={"skipped": "degenerate meta-split"},
        )
    auc = float(roc_auc_score(labels[idx[cut:]], proba))
    wow_delta = None
    if previous_auc is not None:
        wow_delta = auc - float(previous_auc)
    abs_fail = auc > ADVERSARIAL_ABS_THRESHOLD
    wow_fail = wow_delta is not None and wow_delta > ADVERSARIAL_WOW_DELTA_THRESHOLD
    passed = not (abs_fail or wow_fail)
    return ValidationResult(
        name="adversarial_validation",
        passed=passed,
        severity="alert" if not passed else "info",
        metric=auc,
        details={
            "abs_threshold": ADVERSARIAL_ABS_THRESHOLD,
            "wow_delta_threshold": ADVERSARIAL_WOW_DELTA_THRESHOLD,
            "previous_auc": previous_auc,
            "wow_delta": wow_delta,
            "failed_on": [
                *(["absolute"] if abs_fail else []),
                *(["week_over_week_delta"] if wow_fail else []),
            ],
        },
    )


def check_prior_drift(
    train_prior: float,
    recent_prior: float,
    threshold: float = PRIOR_DRIFT_THRESHOLD,
) -> ValidationResult:
    """Fail if recent class balance moved >threshold (relative) from training."""
    if train_prior <= 0:
        return ValidationResult(
            name="prior_drift",
            passed=False,
            severity="alert",
            details={"error": "training prior is zero"},
        )
    delta = abs(recent_prior - train_prior) / train_prior
    passed = delta <= threshold
    return ValidationResult(
        name="prior_drift",
        passed=passed,
        severity="alert" if not passed else "info",
        metric=delta,
        details={
            "train_prior": float(train_prior),
            "recent_prior": float(recent_prior),
            "threshold_relative": threshold,
        },
    )


def check_ks_predictions(
    today_proba: np.ndarray,
    yesterday_proba: list[float] | None,
    p_threshold: float = KS_P_VALUE_THRESHOLD,
) -> ValidationResult:
    """KS-test today's vs yesterday's predicted probabilities."""
    if yesterday_proba is None or len(yesterday_proba) < 20 or len(today_proba) < 20:
        return ValidationResult(
            name="ks_predictions",
            passed=True,
            severity="info",
            details={"skipped": "need baseline from previous run"},
        )
    stat, p = stats.ks_2samp(np.asarray(today_proba), np.asarray(yesterday_proba))
    passed = p >= p_threshold
    return ValidationResult(
        name="ks_predictions",
        passed=passed,
        severity="alert" if not passed else "info",
        metric=float(p),
        details={
            "ks_statistic": float(stat),
            "p_threshold": p_threshold,
            "n_today": len(today_proba),
            "n_yesterday": len(yesterday_proba),
            "interpretation": (
                "Small p = predicted distribution shifted. Could be genuine drift"
                " or silent feature pipeline break."
            ),
        },
    )


def check_calibration(
    y_true: np.ndarray,
    y_proba: np.ndarray,
    n_bins: int = CALIBRATION_N_BINS,
) -> ValidationResult:
    """Brier score + N-bin reliability. INFO-only."""
    if len(y_true) < 10 or y_true.sum() == 0:
        return ValidationResult(
            name="calibration",
            passed=True,
            severity="info",
            details={"skipped": "insufficient sample"},
        )
    brier = float(brier_score_loss(y_true, y_proba))
    # Equal-frequency bins for N=80 stability
    bin_edges = np.linspace(0, 1, n_bins + 1)
    bin_data = []
    for lo, hi in zip(bin_edges[:-1], bin_edges[1:]):
        mask = (
            (y_proba >= lo) & (y_proba < hi)
            if hi < 1
            else ((y_proba >= lo) & (y_proba <= hi))
        )
        if mask.sum() == 0:
            continue
        bin_data.append(
            {
                "range": [float(lo), float(hi)],
                "n": int(mask.sum()),
                "mean_proba": float(y_proba[mask].mean()),
                "actual_rate": float(y_true[mask].mean()),
            }
        )
    return ValidationResult(
        name="calibration",
        passed=True,
        severity="info",
        metric=brier,
        details={"n_bins": n_bins, "bins": bin_data},
    )


def check_feature_importance_sanity(
    model: xgb.XGBClassifier,
    leak_set: set[str] = KNOWN_LEAK_FEATURES,
    top_n: int = 10,
) -> ValidationResult:
    """Alert if a known-leak feature reappears in top-N gain importance."""
    booster = model.get_booster()
    imp = booster.get_score(importance_type="gain")
    ranked = sorted(imp.items(), key=lambda kv: -kv[1])[:top_n]
    leaks_in_top = [name for name, _ in ranked if name in leak_set]
    passed = not leaks_in_top
    return ValidationResult(
        name="feature_importance_sanity",
        passed=passed,
        severity="alert" if not passed else "info",
        metric=float(len(leaks_in_top)),
        details={
            "leak_features_in_top": leaks_in_top,
            "top_features": [{"name": n, "gain": float(g)} for n, g in ranked],
            "known_leaks": sorted(leak_set),
        },
    )


def check_economic_backtest(
    y_true: np.ndarray,
    y_proba: np.ndarray,
    tp_pct: float = 50.0,
    sl_pct: float = 30.0,
    proba_threshold: float = DEFAULT_ENTRY_THRESHOLD,
    realistic: bool = False,
    position_size_sol: float = 0.1,
    realized_pnl_pct: np.ndarray | None = None,
) -> ValidationResult:
    """Simulate policy on holdout — realized PnL in SOL.

    Preferred path: ``realized_pnl_pct`` passed in, uses actual per-trade
    magnitudes. Fallback path (when absent): symmetric TP/-SL
    approximation — overstates magnitudes but keeps the sign honest.
    """
    name = "economic_backtest_realistic" if realistic else "economic_backtest"
    if len(y_true) == 0:
        return ValidationResult(
            name=name,
            passed=True,
            severity="info",
            details={"skipped": "empty holdout"},
        )
    entries = y_proba >= proba_threshold
    if entries.sum() == 0:
        return ValidationResult(
            name=name,
            passed=True,
            severity="info",
            details={"n_entries": 0, "note": "policy didn't enter any trade"},
        )
    # Label v2 (codex 2026-04-22) bakes fees + slippage + priority fee
    # into realized_pnl_pct. When present, standard and realistic both
    # use the same net column — the previous realistic-path deduction
    # would double-count fees.
    if realized_pnl_pct is not None and len(realized_pnl_pct) == len(y_true):
        pnl_pct_per_trade = np.where(
            entries,
            realized_pnl_pct.astype(float),
            0.0,
        )
        approximation_used = False
        label_already_net = True
    else:
        # Approximation fallback: +tp_pct for positive label, -sl_pct for
        # negative. Only used for legacy parquet files without the net
        # realized_pnl_pct column.
        wins = (y_true == 1) & entries
        losses = (y_true == 0) & entries
        pnl_pct_per_trade = wins.astype(float) * tp_pct - losses.astype(float) * sl_pct
        approximation_used = True
        label_already_net = False

    if realistic and not label_already_net:
        # Old approximation path lacks fee math — deduct costs here.
        pnl_pct_per_trade = np.where(
            entries,
            pnl_pct_per_trade - REALISTIC_SLIPPAGE_PCT - PUMPFUN_ROUND_TRIP_FEE_PCT,
            0.0,
        )
        successful_priority_sol = entries.sum() * PUMPFUN_ROUND_TRIP_PRIORITY_SOL
        failed_penalty_sol = (
            entries.sum() * REALISTIC_FAILED_TX_RATE * REALISTIC_PER_TRADE_FIXED_SOL
        )
    elif realistic and label_already_net:
        # Net label already has slippage+fee+priority cost. Realistic
        # variant adds only residual failed-tx penalty (not in label).
        successful_priority_sol = 0.0
        failed_penalty_sol = (
            entries.sum() * REALISTIC_FAILED_TX_RATE * REALISTIC_PER_TRADE_FIXED_SOL
        )
    else:
        successful_priority_sol = 0.0
        failed_penalty_sol = 0.0
    pnl_sol = float(
        (pnl_pct_per_trade[entries] / 100.0 * position_size_sol).sum()
        - successful_priority_sol
        - failed_penalty_sol
    )
    n_trades = int(entries.sum())
    n_wins = int(((y_true == 1) & entries).sum())
    wr = float(n_wins / max(n_trades, 1))
    passed = pnl_sol >= 0
    return ValidationResult(
        name=name,
        passed=passed,
        severity="alert" if not passed else "info",
        metric=pnl_sol,
        details={
            "n_entries": n_trades,
            "n_wins": n_wins,
            "win_rate": wr,
            "tp_pct": tp_pct,
            "sl_pct": sl_pct,
            "proba_threshold": proba_threshold,
            "position_size_sol": position_size_sol,
            "realistic_slippage_pct": REALISTIC_SLIPPAGE_PCT if realistic else 0,
            "pumpfun_round_trip_fee_pct": (
                PUMPFUN_ROUND_TRIP_FEE_PCT if realistic else 0
            ),
            "pumpfun_round_trip_priority_sol": (
                PUMPFUN_ROUND_TRIP_PRIORITY_SOL if realistic else 0
            ),
            "successful_priority_sol_total": successful_priority_sol,
            "failed_penalty_sol": failed_penalty_sol,
            "approximation_used": approximation_used,
            "mean_realized_pct_on_entries": (
                float(pnl_pct_per_trade[entries].mean())
                if n_trades and not realistic
                else None
            ),
        },
    )


def check_rolling_walk_forward(
    X: pd.DataFrame,
    y: pd.Series,
    model_params: dict[str, Any],
    n_windows: int = 5,
    min_positives: int = ROLLING_MIN_POSITIVES,
) -> ValidationResult:
    """Multi-window chronological AUC stability. INFO-only until N large."""
    total_pos = int(y.sum())
    if total_pos < min_positives:
        return ValidationResult(
            name="rolling_walk_forward",
            passed=True,
            severity="info",
            details={
                "skipped": f"need ≥{min_positives} positives, have {total_pos}",
                "note": "informational — gates open once dataset grows",
            },
        )
    params = {
        k: v
        for k, v in model_params.items()
        if k
        not in {
            "early_stopping_rounds",
            "eval_metric",
            "callbacks",
        }
    }
    n_est = params.get("n_estimators")
    params["n_estimators"] = int(n_est) if n_est else 150
    aucs: list[float] = []
    window_size = len(X) // (n_windows + 1)
    for i in range(n_windows):
        train_end = window_size * (i + 1)
        test_end = window_size * (i + 2)
        X_tr = X.iloc[:train_end]
        X_te = X.iloc[train_end:test_end]
        y_tr = y.iloc[:train_end]
        y_te = y.iloc[train_end:test_end]
        if y_tr.sum() < 5 or y_te.sum() < 2:
            continue
        m = xgb.XGBClassifier(**params)
        m.fit(X_tr, y_tr, verbose=False)
        proba = m.predict_proba(X_te)[:, 1]
        aucs.append(float(roc_auc_score(y_te, proba)))
    if len(aucs) < 2:
        return ValidationResult(
            name="rolling_walk_forward",
            passed=True,
            severity="info",
            details={"skipped": "too few usable windows"},
        )
    std = float(np.std(aucs))
    mean = float(np.mean(aucs))
    return ValidationResult(
        name="rolling_walk_forward",
        passed=True,
        severity="info",
        metric=std,
        details={
            "aucs": aucs,
            "mean": mean,
            "std": std,
            "note": "INFO — track trend over time; no alert threshold yet",
        },
    )


# ── Orchestrator ────────────────────────────────────────────────────


def _load_yesterday_report(report_dir: Path, kind: ModelKind) -> dict | None:
    """Find most recent report before today for this model kind."""
    today = date.today()
    for days_back in range(1, 14):
        d = today - timedelta(days=days_back)
        p = report_dir / f"daily_report_{kind}_{d.isoformat()}.json"
        if p.exists():
            try:
                return json.loads(p.read_text())
            except json.JSONDecodeError:
                logger.warning("Corrupt report at %s — skipping", p)
    return None


def _split_chrono(
    df: pd.DataFrame, time_col: str, train_frac: float = 0.8
) -> tuple[pd.DataFrame, pd.DataFrame]:
    df = df.sort_values(time_col).reset_index(drop=True)
    cut = int(len(df) * train_frac)
    return df.iloc[:cut], df.iloc[cut:]


def _split_chrono_exit(df: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Mint-grouped chrono split by entry_ts (matches train.py)."""
    mints_order = df.drop_duplicates("mint").sort_values("entry_ts")["mint"].tolist()
    if not mints_order:
        return df.iloc[:0], df.iloc[:0]
    cut_idx = max(1, int(len(mints_order) * 0.8))
    cut_mint = mints_order[min(cut_idx, len(mints_order) - 1)]
    cut_ts = df.loc[df.mint == cut_mint, "entry_ts"].iloc[0]
    train_mask = df.entry_ts < cut_ts
    return df.loc[train_mask], df.loc[~train_mask]


def run_validation(
    kind: ModelKind,
    db_path: str,
    model_path: Path,
    report_dir: Path,
    today: date | None = None,
) -> dict[str, Any]:
    """Run all daily tests for one model kind, write report, return dict."""
    today = today or date.today()
    logger.info("=" * 70)
    logger.info("DAILY VALIDATION — %s model — %s", kind.upper(), today.isoformat())
    logger.info("=" * 70)

    meta_path = model_path.with_suffix(".meta.json")
    if not model_path.exists() or not meta_path.exists():
        raise FileNotFoundError(f"Missing model or meta: {model_path}")
    meta = json.loads(meta_path.read_text())
    feature_cols: list[str] = meta["features"]
    train_prior = float(meta.get("base_rate", 0.0))

    model = xgb.XGBClassifier()
    model.load_model(model_path)

    if kind == "entry":
        df = build_entry_dataset(db_path)
        time_col = "scored_at"
    else:
        df = build_exit_dataset(db_path)
        time_col = "entry_ts"
    if df.empty:
        raise RuntimeError(f"Empty {kind} dataset — cannot validate")

    # Ensure feature columns exist — missing means schema drift from training
    missing = [c for c in feature_cols if c not in df.columns]
    if missing:
        raise RuntimeError(f"Features missing from fresh dataset: {missing}")
    keep_cols = [*feature_cols, "label", time_col]
    if kind == "exit":
        keep_cols.append("mint")
    if kind == "entry" and "realized_pnl_pct" in df.columns:
        keep_cols.append("realized_pnl_pct")
    df = df[keep_cols]

    if kind == "entry":
        train_df, test_df = _split_chrono(df, time_col)
    else:
        train_df, test_df = _split_chrono_exit(df)
    X_full = df[feature_cols]
    y_full = df["label"]
    X_train, X_test = train_df[feature_cols], test_df[feature_cols]
    _y_train, y_test = train_df["label"].values, test_df["label"].values  # noqa: F841
    realized_pnl_test: np.ndarray | None = None
    if kind == "entry" and "realized_pnl_pct" in test_df.columns:
        realized_pnl_test = test_df["realized_pnl_pct"].values

    proba_test = model.predict_proba(X_test)[:, 1] if len(X_test) else np.array([])
    _proba_all = model.predict_proba(X_full)[:, 1] if len(X_full) else np.array([])  # noqa: F841

    yesterday = _load_yesterday_report(report_dir, kind)
    yesterday_proba = None
    yesterday_adv_auc = None
    if yesterday:
        try:
            yesterday_proba = yesterday.get("proba_sample_test")
            for r in yesterday.get("results", []):
                if (
                    r["name"] == "adversarial_validation"
                    and r.get("metric") is not None
                ):
                    yesterday_adv_auc = r["metric"]
        except (KeyError, TypeError):
            pass

    results: list[ValidationResult] = []

    results.append(check_shuffled_labels(X_full, y_full, model.get_params()))
    results.append(check_adversarial_validation(X_train, X_test, yesterday_adv_auc))
    results.append(check_prior_drift(train_prior, float(y_full.mean())))
    results.append(check_ks_predictions(proba_test, yesterday_proba))
    results.append(check_calibration(y_test, proba_test))
    results.append(check_feature_importance_sanity(model))

    if kind == "entry" and len(y_test) > 0:
        results.append(
            check_economic_backtest(
                y_test,
                proba_test,
                realistic=False,
                realized_pnl_pct=realized_pnl_test,
            )
        )
        results.append(
            check_economic_backtest(
                y_test,
                proba_test,
                realistic=True,
                realized_pnl_pct=realized_pnl_test,
            )
        )

    results.append(
        check_rolling_walk_forward(
            X_full,
            y_full,
            model.get_params(),
        )
    )

    alerts = [r for r in results if r.severity == "alert" and not r.passed]
    for r in results:
        icon = "✓" if r.passed else ("⚠" if r.severity == "warn" else "✗")
        metric_s = f"{r.metric:.4f}" if r.metric is not None else "—"
        logger.info("  %s %-35s metric=%s passed=%s", icon, r.name, metric_s, r.passed)

    report = {
        "kind": kind,
        "date": today.isoformat(),
        "generated_at": datetime.now(UTC).isoformat(),
        "model_path": str(model_path),
        "train_rows_meta": meta.get("train_rows"),
        "fresh_rows_total": len(df),
        "fresh_rows_test": len(test_df),
        "fresh_positives_total": int(y_full.sum()),
        "alerts_fired": len(alerts),
        "alert_names": [r.name for r in alerts],
        "results": [r.as_dict() for r in results],
        # Sample of test-set predictions (capped) for tomorrow's KS-test.
        "proba_sample_test": proba_test[:500].tolist() if len(proba_test) else [],
        "proba_sample_hash": (
            hashlib.sha256(proba_test.tobytes()).hexdigest()
            if len(proba_test)
            else None
        ),
    }

    report_dir.mkdir(parents=True, exist_ok=True)
    report_path = report_dir / f"daily_report_{kind}_{today.isoformat()}.json"
    report_path.write_text(json.dumps(report, indent=2))
    logger.info("Report: %s (%d alerts)", report_path, len(alerts))
    return report


def send_push_alert(reports: list[dict]) -> None:
    """Hook point — actual push wire-up done in main()."""
    total_alerts = sum(r.get("alerts_fired", 0) for r in reports)
    if total_alerts == 0:
        return
    lines = []
    for rep in reports:
        if rep["alerts_fired"]:
            lines.append(
                f"{rep['kind']}: {rep['alerts_fired']} alerts "
                f"({', '.join(rep['alert_names'])})"
            )
    msg = "ML daily validation FAILED — " + " | ".join(lines)
    logger.error(msg)
    # PushNotification tool is wired in at scheduled-task level (via the
    # cron orchestrator), keeping this module free of runtime dependencies.


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", default="pulse_bot.db")
    ap.add_argument("--model-dir", default="data/ml")
    ap.add_argument("--report-dir", default="data/ml/reports")
    ap.add_argument(
        "--kind",
        choices=["entry", "exit", "both"],
        default="both",
    )
    ap.add_argument(
        "--fail-on-alert",
        action="store_true",
        help="Exit nonzero if any alert fires (for cron/CI).",
    )
    args = ap.parse_args()

    model_dir = Path(args.model_dir)
    report_dir = Path(args.report_dir)

    kinds: list[ModelKind]
    if args.kind == "both":
        kinds = ["entry", "exit"]
    else:
        kinds = [args.kind]  # type: ignore[list-item]

    reports: list[dict] = []
    for k in kinds:
        try:
            report = run_validation(
                kind=k,
                db_path=args.db,
                model_path=model_dir / f"{k}_model.ubj",
                report_dir=report_dir,
            )
            reports.append(report)
        except Exception as e:
            logger.exception("Validation failed for %s: %s", k, e)
            reports.append(
                {
                    "kind": k,
                    "error": str(e),
                    "alerts_fired": 1,
                    "alert_names": ["validation_crashed"],
                }
            )

    send_push_alert(reports)
    total_alerts = sum(r.get("alerts_fired", 0) for r in reports)
    if args.fail_on_alert and total_alerts > 0:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
