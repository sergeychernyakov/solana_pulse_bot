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

from pulse_bot.ml.features import (
    ENTRY_FEATURE_ORDER,
    ENTRY_T30_FEATURE_ORDER,
    EXIT_FEATURE_ORDER,
    EXIT_FEATURE_SCHEMA_VERSION,
    FEATURE_SCHEMA_VERSION,
    FEATURE_SCHEMA_VERSION_T30,
    extract_entry_features,
    extract_entry_features_t30,
    extract_entry_vector,
    extract_entry_vector_t30,
    extract_exit_vector,
)

logger = logging.getLogger(__name__)

DEFAULT_ENTRY_MODEL_PATH = Path("data/ml/entry_model.ubj")
DEFAULT_ENTRY_REG_MODEL_PATH = Path("data/ml/entry_model_reg.ubj")
DEFAULT_ENTRY_T30_MODEL_PATH = Path("data/ml/entry_model_t30.ubj")
DEFAULT_ENTRY_THRESHOLD = 0.5
DEFAULT_EXIT_MODEL_PATH = Path("data/ml/exit_model.ubj")
DEFAULT_EXIT_THRESHOLD = 0.5

# Phase 3 defaults — early-decision gates from the roadmap. proba >= 0.75
# fires BUY at T+30; proba < 0.15 fires SKIP at T+30; otherwise the bot
# defers to the T+90 main model.
DEFAULT_ENTRY_T30_BUY_CEILING: float = 0.75
DEFAULT_ENTRY_T30_SKIP_FLOOR: float = 0.15


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


def _check_config_drift(meta_path: Path, runtime_config: Any) -> None:
    """Compare meta-recorded training config to runtime config and WARN.

    Reads ``config_hash`` + ``config_values`` from ``meta.json``. If the
    hash matches, no-op (silent). If it differs, computes a per-field
    diff and logs WARNING listing each changed field with both values.

    Older models without a recorded hash (pre-2026-04-25 training runs)
    are tolerated silently — the WARN would fire on every startup until
    the next retrain, which is just noise.

    Never raises: drift detection must not block model loading. ``policy``
    refuses to load only on schema/feature-order mismatch (still does).
    """
    try:
        if not meta_path.exists():
            return
        meta = json.loads(meta_path.read_text())
        meta_hash = meta.get("config_hash")
        if not meta_hash:
            # Legacy model — silently skip. Retraining will populate it.
            return
        from pulse_bot.ml.config_hash import (
            compute_config_hash,
            diff_relevant_fields_from_dict,
        )

        runtime_hash = compute_config_hash(runtime_config)
        if runtime_hash == meta_hash:
            return
        meta_values = meta.get("config_values") or {}
        diff = diff_relevant_fields_from_dict(meta_values, runtime_config)
        diff_lines = [
            f"    {field}: training={train_v!r} runtime={run_v!r}"
            for field, (train_v, run_v) in sorted(diff.items())
        ]
        logger.warning(
            "Runtime PulseBotConfig differs from training-time config "
            "(meta_hash=%s runtime_hash=%s, %d field(s) drifted). Model "
            "still loaded — operator should retrain or revert config to "
            "match labels distribution.\n%s",
            meta_hash[:12],
            runtime_hash[:12],
            len(diff),
            "\n".join(diff_lines) if diff_lines else "    (no per-field diff)",
        )
    except Exception:
        logger.debug("config_hash drift check skipped", exc_info=True)


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
        # 2026-04-24: config can override meta.json thresholds at startup
        # (optimizer uses this to sweep proba gating without retraining).
        # Precedence: config override > meta.json > hardcoded default.
        try:
            from pulse_bot.config import get_config

            cfg = get_config()
            # config_hash drift check (2026-04-25): compare meta-recorded
            # training-time config against the runtime config. Any drift
            # in label-affecting / hparam-affecting fields is logged as
            # WARNING. We deliberately do NOT refuse to load — operators
            # may flip cosmetic-but-tracked fields (e.g. exit_ml_active
            # for a kill-switch) without retraining; the warning gives
            # visibility, the operator decides.
            _check_config_drift(meta_path, cfg)
            if cfg.entry_ml_proba_floor is not None:
                logger.info(
                    "EntryMLPolicy: config overrides proba_floor %.3f → %.3f",
                    proba_floor if proba_floor is not None else float("nan"),
                    cfg.entry_ml_proba_floor,
                )
                proba_floor = cfg.entry_ml_proba_floor
            if cfg.entry_ml_proba_ceiling is not None:
                logger.info(
                    "EntryMLPolicy: config overrides proba_ceiling %.3f → %.3f",
                    proba_ceiling if proba_ceiling is not None else float("nan"),
                    cfg.entry_ml_proba_ceiling,
                )
                proba_ceiling = cfg.entry_ml_proba_ceiling
        except Exception as exc:
            logger.debug("EntryMLPolicy config override skipped: %s", exc)
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
        *,
        wallet_prior_stats: Mapping[str, Mapping[str, Any]] | None = None,
        top3_buyer_wallets: list[str] | None = None,
        cutoff_ts: float | None = None,
    ) -> float:
        """Raw model score.

        * classification → P(profitable) ∈ [0, 1].
        * regression     → predicted realized PnL %, typically in [-30, +50].

        Same skew-guards apply to both — a zero feature slice with a
        non-None snapshot is a bug in either objective.

        Phase E: ``wallet_prior_stats`` + ``top3_buyer_wallets`` + ``cutoff_ts``
        feed top-3-buyer features. Callers pre-query via
        ``Database.get_wallet_prior_stats_sync``.
        """
        vec = extract_entry_vector(
            scoring_result,
            holder_snapshot,
            creator_snapshot,
            wallet_prior_stats=wallet_prior_stats,
            top3_buyer_wallets=top3_buyer_wallets,
            cutoff_ts=cutoff_ts,
        )
        # 2026-04-23 skew guard (codex-tightened): per-feature-group
        # coverage. A legitimate non-None snapshot should produce at least
        # one non-zero feature in its group (creator / holder). An all-zero
        # group when its snapshot was passed means the lookup broke.
        #
        # 2026-04-24 (Phase E): rebuilt by NAME lookup using
        # ENTRY_FEATURE_ORDER, no longer by positional slicing. Adding
        # DERIVED/WALLET_FEATURES was silently mis-slicing the old code;
        # by-name is immune to group re-ordering.
        from pulse_bot.ml.features import (
            CREATOR_FEATURES,
            ENTRY_FEATURE_ORDER,
            HELIUS_FEATURES,
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

        def _group_values(group_names: list[str]) -> list[float]:
            idx = [ENTRY_FEATURE_ORDER.index(n) for n in group_names]
            return [vec[i] for i in idx]

        if holder_snapshot is not None and not any(
            v != 0.0 for v in _group_values(HELIUS_FEATURES)
        ):
            logger.warning(
                "predict_proba: holder_snapshot provided but all %d "
                "HELIUS_FEATURES resolved to 0.0 — possible lookup regression",
                len(HELIUS_FEATURES),
            )
        if creator_snapshot is not None and not any(
            v != 0.0 for v in _group_values(CREATOR_FEATURES)
        ):
            logger.warning(
                "predict_proba: creator_snapshot provided but all %d "
                "CREATOR_FEATURES resolved to 0.0 — creator skew fingerprint",
                len(CREATOR_FEATURES),
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
        *,
        wallet_prior_stats: Mapping[str, Mapping[str, Any]] | None = None,
        top3_buyer_wallets: list[str] | None = None,
        cutoff_ts: float | None = None,
    ) -> float:
        """Back-compat alias. For classification returns P(profitable).

        For regression returns the predicted PnL% — callers that expected
        [0,1] must either check ``policy.objective`` or switch to
        ``predict_score``. Shadow-logging paths are the main affected
        callers — they log the raw score either way.
        """
        return self.predict_score(
            scoring_result,
            holder_snapshot,
            creator_snapshot,
            wallet_prior_stats=wallet_prior_stats,
            top3_buyer_wallets=top3_buyer_wallets,
            cutoff_ts=cutoff_ts,
        )

    def decide(
        self,
        scoring_result: Any,
        holder_snapshot: Mapping[str, Any] | None = None,
        creator_snapshot: Any = None,
        *,
        wallet_prior_stats: Mapping[str, Mapping[str, Any]] | None = None,
        top3_buyer_wallets: list[str] | None = None,
        cutoff_ts: float | None = None,
    ) -> tuple[bool, float]:
        """Return (should_buy, score). ``score`` is proba for classification,
        predicted PnL% for regression.

        The calling code keeps the rule-based hard rejects (sell pressure,
        curve progress, creator blacklist) as a safety layer *before*
        calling this — ML decides only among tokens that pass those."""
        s = self.predict_score(
            scoring_result,
            holder_snapshot,
            creator_snapshot,
            wallet_prior_stats=wallet_prior_stats,
            top3_buyer_wallets=top3_buyer_wallets,
            cutoff_ts=cutoff_ts,
        )
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
        *,
        wallet_prior_stats: Mapping[str, Mapping[str, Any]] | None = None,
        top3_buyer_wallets: list[str] | None = None,
        cutoff_ts: float | None = None,
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
        s_raw = self.predict_score(
            scoring_result,
            holder_snapshot,
            creator_snapshot,
            wallet_prior_stats=wallet_prior_stats,
            top3_buyer_wallets=top3_buyer_wallets,
            cutoff_ts=cutoff_ts,
        )
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
        *,
        wallet_prior_stats: Mapping[str, Mapping[str, Any]] | None = None,
        top3_buyer_wallets: list[str] | None = None,
        cutoff_ts: float | None = None,
    ) -> str:
        feats = extract_entry_features(
            scoring_result,
            holder_snapshot,
            creator_snapshot,
            wallet_prior_stats=wallet_prior_stats,
            top3_buyer_wallets=top3_buyer_wallets,
            cutoff_ts=cutoff_ts,
        )
        return json.dumps(feats, separators=(",", ":"))


class EntryT30Policy:
    """Phase 3 — @T+30 dual-snapshot entry advisor.

    Loads ``entry_model_t30.ubj`` and applies the early-decision gate
    described in the roadmap:

        proba >= ``buy_ceiling`` (0.75 default) → BUY immediately
        proba <  ``skip_floor``  (0.15 default) → SKIP immediately
        otherwise                                → DEFER to T+90 main model

    Distinct from :class:`EntryMLPolicy` so the two models can coexist
    with independent schemas, calibration, and gating thresholds.
    Pipeline integration is OUT OF SCOPE for this module — caller is the
    Phase 3 deployment task.
    """

    def __init__(
        self,
        model: "xgb.XGBClassifier",
        model_hash: str,
        schema_version: str = FEATURE_SCHEMA_VERSION_T30,
        buy_ceiling: float = DEFAULT_ENTRY_T30_BUY_CEILING,
        skip_floor: float = DEFAULT_ENTRY_T30_SKIP_FLOOR,
        calibration: dict | None = None,
    ) -> None:
        self._model = model
        self.model_hash = model_hash
        self.schema_version = schema_version
        self.buy_ceiling = float(buy_ceiling)
        self.skip_floor = float(skip_floor)
        if self.buy_ceiling <= self.skip_floor:
            raise ValueError(
                f"EntryT30Policy: buy_ceiling ({self.buy_ceiling}) must be "
                f"strictly greater than skip_floor ({self.skip_floor}). "
                "Otherwise the DEFER bucket is empty by construction."
            )
        self.calibration = calibration or {"a": 1.0, "b": 0.0}

    @classmethod
    def from_path(
        cls,
        path: Path | str = DEFAULT_ENTRY_T30_MODEL_PATH,
        buy_ceiling: float = DEFAULT_ENTRY_T30_BUY_CEILING,
        skip_floor: float = DEFAULT_ENTRY_T30_SKIP_FLOOR,
    ) -> "EntryT30Policy":
        p = Path(path)
        if not p.exists():
            raise FileNotFoundError(f"Entry T30 model not found: {p}")
        meta_path = p.with_suffix(".meta.json")
        schema_version = FEATURE_SCHEMA_VERSION_T30
        calibration = None
        if meta_path.exists():
            meta = json.loads(meta_path.read_text())
            meta_features = meta.get("features")
            if meta_features is not None and meta_features != ENTRY_T30_FEATURE_ORDER:
                detail = (
                    f"Model {p} feature schema does not match current "
                    f"ENTRY_T30_FEATURE_ORDER. "
                    f"Expected {len(ENTRY_T30_FEATURE_ORDER)} features, "
                    f"model has {len(meta_features)}. "
                    f"First divergence: "
                    f"{_first_mismatch(ENTRY_T30_FEATURE_ORDER, meta_features)}. "
                    f"Retrain with `python -m pulse_bot.ml.train --dataset entry_t30`."
                )
                if os.environ.get("PULSE_ALLOW_STALE_MODEL") == "1":
                    logger.error(
                        "Loading STALE-SCHEMA T30 model "
                        "(PULSE_ALLOW_STALE_MODEL=1): %s",
                        detail,
                    )
                else:
                    raise RuntimeError(detail)
            meta_schema = meta.get("schema_version")
            if meta_schema is not None and meta_schema != FEATURE_SCHEMA_VERSION_T30:
                logger.warning(
                    "T30 model schema_version=%s differs from code "
                    "FEATURE_SCHEMA_VERSION_T30=%s — consider retraining.",
                    meta_schema,
                    FEATURE_SCHEMA_VERSION_T30,
                )
                schema_version = str(meta_schema)
            calibration = meta.get("calibration")
            _check_config_drift(meta_path, _safe_get_runtime_config())
        model = xgb.XGBClassifier()
        model.load_model(p)
        return cls(
            model,
            sha256_file(p),
            schema_version=schema_version,
            buy_ceiling=buy_ceiling,
            skip_floor=skip_floor,
            calibration=calibration,
        )

    def predict_proba(
        self,
        scoring_result_partial: Any,
        holder_snapshot_t30: Mapping[str, Any] | None = None,
        creator_snapshot: Any = None,
        *,
        wallet_prior_stats: Mapping[str, Mapping[str, Any]] | None = None,
        top3_buyer_wallets: list[str] | None = None,
        cutoff_ts: float | None = None,
    ) -> float:
        """Return P(profitable | observed by T+30) ∈ [0, 1]."""
        vec = extract_entry_vector_t30(
            scoring_result_partial,
            holder_snapshot_t30,
            creator_snapshot,
            wallet_prior_stats=wallet_prior_stats,
            top3_buyer_wallets=top3_buyer_wallets,
            cutoff_ts=cutoff_ts,
        )
        arr = np.asarray([vec], dtype=float)
        return float(self._model.predict_proba(arr)[0, 1])

    def decide_with_confidence(
        self,
        scoring_result_partial: Any,
        holder_snapshot_t30: Mapping[str, Any] | None = None,
        creator_snapshot: Any = None,
        *,
        wallet_prior_stats: Mapping[str, Mapping[str, Any]] | None = None,
        top3_buyer_wallets: list[str] | None = None,
        cutoff_ts: float | None = None,
    ) -> tuple[str, float]:
        """3-way @T+30 decision.

        Returns ``(action, proba)`` where ``action`` is one of:
            * ``"BUY"``    — proba >= buy_ceiling (clear winner, enter early)
            * ``"SKIP"``   — proba <  skip_floor  (clear loser, free slot)
            * ``"DEFER"``  — grey zone; caller waits for T+90 main model.
        """
        p = self.predict_proba(
            scoring_result_partial,
            holder_snapshot_t30,
            creator_snapshot,
            wallet_prior_stats=wallet_prior_stats,
            top3_buyer_wallets=top3_buyer_wallets,
            cutoff_ts=cutoff_ts,
        )
        if p >= self.buy_ceiling:
            return ("BUY", p)
        if p < self.skip_floor:
            return ("SKIP", p)
        return ("DEFER", p)

    def dump_features_json(
        self,
        scoring_result_partial: Any,
        holder_snapshot_t30: Mapping[str, Any] | None = None,
        creator_snapshot: Any = None,
        *,
        wallet_prior_stats: Mapping[str, Mapping[str, Any]] | None = None,
        top3_buyer_wallets: list[str] | None = None,
        cutoff_ts: float | None = None,
    ) -> str:
        feats = extract_entry_features_t30(
            scoring_result_partial,
            holder_snapshot_t30,
            creator_snapshot,
            wallet_prior_stats=wallet_prior_stats,
            top3_buyer_wallets=top3_buyer_wallets,
            cutoff_ts=cutoff_ts,
        )
        return json.dumps(feats, separators=(",", ":"))


def _safe_get_runtime_config() -> Any:
    """Wrapper used by ``_check_config_drift`` from new policy classes.

    Centralises the try/except so a missing ``pulse_bot.config`` import
    (e.g. in narrow unit-test contexts) does not nuke ``from_path``.
    """
    try:
        from pulse_bot.config import get_config

        return get_config()
    except Exception:  # noqa: BLE001 — defensive boundary
        return None


def load_entry_t30_policy_if_available(
    path: Path | str = DEFAULT_ENTRY_T30_MODEL_PATH,
    buy_ceiling: float = DEFAULT_ENTRY_T30_BUY_CEILING,
    skip_floor: float = DEFAULT_ENTRY_T30_SKIP_FLOOR,
) -> "EntryT30Policy | None":
    """Try to load the @T+30 entry model. Return None if missing."""
    p = Path(path)
    if not p.exists():
        logger.info("No entry T30 model at %s — T+30 advisory disabled.", p)
        return None
    try:
        return EntryT30Policy.from_path(
            p, buy_ceiling=buy_ceiling, skip_floor=skip_floor
        )
    except Exception as e:
        logger.exception("Failed to load entry T30 model: %s", e)
        return None


class ExitMLPolicy:
    """Exit advisor. Predicts P(should sell now) ∈ [0, 1].

    Live integration (2026-04-23 v3):
    * ``predict_proba(state, pulse)`` — single scalar ∈ [0, 1].
    * ``decide_with_confidence(...)`` — 4-way gated decision
      (SELL_ALL / SELL_PARTIAL / RULES / HOLD_HARD).

    Safety: HOLD_HARD can ONLY block ``weak_pulse_profit`` and
    ``take_profit`` (the latter under strict conditions); all other
    hard rules (creator_dump, hard_stop, timeout, trailing_stop,
    whale, near_graduation) stay immutable.
    """

    # Phase E2 gate defaults. HOLD_HARD threshold is HARDCODED per codex —
    # val-tuning it on ~100 positives would double-dip the same val set
    # already used for SELL_CEILING selection.
    DEFAULT_SELL_CEILING = 0.80
    DEFAULT_PARTIAL_FLOOR = 0.55
    HOLD_HARD_THRESHOLD = 0.20  # hardcoded — codex E2
    HOLD_HARD_MIN_PNL_PCT = -5.0  # guardrail — don't hold through dips
    # TP-loosen guardrails (2026-04-23, user directive): HOLD_HARD can
    # block take_profit ONLY when model is very-confident hold (proba <
    # TP_HOLD_HARD_STRICT_THRESHOLD) AND position is still within safe
    # runway (peak PnL not yet exhausted, current PnL not in moonshot
    # territory where regression-to-mean is overwhelming).
    TP_HOLD_HARD_STRICT_THRESHOLD = 0.15
    TP_HOLD_HARD_MAX_PEAK_PCT = 300.0
    TP_HOLD_HARD_MAX_CURRENT_PCT = 500.0
    HOLD_HARD_BLOCKABLE_REASONS = frozenset({"weak_pulse_profit", "take_profit"})

    def __init__(
        self,
        model: xgb.XGBClassifier,
        model_hash: str,
        threshold: float = DEFAULT_EXIT_THRESHOLD,
        schema_version: str = EXIT_FEATURE_SCHEMA_VERSION,
        sell_ceiling: float | None = None,
        partial_floor: float | None = None,
        entry_model_hash: str | None = None,
    ) -> None:
        self._model = model
        self.model_hash = model_hash
        self.threshold = float(threshold)
        self.schema_version = schema_version
        self.sell_ceiling = (
            float(sell_ceiling)
            if sell_ceiling is not None
            else self.DEFAULT_SELL_CEILING
        )
        self.partial_floor = (
            float(partial_floor)
            if partial_floor is not None
            else self.DEFAULT_PARTIAL_FLOOR
        )
        self.entry_model_hash = entry_model_hash

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
        sell_ceiling = None
        partial_floor = None
        entry_model_hash: str | None = None
        if meta_path.exists():
            meta = json.loads(meta_path.read_text())
            meta_features = meta.get("features")
            if meta_features is not None and meta_features != EXIT_FEATURE_ORDER:
                detail = (
                    f"Exit model {p} schema does not match current "
                    f"EXIT_FEATURE_ORDER. First divergence: "
                    f"{_first_mismatch(EXIT_FEATURE_ORDER, meta_features)}. "
                    f"Retrain with `python -m pulse_bot.ml.train --dataset exit` "
                    f"or set PULSE_ALLOW_STALE_MODEL=1 to force load (unsafe)."
                )
                if os.environ.get("PULSE_ALLOW_STALE_MODEL") == "1":
                    logger.error(
                        "Loading STALE-SCHEMA exit model "
                        "(PULSE_ALLOW_STALE_MODEL=1): %s",
                        detail,
                    )
                else:
                    raise RuntimeError(detail)
            # TODO(cross-model gate): removed in exit_v3 alongside the
            # entry_ml_proba feature. When the feature is restored,
            # reinstate the hash gate here so a retrained entry doesn't
            # silently shift the exit feature distribution.
            entry_model_hash = meta.get("entry_model_hash")
            # Val-tuned ceiling/floor if persisted
            sell_ceiling = meta.get("sell_ceiling")
            partial_floor = meta.get("partial_floor")
        model = xgb.XGBClassifier()
        model.load_model(p)
        return cls(
            model,
            sha256_file(p),
            threshold=threshold,
            schema_version=EXIT_FEATURE_SCHEMA_VERSION,
            sell_ceiling=sell_ceiling,
            partial_floor=partial_floor,
            entry_model_hash=entry_model_hash,
        )

    def predict_proba(self, state: Any, pulse: Any = None) -> float:
        vec = extract_exit_vector(state, pulse)
        arr = np.asarray([vec], dtype=float)
        return float(self._model.predict_proba(arr)[0, 1])

    def decide_with_confidence(
        self,
        state: Any,
        pulse: Any = None,
        current_pnl_pct: float | None = None,
    ) -> tuple[str, float]:
        """4-way confidence-gated exit decision.

        Returns ``(action, proba)`` where ``action`` is one of:
            * ``"SELL_ALL"``     — force full exit (proba >= sell_ceiling)
            * ``"SELL_PARTIAL"`` — force partial exit (partial_floor <= proba < sell_ceiling)
            * ``"RULES"``        — defer to rule-based logic (grey zone)
            * ``"HOLD_HARD"``    — block ``weak_pulse_profit`` partial only
                                   (proba < HOLD_HARD_THRESHOLD AND
                                   current_pnl_pct >= HOLD_HARD_MIN_PNL_PCT).
                                   Never blocks hard rules (see caller).

        ``HOLD_HARD`` fires only when the position is still salvageable
        (PnL above ``HOLD_HARD_MIN_PNL_PCT``). If PnL is worse than that,
        hard rules alone decide.
        """
        p = self.predict_proba(state, pulse)
        if p >= self.sell_ceiling:
            return ("SELL_ALL", p)
        if p >= self.partial_floor:
            return ("SELL_PARTIAL", p)
        if p < self.HOLD_HARD_THRESHOLD and (
            current_pnl_pct is None or current_pnl_pct >= self.HOLD_HARD_MIN_PNL_PCT
        ):
            return ("HOLD_HARD", p)
        return ("RULES", p)


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


class ExitQuantilePolicy:
    """XGBoost quantile regressor for forward-PnL SL tightening (E3).

    Loaded separately from the binary ExitMLPolicy. ``predict`` returns
    the predicted quantile of forward 60s PnL %. Used by ExitManager
    to preempt hard_stop when the model is directionally confident.

    TP loosening head (q=0.75) is NOT wired into live at the moment:
    at N=1686 the test-set spearman is slightly negative, i.e. the
    prediction is anti-correlated with realized return. Re-evaluate
    after N_exit ≥ 3000.
    """

    def __init__(
        self,
        model: "xgb.XGBRegressor",
        model_hash: str,
        quantile: float,
        spearman_rho: float,
        coverage: float,
    ) -> None:
        self._model = model
        self.model_hash = model_hash
        self.quantile = quantile
        self.spearman_rho = spearman_rho
        self.coverage = coverage

    @classmethod
    def from_path(cls, path: Path | str) -> "ExitQuantilePolicy":
        p = Path(path)
        if not p.exists():
            raise FileNotFoundError(f"Quantile model not found: {p}")
        meta = json.loads(p.with_suffix(".meta.json").read_text())
        model = xgb.XGBRegressor()
        model.load_model(p)
        return cls(
            model,
            sha256_file(p),
            quantile=float(meta.get("quantile", 0.5)),
            spearman_rho=float(meta.get("spearman_rho", 0.0)),
            coverage=float(meta.get("coverage", 0.0)),
        )

    def predict(self, state: Any, pulse: Any = None) -> float:
        """Predicted forward-60s PnL at the requested quantile."""
        vec = extract_exit_vector(state, pulse)
        arr = np.asarray([vec], dtype=float)
        return float(self._model.predict(arr)[0])


def load_exit_quantile_if_available(
    path: Path | str,
) -> "ExitQuantilePolicy | None":
    """Return None when the quantile model is missing or meta is bad."""
    p = Path(path)
    if not p.exists():
        logger.info("No quantile model at %s — dynamic SL override disabled.", p)
        return None
    try:
        return ExitQuantilePolicy.from_path(p)
    except Exception as e:
        logger.exception("Failed to load quantile model %s: %s", p, e)
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
