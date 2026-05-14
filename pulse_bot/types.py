# pulse_bot/types.py
"""Typed value objects for entry / exit decisions (architecture phase J,
codex review 2026-04-28).

Stringly-typed decisions ("BUY", "SKIP", "BUY_EARLY", "RULES") are easy
to typo and silently diverge between modules. Enums give:

* mypy / ruff catch typos at write time
* IDE autocomplete on `EntryAction.`
* enumerable for switch-style dispatch

Strings remain the wire format (logs, DB, /metrics). The enums carry
``.value`` for serialization; helper ``from_string`` for incoming data.
The ML model returns strings — we wrap with ``EntryAction.from_string``
once at the boundary.
"""

from __future__ import annotations

from enum import Enum


class EntryAction(str, Enum):
    """Three-way decision from EntryMLPolicy.decide_with_confidence()."""

    BUY = "BUY"
    SKIP = "SKIP"
    RULES = "RULES"  # grey zone — defer to rules engine

    @classmethod
    def from_string(cls, s: str) -> "EntryAction":
        """Parse an incoming string. Raises ValueError on unknown."""
        return cls(s)


class EntryType(str, Enum):
    """How an entry was triggered. Persisted in paper_trades.entry_type."""

    FAST = "fast"
    FULL = "full"
    ML_OVERRIDE = "ml_override"
    T30 = "t30"
    T30_SKIP = "t30_skip"  # T+30 SKIP-only mode (legacy)
    TIMING = "timing"
    BUY_EARLY = "BUY_EARLY"

    @classmethod
    def is_real_entry(cls, value: str | None) -> bool:
        """Is this entry_type a real bot-driven entry (vs shadow tracking)?

        Used by dashboard / analytics to distinguish ml_override entries
        with entry_buyer_number=0 from genuine shadow rows."""
        if not value:
            return False
        try:
            cls(value)
            return True
        except ValueError:
            return False


class CheckpointVerdict(str, Enum):
    """Verdict from the observation_checkpoint_loop (T+30 / timing)."""

    BUY_EARLY = "BUY_EARLY"
    SKIP_EARLY = "SKIP_EARLY"


class TimingClass(str, Enum):
    """Output of the 3-class entry_timing softmax."""

    WAIT_MORE = "WAIT_MORE"
    BUY_NOW = "BUY_NOW"
    SKIP = "SKIP"


class ExitReason(str, Enum):
    """All known exit reasons that ExitManager / paper trade close path
    can produce. Used for /metrics labels and analytics queries.

    NB: this is not a closed set — code paths still produce strings
    that don't appear here (e.g. operator-typed override). Treat as
    documentation + autocomplete, not a hard contract."""

    # B105 false positive: this is an exit-reason label, not a secret string.
    DEAD_TOKEN = "dead_token"  # nosec B105
    HARD_STOP = "hard_stop"
    TAKE_PROFIT = "take_profit"
    TRAILING_STOP = "trailing_stop"
    TIMEOUT = "timeout"
    INACTIVITY = "inactivity"
    SELL_PRESSURE = "sell_pressure"
    NO_NEW_BLOOD = "no_new_blood"
    NEAR_GRADUATION = "near_graduation"
    PULSE_DEAD = "pulse_dead"
    CREATOR_DUMP = "creator_dump"
    ML_SL_TIGHTENED = "ml_sl_tightened"
    SURVIVAL_PREDICT = "survival_predict"
    BOT_CLUSTER_SKIP = "bot_cluster_skip"
    ERROR = "error"


class ModelHealth(str, Enum):
    """Status flag written to model meta.json by train.py."""

    OK = "ok"
    DEGENERATE = "degenerate"
    DEGENERATE_OVERLAP = "degenerate_overlap"
    NARROW_PROBA_SPREAD = "narrow_proba_spread"
    AUC_REGRESSION = "auc_regression"
    ANTI_CORRELATED = "anti_correlated"
    WEAK_SIGNAL = "weak_signal"
    MISSING = "missing"

    @classmethod
    def is_healthy(cls, status: str | None) -> bool:
        """Treat None / missing flag as healthy (legacy artifacts)."""
        if status is None:
            return True
        return status == cls.OK.value
