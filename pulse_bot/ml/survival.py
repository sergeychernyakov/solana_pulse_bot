# pulse_bot/ml/survival.py
"""Discrete-time hazard / survival model for pump.fun token "death".

Phase 4B of the 2026-05 roadmap. Replaces the fixed
``exit_max_hold_seconds=90`` cap with a data-driven prediction:
``min(predicted_remaining_life, 180s)``. The model is **not** integrated
into the live :class:`ExitManager` here — that lives in the Phase 4
deployment patch. This module only:

1. Builds (mint, time-bucket) hazard rows from labeled trades.
2. Fits an XGBoost binary classifier on
   ``P(token dies in this 5s bucket | alive at bucket start)``.
3. At inference time, accumulates ``S(t) = Π (1 - h_i)`` and reports the
   first ``t`` where ``S(t) < 0.5`` as the predicted remaining life.

Token death = the same exit reasons :class:`ExitManager` treats as a
hard "no liquidity / no demand" signal::

    {pulse_dead, no_new_blood, sell_pressure}

Tokens that closed for any other reason (TP, SL, trailing, max_hold, etc)
are right-censored — the bot left voluntarily, the token might have
lived on. Tokens still in ``status='open'`` are right-censored at the
current wall clock or at ``exit_max_hold_seconds`` (whichever is smaller),
matching the live exit-cap.

Why discrete-time hazard over Cox PH:
* XGBoost handles non-linearities + missing features the same way as
  the entry model — no scikit-survival dependency.
* Tree splits naturally model "step changes" in mortality rate (e.g.
  the cliff right after T+90s when many bots auto-sell).
* Cumulative survival is a trivial post-process; we don't need full
  hazard ratios.
"""

from __future__ import annotations

import json
import logging
import math
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

import pandas as pd

from pulse_bot.db import Database, _resolve_dsn

logger = logging.getLogger(__name__)


# Exit reasons that we count as "the token actually died" — i.e. the
# bot's death-signal code path fired and a healthy token would NOT have
# triggered it. Anything else (TP/SL/timeout/manual) is censoring.
DEATH_EXIT_REASONS: frozenset[str] = frozenset(
    {"pulse_dead", "no_new_blood", "sell_pressure"}
)

# Time-discretisation. 5s buckets give us ~36 rows per token at the
# default 180s observation cap — fine-grained enough to localize the
# T+90s mortality cliff, sparse enough to keep dataset rows reasonable.
DEFAULT_BUCKET_SECONDS: float = 5.0
DEFAULT_MAX_HORIZON_SECONDS: float = 180.0

SURVIVAL_SCHEMA_VERSION: str = "survival_v1_20260425"


# ── Data classes ────────────────────────────────────────────────────


@dataclass(frozen=True)
class SurvivalPrediction:
    """Inference output for a single token at a single moment.

    Attributes:
        remaining_life_seconds: First bucket boundary at which cumulative
            survival probability falls below 0.5. ``inf`` if survival
            stays ≥ 0.5 across the full horizon (bot should fall back to
            its hard ceiling of 180s).
        hazard_curve: Per-bucket conditional death probability, indexed
            by bucket. ``hazard_curve[i]`` = P(die in bucket i | alive
            at bucket i start). Useful for diagnostics + plotting.
        confidence: 1 - average hazard variance proxy. Coarse measure
            of how peaky the curve is — high confidence ↔ the model is
            sure when death will (or will not) happen. Bounded [0, 1].
    """

    remaining_life_seconds: float
    hazard_curve: list[float] = field(default_factory=list)
    confidence: float = 0.0


# ── Label builder ───────────────────────────────────────────────────


class SurvivalLabelBuilder:
    """Materialise (mint, bucket) hazard rows from ``paper_trades``.

    For each closed paper-trade row we emit ``ceil(duration / bucket)``
    rows. The last row carries ``died=1`` iff the exit reason is in
    :data:`DEATH_EXIT_REASONS`; all earlier rows + every row of a
    censored token carry ``died=0``.

    Live tokens (``status='open'``) are right-censored at
    ``min(now - entry_time, max_horizon_seconds)`` so we don't introduce
    a "future labels we haven't observed yet" leakage.
    """

    def __init__(
        self,
        bucket_seconds: float = DEFAULT_BUCKET_SECONDS,
        max_horizon_seconds: float = DEFAULT_MAX_HORIZON_SECONDS,
    ) -> None:
        if bucket_seconds <= 0:
            raise ValueError("bucket_seconds must be positive")
        if max_horizon_seconds <= 0:
            raise ValueError("max_horizon_seconds must be positive")
        self.bucket_seconds = float(bucket_seconds)
        self.max_horizon_seconds = float(max_horizon_seconds)

    def build_from_records(
        self,
        records: Iterable[Mapping[str, Any]],
        *,
        now_ts: float | None = None,
    ) -> pd.DataFrame:
        """Expand iterable of paper-trade-shaped dicts into hazard rows.

        Each record needs at least: ``mint``, ``entry_time``, ``status``,
        ``exit_time``, ``exit_reason``. Any additional keys are passed
        through unchanged onto every emitted bucket row — handy for
        joining entry features later.
        """
        if now_ts is None:
            now_ts = time.time()
        out_rows: list[dict[str, Any]] = []
        for rec in records:
            duration, died = self._record_duration(rec, now_ts=now_ts)
            if duration <= 0.0:
                continue
            n_buckets = max(1, int(math.ceil(duration / self.bucket_seconds)))
            for i in range(n_buckets):
                bucket_start = i * self.bucket_seconds
                bucket_end = bucket_start + self.bucket_seconds
                # Death lands in the bucket containing the exit time.
                # Censored tokens leave all hazards = 0.
                hazard = 1 if (died and i == n_buckets - 1) else 0
                row = {
                    **rec,
                    "bucket_index": i,
                    "elapsed_seconds": bucket_start,
                    "bucket_end_seconds": bucket_end,
                    "died_in_bucket": hazard,
                }
                out_rows.append(row)
        return pd.DataFrame(out_rows)

    def _record_duration(
        self,
        rec: Mapping[str, Any],
        *,
        now_ts: float,
    ) -> tuple[float, bool]:
        """Return ``(duration_seconds, died_bool)`` for a single trade.

        Censoring rules:
        * Closed trades whose ``exit_reason`` is in
          :data:`DEATH_EXIT_REASONS` → died, duration = exit - entry.
        * Closed trades exiting for any other reason → censored at
          their actual exit time (we only know they survived that long).
        * Open trades → censored at ``now_ts``.
        * Duration is clamped to ``max_horizon_seconds`` regardless of
          status to match the live ceiling.
        """
        raw_entry = rec.get("entry_time")
        if raw_entry is None:
            return 0.0, False
        entry_time = float(raw_entry)
        if entry_time < 0:
            return 0.0, False
        status = str(rec.get("status") or "").lower()
        exit_reason = str(rec.get("exit_reason") or "").lower()
        if status == "closed":
            exit_time = float(rec.get("exit_time") or 0.0)
            raw_duration = max(0.0, exit_time - entry_time)
            died = exit_reason in DEATH_EXIT_REASONS
        else:
            raw_duration = max(0.0, now_ts - entry_time)
            died = False
        # Death beyond the horizon → censor (we never observed it
        # within the modelled window, so the hazard label must be 0).
        if raw_duration > self.max_horizon_seconds:
            return self.max_horizon_seconds, False
        return raw_duration, died

    # ── Convenience: load straight from PG ─────────────────────────
    def build_from_db(
        self,
        db_path: str | None = None,
        *,
        limit: int | None = None,
        now_ts: float | None = None,
    ) -> pd.DataFrame:
        """Pull paper_trades from PG and build the hazard frame."""
        dsn = _resolve_dsn(db_path)
        # Minimal columns; downstream feature joins happen in train().
        sql = (
            "SELECT mint, status, entry_time, exit_time, exit_reason, "
            "entry_score, entry_mcap_sol, entry_buyer_number "
            "FROM paper_trades WHERE entry_time IS NOT NULL AND entry_time > 0"
        )
        if limit is not None and limit > 0:
            sql += f" LIMIT {int(limit)}"
        db = Database(dsn)
        rows = db._sync_query(sql)
        return self.build_from_records(rows, now_ts=now_ts)


# ── Training ────────────────────────────────────────────────────────


def _select_feature_columns(df: pd.DataFrame) -> list[str]:
    """Pick numeric columns suitable as model inputs.

    Excludes the label, identity columns, and timestamp columns that
    would leak future state. Anything else numeric is fair game (the
    expectation is the caller has already merged in entry features
    when richer signal is needed).
    """
    drop = {
        "died_in_bucket",
        "mint",
        "status",
        "exit_reason",
        "exit_time",
        "entry_time",
        "bucket_end_seconds",
    }
    cols: list[str] = []
    for c in df.columns:
        if c in drop:
            continue
        if pd.api.types.is_numeric_dtype(df[c]):
            cols.append(c)
    return cols


def train_survival_model(
    df: pd.DataFrame,
    model_out: Path,
    *,
    n_estimators: int = 200,
    max_depth: int = 4,
    learning_rate: float = 0.05,
    bucket_seconds: float = DEFAULT_BUCKET_SECONDS,
    max_horizon_seconds: float = DEFAULT_MAX_HORIZON_SECONDS,
) -> dict[str, Any]:
    """Fit an XGBoost classifier on the hazard rows.

    Args:
        df: Output of :meth:`SurvivalLabelBuilder.build_from_records`
            with at minimum ``died_in_bucket``, ``bucket_index``,
            ``elapsed_seconds`` columns. Additional numeric columns will
            be picked up automatically as features.
        model_out: Where to write the .ubj model file. ``.meta.json`` is
            written alongside.

    Returns a metrics dict (rows, positive rate, feature list).
    """
    import xgboost as xgb  # lazy: only available in .venv

    if "died_in_bucket" not in df.columns:
        raise ValueError("DataFrame missing 'died_in_bucket' column")
    feature_cols = _select_feature_columns(df)
    if not feature_cols:
        raise ValueError("No numeric feature columns available for training")
    x = df[feature_cols].astype(float).fillna(0.0).values
    y = df["died_in_bucket"].astype(int).values
    pos = int(y.sum())
    if pos == 0:
        raise ValueError(
            "Training set has zero positive (died) buckets — cannot fit hazard"
        )
    # Hazard rate is naturally low (most buckets are non-death). Use
    # scale_pos_weight to keep the gradient balanced.
    spw = float((len(y) - pos) / max(pos, 1))
    model = xgb.XGBClassifier(
        n_estimators=n_estimators,
        max_depth=max_depth,
        learning_rate=learning_rate,
        scale_pos_weight=spw,
        objective="binary:logistic",
        eval_metric="logloss",
        n_jobs=-1,
        tree_method="hist",
    )
    model.fit(x, y)
    model_out.parent.mkdir(parents=True, exist_ok=True)
    model.save_model(model_out)
    meta = {
        "schema_version": SURVIVAL_SCHEMA_VERSION,
        "features": feature_cols,
        "bucket_seconds": bucket_seconds,
        "max_horizon_seconds": max_horizon_seconds,
        "rows": int(len(df)),
        "positives": pos,
        "positive_rate": pos / max(len(y), 1),
        "scale_pos_weight": spw,
        "n_estimators": n_estimators,
        "max_depth": max_depth,
        "learning_rate": learning_rate,
    }
    meta_out = model_out.with_suffix(".meta.json")
    meta_out.write_text(json.dumps(meta, indent=2))
    logger.info(
        "Trained survival model: %d rows, %d positives (%.2f%%) → %s",
        meta["rows"],
        meta["positives"],
        float(meta["positive_rate"]) * 100.0,
        model_out,
    )
    return meta


# ── Inference ───────────────────────────────────────────────────────


def _confidence_from_curve(curve: Sequence[float]) -> float:
    """Crude confidence proxy.

    A curve of all 0.5 is maximally uncertain (random); a curve full of
    0/1 spikes is confident either way. Use mean absolute distance from
    0.5, scaled to [0, 1].
    """
    if not curve:
        return 0.0
    avg_dist = sum(abs(h - 0.5) for h in curve) / len(curve)
    return min(1.0, max(0.0, avg_dist * 2.0))


def predict_remaining_life(
    model: Any,
    features_at_now: Mapping[str, float],
    *,
    feature_order: Sequence[str],
    bucket_seconds: float = DEFAULT_BUCKET_SECONDS,
    max_horizon_seconds: float = DEFAULT_MAX_HORIZON_SECONDS,
    now_elapsed_seconds: float = 0.0,
    survival_threshold: float = 0.5,
) -> SurvivalPrediction:
    """Project the survival curve forward and return predicted remaining life.

    The classifier predicts hazard at a *single* (features, bucket)
    pair. For inference we evaluate it once per future bucket, holding
    feature values constant except for ``elapsed_seconds`` /
    ``bucket_index`` which advance with the projection. This is the
    standard discrete-time-hazard inference pattern; downstream
    integration can re-feed updated features each timer tick if they
    become available.

    Args:
        model: Loaded XGBoost classifier (any object with
            ``predict_proba``).
        features_at_now: Current feature values keyed by name.
        feature_order: Order of features as passed to ``fit``. Pulled
            from the meta.json that ships alongside the model.
        bucket_seconds / max_horizon_seconds: Must match training.
        now_elapsed_seconds: How long the token has already been alive
            since entry. Used to advance ``elapsed_seconds`` /
            ``bucket_index`` as we project forward.
        survival_threshold: Survival probability that defines "expected
            death" — 0.5 = the median. Lowering to e.g. 0.3 yields a
            more conservative (earlier) predicted death.

    Returns:
        :class:`SurvivalPrediction`. ``remaining_life_seconds = inf``
        when survival never crosses the threshold within the horizon.
    """
    horizon_left = max(0.0, max_horizon_seconds - now_elapsed_seconds)
    if horizon_left <= 0.0:
        return SurvivalPrediction(remaining_life_seconds=0.0)
    n_steps = max(1, int(math.ceil(horizon_left / bucket_seconds)))
    base = dict(features_at_now)
    rows: list[list[float]] = []
    for i in range(n_steps):
        future_elapsed = now_elapsed_seconds + i * bucket_seconds
        base["elapsed_seconds"] = future_elapsed
        # ``bucket_index`` may or may not be in feature_order depending
        # on what survived ``_select_feature_columns``; set both
        # defensively.
        base["bucket_index"] = future_elapsed / bucket_seconds
        rows.append([float(base.get(k, 0.0)) for k in feature_order])
    # XGBoost's predict_proba returns shape (N, 2); column 1 is the
    # positive (died) class probability = the hazard rate per bucket.
    proba = model.predict_proba(rows)
    if hasattr(proba, "tolist"):
        proba = proba.tolist()
    hazards: list[float] = []
    for p in proba:
        if isinstance(p, (list, tuple)):
            hazards.append(float(p[1]))
        else:
            hazards.append(float(p))
    cumulative_survival = 1.0
    remaining_life = math.inf
    for i, h in enumerate(hazards):
        h_clamped = min(max(h, 0.0), 1.0)
        cumulative_survival *= 1.0 - h_clamped
        if cumulative_survival < survival_threshold:
            # Predicted death lands at the END of bucket i.
            remaining_life = (i + 1) * bucket_seconds
            break
    confidence = _confidence_from_curve(hazards)
    return SurvivalPrediction(
        remaining_life_seconds=float(remaining_life),
        hazard_curve=hazards,
        confidence=confidence,
    )


def load_survival_model(model_path: Path) -> tuple[Any, dict[str, Any]]:
    """Load a saved .ubj + sidecar meta.json.

    Returns ``(model, meta_dict)``. ``meta_dict['features']`` holds the
    canonical feature order needed by :func:`predict_remaining_life`.
    """
    import xgboost as xgb

    meta_path = model_path.with_suffix(".meta.json")
    meta = json.loads(meta_path.read_text())
    model = xgb.XGBClassifier()
    model.load_model(model_path)
    return model, meta


def predict_from_dict(
    model_path: Path,
    features_at_now: Mapping[str, float],
    *,
    now_elapsed_seconds: float = 0.0,
) -> SurvivalPrediction:
    """End-to-end helper: load model + meta and run inference."""
    model, meta = load_survival_model(model_path)
    return predict_remaining_life(
        model,
        features_at_now,
        feature_order=meta["features"],
        bucket_seconds=float(meta.get("bucket_seconds", DEFAULT_BUCKET_SECONDS)),
        max_horizon_seconds=float(
            meta.get("max_horizon_seconds", DEFAULT_MAX_HORIZON_SECONDS)
        ),
        now_elapsed_seconds=now_elapsed_seconds,
    )


__all__ = [
    "DEATH_EXIT_REASONS",
    "DEFAULT_BUCKET_SECONDS",
    "DEFAULT_MAX_HORIZON_SECONDS",
    "SURVIVAL_SCHEMA_VERSION",
    "SurvivalLabelBuilder",
    "SurvivalPrediction",
    "load_survival_model",
    "predict_from_dict",
    "predict_remaining_life",
    "train_survival_model",
]


# ``asdict`` is exported here as a convenience for callers that want to
# log a SurvivalPrediction as JSON without importing from dataclasses.
def to_dict(pred: SurvivalPrediction) -> dict[str, Any]:
    """Serialise :class:`SurvivalPrediction` to a plain dict (JSON-safe)."""
    d = asdict(pred)
    if not math.isfinite(d["remaining_life_seconds"]):
        d["remaining_life_seconds"] = None
    return d
