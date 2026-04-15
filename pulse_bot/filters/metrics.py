# pulse_bot/filters/metrics.py
"""MetricsCalculator — computes all token metrics from trades for scoring and backtesting."""

from __future__ import annotations

import statistics
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pulse_bot.models import Token, Trade


@dataclass
class TokenMetrics:
    """All computed metrics for a token. Stored in DB for backtesting."""

    # ── Trade pattern metrics ──────────────────────────────
    buy_count: int = 0
    sell_count: int = 0
    unique_buyers: int = 0
    unique_sellers: int = 0
    total_buy_volume_sol: float = 0.0
    total_sell_volume_sol: float = 0.0
    buy_diversity: int = 0  # unique buy amounts (rounded)
    max_buy_sol: float = 0.0  # largest single buy
    creator_sold: bool = False

    # New: trade pattern analysis
    avg_buy_sol: float = 0.0  # mean buy size
    median_buy_sol: float = 0.0  # median buy size (diff from avg = skew)
    std_buy_sol: float = 0.0  # std dev of buy sizes (high = organic)
    top3_buyer_pct: float = 0.0  # top 3 buyers % of total volume
    repeat_buyer_count: int = 0  # wallets that bought more than once
    first_buy_sol: float = 0.0  # first buy amount (large = sniper)
    buy_velocity_trend: float = 0.0  # >1 = accelerating, <1 = decelerating
    buy_size_trend: float = 0.0  # >1 = buys getting larger, <1 = smaller
    time_to_first_buy: float = 0.0  # seconds from create to first buy
    buys_per_unique: float = 0.0  # buy_count / unique_buyers (>1.5 = wash)

    # ── Bonding curve metrics ──────────────────────────────
    curve_progress_pct: float = 0.0  # % of graduation threshold
    curve_velocity: float = 0.0  # SOL/sec into curve
    curve_acceleration: float = 0.0  # velocity change: >0 accelerating
    sol_to_graduation: float = 0.0  # SOL remaining to graduate
    market_cap_sol: float = 0.0

    # ── Token metadata ─────────────────────────────────────
    name_length: int = 0
    symbol_length: int = 0
    has_uri: bool = False
    is_all_caps: bool = False
    has_numbers: bool = False

    # ── Timing ─────────────────────────────────────────────
    hour_utc: int = 0  # hour of creation (0-23)
    creator_tokens_today: int = 0  # how many tokens creator made today
    gap_create_to_first_trade: float = 0.0  # delay in seconds

    # ── Market context ─────────────────────────────────────
    tokens_last_5min: int = 0  # market activity
    concurrent_observations: int = 0  # how busy the bot is

    # ── Sell pressure ──────────────────────────────────────
    sell_ratio: float = 0.0  # sell_count / buy_count

    # ── Price data ─────────────────────────────────────────
    token_price_sol: float = 0.0
    exit_price: float = 0.0

    # ── Observation timing ─────────────────────────────────
    observation_seconds: float = 0.0


class MetricsCalculator:
    """Computes all metrics from token + trades. Pure computation, no I/O."""

    def __init__(self, graduation_sol: float = 85.0) -> None:
        self._graduation_sol = graduation_sol

    def compute(
        self,
        token: Token,
        trades: list[Trade],
        creator_tokens_today: int = 0,
        tokens_last_5min: int = 0,
        concurrent_observations: int = 0,
    ) -> TokenMetrics:
        """Compute all metrics from token data and trade list."""
        m = TokenMetrics()

        buys = [t for t in trades if t.tx_type == "buy"]
        sells = [t for t in trades if t.tx_type == "sell"]
        buy_amounts = [t.sol_amount for t in buys]

        # ── Basic counts ───────────────────────────────────
        m.buy_count = len(buys)
        m.sell_count = len(sells)

        buyer_wallets = [t.wallet for t in buys]
        m.unique_buyers = len(set(buyer_wallets))
        m.unique_sellers = len({t.wallet for t in sells})

        m.total_buy_volume_sol = sum(buy_amounts)
        m.total_sell_volume_sol = sum(t.sol_amount for t in sells)

        m.buy_diversity = len({round(a, 4) for a in buy_amounts}) if buy_amounts else 0
        m.max_buy_sol = max(buy_amounts, default=0.0)
        m.creator_sold = any(t.wallet == token.creator for t in sells)

        m.sell_ratio = m.sell_count / max(m.buy_count, 1)

        # ── Buy size statistics ────────────────────────────
        if buy_amounts:
            m.avg_buy_sol = statistics.mean(buy_amounts)
            m.median_buy_sol = statistics.median(buy_amounts)
            m.std_buy_sol = (
                statistics.stdev(buy_amounts) if len(buy_amounts) > 1 else 0.0
            )
            m.first_buy_sol = buy_amounts[0]

        # ── Top 3 concentration ────────────────────────────
        if buys and m.total_buy_volume_sol > 0:
            wallet_volume: dict[str, float] = {}
            for t in buys:
                wallet_volume[t.wallet] = wallet_volume.get(t.wallet, 0) + t.sol_amount
            top3_vol = sum(sorted(wallet_volume.values(), reverse=True)[:3])
            m.top3_buyer_pct = (top3_vol / m.total_buy_volume_sol) * 100.0

        # ── Repeat buyers ──────────────────────────────────
        if buyer_wallets:
            from collections import Counter

            wallet_counts = Counter(buyer_wallets)
            m.repeat_buyer_count = sum(1 for c in wallet_counts.values() if c > 1)

        # ── Buys per unique ────────────────────────────────
        if m.unique_buyers > 0:
            m.buys_per_unique = m.buy_count / m.unique_buyers

        # ── Velocity trend (first half vs second half buy rate) ─
        if len(buys) >= 4:
            mid = len(buys) // 2
            first_half = buys[:mid]
            second_half = buys[mid:]
            t1 = (
                first_half[-1].timestamp - first_half[0].timestamp
                if len(first_half) > 1
                else 1.0
            )
            t2 = (
                second_half[-1].timestamp - second_half[0].timestamp
                if len(second_half) > 1
                else 1.0
            )
            rate1 = len(first_half) / max(t1, 0.1)
            rate2 = len(second_half) / max(t2, 0.1)
            m.buy_velocity_trend = rate2 / max(rate1, 0.01)

        # ── Buy size trend (avg first half vs avg second half) ─
        if len(buy_amounts) >= 4:
            mid = len(buy_amounts) // 2
            avg_first = statistics.mean(buy_amounts[:mid])
            avg_second = statistics.mean(buy_amounts[mid:])
            m.buy_size_trend = avg_second / max(avg_first, 0.000001)

        # ── Time to first buy ──────────────────────────────
        if trades:
            m.time_to_first_buy = max(trades[0].timestamp - token.created_at, 0.0)

        # ── Bonding curve ──────────────────────────────────
        if trades:
            last = trades[-1]
            v_sol = last.v_sol_in_bonding_curve
            m.curve_progress_pct = (
                min((v_sol / self._graduation_sol) * 100.0, 100.0) if v_sol > 0 else 0.0
            )
            m.sol_to_graduation = max(self._graduation_sol - v_sol, 0.0)
            m.market_cap_sol = last.market_cap_sol

            # Curve velocity
            if len(trades) > 1:
                elapsed = trades[-1].timestamp - trades[0].timestamp
                if elapsed > 0:
                    first_v_sol = trades[0].v_sol_in_bonding_curve
                    m.curve_velocity = (v_sol - first_v_sol) / elapsed

            # Curve acceleration (first half velocity vs second half)
            if len(trades) >= 4:
                mid_idx = len(trades) // 2
                t_mid = trades[mid_idx]
                elapsed_1 = t_mid.timestamp - trades[0].timestamp
                elapsed_2 = trades[-1].timestamp - t_mid.timestamp
                if elapsed_1 > 0 and elapsed_2 > 0:
                    vel_1 = (
                        t_mid.v_sol_in_bonding_curve - trades[0].v_sol_in_bonding_curve
                    ) / elapsed_1
                    vel_2 = (
                        last.v_sol_in_bonding_curve - t_mid.v_sol_in_bonding_curve
                    ) / elapsed_2
                    m.curve_acceleration = vel_2 - vel_1

        # ── Price ──────────────────────────────────────────
        price_buys = [t for t in buys if t.token_amount > 0 and t.sol_amount > 0]
        if price_buys:
            last_buy = price_buys[-1]
            m.token_price_sol = last_buy.sol_amount / last_buy.token_amount
            m.exit_price = m.token_price_sol

        # ── Token metadata ─────────────────────────────────
        m.name_length = len(token.name)
        m.symbol_length = len(token.symbol)
        m.has_uri = bool(token.uri)
        m.is_all_caps = token.symbol == token.symbol.upper() and token.symbol.isalpha()
        m.has_numbers = any(c.isdigit() for c in token.name + token.symbol)

        # ── Timing ─────────────────────────────────────────
        import datetime

        m.hour_utc = datetime.datetime.fromtimestamp(
            token.created_at, tz=datetime.timezone.utc
        ).hour
        m.creator_tokens_today = creator_tokens_today
        if trades:
            m.gap_create_to_first_trade = max(
                trades[0].timestamp - token.created_at, 0.0
            )

        # ── Market context ─────────────────────────────────
        m.tokens_last_5min = tokens_last_5min
        m.concurrent_observations = concurrent_observations

        # ── Observation timing ─────────────────────────────
        if trades:
            m.observation_seconds = trades[-1].timestamp - trades[0].timestamp

        return m
