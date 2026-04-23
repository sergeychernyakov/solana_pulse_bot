# pulse_bot/launchpads/letsbonk.py
"""LetsBonk launchpad adapter — STUB (task #100, Phase C).

Codex Q4 recommendation: running a parallel LetsBonk collector 2x the
effective training throughput going forward. Currently ~99.6% of scored
tokens are pump.fun (0 variance on ``launchpad`` feature). LetsBonk
exposes a similar WebSocket stream, but the message schema differs.

Implementation plan (pending WebSocket protocol research):
  1. Subscribe to LetsBonk's new-token-mint firehose (URL TBD — need
     API key + protocol docs).
  2. Parse create events → ``Token`` dataclass.
  3. Subscribe to trade stream per-mint (bonding curve on LetsBonk uses
     different constants — curve_progress math needs adapting).
  4. Emit trades into same pipeline as pumpfun adapter.

Blocker: requires endpoint credentials + schema docs. When available,
mirror the structure of ``pulse_bot/launchpads/pumpfun.py``.
"""

from __future__ import annotations

from typing import AsyncIterator

from pulse_bot.launchpads.base import Launchpad
from pulse_bot.models import Token, Trade


class LetsBonkLaunchpad(Launchpad):
    """LetsBonk adapter — NOT YET IMPLEMENTED."""

    name = "letsbonk"
    ws_url = ""  # TBD

    async def connect(self) -> None:
        raise NotImplementedError(
            "LetsBonk adapter is a stub. See task #100 / Phase C. "
            "Requires WS endpoint + protocol research before activation."
        )

    async def disconnect(self) -> None:
        raise NotImplementedError

    def stream_new_tokens(self) -> AsyncIterator[Token]:
        raise NotImplementedError

    async def subscribe_trades(self, mint: str) -> None:
        raise NotImplementedError

    async def unsubscribe_trades(self, mint: str) -> None:
        raise NotImplementedError

    def stream_trades(
        self, mint: str, duration_seconds: float, inactivity_timeout: float = 0
    ) -> AsyncIterator[Trade]:
        raise NotImplementedError

    def parse_create_event(self, raw: dict) -> Token:
        raise NotImplementedError

    def parse_trade_event(self, raw: dict, creator: str) -> Trade:
        raise NotImplementedError

    def compute_curve_progress(self, v_sol_in_bonding_curve: float) -> float:
        raise NotImplementedError
