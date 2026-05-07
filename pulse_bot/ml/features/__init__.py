# pulse_bot/ml/features_pkg/__init__.py
"""Backwards-compatible re-exports for pulse_bot.ml.features.

2026-04-28 (architecture phase G, codex review): the implementation
moved from a 971-line ``features.py`` to ``features_pkg/_main.py``
behind this shim. Every external import (``from pulse_bot.ml.features
import ...``) keeps working unchanged.

This is a SAFETY scaffold — the next iteration will split _main.py
into ``_lists.py`` (constants), ``_helpers.py`` (compute_*), and
``_extract.py`` (extract_entry_features*). For now the shim lets us
take that next step without touching any caller.
"""

# Re-export everything from the implementation module.
from pulse_bot.ml.features._main import *  # noqa: F401,F403
from pulse_bot.ml.features import _main as _impl  # noqa: F401

# Explicit names for static analysers / IDEs that don't follow `*`.
from pulse_bot.ml.features._main import (  # noqa: F401
    SCORER_FEATURES,
    DERIVED_FEATURES,
    HELIUS_FEATURES,
    CREATOR_FEATURES,
    WALLET_FEATURES,
    TIME_AWARE_FEATURES,
    TIME_AWARE_DERIVED_FEATURES,
    ENTRY_FEATURE_ORDER,
    FEATURE_SCHEMA_VERSION,
    FEATURE_SCHEMA_VERSION_T30,
    EXIT_FEATURE_ORDER,
    EXIT_FEATURE_SCHEMA_VERSION,
    SCORER_FEATURES_T30,
    DERIVED_FEATURES_T30,
    HELIUS_FEATURES_T30,
    ENTRY_T30_FEATURE_ORDER,
    extract_exit_features,
    extract_exit_vector,
    compute_top3_buyer_wallets,
    compute_topN_buyer_wallets,
    compute_n_buyers_first_5s,
    extract_entry_features,
    extract_entry_vector,
    extract_entry_features_t30,
    extract_entry_vector_t30,
    _extract_wallet_prior_features,
    _cyclical_hour,
    _get,
    _get_creator_feat,
)
