-- scripts/migrate_multi_config.sql
-- 2026-05-13: multi-config A/B framework migration.
--
-- Adds entry_configs table + paper_trades.config_id column.
-- Backfills existing paper_trades to config_id='LIVE' so historical
-- queries keep working.
--
-- Idempotent — safe to re-run.

BEGIN;

-- 1. entry_configs registry table (source of truth for config history)
CREATE TABLE IF NOT EXISTS entry_configs (
    config_id      TEXT PRIMARY KEY,
    name           TEXT NOT NULL,
    description    TEXT,
    params         JSONB NOT NULL,
    is_active      SMALLINT NOT NULL DEFAULT 1,
    created_at     DOUBLE PRECISION NOT NULL,
    deprecated_at  DOUBLE PRECISION
);

-- 2. paper_trades.config_id column + FK + index
ALTER TABLE paper_trades
    ADD COLUMN IF NOT EXISTS config_id TEXT;

-- 3. Backfill: tag all pre-migration rows as 'LIVE' (the production config).
--    Without this, dashboard's per-config view would show NULL bucket.
UPDATE paper_trades SET config_id = 'LIVE' WHERE config_id IS NULL;

-- 4. Index for per-config aggregates (dashboard hot path).
CREATE INDEX IF NOT EXISTS idx_paper_trades_config ON paper_trades(config_id);

-- 5. (Optional but useful) foreign key — but ONLY after entry_configs rows
--    are populated by the bot's startup upsert. Defer adding FK so this
--    SQL can run before bot starts.
--    To add later:
--      ALTER TABLE paper_trades
--      ADD CONSTRAINT fk_paper_trades_config
--      FOREIGN KEY (config_id) REFERENCES entry_configs(config_id);

COMMIT;
