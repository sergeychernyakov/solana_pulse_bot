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

    name: str            # logical name (e.g. "entry")
    path: Path           # .ubj file
    meta_path: Path      # .meta.json file
    exists: bool         # both files present
    schema_version: str | None = None
    auc: float | None = None
    p_at_top10: float | None = None
    spearman_rho: float | None = None
    health: dict[str, Any] = field(default_factory=dict)
    raw_meta: dict[str, Any] = field(default_factory=dict)

    @property
    def healthy(self) -> bool:
        """True iff the model has model_health.status == 'ok' (or no
        health field at all — legacy artifacts trained before health
        gate). False when degenerate / narrow_proba_spread /
        auc_regression / anti_correlated."""
        if not self.exists:
            return False
        status = self.health.get("status")
        if status is None:
            return True  # legacy — no gate ever ran, assume usable
        return status == "ok"

    @property
    def status(self) -> str:
        if not self.exists:
            return "missing"
        return self.health.get("status") or "ok"

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
        bits.append(f"status={self.status}")
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
            notes = spec.health.get("notes") or []
            logger.warning(
                "MODEL %s status=%s — %s",
                name, spec.status,
                "; ".join(notes) if notes else "(no notes)",
            )
        return spec
