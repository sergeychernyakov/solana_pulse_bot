# pulse_bot/ml/shadow.py
"""Shadow predictions logging.

Models in shadow mode predict on the same inputs as live decisions but
do NOT influence those decisions. Each prediction is logged to
``shadow_predictions`` so we can later compare it against the realized
outcome and decide whether to flip the model into live mode.

Activation gates (env, default OFF — explicit opt-in):
    * ``PULSE_ENTRY_T30_SHADOW=1`` — log T+30 model predictions every
      time the T+30 checkpoint fires.
    * ``PULSE_EXIT_QUANTILE_SHADOW=1`` — log SL/TP quantile predictions
      every time the exit-manager re-evaluates a held trade.

The functions are best-effort: a failed insert never raises into the
live decision path. We prefer dropping a shadow row over crashing the
trader.
"""

from __future__ import annotations

import json
import logging
import os
import time
from typing import Any

import psycopg2

from pulse_bot.db import _resolve_dsn

logger = logging.getLogger(__name__)


_T30_SHADOW_ENABLED = os.environ.get("PULSE_ENTRY_T30_SHADOW", "0") == "1"
_QUANTILE_SHADOW_ENABLED = os.environ.get("PULSE_EXIT_QUANTILE_SHADOW", "0") == "1"
_TIMING_SHADOW_ENABLED = os.environ.get("PULSE_TIMING_SHADOW", "0") == "1"
_SURVIVAL_SHADOW_ENABLED = os.environ.get("PULSE_SURVIVAL_SHADOW", "0") == "1"


def t30_shadow_enabled() -> bool:
    return _T30_SHADOW_ENABLED


def quantile_shadow_enabled() -> bool:
    return _QUANTILE_SHADOW_ENABLED


def timing_shadow_enabled() -> bool:
    return _TIMING_SHADOW_ENABLED


def survival_shadow_enabled() -> bool:
    return _SURVIVAL_SHADOW_ENABLED


def _insert(
    *,
    mint: str,
    model_name: str,
    scored_at: float,
    snapshot_t: float | None,
    prediction: dict[str, Any],
    confidence: float,
    model_hash: str | None,
    schema_version: str | None,
) -> None:
    """Synchronous one-shot insert. Swallows exceptions — shadow MUST
    NOT poison the live decision path."""
    dsn = _resolve_dsn(None)
    conn = None
    try:
        conn = psycopg2.connect(dsn)
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO shadow_predictions
                    (mint, model_name, scored_at, snapshot_t,
                     prediction, confidence, model_hash, schema_version,
                     inserted_at)
                VALUES (%s, %s, %s, %s, %s::jsonb, %s, %s, %s, %s)
                """,
                (
                    mint,
                    model_name,
                    float(scored_at),
                    float(snapshot_t) if snapshot_t is not None else None,
                    json.dumps(prediction),
                    float(confidence),
                    model_hash,
                    schema_version,
                    time.time(),
                ),
            )
            conn.commit()
    except Exception:
        # Best-effort — never propagate.
        logger.debug("shadow insert failed for %s/%s", mint[:12], model_name)
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:  # nosec B110
                # Reason: best-effort cleanup of psycopg2 connection;
                # any close error is moot since we already committed (or
                # silently failed) above.
                pass


def record_t30_shadow(
    *,
    mint: str,
    scored_at: float,
    proba: float,
    action: str,
    model_hash: str | None,
    schema_version: str | None = None,
) -> None:
    """Log one T+30 model prediction. ``action`` is the policy verdict
    (``BUY`` / ``SKIP`` / ``DEFER``); ``proba`` is the raw probability.
    Confidence is the distance from 0.5 — high = confident either way."""
    if not _T30_SHADOW_ENABLED:
        return
    confidence = abs(proba - 0.5) * 2.0  # 0=indecisive, 1=fully confident
    _insert(
        mint=mint,
        model_name="entry_model_t30",
        scored_at=scored_at,
        snapshot_t=30.0,
        prediction={"proba": proba, "action": action},
        confidence=confidence,
        model_hash=model_hash,
        schema_version=schema_version,
    )


def record_timing_shadow(
    *,
    mint: str,
    scored_at: float,
    snapshot_t: float,
    action: str,
    proba: float,
) -> None:
    """Log one entry-timing prediction at a 15s checkpoint.

    ``action`` is the policy verdict (``BUY``/``SKIP``/``WAIT``);
    ``proba`` is the max class probability (already used as confidence).
    """
    if not _TIMING_SHADOW_ENABLED:
        return
    _insert(
        mint=mint,
        model_name="entry_timing_model",
        scored_at=scored_at,
        snapshot_t=snapshot_t,
        prediction={"action": action, "proba": proba},
        confidence=float(proba),
        model_hash=None,
        schema_version=None,
    )


def record_survival_shadow(
    *,
    mint: str,
    scored_at: float,
    snapshot_t: float,
    remaining_life_seconds: float,
    confidence: float,
    hazard_curve: list[float] | None = None,
) -> None:
    """Log one survival-model prediction during a paper-trade tick."""
    if not _SURVIVAL_SHADOW_ENABLED:
        return
    payload: dict[str, Any] = {
        "remaining_life_seconds": remaining_life_seconds,
    }
    if hazard_curve is not None:
        # Cap to bounded length to keep JSONB rows compact (curve usually
        # 36 buckets — safe).
        payload["hazard_curve"] = list(hazard_curve)[:64]
    _insert(
        mint=mint,
        model_name="survival_model",
        scored_at=scored_at,
        snapshot_t=snapshot_t,
        prediction=payload,
        confidence=float(confidence),
        model_hash=None,
        schema_version=None,
    )


def record_quantile_shadow(
    *,
    mint: str,
    scored_at: float,
    snapshot_t: float | None,
    q25_pred: float,
    q75_pred: float,
    actual_pnl_pct: float | None,
    model_hash_sl: str | None = None,
    model_hash_tp: str | None = None,
) -> None:
    """Log one paired quantile prediction (q25 SL + q75 TP) at an exit
    decision tick. ``actual_pnl_pct`` is the live realized PnL at this
    tick (None if not yet available)."""
    if not _QUANTILE_SHADOW_ENABLED:
        return
    confidence = max(0.0, q75_pred - q25_pred)  # spread = uncertainty proxy
    _insert(
        mint=mint,
        model_name="exit_quantile",
        scored_at=scored_at,
        snapshot_t=snapshot_t,
        prediction={
            "q25_pred": q25_pred,
            "q75_pred": q75_pred,
            "actual_pnl_pct": actual_pnl_pct,
        },
        confidence=confidence,
        model_hash=f"sl={model_hash_sl}|tp={model_hash_tp}",
        schema_version=None,
    )
