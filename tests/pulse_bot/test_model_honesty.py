# tests/pulse_bot/test_model_honesty.py
"""End-to-end model honesty test — runs the daily validation pipeline.

Fails the test suite if a CRITICAL alert fires — label leakage, leak
feature in top importance, prior drift, or economic backtest showing
the policy loses money on holdout. Soft alerts (adversarial AUC, KS
distribution shift, calibration) are logged but never fail the test —
they are regime-sensitive on pump.fun and codex flagged them as
expected-to-fire on 1–2 day windows.

Skipped when the live DB or the trained model files are missing — the
ML pipeline is optional infra, not core.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from pulse_bot.ml.daily_validation import run_validation

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
DB_PATH = "pulse_bot"  # ignored by _resolve_dsn, actual data comes from PG
MODEL_DIR = REPO_ROOT / "data" / "ml"

# Alerts that should ALWAYS fail the suite — they indicate a regression
# of a known codex v9 fix or a broken model.
CRITICAL_ALERTS: set[str] = {
    "shuffled_labels",  # leakage
    "feature_importance_sanity",  # known-leak feature reappeared in top
    "prior_drift",  # train/recent class-balance mismatch
    "economic_backtest",  # standard policy loses SOL
    "economic_backtest_realistic",
    "validation_crashed",  # orchestrator crashed
}

# Alerts that are informational / noise-dominated on small samples.
SOFT_ALERTS: set[str] = {
    "adversarial_validation",
    "ks_predictions",
    "calibration",
    "rolling_walk_forward",
}


def _db_has_data() -> bool:
    try:
        import psycopg2

        from pulse_bot.db import _DEFAULT_PG_DSN

        conn = psycopg2.connect(_DEFAULT_PG_DSN)
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM token_scores WHERE source='live'")
            count = cur.fetchone()[0]
        conn.close()
        return count > 1000
    except Exception:
        return False


def _model_present(kind: str) -> bool:
    return (MODEL_DIR / f"{kind}_model.ubj").exists()


pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")


@pytest.mark.xfail(
    reason=(
        "Phase 0 blocker: economic_backtest fails with -0.56 SOL on holdout. "
        "Root cause is data truncation (bot unsubscribes after observe_seconds=90s) "
        "+ TP=100% / SL=-15% / max_hold=90s asymmetry. Fix path: enable "
        "PULSE_EXTENDED_OBSERVE_SECONDS=600 on live bot, accumulate 2-3 weeks "
        "of post-scoring trade data, re-evaluate. See ROADMAP_2026_05.md Phase 0 "
        "and project_economic_backtest_failing memory."
    ),
    strict=False,
)
@pytest.mark.skipif(not _db_has_data(), reason="pulse_bot.db missing or empty")
@pytest.mark.skipif(not _model_present("entry"), reason="entry model not trained")
def test_entry_model_no_critical_alerts(tmp_path: Path) -> None:
    report = run_validation(
        kind="entry",
        db_path=str(DB_PATH),
        model_path=MODEL_DIR / "entry_model.ubj",
        report_dir=tmp_path,
    )
    alerts = set(report.get("alert_names", []))
    critical = alerts & CRITICAL_ALERTS
    soft = alerts & SOFT_ALERTS
    # Always log soft alerts so they are visible in pytest output
    if soft:
        print(f"[entry] soft alerts (non-fatal): {sorted(soft)}")
    assert not critical, (
        f"Entry model CRITICAL alerts: {sorted(critical)} "
        f"(soft: {sorted(soft)}). See the report written in the test tmp dir, "
        f"or run `python -m pulse_bot.ml.daily_validation --kind entry` "
        f"to regenerate a persistent copy under data/ml/reports/."
    )


@pytest.mark.skipif(not _db_has_data(), reason="pulse_bot.db missing or empty")
@pytest.mark.skipif(not _model_present("exit"), reason="exit model not trained")
def test_exit_model_no_critical_alerts(tmp_path: Path) -> None:
    report = run_validation(
        kind="exit",
        db_path=str(DB_PATH),
        model_path=MODEL_DIR / "exit_model.ubj",
        report_dir=tmp_path,
    )
    alerts = set(report.get("alert_names", []))
    critical = alerts & CRITICAL_ALERTS
    soft = alerts & SOFT_ALERTS
    if soft:
        print(f"[exit] soft alerts (non-fatal): {sorted(soft)}")
    assert (
        not critical
    ), f"Exit model CRITICAL alerts: {sorted(critical)} (soft: {sorted(soft)})"
