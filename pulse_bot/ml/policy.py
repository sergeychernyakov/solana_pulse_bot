# pulse_bot/ml/policy.py
"""ML inference policy: loads entry model, predicts proba on ScoringResult.

Deliberately minimal. No ABC, no pluggable framework — per codex review
2026-04-22: two policies (rules / ml), decided at pipeline start via
``PULSE_POLICY`` env var, no runtime swapping. Hard safety floor (TP/SL/
max_hold in ExitManager) remains immutable regardless of policy.

Shadow mode: even in ``rules`` mode the pipeline may instantiate this
policy and call ``predict_proba`` for parity logging. Actual live BUY
decision is still computed by Scorer/rules.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from pathlib import Path
from typing import Any, Mapping

import numpy as np
import xgboost as xgb

from pulse_bot.ml.features import (ENTRY_FEATURE_ORDER, EXIT_FEATURE_ORDER,
                                   EXIT_FEATURE_SCHEMA_VERSION,
                                   FEATURE_SCHEMA_VERSION,
                                   extract_entry_features,
                                   extract_entry_vector, extract_exit_vector)

logger = logging.getLogger(__name__)

DEFAULT_ENTRY_MODEL_PATH = Path("data/ml/entry_model.ubj")
DEFAULT_ENTRY_REG_MODEL_PATH = Path("data/ml/entry_model_reg.ubj")
DEFAULT_ENTRY_THRESHOLD = 0.5
DEFAULT_EXIT_MODEL_PATH = Path("data/ml/exit_model.ubj")
DEFAULT_EXIT_THRESHOLD = 0.5


def _resolve_entry_model_path() -> Path:
    """Pick entry model based on ``PULSE_ML_OBJECTIVE`` env var.

    ``classification`` (default) → entry_model.ubj (binary).
    ``regression``              → entry_model_reg.ubj (realized_pnl_pct).

    Codex Q4 #1 (2026-04-23): regression head uses realized-PnL magnitude
    instead of sign-only label — same dataset, richer gradient.
    """
    obj = os.environ.get("PULSE_ML_OBJECTIVE", "classification").lower()
    if obj == "regression":
        return DEFAULT_ENTRY_REG_MODEL_PATH
    return DEFAULT_ENTRY_MODEL_PATH


def sha256_file(path: Path, chunk_bytes: int = 65536) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(chunk_bytes), b""):
            h.update(chunk)
    return h.hexdigest()


def _first_mismatch(expected: list[str], got: list[str]) -> str:
    """Human-readable first point where two feature-order lists diverge."""
    for i, (e, g) in enumerate(zip(expected, got)):
        if e != g:
            return f"idx {i}: expected {e!r}, model has {g!r}"
    if len(expected) != len(got):
        return f"length mismatch ({len(expected)} vs {len(got)})"
    return "no per-index mismatch (ordering identical — check schema_version)"


class EntryMLPolicy:
    """Load entry model once, predict on demand.

    Usage:
        policy = EntryMLPolicy.from_path("data/ml/entry_model.ubj")
        # later, per scored token:
        proba = policy.predict_proba(scoring_result, holder_snapshot)
        action, calibrated = policy.decide_with_confidence(scoring_result, ...)
        feature_json = policy.dump_features_json(scoring_result, holder_snapshot)
    """

    def __init__(
        self,
        model: "xgb.XGBClassifier | xgb.XGBRegressor",
        model_hash: str,
        threshold: float = DEFAULT_ENTRY_THRESHOLD,
        schema_version: str = FEATURE_SCHEMA_VERSION,
        proba_floor: float | None = None,
        proba_ceiling: float | None = None,
        calibration: dict | None = None,
        objective: str = "binary:logistic",
    ) -> None:
        self._model = model
        self.model_hash = model_hash
        self.threshold = float(threshold)
        self.schema_version = schema_version
        # Codex Q4 #1: regression head uses predicted PnL% (not proba) as
        # the gated score. Same floor/ceiling contract — the UNIT of the
        # threshold just switches from [0,1] to PnL%. ``objective`` is
        # authoritative for that interpretation.
        self.objective = objective
        default_floor = self.threshold - 0.2 if objective == "binary:logistic" else 0.0
        default_ceiling = (
            self.threshold + 0.2 if objective == "binary:logistic" else 10.0
        )
        self.proba_floor = (
            float(proba_floor) if proba_floor is not None else default_floor
        )
        self.proba_ceiling = (
            float(proba_ceiling) if proba_ceiling is not None else default_ceiling
        )
        self.calibration = calibration or {"a": 1.0, "b": 0.0}

    @classmethod
    def from_path(
        cls,
        path: Path | str = DEFAULT_ENTRY_MODEL_PATH,
        threshold: float = DEFAULT_ENTRY_THRESHOLD,
    ) -> "EntryMLPolicy":
        p = Path(path)
        if not p.exists():
            raise FileNotFoundError(f"Entry model not found: {p}")
        meta_path = p.with_suffix(".meta.json")
        schema_version = FEATURE_SCHEMA_VERSION
        objective = "binary:logistic"
        if meta_path.exists():
            meta = json.loads(meta_path.read_text())
            meta_features = meta.get("features")
            if meta_features is not None and meta_features != ENTRY_FEATURE_ORDER:
                detail = (
                    f"Model {p} feature schema does not match current "
                    f"ENTRY_FEATURE_ORDER. "
                    f"Expected {len(ENTRY_FEATURE_ORDER)} features, model has "
                    f"{len(meta_features)}. "
                    f"First divergence: "
                    f"{_first_mismatch(ENTRY_FEATURE_ORDER, meta_features)}. "
                    f"Retrain with `python -m pulse_bot.ml.train` or set "
                    f"PULSE_ALLOW_STALE_MODEL=1 to force load (unsafe)."
                )
                if os.environ.get("PULSE_ALLOW_STALE_MODEL") == "1":
                    logger.error(
                        "Loading STALE-SCHEMA model (PULSE_ALLOW_STALE_MODEL=1): %s",
                        detail,
                    )
                else:
                    raise RuntimeError(detail)
            meta_schema = meta.get("schema_version")
            if meta_schema is not None and meta_schema != FEATURE_SCHEMA_VERSION:
                logger.warning(
                    "Model schema_version=%s differs from code "
                    "FEATURE_SCHEMA_VERSION=%s. Ordering was accepted but "
                    "the semantic version disagrees — consider retraining.",
                    meta_schema,
                    FEATURE_SCHEMA_VERSION,
                )
                schema_version = str(meta_schema)
            objective = str(meta.get("objective", "binary:logistic"))
        # Codex Q4 #1: load XGBRegressor for regression objective, else
        # XGBClassifier. File format is the same (.ubj), but the wrapper
        # class determines which predict* method is valid.
        if objective == "reg:squarederror":
            model: "xgb.XGBRegressor | xgb.XGBClassifier" = xgb.XGBRegressor()
        else:
            model = xgb.XGBClassifier()
        model.load_model(p)
        model_hash = sha256_file(p)
        proba_floor = None
        proba_ceiling = None
        calibration = None
        if meta_path.exists():
            meta = json.loads(meta_path.read_text())
            conf = meta.get("confidence_thresholds")
            if conf:
                proba_floor = conf.get("floor")
                proba_ceiling = conf.get("ceiling")
            calibration = meta.get("calibration")
        return cls(
            model,
            model_hash,
            threshold=threshold,
            schema_version=schema_version,
            proba_floor=proba_floor,
            proba_ceiling=proba_ceiling,
            calibration=calibration,
            objective=objective,
        )

    def predict_score(
        self,
        scoring_result: Any,
        holder_snapshot: Mapping[str, Any] | None = None,
        creator_snapshot: Any = None,
    ) -> float:
        """Raw model score.

        * classification → P(profitable) ∈ [0, 1].
        * regression     → predicted realized PnL %, typically in [-30, +50].

        Same skew-guards apply to both — a zero feature slice with a
        non-None snapshot is a bug in either objective.
        """
        vec = extract_entry_vector(scoring_result, holder_snapshot, creator_snapshot)
        # 2026-04-23 skew guard (codex-tightened): the original 20%
        # global threshold (≥10 non-zero of 51) would NOT have caught
        # the creator skew bug, because only 3-4 of 51 features were
        # affected. Fix: also verify *per-feature-group* coverage. A
        # legitimate non-None snapshot should produce at least one
        # non-zero feature in its group (creator / holder). An all-zero
        # group when its snapshot was passed means lookup broke.
        from pulse_bot.ml.features import (
            CREATOR_FEATURES,
            HELIUS_FEATURES,
            SCORER_FEATURES,
        )

        nz = sum(1 for v in vec if v != 0.0)
        if nz < max(5, int(0.4 * len(vec))):
            logger.warning(
                "predict_proba: only %d/%d features non-zero for %s "
                "(creator=%s holder=%s). Suspected train/serve skew — "
                "prediction may not reflect training data distribution.",
                nz,
                len(vec),
                type(scoring_result).__name__,
                type(creator_snapshot).__name__ if creator_snapshot else "None",
                type(holder_snapshot).__name__ if holder_snapshot else "None",
            )
        # Per-group coverage — catches the creator skew class directly:
        # if snapshot was provided (non-None) but every feature in that
        # group is still 0, a lookup path is broken.
        n_scorer = len(SCORER_FEATURES)
        n_helius = len(HELIUS_FEATURES)
        n_creator = len(CREATOR_FEATURES)
        # SCORER_FEATURES + DERIVED (2 hour_sin/cos) + HELIUS + CREATOR — slice indices
        helius_slice = vec[n_scorer + 2 : n_scorer + 2 + n_helius]
        creator_slice = vec[n_scorer + 2 + n_helius :]
        if holder_snapshot is not None and not any(v != 0.0 for v in helius_slice):
            logger.warning(
                "predict_proba: holder_snapshot provided but all %d "
                "HELIUS_FEATURES resolved to 0.0 — possible lookup regression",
                n_helius,
            )
        if creator_snapshot is not None and not any(v != 0.0 for v in creator_slice):
            logger.warning(
                "predict_proba: creator_snapshot provided but all %d "
                "CREATOR_FEATURES resolved to 0.0 — creator skew fingerprint",
                n_creator,
            )
        arr = np.asarray([vec], dtype=float)
        if self.objective == "reg:squarederror":
            # XGBRegressor.predict returns 1-D array of predicted targets
            return float(self._model.predict(arr)[0])
        # XGBClassifier.predict_proba returns [[P(0), P(1)]] → take P(1)
        return float(self._model.predict_proba(arr)[0, 1])

    def predict_proba(
        self,
        scoring_result: Any,
        holder_snapshot: Mapping[str, Any] | None = None,
        creator_snapshot: Any = None,
    ) -> float:
        """Back-compat alias. For classification returns P(profitable).

        For regression returns the predicted PnL% — callers that expected
        [0,1] must either check ``policy.objective`` or switch to
        ``predict_score``. Shadow-logging paths are the main affected
        callers — they log the raw score either way.
        """
        return self.predict_score(scoring_result, holder_snapshot, creator_snapshot)

    def decide(
        self,
        scoring_result: Any,
        holder_snapshot: Mapping[str, Any] | None = None,
        creator_snapshot: Any = None,
    ) -> tuple[bool, float]:
        """Return (should_buy, score). ``score`` is proba for classification,
        predicted PnL% for regression.

        The calling code keeps the rule-based hard rejects (sell pressure,
        curve progress, creator blacklist) as a safety layer *before*
        calling this — ML decides only among tokens that pass those."""
        s = self.predict_score(scoring_result, holder_snapshot, creator_snapshot)
        # For regression, threshold semantics differ: a PnL > 0 is the
        # natural "buy" signal. For classification, >= threshold (0.5
        # default) remains.
        if self.objective == "reg:squarederror":
            return (s > 0.0, s)
        return (s >= self.threshold, s)

    def decide_with_confidence(
        self,
        scoring_result: Any,
        holder_snapshot: Mapping[str, Any] | None = None,
        creator_snapshot: Any = None,
    ) -> tuple[str, float, float]:
        """Confidence-gated three-way decision.

        Returns ``(action, score_raw, score_calibrated)`` where ``action``
        is one of:
            * ``"BUY"``    — score >= ceiling (high-EV winner)
            * ``"SKIP"``   — score <  floor   (high-EV loser)
            * ``"RULES"``  — grey zone; caller defers to rules.

        Thresholds come from ``meta.json`` (val-tuned at train time). For
        classification, ``score_calibrated`` applies Platt scaling. For
        regression, calibration is a no-op (predicted PnL is already in
        physical units — no logit remap needed) and ``score_raw`` ==
        ``score_calibrated``.
        """
        s_raw = self.predict_score(scoring_result, holder_snapshot, creator_snapshot)
        s_cal = self._calibrate(s_raw)
        if s_raw >= self.proba_ceiling:
            action = "BUY"
        elif s_raw < self.proba_floor:
            action = "SKIP"
        else:
            action = "RULES"
        return action, s_raw, s_cal

    def _calibrate(self, raw: float) -> float:
        """Platt scaling for classification (sigmoid(a*p + b)). No-op for
        regression — PnL predictions are already in physical units.
        """
        if getattr(self, "objective", "binary:logistic") == "reg:squarederror":
            return float(raw)
        import math

        a = float(self.calibration.get("a", 1.0))
        b = float(self.calibration.get("b", 0.0))
        z = a * float(raw) + b
        if z > 50:
            return 1.0
        if z < -50:
            return 0.0
        return 1.0 / (1.0 + math.exp(-z))

    def dump_features_json(
        self,
        scoring_result: Any,
        holder_snapshot: Mapping[str, Any] | None = None,
        creator_snapshot: Any = None,
    ) -> str:
        feats = extract_entry_features(
            scoring_result, holder_snapshot, creator_snapshot
        )
        return json.dumps(feats, separators=(",", ":"))


class ExitMLPolicy:
    """Exit advisor. Predicts P(should sell now).

    Currently advisory-only: ExitManager attaches proba to ExitSignal
    for later analysis but does NOT override rule-based decisions. Hard
    floors (TP/SL/max_hold) stay immutable regardless of model output.
    """

    def __init__(
        self,
        model: xgb.XGBClassifier,
        model_hash: str,
        threshold: float = DEFAULT_EXIT_THRESHOLD,
        schema_version: str = EXIT_FEATURE_SCHEMA_VERSION,
    ) -> None:
        self._model = model
        self.model_hash = model_hash
        self.threshold = float(threshold)
        self.schema_version = schema_version

    @classmethod
    def from_path(
        cls,
        path: Path | str = DEFAULT_EXIT_MODEL_PATH,
        threshold: float = DEFAULT_EXIT_THRESHOLD,
    ) -> "ExitMLPolicy":
        p = Path(path)
        if not p.exists():
            raise FileNotFoundError(f"Exit model not found: {p}")
        meta_path = p.with_suffix(".meta.json")
        if meta_path.exists():
            meta = json.loads(meta_path.read_text())
            meta_features = meta.get("features")
            if meta_features is not None and meta_features != EXIT_FEATURE_ORDER:
                logger.warning(
                    "Exit model %s trained with different feature order. "
                    "Retrain before wiring into live decisions.",
                    p,
                )
        model = xgb.XGBClassifier()
        model.load_model(p)
        return cls(
            model,
            sha256_file(p),
            threshold=threshold,
            schema_version=EXIT_FEATURE_SCHEMA_VERSION,
        )

    def predict_proba(self, state: Any, pulse: Any = None) -> float:
        vec = extract_exit_vector(state, pulse)
        arr = np.asarray([vec], dtype=float)
        return float(self._model.predict_proba(arr)[0, 1])


def load_exit_policy_if_available(
    path: Path | str = DEFAULT_EXIT_MODEL_PATH,
    threshold: float = DEFAULT_EXIT_THRESHOLD,
) -> "ExitMLPolicy | None":
    p = Path(path)
    if not p.exists():
        logger.info("No exit model at %s — exit ML advisory disabled.", p)
        return None
    try:
        return ExitMLPolicy.from_path(p, threshold=threshold)
    except Exception as e:
        logger.exception("Failed to load exit model: %s", e)
        return None


def get_active_policy_name() -> str:
    """Read ``PULSE_POLICY`` env var. Default: rules (ML in shadow only)."""
    return os.environ.get("PULSE_POLICY", "rules").lower()


def load_entry_policy_if_available(
    path: Path | str | None = None,
    threshold: float = DEFAULT_ENTRY_THRESHOLD,
) -> EntryMLPolicy | None:
    """Try to load the entry model. Return None if missing — caller falls
    back to rules-only (no shadow logging). Prevents production crash on
    fresh clones that haven't run weekly_retrain yet.

    ``path`` defaults to ``_resolve_entry_model_path()`` which picks
    classification vs regression based on ``PULSE_ML_OBJECTIVE``.
    """
    p = Path(path) if path is not None else _resolve_entry_model_path()
    if not p.exists():
        logger.info("No entry model at %s — shadow logging disabled.", p)
        return None
    try:
        return EntryMLPolicy.from_path(p, threshold=threshold)
    except Exception as e:
        logger.exception("Failed to load entry model — shadow disabled: %s", e)
        return None
