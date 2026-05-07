#!/usr/bin/env bash
# scripts/retrain_all_8.sh — sequential rebuild + retrain of all 8 models.
#
# Wave 1 baseline (2026-04-30): runs on existing 88k tokens with current
# live exit config (TP=30/SL=15/MH=120 — see PULSE_EXIT_* env). Each
# step writes its own marker line so the operator can grep progress.
#
# Order matters:
#   1. build_dataset --dataset entry   → entry.parquet (entry_model + reg)
#   2. build_dataset_t30                → entry_t30.parquet
#   3. build_dataset --dataset exit    → exit.parquet (quantile + survival)
#   4. train --dataset entry           → entry_model.ubj
#   5. train --dataset entry --objective regression → entry_model_reg.ubj
#   6. train_entry_t30                  → entry_model_t30.ubj
#   7. train --dataset exit --train-exit-quantile   → 3 quantile heads
#   8. retrain_entry_timing             → entry_timing_model.ubj
#   9. train survival                   → survival_model.ubj
#
# Health checks happen inside each train function (status=ok / degenerate
# / narrow_proba_spread / auc_regression). .prev backups created auto.

set -e
cd /home/sergey/www/gg
set -a; source .env; set +a

LOG=logs/retrain_all_$(date +%Y%m%d_%H%M).log
echo "=== retrain_all_8 START $(date) ===" | tee -a "$LOG"
echo "Live exit config: TP=${PULSE_EXIT_TAKE_PROFIT_PCT}, SL=${PULSE_EXIT_HARD_STOP_LOSS_PCT}, MH=${PULSE_EXIT_MAX_HOLD_SECONDS}" | tee -a "$LOG"
PY=.venv/bin/python

step() {
  echo "" | tee -a "$LOG"
  echo "=== STEP: $1 ($(date +%H:%M:%S)) ===" | tee -a "$LOG"
}

# 1. Entry dataset
step "1/9 build_dataset entry"
PYTHONPATH=. $PY -m pulse_bot.ml.build_dataset --dataset entry 2>&1 | tee -a "$LOG" | tail -10

# 2. T30 dataset
step "2/9 build_dataset_t30"
PYTHONPATH=. $PY -m pulse_bot.ml.build_dataset_t30 2>&1 | tee -a "$LOG" | tail -10

# 3. Exit dataset
step "3/9 build_dataset exit"
PYTHONPATH=. $PY -m pulse_bot.ml.build_dataset --dataset exit 2>&1 | tee -a "$LOG" | tail -10

# 4. Entry model (binary)
step "4/9 train entry classifier"
PYTHONPATH=. $PY -m pulse_bot.ml.train --dataset entry 2>&1 | tee -a "$LOG" | tail -25

# 5. Entry regression
step "5/9 train entry regression"
PYTHONPATH=. $PY -m pulse_bot.ml.train --dataset entry --objective regression 2>&1 | tee -a "$LOG" | tail -25

# 6. T30 model — has its own train function, needs scripted invocation
step "6/9 train entry_t30"
PYTHONPATH=. $PY -c "
from pathlib import Path
from pulse_bot.ml.train import train_entry_t30
train_entry_t30(Path('data/ml/entry_t30.parquet'), Path('data/ml/entry_model_t30.ubj'))
" 2>&1 | tee -a "$LOG" | tail -25

# 7. Exit quantile heads (sl + tp + max_hold)
step "7/9 train exit quantile heads"
PYTHONPATH=. $PY -m pulse_bot.ml.train --dataset exit --train-exit-quantile 2>&1 | tee -a "$LOG" | tail -25

# 8. Entry timing classifier
step "8/9 train entry_timing"
PYTHONPATH=. $PY -m scripts.retrain_entry_timing --max-mints 5000 2>&1 | tee -a "$LOG" | tail -10

# 9. Survival model — needs SurvivalLabelBuilder.build_from_db() output
#    (hazard frame, not raw exit.parquet). Pulls paper_trades from PG
#    and expands each closed trade into bucket-rows with died_in_bucket
#    label. Bug fixed 2026-04-30: prior coordinator passed exit.parquet
#    directly which lacks ``died_in_bucket`` column.
step "9/9 train survival"
PYTHONPATH=. $PY -c "
from pathlib import Path
from pulse_bot.ml.survival import SurvivalLabelBuilder, train_survival_model
df = SurvivalLabelBuilder().build_from_db()
print(f'Built hazard frame: {len(df)} rows, {df.died_in_bucket.sum()} positives')
train_survival_model(df, Path('data/ml/survival_model.ubj'))
" 2>&1 | tee -a "$LOG" | tail -10

echo "" | tee -a "$LOG"
echo "=== retrain_all_8 DONE $(date) ===" | tee -a "$LOG"

# Final health summary
echo "" | tee -a "$LOG"
echo "=== Model meta summary ===" | tee -a "$LOG"
for f in entry_model entry_model_reg entry_model_t30 entry_timing_model exit_quantile_sl exit_quantile_tp exit_quantile_max_hold survival_model; do
  meta="data/ml/${f}.meta.json"
  if [ -f "$meta" ]; then
    echo "--- $f ---" | tee -a "$LOG"
    $PY -c "
import json
m = json.load(open('$meta'))
keys = ['schema_version','auc','precision_top10','spearman_rho','rho','status','threshold_status','positives','positive_rate']
for k in keys:
    if k in m: print(f'  {k}:', m[k])
mh = m.get('model_health')
if mh:
    print(f'  health.status:', mh.get('status'), 'threshold_status:', mh.get('threshold_status'))
" 2>&1 | tee -a "$LOG"
  fi
done
