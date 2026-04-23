#!/usr/bin/env bash
# scripts/run_daily_validation.sh
# Cron runner for pulse_bot daily ML validation.
# Invoked by crontab; wraps venv activation + working directory.
set -euo pipefail

REPO="/Users/sergeychernyakov/www/gg"
cd "$REPO"

# shellcheck disable=SC1091
source "$REPO/.venv/bin/activate"

exec python -m pulse_bot.ml.daily_validation \
    --kind both \
    --db "$REPO/pulse_bot.db" \
    --model-dir "$REPO/data/ml" \
    --report-dir "$REPO/data/ml/reports" \
    --fail-on-alert
