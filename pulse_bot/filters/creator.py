# pulse_bot/filters/creator.py
"""Creator history filter — scores based on cached creator behavior."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from pulse_bot.filters.base import Filter
from pulse_bot.models import FilterResult, Token, Trade

if TYPE_CHECKING:
    from pulse_bot.config import PulseBotConfig
    from pulse_bot.db import Database

logger = logging.getLogger(__name__)


class CreatorFilter(Filter):
    """Score based on creator history from SQLite cache.

    Hard reject for blacklisted creators.
    Penalty for serial creators with high early-sell rate.
    Bonus for creators with clean history.
    """

    name = "creator"

    def __init__(self, db: Database, config: PulseBotConfig) -> None:
        self._db = db
        self._config = config

    def check(self, token: Token, trades: list[Trade]) -> FilterResult:
        """Evaluate creator history."""
        stats = self._db.get_creator_stats_sync(token.creator)

        if not stats:
            return FilterResult(
                filter_name=self.name,
                score=0,
                hard_reject=False,
                reason="creator_unknown",
            )

        if stats.blacklisted:
            return FilterResult(
                filter_name=self.name,
                score=0,
                hard_reject=True,
                reason="creator_blacklisted",
            )

        score = 0
        reasons: list[str] = []

        # Serial creator penalty
        if stats.total_tokens_created > self._config.creator_serial_threshold:
            if stats.tokens_where_creator_sold_early > 0:
                sell_rate = stats.tokens_where_creator_sold_early / stats.total_tokens_created
                if sell_rate > 0.5:
                    score -= 20
                    reasons.append(f"serial_dumper({stats.total_tokens_created}tok,{sell_rate:.0%}sell)")
                else:
                    score -= 10
                    reasons.append(f"serial_creator({stats.total_tokens_created}tok)")
            else:
                score -= 5
                reasons.append(f"serial_creator({stats.total_tokens_created}tok,no_dumps)")
        elif stats.total_tokens_created > 1 and stats.tokens_where_creator_sold_early == 0:
            score += 10
            reasons.append(f"clean_creator({stats.total_tokens_created}tok)")

        reason_str = " | ".join(reasons) if reasons else "creator_ok"
        return FilterResult(
            filter_name=self.name,
            score=score,
            hard_reject=False,
            reason=reason_str,
        )
