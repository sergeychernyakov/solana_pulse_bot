# pulse_bot/models.py
"""Data models for the Pulse Bot pipeline."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Token:
    """A newly created token detected on a launchpad."""

    mint: str
    name: str
    symbol: str
    creator: str
    created_at: float
    uri: str
    launchpad: str = "pumpfun"


@dataclass
class Trade:
    """A single buy/sell trade observed on a bonding curve."""

    mint: str
    wallet: str
    tx_type: str  # "buy" | "sell"
    sol_amount: float
    token_amount: float
    new_token_balance: float
    bonding_curve_key: str
    v_sol_in_bonding_curve: float
    v_tokens_in_bonding_curve: float
    market_cap_sol: float
    timestamp: float
    is_creator: bool = False


@dataclass
class FilterResult:
    """Result from a single filter evaluation."""

    filter_name: str
    score: int
    hard_reject: bool
    reason: str
    available: bool = True


@dataclass
class ObservationResult:
    """Aggregated trade metrics for an observation window."""

    mint: str
    unique_buyers: int
    unique_sellers: int
    total_buy_volume_sol: float
    total_sell_volume_sol: float
    buy_count: int
    sell_count: int
    buy_amounts: list[float]
    curve_progress_pct: float
    creator_sold: bool
    max_single_buy_sol: float
    observation_seconds: float


@dataclass
class ScoringResult:
    """Final scoring decision for a token. All metrics stored for backtesting."""

    # ── Identity ───────────────────────────────────────────
    mint: str = ""
    source: str = "live"  # "live" or "backtest"
    symbol: str = ""
    name: str = ""
    creator: str = ""

    # ── Full phase decision ────────────────────────────────
    total_score: int = 0
    decision: str = ""  # "BUY" | "SKIP" | "BORDERLINE"
    reasons_summary: str = ""
    filter_results: list[FilterResult] = field(default_factory=list)

    # ── Fast phase decision ────────────────────────────────
    fast_decision: str = ""  # "FAST_BUY" | "WAIT"
    fast_score: int = 0
    fast_reasons: str = ""
    fast_buy_count: int = 0
    fast_volume_sol: float = 0.0
    fast_buy_rate: float = 0.0
    fast_unique_buyers: int = 0
    fast_sell_ratio: float = 0.0
    fast_elapsed: float = 0.0
    fast_scored_at: float = 0.0
    fast_entry_price: float = 0.0
    pnl_at_fast_entry_pct: float = 0.0

    # ── Trade pattern metrics ──────────────────────────────
    buy_count: int = 0
    sell_count: int = 0
    unique_buyers: int = 0
    unique_sellers: int = 0
    buy_volume_sol: float = 0.0
    sell_volume_sol: float = 0.0
    buy_diversity: int = 0
    max_buy_sol: float = 0.0
    creator_sold: bool = False
    sell_pressure: float = 0.0
    avg_buy_sol: float = 0.0
    median_buy_sol: float = 0.0
    std_buy_sol: float = 0.0
    top3_buyer_pct: float = 0.0
    repeat_buyer_count: int = 0
    first_buy_sol: float = 0.0
    buy_velocity_trend: float = 0.0
    buy_size_trend: float = 0.0
    # Raw halves (codex 2026-04-22) — exposed so ML doesn't depend on
    # the capped-denominator ratios above.
    first_half_buy_rate: float = 0.0
    second_half_buy_rate: float = 0.0
    avg_first_half_buy_sol: float = 0.0
    avg_second_half_buy_sol: float = 0.0
    # 2026-04-23 additions
    time_gap_median_first20: float = 0.0
    buy_volume_first10s: float = 0.0
    unique_buyers_first30s: int = 0
    unique_buyers_last30s: int = 0
    curve_progress_at_t30: float = 0.0
    curve_progress_at_t60: float = 0.0
    curve_progress_at_t90: float = 0.0
    time_to_first_buy: float = 0.0
    buys_per_unique: float = 0.0

    # ── Bonding curve ──────────────────────────────────────
    curve_progress_pct: float = 0.0
    curve_velocity: float = 0.0
    curve_acceleration: float = 0.0
    sol_to_graduation: float = 0.0
    market_cap_sol: float = 0.0

    # ── Price ──────────────────────────────────────────────
    token_price_sol: float = 0.0
    exit_price: float = 0.0
    pnl_5th_pct: float = 0.0
    pnl_10th_pct: float = 0.0
    pnl_20th_pct: float = 0.0
    pnl_50th_pct: float = 0.0
    pnl_100th_pct: float = 0.0

    # ── Token metadata ─────────────────────────────────────
    name_length: int = 0
    symbol_length: int = 0
    has_uri: bool = False
    is_all_caps: bool = False
    has_numbers: bool = False

    # ── Timing ─────────────────────────────────────────────
    hour_utc: int = 0
    creator_tokens_today: int = 0
    gap_create_to_first_trade: float = 0.0

    # ── Market context ─────────────────────────────────────
    tokens_last_5min: int = 0
    concurrent_observations: int = 0

    # ── Trade counts for replay (exact match live ↔ backtest) ─
    fast_trade_count: int = 0  # total trades (buy+sell) in fast window
    full_trade_count: int = 0  # total trades (buy+sell) in full window
    fast_trade_ids: str = ""  # comma-separated trade DB ids
    creator_score: int = 0  # creator filter score for exact replay
    creator_reason: str = ""  # creator filter reason for exact replay
    full_trade_ids: str = ""  # comma-separated trade DB ids

    # ── Timestamps ─────────────────────────────────────────
    created_at: float = 0.0
    scored_at: float = 0.0


@dataclass
class CreatorStats:
    """Cached statistics about a token creator.

    Fields after ``blacklisted`` are populated from the leak-free
    ``creator_snapshots`` table (#48/#49) when a snapshot exists at or
    before the as-of timestamp. Default 0.0 means "no data" and scoring
    rules must treat that as neutral (never reject on missing data).
    """

    wallet: str
    total_tokens_created: int = 0
    times_seen: int = 0
    tokens_where_creator_sold_early: int = 0
    first_seen_at: float = 0.0
    last_seen_at: float = 0.0
    blacklisted: bool = False
    # Enriched snapshot fields (all optional — 0 means unavailable):
    rug_rate: float = 0.0  # prior tokens rugged / total
    graduation_rate: float = 0.0  # prior tokens graduated / total
    median_peak_mc_sol: float = 0.0  # median peak MC across prior tokens
    creator_age_days: float = 0.0  # days since earliest prior token
    inter_token_interval_sec: float = 0.0  # mean gap between creations
    creator_balance_sol: float = (
        0.0  # wallet balance at snapshot time (Helius-only; 0 when unavailable)
    )
    rug_count: int = 0  # raw prior rugs (v9 add — unsmeared by divide-by-N)
    graduated_count: int = 0  # raw prior graduations (v9 add)
    snapshot_prior_tokens: int = (
        0  # n in snapshot (may differ from total_tokens_created)
    )
