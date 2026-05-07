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
    # 2026-04-29 — creator self-buy detection (rug-pull / fake-demand
    # signal). Mirrors the fast-window detector in FastFilter but on the
    # full window so the ML features see it for any window size.
    # ``creator_self_buy_position`` is 1-indexed among buys in the window
    # (1 = first buyer, 0 = creator never bought in window).
    creator_self_buy: bool = False
    creator_self_buy_position: int = 0

    # New: trade pattern analysis
    avg_buy_sol: float = 0.0  # mean buy size
    median_buy_sol: float = 0.0  # median buy size (diff from avg = skew)
    std_buy_sol: float = 0.0  # std dev of buy sizes (high = organic)
    top3_buyer_pct: float = 0.0  # top 3 buyers % of total volume
    repeat_buyer_count: int = 0  # wallets that bought more than once
    first_buy_sol: float = 0.0  # first buy amount (large = sniper)
    buy_velocity_trend: float = 0.0  # >1 = accelerating, <1 = decelerating
    buy_size_trend: float = 0.0  # >1 = buys getting larger, <1 = smaller
    # Raw halves — per codex 2026-04-22: ratio fields above have
    # denom-floor non-monotonic cliffs; also expose the pre-division
    # components so the tree can split on them directly.
    first_half_buy_rate: float = 0.0  # buys/sec in first half of window
    second_half_buy_rate: float = 0.0  # buys/sec in second half
    avg_first_half_buy_sol: float = 0.0
    avg_second_half_buy_sol: float = 0.0
    # 2026-04-23 feature additions — expose specific patterns the raw
    # aggregates don't capture. All derivable from the trade stream.
    time_gap_median_first20: float = 0.0  # low var = bot signature
    buy_volume_first10s: float = 0.0  # early momentum
    unique_buyers_first30s: int = 0  # attention window 1
    unique_buyers_last30s: int = 0  # attention window 2 (end of full obs)
    curve_progress_at_t30: float = 0.0  # curve shape @ T+30s
    curve_progress_at_t60: float = 0.0
    curve_progress_at_t90: float = 0.0
    time_to_first_buy: float = 0.0  # seconds from create to first buy
    buys_per_unique: float = 0.0  # buy_count / unique_buyers (>1.5 = wash)
    # 2026-04-23 v11 additions
    median_time_between_buys: float = 0.0  # bot-throttle detector (sec)
    buy_wallet_entropy: float = (
        0.0  # normalized Shannon [0,1]; 1=diffuse, 0=concentrated
    )

    # ── Phase 2.5 (2026-04-25): time-aware snapshots ────────
    # Trade-stream metrics truncated at T+30s, T+60s, T+90s relative to
    # ``token.created_at``. Lets ML learn token "evolution" rather than
    # only summary stats over the full observation window. The @90 values
    # are bit-for-bit equal to the existing full-window features when
    # observation_seconds == 90s — parity-tested in
    # tests/pulse_bot/test_time_aware_features.py.
    unique_buyers_at_30: int = 0
    unique_buyers_at_60: int = 0
    unique_buyers_at_90: int = 0
    buy_rate_at_30: float = 0.0  # buys/sec on the truncated window
    buy_rate_at_60: float = 0.0
    buy_rate_at_90: float = 0.0
    buy_volume_sol_at_30: float = 0.0
    buy_volume_sol_at_60: float = 0.0
    buy_volume_sol_at_90: float = 0.0

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

        # Defensive sort — codex 2026-04-22: ``first_buy_sol``, velocity
        # trend and curve velocity all implicitly assume chronological
        # order. Callers usually provide sorted input, but an unsorted
        # list silently corrupts those features.
        trades = sorted(trades, key=lambda t: t.timestamp)

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
        # Creator self-buy: 1-indexed position among buys, 0 = never bought
        m.creator_self_buy_position = 0
        for idx, b in enumerate(buys, start=1):
            if b.wallet == token.creator:
                m.creator_self_buy_position = idx
                break
        m.creator_self_buy = m.creator_self_buy_position > 0

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
        # Codex 2026-04-22: return -1.0 sentinel when unique_buyers < 3
        # to distinguish "concentrated" from "tiny sample" (trivially
        # 100% if only 2 buyers). Tree splits can learn that -1 means
        # insufficient data.
        if buys and m.total_buy_volume_sol > 0 and m.unique_buyers >= 3:
            wallet_volume: dict[str, float] = {}
            for t in buys:
                wallet_volume[t.wallet] = wallet_volume.get(t.wallet, 0) + t.sol_amount
            top3_vol = sum(sorted(wallet_volume.values(), reverse=True)[:3])
            m.top3_buyer_pct = (top3_vol / m.total_buy_volume_sol) * 100.0
        elif buys:
            m.top3_buyer_pct = -1.0

        # ── Repeat buyers ──────────────────────────────────
        if buyer_wallets:
            from collections import Counter

            wallet_counts = Counter(buyer_wallets)
            m.repeat_buyer_count = sum(1 for c in wallet_counts.values() if c > 1)

        # ── Buys per unique ────────────────────────────────
        if m.unique_buyers > 0:
            m.buys_per_unique = m.buy_count / m.unique_buyers

        # ── Median time between consecutive buys (v11 add) ─────
        # Low value (and low variance) = bot-like throttle. High = organic
        # interaction. Zero = single buy or no buys. Unit: seconds.
        if len(buys) >= 3:
            gaps = [
                buys[i + 1].timestamp - buys[i].timestamp for i in range(len(buys) - 1)
            ]
            if gaps:
                m.median_time_between_buys = statistics.median(gaps)

        # ── Buyer-wallet Shannon entropy (v11 add) ─────────────
        # Measures how evenly the BUY VOLUME is distributed across
        # distinct wallets. Low = concentrated (few whales), high =
        # diffuse (organic). Normalized to [0, 1] by dividing by
        # log2(unique_buyers) so value is invariant to sample size.
        if buys and m.total_buy_volume_sol > 0 and m.unique_buyers >= 2:
            import math as _math

            wv: dict[str, float] = {}
            for tr in buys:
                wv[tr.wallet] = wv.get(tr.wallet, 0.0) + tr.sol_amount
            total = sum(wv.values())
            # Shannon H = -Σ p·log2(p); normalize by log2(n)
            h = 0.0
            for v in wv.values():
                p = v / total
                if p > 0:
                    h -= p * _math.log2(p)
            max_h = _math.log2(len(wv)) if len(wv) > 1 else 1.0
            m.buy_wallet_entropy = h / max_h

        # ── Velocity + size half-splits ────────────────────
        # Keep the ratio fields for backward compat (scorer still reads
        # them for display), but also expose raw halves so ML doesn't
        # depend on the capped division. Codex 2026-04-22.
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
            m.first_half_buy_rate = rate1
            m.second_half_buy_rate = rate2
            m.buy_velocity_trend = rate2 / max(rate1, 0.01)

        if len(buy_amounts) >= 4:
            mid = len(buy_amounts) // 2
            avg_first = statistics.mean(buy_amounts[:mid])
            avg_second = statistics.mean(buy_amounts[mid:])
            m.avg_first_half_buy_sol = avg_first
            m.avg_second_half_buy_sol = avg_second
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

        # ── New features (2026-04-23) ──────────────────────
        # time_gap_median_first20: bot signature — coordinated bots
        # fire trades at near-uniform intervals, organic activity is
        # bursty. Computed on first 20 trades, not all, to catch the
        # early coordination window.
        if len(trades) >= 21:
            first20 = trades[:20]
            gaps = [
                first20[i + 1].timestamp - first20[i].timestamp
                for i in range(len(first20) - 1)
            ]
            if gaps:
                m.time_gap_median_first20 = statistics.median(gaps)

        # buy_volume_first10s: how much SOL went in the first 10 sec.
        # Captures initial momentum independent of total window size.
        first10s_end = token.created_at + 10.0
        m.buy_volume_first10s = sum(
            t.sol_amount for t in buys if t.timestamp <= first10s_end
        )

        # unique_buyers_first30s vs last30s: attention trend. If last
        # window has fewer unique buyers than first, attention fading.
        first30s_end = token.created_at + 30.0
        last30s_start = (trades[-1].timestamp - 30.0) if trades else token.created_at
        m.unique_buyers_first30s = len(
            {t.wallet for t in buys if t.timestamp <= first30s_end}
        )
        m.unique_buyers_last30s = len(
            {t.wallet for t in buys if t.timestamp >= last30s_start}
        )

        # curve_progress_at_t30/60/90: snapshot bonding-curve progress
        # at three fixed ages — captures trajectory shape (fast-ramp
        # vs slow-start vs late-surge) independent of final progress.
        def _curve_pct_at(age_sec: float) -> float:
            target_ts = token.created_at + age_sec
            # Find last trade at or before target
            prior = [t for t in trades if t.timestamp <= target_ts]
            if not prior:
                return 0.0
            v_sol = prior[-1].v_sol_in_bonding_curve
            if v_sol <= 0 or self._graduation_sol <= 0:
                return 0.0
            return min((v_sol / self._graduation_sol) * 100.0, 100.0)

        m.curve_progress_at_t30 = _curve_pct_at(30.0)
        m.curve_progress_at_t60 = _curve_pct_at(60.0)
        m.curve_progress_at_t90 = _curve_pct_at(90.0)

        # ── Phase 2.5 (2026-04-25) — time-aware snapshots ───────────
        # Truncate the BUY stream at create_at+30/60/90 and re-aggregate
        # unique buyers, buys/sec, and SOL volume. Buy rate denominator
        # is the snapshot age (30/60/90), not observation_seconds — it
        # represents "buys/sec since launch up to T+N", which is
        # invariant across rows with different observation windows and
        # which the model can compare across snapshots without a
        # division-by-window confound.
        def _stats_up_to(age_sec: float) -> tuple[int, float, float]:
            cutoff = token.created_at + age_sec
            sub = [t for t in buys if t.timestamp <= cutoff]
            if not sub:
                return 0, 0.0, 0.0
            uniq = len({t.wallet for t in sub})
            vol = sum(t.sol_amount for t in sub)
            rate = len(sub) / age_sec if age_sec > 0 else 0.0
            return uniq, rate, vol

        u30, r30, v30 = _stats_up_to(30.0)
        u60, r60, v60 = _stats_up_to(60.0)
        u90, r90, v90 = _stats_up_to(90.0)
        m.unique_buyers_at_30 = u30
        m.unique_buyers_at_60 = u60
        m.unique_buyers_at_90 = u90
        m.buy_rate_at_30 = r30
        m.buy_rate_at_60 = r60
        m.buy_rate_at_90 = r90
        m.buy_volume_sol_at_30 = v30
        m.buy_volume_sol_at_60 = v60
        m.buy_volume_sol_at_90 = v90

        return m
