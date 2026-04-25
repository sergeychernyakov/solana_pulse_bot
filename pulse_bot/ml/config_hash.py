# pulse_bot/ml/config_hash.py
"""Stable SHA-256 hash of training-relevant ``PulseBotConfig`` fields.

Goal: protect deployed models from silent config drift. When ``train.py``
saves ``entry_model.meta.json`` it stores ``config_hash`` of the active
config. At load time ``policy.py`` recomputes the hash on the *runtime*
config and emits a WARNING (not a refuse-to-load) if they differ — the
exact failure mode behind the 2026-04-22 Option-B labels-vs-config
regression where exit defaults moved but training labels still reflected
the old SL/TP/max_hold combo.

Only fields that **affect labels, features, or hyperparameters** count.
Cosmetic fields (``dashboard_refresh_seconds``, ``log_level``,
``db_path``, ``backtest_db_path``, ``optimizer_db_path``, ``ws_url``)
are intentionally excluded — flipping them must not invalidate a model.

Hash is canonicalised: keys sorted, JSON serialised with separators
fixed, no whitespace, no NaN/Inf. Field list itself is also versioned —
the field set is appended to the canonical payload so adding a new
relevant field changes the hash even at default values.
"""

from __future__ import annotations

import hashlib
import json
import logging
from typing import Any, Iterable

logger = logging.getLogger(__name__)


# Fields that influence training labels, features, or model hyperparameters.
# Order is irrelevant (we sort), but the *set* must be deterministic across
# train/serve. Adding a field here is a deliberate act — it changes the
# hash of every model trained afterwards.
TRAIN_RELEVANT_FIELDS: tuple[str, ...] = (
    # Score thresholds — feed scorer outputs that define BUY/SKIP labels
    # in any rules-shadow comparison and gate label inclusion at build.
    "score_threshold_buy",
    "score_threshold_borderline",
    "fast_score_threshold",
    # Exit rule defaults — the simulate_exit label generator reads these
    # directly. Any drift here silently invalidates labels (Option-B bug).
    "exit_min_hold_seconds",
    "exit_on_creator_dump",
    "exit_on_whale",
    "exit_sell_pressure_ratio",
    "exit_peak_buy_rate_drop_ratio",
    "exit_peak_buy_rate_floor",
    "exit_no_new_wallets_events",
    "exit_near_graduation_pct",
    "exit_hard_stop_loss_pct",
    "exit_max_hold_seconds",
    "exit_inactivity_seconds",
    "exit_trend_dying_count",
    "exit_take_profit_pct",
    "exit_take_profit_enabled",
    "exit_trailing_stop_enabled",
    "exit_trailing_stop_activation_pct",
    "exit_trailing_stop_distance_pct",
    "exit_partial_on_profit_pct",
    "exit_profit_threshold_pct",
    "exit_partial_on_weak_pulse_pct",
    "exit_weak_pulse_min_profit_pct",
    "exit_moonbag_pct",
    # Exit ML activation — affects which trades close via ML vs rules in
    # downstream simulate_exit and live behavior comparisons.
    "exit_ml_active",
    "exit_ml_sell_threshold",
    "exit_ml_partial_floor",
    "exit_ml_min_hold_seconds",
    "exit_ml_hold_hard_enabled",
    "exit_regression_active",
    # Entry ML gating + sizing — proba floor/ceiling drive BUY/SKIP/RULES
    # split that determines effective training distribution.
    "entry_ml_proba_floor",
    "entry_ml_proba_ceiling",
    "ml_sizing_proba_1",
    "ml_sizing_frac_1",
    "ml_sizing_proba_2",
    "ml_sizing_frac_2",
    "ml_sizing_proba_3",
    "ml_sizing_frac_3",
    # Entry training hyperparameters — directly bake into the saved model.
    "entry_train_n_estimators",
    "entry_train_max_depth",
    "entry_train_learning_rate",
    "entry_train_min_child_weight",
    "entry_train_subsample",
    "entry_train_colsample_bytree",
)


def _coerce(value: Any) -> Any:
    """Make a value JSON-canonicalisable.

    NaN/Inf floats are forbidden (json.dumps with allow_nan=False raises);
    enums/dataclasses are stringified.
    """
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        # json.dumps(allow_nan=False) handles non-finite already; this is
        # purely defensive — a NaN config field is a bug we want to crash on.
        return value
    return str(value)


def extract_relevant_fields(
    config: Any,
    fields: Iterable[str] = TRAIN_RELEVANT_FIELDS,
) -> dict[str, Any]:
    """Pull the train-relevant subset of fields off any object exposing them.

    Missing fields are recorded as ``"__MISSING__"`` so the hash is stable
    across config-class refactors — the field disappearing is itself a
    detectable diff rather than silently dropping out.
    """
    out: dict[str, Any] = {}
    for name in fields:
        if hasattr(config, name):
            out[name] = _coerce(getattr(config, name))
        else:
            out[name] = "__MISSING__"
    return out


def canonical_payload(config: Any) -> str:
    """Deterministic string form of ``extract_relevant_fields``."""
    payload = {
        "fields_version": 1,  # bump if TRAIN_RELEVANT_FIELDS semantics change
        "values": extract_relevant_fields(config),
    }
    return json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
        ensure_ascii=True,
    )


def compute_config_hash(config: Any) -> str:
    """Hex SHA-256 over the canonical payload."""
    return hashlib.sha256(canonical_payload(config).encode("ascii")).hexdigest()


def diff_relevant_fields(
    config_a: Any,
    config_b: Any,
) -> dict[str, tuple[Any, Any]]:
    """Field-by-field diff. Empty dict means hashes will agree.

    Useful for human-readable WARNING output at load time.
    """
    a = extract_relevant_fields(config_a)
    b = extract_relevant_fields(config_b)
    return {k: (a[k], b[k]) for k in a.keys() | b.keys() if a.get(k) != b.get(k)}


def diff_relevant_fields_from_dict(
    expected_values: dict[str, Any],
    runtime_config: Any,
) -> dict[str, tuple[Any, Any]]:
    """Diff a dict of *expected* values (e.g. loaded from meta.json) against
    a live config object. Returns {field: (expected, runtime)}.
    """
    runtime_values = extract_relevant_fields(runtime_config)
    keys = set(expected_values.keys()) | set(runtime_values.keys())
    return {
        k: (expected_values.get(k, "__MISSING__"), runtime_values.get(k, "__MISSING__"))
        for k in keys
        if expected_values.get(k, "__MISSING__") != runtime_values.get(k, "__MISSING__")
    }
