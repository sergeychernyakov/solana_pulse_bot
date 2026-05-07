# pulse_bot/filters/fast.py
"""Fast phase filter — evaluates token after 3-5 seconds for early entry."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from pulse_bot.config import PUMPFUN_GRADUATION_SOL
from pulse_bot.models import Token, Trade

if TYPE_CHECKING:
    from pulse_bot.config import PulseBotConfig

logger = logging.getLogger(__name__)


class FastFilter:
    """Evaluates token after fast observation window (3-5 sec).

    All thresholds are configurable for backtesting.
    Returns FAST_BUY if token shows explosive organic start, WAIT otherwise.
    """

    def __init__(self, config: PulseBotConfig) -> None:
        self._cfg = config

    def evaluate(self, token: Token, trades: list[Trade]) -> FastResult:
        """Run fast evaluation on collected trades."""
        # No trades = no signal
        if not trades:
            return FastResult(
                decision="WAIT",
                score=0,
                reasons="no_trades",
                buy_count=0,
                sell_count=0,
                unique_buyers=0,
                volume_sol=0.0,
                buy_rate=0.0,
                sell_ratio=0.0,
                curve_pct=0.0,
                elapsed=0.0,
                creator_self_buy=False,
                creator_self_buy_position=0,
            )

        buys = [t for t in trades if t.tx_type == "buy"]
        sells = [t for t in trades if t.tx_type == "sell"]

        total_score = 0
        reasons: list[str] = []

        buy_count = len(buys)
        sell_count = len(sells)
        unique_buyers = len({t.wallet for t in buys})
        buy_amounts = [t.sol_amount for t in buys]
        total_volume = sum(buy_amounts)
        unique_amounts = len({round(a, 4) for a in buy_amounts}) if buy_amounts else 0

        # Elapsed time
        elapsed = 0.0
        if trades:
            elapsed = max(trades[-1].timestamp - trades[0].timestamp, 0.1)
        buy_rate = buy_count / elapsed if elapsed > 0 else 0.0

        # Sell ratio
        sell_ratio = sell_count / max(buy_count, 1)

        # Curve progress
        curve_pct = 0.0
        if trades and trades[-1].v_sol_in_bonding_curve > 0:
            curve_pct = min(
                (trades[-1].v_sol_in_bonding_curve / PUMPFUN_GRADUATION_SOL) * 100.0,
                100.0,
            )

        # Creator sold?
        creator_sold = any(
            t.wallet == token.creator and t.tx_type == "sell" for t in trades
        )

        # Creator self-buy? (rug-pull / fake-demand signal — dev sniping
        # their own token to manufacture early-momentum appearance).
        # Tracks 1-indexed position among buys; 0 = creator never bought.
        creator_self_buy_position = 0
        for idx, b in enumerate(buys, start=1):
            if b.wallet == token.creator:
                creator_self_buy_position = idx
                break
        creator_self_buy = creator_self_buy_position > 0

        # ── Hard rejects ───────────────────────────────────
        # Shared entry filters (same thresholds as Scorer._apply_rules): mirror
        # hard rejects here so fast-mode entries respect min_market_cap_sol /
        # max_sell_pressure_for_entry / min_curve_for_entry. Without this the
        # fast path bypasses user-configured entry safety limits.
        mcap_sol = trades[-1].market_cap_sol if trades else 0.0
        cfg = self._cfg

        def _reject(reason: str) -> FastResult:
            return FastResult(
                decision="WAIT",
                score=0,
                reasons=reason,
                buy_count=buy_count,
                sell_count=sell_count,
                unique_buyers=unique_buyers,
                volume_sol=total_volume,
                buy_rate=buy_rate,
                sell_ratio=sell_ratio,
                curve_pct=curve_pct,
                elapsed=elapsed,
                creator_self_buy=creator_self_buy,
                creator_self_buy_position=creator_self_buy_position,
            )

        if cfg.min_market_cap_sol > 0 and mcap_sol < cfg.min_market_cap_sol:
            return _reject(f"mcap_too_low_{mcap_sol:.0f}")
        if (
            cfg.max_sell_pressure_for_entry < 999
            and sell_ratio > cfg.max_sell_pressure_for_entry
        ):
            return _reject(f"sell_pressure_reject_{sell_ratio:.1f}")
        if cfg.min_curve_for_entry > 0 and curve_pct < cfg.min_curve_for_entry:
            return _reject(f"curve_too_low_{curve_pct:.0f}%")

        if self._cfg.fast_creator_sold_reject and creator_sold:
            return _reject("creator_sold_fast")

        # Creator self-buy hard-reject (opt-in).  Most pump.fun rugs feature
        # the creator buying their own token in the first ~3 seconds to
        # manufacture momentum.  Default off — collect data + ML fits the
        # sniping pattern naturally — operator can flip on for aggressive
        # filtering.
        creator_self_buy_max_pos = getattr(
            self._cfg, "fast_creator_self_buy_reject_max_position", 0
        )
        if (
            creator_self_buy
            and creator_self_buy_max_pos > 0
            and creator_self_buy_position <= creator_self_buy_max_pos
        ):
            return _reject(
                f"creator_self_buy_pos{creator_self_buy_position}"
            )

        # ── Scoring ────────────────────────────────────────

        # 1. Enough unique buyers
        if unique_buyers >= self._cfg.fast_min_unique_buyers:
            total_score += self._cfg.fast_w_buyers
            reasons.append(f"buyers_{unique_buyers}(+{self._cfg.fast_w_buyers})")
        else:
            reasons.append(f"buyers_low_{unique_buyers}")

        # 2. Enough volume
        if total_volume >= self._cfg.fast_min_volume_sol:
            total_score += self._cfg.fast_w_volume
            reasons.append(f"vol_{total_volume:.2f}(+{self._cfg.fast_w_volume})")
        else:
            reasons.append(f"vol_low_{total_volume:.2f}")

        # 3. Velocity (buys per second)
        if buy_rate >= self._cfg.fast_min_buy_rate:
            total_score += self._cfg.fast_w_velocity
            reasons.append(f"rate_{buy_rate:.1f}/s(+{self._cfg.fast_w_velocity})")
        else:
            reasons.append(f"rate_slow_{buy_rate:.1f}/s")

        # 4. Diversity (anti-bot)
        if unique_amounts >= self._cfg.fast_min_diversity:
            total_score += self._cfg.fast_w_diversity
            reasons.append(f"div_{unique_amounts}(+{self._cfg.fast_w_diversity})")
        else:
            reasons.append(f"div_low_{unique_amounts}")

        # 5. No sell pressure
        if sell_ratio <= self._cfg.fast_max_sell_ratio:
            total_score += self._cfg.fast_w_no_sells
            reasons.append(f"sells_ok_{sell_ratio:.2f}(+{self._cfg.fast_w_no_sells})")
        else:
            reasons.append(f"sell_pressure_{sell_ratio:.2f}")

        # 6. Curve not too high
        if curve_pct <= self._cfg.fast_max_curve_pct:
            total_score += self._cfg.fast_w_curve_healthy
            reasons.append(f"curve_{curve_pct:.1f}%(+{self._cfg.fast_w_curve_healthy})")
        else:
            reasons.append(f"curve_high_{curve_pct:.1f}%")

        # 7. Creator self-buy soft penalty (when not hard-rejected above).
        # Even if we don't reject outright, flag this as suspicious so
        # the score reflects the rug risk.
        creator_self_buy_score = getattr(
            self._cfg, "fast_creator_self_buy_score", 0
        )
        if creator_self_buy and creator_self_buy_score:
            total_score += creator_self_buy_score
            reasons.append(
                f"creator_self_buy_pos{creator_self_buy_position}"
                f"({creator_self_buy_score:+d})"
            )

        # HARD reject gates (April 2026): reject dead-on-arrival tokens even
        # if scoring threshold met. 92% of fast=BUY historically went to
        # full=SKIP (noise); these gates kill most of that tail.
        fast_hard_min_vol = getattr(self._cfg, "fast_hard_min_volume_sol", 0.0)
        fast_hard_min_buyers = getattr(self._cfg, "fast_hard_min_unique_buyers", 0)
        hard_reject = (
            total_volume < fast_hard_min_vol or unique_buyers < fast_hard_min_buyers
        )

        # Decision
        if hard_reject:
            decision = "WAIT"
            reasons.append(f"hard_reject_vol{total_volume:.2f}_buyers{unique_buyers}")
        else:
            decision = (
                "FAST_BUY" if total_score >= self._cfg.fast_score_threshold else "WAIT"
            )

        return FastResult(
            decision=decision,
            score=total_score,
            reasons=" | ".join(reasons),
            buy_count=buy_count,
            sell_count=sell_count,
            unique_buyers=unique_buyers,
            volume_sol=total_volume,
            buy_rate=buy_rate,
            sell_ratio=sell_ratio,
            curve_pct=curve_pct,
            elapsed=elapsed,
            creator_self_buy=creator_self_buy,
            creator_self_buy_position=creator_self_buy_position,
        )


class FastResult:
    """Result of fast phase evaluation."""

    __slots__ = (
        "decision",
        "score",
        "reasons",
        "buy_count",
        "sell_count",
        "unique_buyers",
        "volume_sol",
        "buy_rate",
        "sell_ratio",
        "curve_pct",
        "elapsed",
        # 2026-04-29 — creator self-buy detection (rug-pull / fake-demand
        # signal). ``creator_self_buy`` is True when the token's creator
        # wallet appears as a buyer in the fast-window trade stream.
        # ``creator_self_buy_position`` is the 1-indexed position among
        # buyers (1 = first buy, 0 = creator never bought).
        "creator_self_buy",
        "creator_self_buy_position",
    )

    def __init__(
        self,
        decision: str,
        score: int,
        reasons: str,
        buy_count: int,
        sell_count: int,
        unique_buyers: int,
        volume_sol: float,
        buy_rate: float,
        sell_ratio: float,
        curve_pct: float,
        elapsed: float,
        creator_self_buy: bool = False,
        creator_self_buy_position: int = 0,
    ) -> None:
        self.decision = decision
        self.score = score
        self.reasons = reasons
        self.buy_count = buy_count
        self.sell_count = sell_count
        self.unique_buyers = unique_buyers
        self.volume_sol = volume_sol
        self.buy_rate = buy_rate
        self.sell_ratio = sell_ratio
        self.curve_pct = curve_pct
        self.elapsed = elapsed
        self.creator_self_buy = creator_self_buy
        self.creator_self_buy_position = creator_self_buy_position
