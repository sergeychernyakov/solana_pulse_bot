"""One-shot migration: create ``shadow_predictions`` table.

Idempotent — safe to run multiple times. Run on rich (production) and
Mac (dev) before enabling any ``PULSE_*_SHADOW=1`` env flags.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import psycopg2
from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parents[1]
load_dotenv(REPO_ROOT / ".env")

DDL = """
CREATE TABLE IF NOT EXISTS shadow_predictions (
    id           BIGSERIAL PRIMARY KEY,
    mint         TEXT             NOT NULL,
    model_name   TEXT             NOT NULL,
    scored_at    DOUBLE PRECISION NOT NULL,
    snapshot_t   DOUBLE PRECISION,
    prediction   JSONB            NOT NULL,
    confidence   DOUBLE PRECISION NOT NULL,
    model_hash   TEXT,
    schema_version TEXT,
    inserted_at  DOUBLE PRECISION NOT NULL DEFAULT EXTRACT(EPOCH FROM NOW())
);

CREATE INDEX IF NOT EXISTS shadow_predictions_mint_idx
    ON shadow_predictions(mint);

CREATE INDEX IF NOT EXISTS shadow_predictions_model_scored_idx
    ON shadow_predictions(model_name, scored_at);
"""


def main() -> int:
    dsn = os.environ.get("PULSE_PG_DSN") or os.environ.get("DATABASE_URL")
    if not dsn or not dsn.startswith("postgres"):
        print("ERROR: PULSE_PG_DSN not set", file=sys.stderr)
        return 1

    with psycopg2.connect(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute(DDL)
        conn.commit()
    print("OK — shadow_predictions table ready")
    return 0


if __name__ == "__main__":
    sys.exit(main())
