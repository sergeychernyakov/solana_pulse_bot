# pulse_bot/ml/policy/__init__.py
"""Backwards-compatible re-exports for pulse_bot.ml.policy.

2026-04-28 (architecture phase G, codex review): the implementation
moved from a 1034-line ``policy.py`` to ``policy/_main.py`` behind
this shim. Every external import keeps working unchanged.

Future split: ``_entry.py`` (EntryMLPolicy, EntryT30Policy),
``_exit.py`` (ExitMLPolicy, ExitQuantilePolicy), ``_loaders.py``
(load_*_if_available + sha256_file). Done lazily when next adding
a new policy class.
"""

from pulse_bot.ml.policy import _main as _impl  # noqa: F401

# Explicit re-exports for static analysers + IDE autocomplete.
from pulse_bot.ml.policy._main import *  # noqa: F401,F403
from pulse_bot.ml.policy._main import (  # noqa: F401
    DEFAULT_ENTRY_MODEL_PATH,
    DEFAULT_ENTRY_REG_MODEL_PATH,
    DEFAULT_ENTRY_THRESHOLD,
    DEFAULT_EXIT_MODEL_PATH,
    DEFAULT_EXIT_THRESHOLD,
    EntryMLPolicy,
    EntryT30Policy,
    ExitMLPolicy,
    ExitQuantilePolicy,
    _check_config_drift,
    _first_mismatch,
    _resolve_entry_model_path,
    _safe_get_runtime_config,
    get_active_policy_name,
    load_entry_policy_if_available,
    load_entry_t30_policy_if_available,
    load_exit_policy_if_available,
    load_exit_quantile_if_available,
    sha256_file,
)
