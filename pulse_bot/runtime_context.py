# pulse_bot/runtime_context.py
"""ObservationContext — single typed carrier for everything Pipeline
needs about a token at decision time.

Codex review 2026-04-28 (next-level recommendations): right now
Pipeline._handle_token passes ``token``, ``collected``, ``creator_snapshot``,
``holder_snapshot_all``, ``wallet_prior_stats``, ``checkpoint_state`` as
separate locals through 500 lines of code, with each helper signature
re-declaring some subset. The protocol between sections is implicit
and prone to silent drift when one location updates a value but
another reads the stale copy.

This dataclass is the explicit contract. Phase B step 3 (when
_handle_token actually splits into ObservationSession +
DecisionService) will accept this object as the inter-stage carrier.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping


@dataclass
class ObservationContext:
    """All state about a single token's scoring window.

    Two stages:
      1. ``Intake`` populates token / trades / scoring_result.
      2. ``Hydration`` populates creator_snapshot / holder_snapshot /
         wallet_prior_stats / top_n_wallets / n_buyers_first_5s.

    Followed by ``Decision`` which reads everything but writes
    nothing here (it produces an EntryDecision instead).

    Frozen=False because the staged population pattern is core to
    how Pipeline builds it up. Tests use ``replace`` for immutability.
    """

    # Stage 1 — Intake
    token: Any
    all_trades: list[Any] = field(default_factory=list)
    fast_trades: list[Any] = field(default_factory=list)
    fast_result: Any | None = None
    scoring_result: Any | None = None
    scored_at: float = 0.0

    # Stage 2 — Hydration
    creator_snapshot: Any | None = None
    holder_snapshot: dict[str, Any] | None = None
    top_n_wallets: list[str] = field(default_factory=list)
    wallet_prior_stats: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    n_buyers_first_5s: float = float("nan")

    # Stage 3 — Checkpoint state (mutated by observation_checkpoint_loop)
    checkpoint_verdict: str | None = None     # "BUY_EARLY" | "SKIP_EARLY" | None
    checkpoint_proba: float | None = None
    checkpoint_source: str = "checkpoint"

    @property
    def mint(self) -> str:
        return getattr(self.token, "mint", "")

    @property
    def mint_short(self) -> str:
        return self.mint[:12]

    @property
    def is_replay(self) -> bool:
        """Best-effort detection — lives here so callers don't need to
        know about launchpad internals."""
        return getattr(self.token, "_is_replay", False)
