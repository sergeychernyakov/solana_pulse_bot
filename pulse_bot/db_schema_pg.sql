-- pulse_bot/db_schema_pg.sql
-- PostgreSQL schema for pulse_bot.
-- Migrated from SQLite 2026-04-24 to fix database-locked crashes.
--
-- Key differences from SQLite:
-- * REAL → DOUBLE PRECISION (true 64-bit float, no silent truncation).
-- * INTEGER PRIMARY KEY AUTOINCREMENT → BIGSERIAL PRIMARY KEY.
-- * Bool flags stored as INTEGER 0/1 kept as SMALLINT for compactness.
-- * Per codex: ROWID is not stable across VACUUM in SQLite; replaced by
--   explicit BIGSERIAL `insert_order` column on tokens + trades for
--   deterministic "as-of" joins.
-- * JSON-ish blobs (data_json, trades_json, evidence_json, ml_feature_vector,
--   reasons) → JSONB. Faster to parse + GIN-indexable.

CREATE TABLE IF NOT EXISTS tokens (
    insert_order BIGSERIAL UNIQUE,
    mint TEXT PRIMARY KEY,
    name TEXT, symbol TEXT, creator TEXT,
    created_at DOUBLE PRECISION, uri TEXT, launchpad TEXT DEFAULT 'pumpfun'
);
CREATE INDEX IF NOT EXISTS idx_tokens_created ON tokens(created_at);
CREATE INDEX IF NOT EXISTS idx_tokens_creator ON tokens(creator);

CREATE TABLE IF NOT EXISTS trades (
    id BIGSERIAL PRIMARY KEY,
    mint TEXT NOT NULL, wallet TEXT NOT NULL, tx_type TEXT NOT NULL,
    sol_amount DOUBLE PRECISION, token_amount DOUBLE PRECISION,
    market_cap_sol DOUBLE PRECISION, v_sol_in_bonding_curve DOUBLE PRECISION,
    timestamp DOUBLE PRECISION, is_creator SMALLINT DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_trades_mint_ts ON trades(mint, timestamp);
CREATE INDEX IF NOT EXISTS idx_trades_ts ON trades(timestamp);
CREATE INDEX IF NOT EXISTS idx_trades_wallet_ts ON trades(wallet, timestamp);

CREATE TABLE IF NOT EXISTS token_scores (
    mint TEXT,
    source TEXT DEFAULT 'live',
    symbol TEXT, name TEXT, creator TEXT,

    total_score INTEGER, decision TEXT,
    fast_decision TEXT DEFAULT '', fast_score INTEGER DEFAULT 0,
    fast_reasons TEXT DEFAULT '', reasons TEXT,

    fast_buy_count INTEGER DEFAULT 0, fast_volume_sol DOUBLE PRECISION DEFAULT 0.0,
    fast_buy_rate DOUBLE PRECISION DEFAULT 0.0, fast_unique_buyers INTEGER DEFAULT 0,
    fast_sell_ratio DOUBLE PRECISION DEFAULT 0.0, fast_elapsed DOUBLE PRECISION DEFAULT 0.0,
    fast_scored_at DOUBLE PRECISION DEFAULT 0.0, fast_entry_price DOUBLE PRECISION DEFAULT 0.0,
    pnl_at_fast_entry_pct DOUBLE PRECISION DEFAULT 0.0,

    buy_count INTEGER DEFAULT 0, sell_count INTEGER DEFAULT 0,
    unique_buyers INTEGER DEFAULT 0, unique_sellers INTEGER DEFAULT 0,
    buy_volume_sol DOUBLE PRECISION DEFAULT 0.0, sell_volume_sol DOUBLE PRECISION DEFAULT 0.0,
    buy_diversity INTEGER DEFAULT 0, max_buy_sol DOUBLE PRECISION DEFAULT 0.0,
    creator_sold SMALLINT DEFAULT 0, sell_pressure DOUBLE PRECISION DEFAULT 0.0,
    avg_buy_sol DOUBLE PRECISION DEFAULT 0.0, median_buy_sol DOUBLE PRECISION DEFAULT 0.0,
    std_buy_sol DOUBLE PRECISION DEFAULT 0.0, top3_buyer_pct DOUBLE PRECISION DEFAULT 0.0,
    repeat_buyer_count INTEGER DEFAULT 0, first_buy_sol DOUBLE PRECISION DEFAULT 0.0,
    buy_velocity_trend DOUBLE PRECISION DEFAULT 0.0, buy_size_trend DOUBLE PRECISION DEFAULT 0.0,
    time_to_first_buy DOUBLE PRECISION DEFAULT 0.0, buys_per_unique DOUBLE PRECISION DEFAULT 0.0,

    first_half_buy_rate DOUBLE PRECISION DEFAULT 0.0,
    second_half_buy_rate DOUBLE PRECISION DEFAULT 0.0,
    avg_first_half_buy_sol DOUBLE PRECISION DEFAULT 0.0,
    avg_second_half_buy_sol DOUBLE PRECISION DEFAULT 0.0,
    time_gap_median_first20 DOUBLE PRECISION DEFAULT 0.0,
    buy_volume_first10s DOUBLE PRECISION DEFAULT 0.0,
    unique_buyers_first30s INTEGER DEFAULT 0,
    unique_buyers_last30s INTEGER DEFAULT 0,
    curve_progress_at_t30 DOUBLE PRECISION DEFAULT 0.0,
    curve_progress_at_t60 DOUBLE PRECISION DEFAULT 0.0,
    curve_progress_at_t90 DOUBLE PRECISION DEFAULT 0.0,

    curve_progress_pct DOUBLE PRECISION DEFAULT 0.0, curve_velocity DOUBLE PRECISION DEFAULT 0.0,
    curve_acceleration DOUBLE PRECISION DEFAULT 0.0, sol_to_graduation DOUBLE PRECISION DEFAULT 0.0,
    market_cap_sol DOUBLE PRECISION DEFAULT 0.0,

    token_price_sol DOUBLE PRECISION DEFAULT 0.0, exit_price DOUBLE PRECISION DEFAULT 0.0,
    pnl_5th_pct DOUBLE PRECISION DEFAULT 0.0, pnl_10th_pct DOUBLE PRECISION DEFAULT 0.0,
    pnl_20th_pct DOUBLE PRECISION DEFAULT 0.0, pnl_50th_pct DOUBLE PRECISION DEFAULT 0.0,
    pnl_100th_pct DOUBLE PRECISION DEFAULT 0.0,

    name_length INTEGER DEFAULT 0, symbol_length INTEGER DEFAULT 0,
    has_uri SMALLINT DEFAULT 0, is_all_caps SMALLINT DEFAULT 0,
    has_numbers SMALLINT DEFAULT 0,

    hour_utc INTEGER DEFAULT 0, creator_tokens_today INTEGER DEFAULT 0,
    gap_create_to_first_trade DOUBLE PRECISION DEFAULT 0.0,

    tokens_last_5min INTEGER DEFAULT 0, concurrent_observations INTEGER DEFAULT 0,

    current_price DOUBLE PRECISION DEFAULT 0.0,
    live_pnl_pct DOUBLE PRECISION DEFAULT 0.0,
    price_updated_at DOUBLE PRECISION DEFAULT 0.0,

    fast_trade_count INTEGER DEFAULT 0,
    full_trade_count INTEGER DEFAULT 0,
    fast_trade_ids TEXT DEFAULT '',
    full_trade_ids TEXT DEFAULT '',
    creator_score INTEGER DEFAULT 0,
    creator_reason TEXT DEFAULT '',

    -- ML shadow logging
    ml_entry_proba DOUBLE PRECISION,
    ml_model_hash TEXT,
    ml_feature_vector TEXT,
    ml_feature_schema TEXT,

    -- SOL price context (Phase A1)
    sol_price_usd DOUBLE PRECISION,

    created_at DOUBLE PRECISION, scored_at DOUBLE PRECISION,

    PRIMARY KEY (mint, source)
);
CREATE INDEX IF NOT EXISTS idx_token_scores_scored ON token_scores(scored_at);
CREATE INDEX IF NOT EXISTS idx_token_scores_decision ON token_scores(decision);
CREATE INDEX IF NOT EXISTS idx_token_scores_fast ON token_scores(fast_decision);
CREATE INDEX IF NOT EXISTS idx_token_scores_creator ON token_scores(creator);

CREATE TABLE IF NOT EXISTS creators (
    wallet TEXT PRIMARY KEY,
    total_tokens_created INTEGER DEFAULT 0, times_seen INTEGER DEFAULT 0,
    tokens_where_creator_sold_early INTEGER DEFAULT 0,
    first_seen_at DOUBLE PRECISION, last_seen_at DOUBLE PRECISION,
    blacklisted SMALLINT DEFAULT 0
);

CREATE TABLE IF NOT EXISTS event_log (
    id BIGSERIAL PRIMARY KEY,
    event_type TEXT, data TEXT, timestamp DOUBLE PRECISION
);

CREATE TABLE IF NOT EXISTS optimization_runs (
    run_id TEXT PRIMARY KEY,
    optimizer_session TEXT,
    params TEXT,
    entry_mode TEXT,
    total_trades INTEGER, wins INTEGER, losses INTEGER,
    win_rate DOUBLE PRECISION, total_pnl_sol DOUBLE PRECISION,
    gross_profit_sol DOUBLE PRECISION, gross_loss_sol DOUBLE PRECISION,
    profit_factor DOUBLE PRECISION,
    avg_win_pct DOUBLE PRECISION, avg_loss_pct DOUBLE PRECISION,
    avg_win_sol DOUBLE PRECISION, avg_loss_sol DOUBLE PRECISION,
    max_drawdown_pct DOUBLE PRECISION,
    initial_balance_sol DOUBLE PRECISION, final_balance_sol DOUBLE PRECISION,
    roi_pct DOUBLE PRECISION,
    avg_hold_seconds DOUBLE PRECISION,
    fast_buys INTEGER, full_buys INTEGER,
    exit_reasons TEXT,
    trades_json TEXT,
    created_at DOUBLE PRECISION
);
CREATE INDEX IF NOT EXISTS idx_opt_runs_session ON optimization_runs(optimizer_session);
CREATE INDEX IF NOT EXISTS idx_opt_runs_pf ON optimization_runs(profit_factor);

CREATE TABLE IF NOT EXISTS optimization_trades (
    id BIGSERIAL PRIMARY KEY,
    run_id TEXT,
    mint TEXT, symbol TEXT,
    entry_type TEXT, exit_reason TEXT,
    entry_price DOUBLE PRECISION, exit_price DOUBLE PRECISION,
    entry_time DOUBLE PRECISION, exit_time DOUBLE PRECISION,
    sol_invested DOUBLE PRECISION, sol_received DOUBLE PRECISION,
    pnl_sol DOUBLE PRECISION, pnl_pct DOUBLE PRECISION,
    hold_seconds DOUBLE PRECISION, partial_sells INTEGER
);
CREATE INDEX IF NOT EXISTS idx_opt_trades_run ON optimization_trades(run_id);

CREATE TABLE IF NOT EXISTS paper_trades (
    id BIGSERIAL PRIMARY KEY,
    mint TEXT NOT NULL,
    symbol TEXT,
    status TEXT DEFAULT 'open',

    entry_price DOUBLE PRECISION,
    entry_time DOUBLE PRECISION,
    entry_mcap_sol DOUBLE PRECISION DEFAULT 0.0,
    entry_buyer_number INTEGER DEFAULT 0,
    entry_type TEXT DEFAULT 'full',
    entry_score INTEGER DEFAULT 0,

    current_price DOUBLE PRECISION DEFAULT 0.0,
    current_pnl_pct DOUBLE PRECISION DEFAULT 0.0,
    current_mcap_sol DOUBLE PRECISION DEFAULT 0.0,
    total_buys INTEGER DEFAULT 0,
    total_sells INTEGER DEFAULT 0,
    price_updated_at DOUBLE PRECISION DEFAULT 0.0,

    exit_price DOUBLE PRECISION DEFAULT 0.0,
    exit_time DOUBLE PRECISION DEFAULT 0.0,
    exit_reason TEXT DEFAULT '',
    exit_buyer_number INTEGER DEFAULT 0,
    exit_mcap_sol DOUBLE PRECISION DEFAULT 0.0,
    pnl_pct DOUBLE PRECISION DEFAULT 0.0,
    pnl_sol DOUBLE PRECISION DEFAULT 0.0,
    hold_seconds DOUBLE PRECISION DEFAULT 0.0,

    buy_amount_sol DOUBLE PRECISION DEFAULT 0.03
);
CREATE INDEX IF NOT EXISTS idx_paper_status ON paper_trades(status);
CREATE INDEX IF NOT EXISTS idx_paper_mint ON paper_trades(mint);
CREATE INDEX IF NOT EXISTS idx_paper_entry_time ON paper_trades(entry_time);

CREATE TABLE IF NOT EXISTS live_decisions (
    mint TEXT PRIMARY KEY,
    symbol TEXT,
    fast_decision TEXT,
    fast_score INTEGER,
    full_decision TEXT,
    full_score INTEGER,
    buy_count INTEGER,
    unique_buyers INTEGER,
    buy_volume_sol DOUBLE PRECISION,
    created_at DOUBLE PRECISION,
    decided_at DOUBLE PRECISION
);

CREATE TABLE IF NOT EXISTS creator_snapshots (
    id BIGSERIAL PRIMARY KEY,
    creator TEXT NOT NULL,
    observed_at DOUBLE PRECISION NOT NULL,
    computed_through_ts DOUBLE PRECISION NOT NULL,
    api_source TEXT NOT NULL,
    feature_version INTEGER DEFAULT 1,
    total_prior_tokens INTEGER DEFAULT 0,
    rug_count INTEGER DEFAULT 0,
    graduated_count INTEGER DEFAULT 0,
    rug_rate DOUBLE PRECISION DEFAULT 0.0,
    graduation_rate DOUBLE PRECISION DEFAULT 0.0,
    median_peak_mc_sol DOUBLE PRECISION DEFAULT 0.0,
    avg_ttl_sec DOUBLE PRECISION DEFAULT 0.0,
    inter_token_interval_sec DOUBLE PRECISION DEFAULT 0.0,
    creator_age_days DOUBLE PRECISION DEFAULT 0.0,
    creator_balance_sol DOUBLE PRECISION DEFAULT 0.0,
    data_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_creator_snap_creator_obs
    ON creator_snapshots(creator, observed_at);

CREATE TABLE IF NOT EXISTS token_holders_snapshots (
    id BIGSERIAL PRIMARY KEY,
    mint TEXT NOT NULL,
    observed_at DOUBLE PRECISION NOT NULL,
    capture_at_age_sec DOUBLE PRECISION NOT NULL,
    total_supply_raw TEXT,
    top1_raw TEXT,
    top5_raw TEXT,
    top10_raw TEXT,
    top1_pct DOUBLE PRECISION,
    top5_pct DOUBLE PRECISION,
    top10_pct DOUBLE PRECISION,
    holder_count INTEGER,
    is_partial SMALLINT DEFAULT 0,
    is_negative_row SMALLINT DEFAULT 0,
    api_source TEXT DEFAULT 'helius'
);
CREATE INDEX IF NOT EXISTS idx_holder_snap_mint_obs
    ON token_holders_snapshots(mint, observed_at);
CREATE INDEX IF NOT EXISTS idx_holder_snap_mint_age
    ON token_holders_snapshots(mint, capture_at_age_sec);

CREATE TABLE IF NOT EXISTS holder_capture_failures (
    id BIGSERIAL PRIMARY KEY,
    mint TEXT NOT NULL,
    attempted_at DOUBLE PRECISION NOT NULL,
    target_age_sec DOUBLE PRECISION NOT NULL,
    error_type TEXT NOT NULL,
    error_detail TEXT
);
CREATE INDEX IF NOT EXISTS idx_holder_fail_attempt
    ON holder_capture_failures(attempted_at);

CREATE TABLE IF NOT EXISTS creator_flag_history (
    id BIGSERIAL PRIMARY KEY,
    creator TEXT NOT NULL,
    flag TEXT NOT NULL,
    reason TEXT,
    source TEXT DEFAULT 'auto',
    valid_from DOUBLE PRECISION NOT NULL,
    valid_to DOUBLE PRECISION,
    posterior_rug_prob DOUBLE PRECISION,
    evidence_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_creator_flag_creator_valid
    ON creator_flag_history(creator, valid_from, valid_to);

CREATE TABLE IF NOT EXISTS wallet_activity (
    wallet TEXT NOT NULL,
    mint TEXT NOT NULL,
    first_buy_ts DOUBLE PRECISION NOT NULL,
    last_trade_ts DOUBLE PRECISION NOT NULL,
    buy_volume_sol DOUBLE PRECISION DEFAULT 0.0,
    sell_volume_sol DOUBLE PRECISION DEFAULT 0.0,
    realized_pnl_sol DOUBLE PRECISION,
    is_closed_at_ingest_time SMALLINT DEFAULT 0,
    updated_at DOUBLE PRECISION DEFAULT 0.0,
    PRIMARY KEY (wallet, mint)
);
CREATE INDEX IF NOT EXISTS idx_wallet_activity_wallet_last_ts
    ON wallet_activity(wallet, last_trade_ts);
CREATE INDEX IF NOT EXISTS idx_wallet_activity_mint
    ON wallet_activity(mint);
