#!/usr/bin/env bash
# scripts/run_weekly_retrain.sh
# Cron runner for pulse_bot weekly retrain + snapshot + noise floor.
set -euo pipefail

REPO="/Users/sergeychernyakov/www/gg"
cd "$REPO"

# shellcheck disable=SC1091
source "$REPO/.venv/bin/activate"

exec python -m pulse_bot.ml.weekly_retrain \
    --db "$REPO/pulse_bot.db" \
    --data-dir "$REPO/data/ml" \
    --history-dir "$REPO/data/ml/history"
