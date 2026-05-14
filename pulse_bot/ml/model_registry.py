# pulse_bot/ml/model_registry.py
"""ModelRegistry — single source of truth for ML artifact paths +
health status (architecture phase E, codex review 2026-04-28).

Before:
    from pathlib import Path
    entry_path = Path("data/ml/entry_model.ubj")
    meta = json.loads(Path("data/ml/entry_model.meta.json").read_text())
    if meta.get("model_health", {}).get("status") != "ok": ...

Same code, but scattered across pipeline.py, policy.py, train.py,
shadow.py — every consumer reimplements path-resolution + health
parsing + status checks.

After:
    from pulse_bot.ml.model_registry import ModelRegistry
    registry = ModelRegistry()
    spec = registry.get("entry")
    if not spec.healthy: registry.warn(spec)
    model = xgb.Booster(); model.load_model(str(spec.path))

Why this matters:
* All status-aware decisions (refuse-ML-on-degenerate, AUC regression
  alerts, schema-version checks) gather in ONE place.
* Boot-time summary: every consumer can dump full state of the model
  ensemble in one call.
* Future extensions (rollback to .prev, A/B model selection,
  per-mint model preference) plug in here, not into 4 god-objects.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


# ─────────────────────────── skill thresholds ──────────────────────────
# A model below these bars has no demonstrated ability to separate
# winners from losers (or, for quantile heads, is not calibrated), so it
# must not influence live buy/sell decisions. These are code-health
# guards, NOT trading parameters — they gate whether a model is trusted
# at all, they do not tune entry/exit behaviour.
MIN_CLASSIFIER_AUC = 0.55
MIN_REG_SPEARMAN = 0.10
# auc_sign <= 0.50 means the regression head is worse than a coin flip at
# predicting whether realized PnL is positive — an unambiguous "no skill".
MIN_REG_AUC_SIGN = 0.50
QUANTILE_COVERAGE_TOLERANCE = 0.15
# Multiclass (entry_timing: WAIT_MORE / BUY_NOW / SKIP) — one-vs-rest AUC.
MIN_TIMING_AUC_OVR = 0.55
# Entry classifiers: advisory threshold on the most-confident proba
# bucket's expected value (``ceiling_ev`` — mean realized PnL%, fees +
# slippage applied). A non-positive value is a flag to INVESTIGATE, not
# proof of no skill: ``ceiling_ev`` is derived from ``simulate_exit`` run
# over training labels, which can be wrong (simulator bugs, train/serve
# skew, a stale exit config). The only ground truth for "does this model
# make money" is LIVE realized PnL. So the EV check is advisory — it
# warns loudly, it never disables a model on its own.
MIN_CEILING_EV = 0.0


def _ev_advisory(meta: dict[str, Any]) -> str | None:
    """Advisory check on a classifier's training-label EV.

    Returns a human-readable warning when the most-confident proba
    bucket's ``ceiling_ev`` is non-positive, else ``None``. This is NOT a
    skill verdict — see :data:`MIN_CEILING_EV` for why the metric is not
    trustworthy enough to gate on. Callers surface the warning; disabling
    a model on EV grounds requires corroboration from live realized PnL.
    """
    ct = meta.get("confidence_thresholds") or None
    if ct is None or ct.get("objective") != "ev" or "ceiling_ev" not in ct:
        return None
    ceiling_ev = float(ct.get("ceiling_ev") or 0.0)
    if ceiling_ev > MIN_CEILING_EV:
        return None
    base_ev = float(ct.get("val_base_ev") or 0.0)
    return (
        f"training-label EV non-positive (ceiling_ev={ceiling_ev:+.3f}%, "
        f"base {base_ev:+.3f}%) — ADVISORY ONLY; cross-check against live "
        f"realized PnL before trusting (may be a simulator artifact)"
    )


def _with_ev_advisory(
    skilled: bool, status: str, reason: str, meta: dict[str, Any]
) -> tuple[bool, str, str]:
    """Fold the EV advisory into a classifier verdict WITHOUT changing
    ``skilled``. A passing model whose training-label EV is non-positive
    gets status ``ev_warning`` (visible in logs / boot summary) but stays
    usable — the EV metric is advisory, never authoritative."""
    ev_warn = _ev_advisory(meta)
    if ev_warn is None:
        return skilled, status, reason
    reason = f"{reason}; EV advisory: {ev_warn}"
    if skilled and status == "ok":
        status = "ev_warning"
    return skilled, status, reason


def assess_skill(meta: dict[str, Any]) -> tuple[bool, str, str]:
    """Judge whether a model has enough demonstrated skill to influence
    live buy/sell decisions.

    Returns ``(skilled, status, reason)``:
        * ``skilled`` — True iff the model may influence decisions.
        * ``status``  — ``ok`` / ``ev_warning`` / ``degenerate`` /
          ``unmeasured``.
        * ``reason``  — human-readable explanation for logs.

    Precedence:
        1. An explicit ``model_health`` block (written by train.py for
           models that compute it) is authoritative for the skill
           verdict.
        2. Otherwise judge from whatever skill metric the meta carries,
           detected by model type: regression head (``auc_sign`` /
           ``objective``), quantile regressor (``coverage``), or plain
           classifier (``auc``).
        3. A model carrying no skill metric at all → ``unmeasured``.
           Left usable (``skilled=True``) so a working model is not
           abruptly disabled — but callers should log a warning, and
           train.py should be taught to emit a metric (task #96).

    EV advisory: for classifiers, a non-positive training-label
    ``ceiling_ev`` downgrades status to ``ev_warning`` but does NOT set
    ``skilled=False`` — the metric comes from ``simulate_exit`` over
    training labels and can be wrong; only live realized PnL is ground
    truth. See :func:`_ev_advisory`.
    """
    health = meta.get("model_health") or None
    if health is not None:
        status = health.get("status", "ok")
        skilled = status in ("ok", "ok_percentile_fallback")
        notes = health.get("notes") or []
        reason = "model_health.status=%s%s" % (
            status,
            (" — " + "; ".join(notes)) if notes else "",
        )
        return _with_ev_advisory(skilled, ("ok" if skilled else status), reason, meta)

    # Regression head — judged by rank correlation AND sign accuracy.
    if "auc_sign" in meta or meta.get("objective") == "reg:squarederror":
        rho = float(meta.get("spearman_rho") or 0.0)
        auc_sign = float(meta.get("auc_sign") or 0.0)
        skilled = auc_sign > MIN_REG_AUC_SIGN and rho >= MIN_REG_SPEARMAN
        reason = (
            f"regression head: spearman_rho={rho:+.3f} "
            f"(need >={MIN_REG_SPEARMAN}), auc_sign={auc_sign:.3f} "
            f"(need >{MIN_REG_AUC_SIGN})"
        )
        return skilled, ("ok" if skilled else "degenerate"), reason

    # Quantile regressor — judged by calibration (achieved coverage close
    # to the target quantile level).
    if "coverage" in meta:
        coverage = float(meta.get("coverage") or 0.0)
        quantile = float(meta.get("quantile") or 0.5)
        drift = abs(coverage - quantile)
        skilled = drift <= QUANTILE_COVERAGE_TOLERANCE
        reason = (
            f"quantile q={quantile}: coverage={coverage:.3f} "
            f"(drift {drift:.3f}, tol {QUANTILE_COVERAGE_TOLERANCE})"
        )
        return skilled, ("ok" if skilled else "degenerate"), reason

    # Multiclass classifier (entry_timing) — judged by one-vs-rest AUC.
    if "auc_ovr" in meta:
        auc_ovr = meta.get("auc_ovr")
        if auc_ovr is None:
            return False, "degenerate", "multiclass: auc_ovr could not be computed"
        auc_ovr = float(auc_ovr)
        skilled = auc_ovr >= MIN_TIMING_AUC_OVR
        reason = f"multiclass: auc_ovr={auc_ovr:.4f} (need >={MIN_TIMING_AUC_OVR})"
        return skilled, ("ok" if skilled else "degenerate"), reason

    # Survival hazard model — judged by the degeneracy sanity test and,
    # when available, the in-sample hazard discrimination AUC.
    if "sanity_status" in meta:
        sane = meta.get("sanity_status") == "ok"
        hazard_auc = meta.get("hazard_auc")
        if hazard_auc is not None:
            hazard_auc = float(hazard_auc)
            skilled = sane and hazard_auc >= MIN_CLASSIFIER_AUC
            reason = (
                f"survival: sanity={meta.get('sanity_status')}, "
                f"hazard_auc={hazard_auc:.4f} (need >={MIN_CLASSIFIER_AUC})"
            )
        else:
            skilled = sane
            reason = f"survival: sanity={meta.get('sanity_status')} (no hazard_auc)"
        return skilled, ("ok" if skilled else "degenerate"), reason

    # Plain classifier without a model_health block — judged by AUC.
    if "auc" in meta:
        auc = float(meta.get("auc") or 0.0)
        skilled = auc >= MIN_CLASSIFIER_AUC
        reason = f"classifier: auc={auc:.4f} (need >={MIN_CLASSIFIER_AUC})"
        return _with_ev_advisory(
            skilled, ("ok" if skilled else "degenerate"), reason, meta
        )

    # No skill metric at all.
    return True, "unmeasured", "no skill metric in meta — running ungated"


# Model name → on-disk artifact name (without extension).
# Single canonical mapping replaces scattered Path("data/ml/...") calls.
DEFAULT_NAMES: dict[str, str] = {
    "entry": "entry_model",
    "entry_t30": "entry_model_t30",
    "entry_reg": "entry_model_reg",
    "entry_timing": "entry_timing_model",
    "exit_quantile_sl": "exit_quantile_sl",
    "exit_quantile_tp": "exit_quantile_tp",
    "exit_quantile_max_hold": "exit_quantile_max_hold",
    "survival": "survival_model",
}


@dataclass
class ModelSpec:
    """Resolved info about one model artifact."""

    name: str  # logical name (e.g. "entry")
    path: Path  # .ubj file
    meta_path: Path  # .meta.json file
    exists: bool  # both files present
    schema_version: str | None = None
    auc: float | None = None
    p_at_top10: float | None = None
    spearman_rho: float | None = None
    auc_sign: float | None = None
    coverage: float | None = None
    quantile: float | None = None
    health: dict[str, Any] = field(default_factory=dict)
    raw_meta: dict[str, Any] = field(default_factory=dict)

    @property
    def healthy(self) -> bool:
        """True iff the model has demonstrated enough skill to influence
        live buy/sell decisions. See :func:`assess_skill` for the per-
        model-type rules. ``False`` for missing artifacts."""
        if not self.exists:
            return False
        return assess_skill(self.raw_meta)[0]

    @property
    def status(self) -> str:
        if not self.exists:
            return "missing"
        return assess_skill(self.raw_meta)[1]

    @property
    def skill_reason(self) -> str:
        """Human-readable explanation of the skill verdict."""
        if not self.exists:
            return "artifact missing"
        return assess_skill(self.raw_meta)[2]

    def summary(self) -> str:
        """One-line human-readable summary suitable for boot logs."""
        if not self.exists:
            return f"{self.name:<22s}  MISSING ({self.path})"
        bits = [f"{self.name:<22s}", f"schema={self.schema_version or '?'}"]
        if self.auc is not None:
            bits.append(f"AUC={self.auc:.4f}")
        if self.p_at_top10 is not None:
            bits.append(f"P@10={self.p_at_top10*100:.1f}%")
        if self.spearman_rho is not None:
            bits.append(f"rho={self.spearman_rho:+.3f}")
        if self.auc_sign is not None:
            bits.append(f"auc_sign={self.auc_sign:.3f}")
        status = self.status
        bits.append(f"status={status}")
        if status != "ok":
            bits.append(f"({self.skill_reason})")
        return "  ".join(bits)


class ModelRegistry:
    """Read-side lookup of model artifacts and their health.

    Args:
        data_dir: Where artifacts live. Defaults to ``data/ml/``.
        names: Override the logical→file mapping (mostly for tests).
    """

    def __init__(
        self,
        data_dir: str | Path = "data/ml",
        names: dict[str, str] | None = None,
    ) -> None:
        self._data_dir = Path(data_dir)
        self._names = dict(names) if names is not None else dict(DEFAULT_NAMES)

    def path_for(self, name: str) -> Path:
        if name not in self._names:
            raise KeyError(f"Unknown model name: {name}")
        return self._data_dir / f"{self._names[name]}.ubj"

    def meta_path_for(self, name: str) -> Path:
        if name not in self._names:
            raise KeyError(f"Unknown model name: {name}")
        return self._data_dir / f"{self._names[name]}.meta.json"

    def get(self, name: str) -> ModelSpec:
        """Resolve a model spec — files + meta + health flags."""
        path = self.path_for(name)
        meta_path = self.meta_path_for(name)
        exists = path.exists() and meta_path.exists()
        spec = ModelSpec(
            name=name,
            path=path,
            meta_path=meta_path,
            exists=exists,
        )
        if not exists:
            return spec
        try:
            meta = json.loads(meta_path.read_text())
        except Exception as exc:
            logger.warning("ModelRegistry: failed to parse %s: %s", meta_path, exc)
            return spec
        spec.raw_meta = meta
        spec.schema_version = meta.get("schema_version")
        if "auc" in meta:
            spec.auc = float(meta["auc"])
        if "precision_top10" in meta:
            spec.p_at_top10 = float(meta["precision_top10"])
        if "spearman_rho" in meta:
            spec.spearman_rho = float(meta["spearman_rho"])
        if "auc_sign" in meta:
            spec.auc_sign = float(meta["auc_sign"])
        if "coverage" in meta:
            spec.coverage = float(meta["coverage"])
        if "quantile" in meta:
            spec.quantile = float(meta["quantile"])
        spec.health = meta.get("model_health") or {}
        return spec

    def list_all(self) -> list[ModelSpec]:
        return [self.get(n) for n in self._names]

    def healthy_only(self) -> list[ModelSpec]:
        return [s for s in self.list_all() if s.healthy]

    def log_boot_summary(self) -> None:
        """Dump a one-screen view of all model statuses. Call at startup
        so operators see the full model ensemble in the bot.log."""
        logger.info("=" * 78)
        logger.info("MODEL REGISTRY — boot summary")
        for spec in self.list_all():
            logger.info("  " + spec.summary())
        logger.info("=" * 78)

    def warn_if_unhealthy(self, name: str) -> ModelSpec:
        """Resolve + log a WARNING if the model is not healthy. Useful
        for code that's about to load a model and wants the operator
        to see why ML override might refuse to act."""
        spec = self.get(name)
        if spec.exists and not spec.healthy:
            logger.warning(
                "MODEL %s status=%s — %s",
                name,
                spec.status,
                spec.skill_reason,
            )
        return spec
