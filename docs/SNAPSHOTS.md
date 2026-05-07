# Bot snapshots — known-good restore points

Each snapshot captures **everything needed to bring the bot back to
a specific state**: code (git tag), trained models (`.ubj` binaries),
model metadata (`meta.json`), and runtime knobs (`PULSE_*` env vars).
Secrets (`HELIUS_API_KEYS`, `PULSE_PG_DSN`, `PUMPPORTAL_API_KEY`) are
**not** in the snapshot — they live in the host's `.env` and are
preserved across restore.

## Why three layers?

* **Code** — git tags, lightweight, every commit hash already
  reproducible.
* **Models** — `.ubj` files are not in git (`data/` is in
  `.gitignore`). Trained weights are produced by retrain pipelines
  and represent expensive state that the live bot loads at boot.
  Without these, restoring a tag just gives you the source code; the
  bot would re-train from current data.
* **Config** — `PULSE_*` env vars in `.env` control runtime
  behaviour. Operator changes (lowering `PULSE_SURVIVAL_THRESHOLD`,
  setting `PULSE_ENTRY_MODE=full`) drift independently of code, and
  forgetting to roll them back has caused incidents before.

## Snapshot layout

```
~/backups/gg/<snapshot-name>/
├── models/
│   ├── entry_model.ubj
│   ├── entry_model.meta.json
│   ├── entry_model_t30.ubj
│   ├── entry_model_t30.meta.json
│   ├── entry_model_reg.ubj
│   ├── entry_model_reg.meta.json
│   ├── entry_timing_model.ubj
│   ├── entry_timing_model.meta.json
│   ├── exit_quantile_sl.ubj
│   ├── exit_quantile_sl.meta.json
│   ├── exit_quantile_tp.ubj
│   ├── exit_quantile_tp.meta.json
│   ├── exit_quantile_max_hold.ubj
│   ├── exit_quantile_max_hold.meta.json
│   ├── survival_model.ubj
│   └── survival_model.meta.json
├── env_pulse_vars.txt        # only PULSE_* lines from .env at snapshot time
├── manifest.txt              # sha256sum of every file (verified on restore)
└── restore.sh                # one-shot restore script
```

Mirrored on **rich** (`~/backups/gg/`) and **Mac**
(`~/backups/gg/`) for redundancy.

## Available snapshots

| Tag                                            | Date       | Status              | Notes |
|------------------------------------------------|------------|---------------------|-------|
| `known-good-2026-05-07-paper-profitable`       | 2026-05-07 | Paper profitable    | PnL +2.78 SOL/19h, WR 18.6 %, N=161, all 8 heads OK, survival confidence-gated at 0.50 |

## Creating a new snapshot

```bash
SNAP="known-good-$(date -u +%Y-%m-%d)-<short-description>"
ssh rich "
  set -e
  mkdir -p ~/backups/gg/$SNAP/models
  cp /home/sergey/www/gg/data/ml/*.ubj      ~/backups/gg/$SNAP/models/
  cp /home/sergey/www/gg/data/ml/*.meta.json ~/backups/gg/$SNAP/models/
  grep -E '^PULSE_' /home/sergey/www/gg/.env > ~/backups/gg/$SNAP/env_pulse_vars.txt
  (cd ~/backups/gg/$SNAP && find . -type f ! -name manifest.txt | xargs sha256sum > manifest.txt)
"
# Pull to Mac for redundancy:
rsync -av rich:~/backups/gg/$SNAP ~/backups/gg/
# Tag the matching commit:
git tag -a "$SNAP" -m "Bot snapshot — <reason>"
```

## Restoring from a snapshot

```bash
# 1. Roll code back to the tagged commit:
git checkout known-good-2026-05-07-paper-profitable
ssh rich "cd ~/www/gg && git fetch && git checkout known-good-2026-05-07-paper-profitable"

# 2. Run the restore.sh from the snapshot dir:
ssh rich "bash ~/backups/gg/known-good-2026-05-07-paper-profitable/restore.sh"
```

`restore.sh` does the following automatically:

1. **Stops** `pulse-bot.service`.
2. **Backs up** the current `data/ml/` to
   `data/ml.before-restore.<TS>/` (so the restore is itself
   reversible).
3. **Copies** snapshot models back to `data/ml/`.
4. **Backs up** `.env` to `.env.before-restore.<TS>`.
5. **Replaces** every `PULSE_*` line in `.env` with the snapshot's
   versions; preserves all non-`PULSE_*` lines (secrets stay).
6. **Verifies** sha256 of restored model files against
   `manifest.txt`.
7. **Restarts** the bot and tails 30 lines of `bot.log` so you see
   boot health.

Manual rollback if `restore.sh` produces something worse: each step
left a `.before-restore.<TS>` backup; copy it back and restart.

## Promoting a snapshot to "current good baseline"

If a new snapshot is sustained-better for ≥1 week:

1. Refresh the regression-gate baseline against this snapshot:
   ```bash
   ssh rich "cd ~/www/gg && set -a && source .env && set +a && \
     PYTHONPATH=. .venv/bin/python scripts/regression_gate.py --freeze"
   git add pulse_bot/ml/regression_baseline.json
   git commit -m "Refresh regression baseline @ <snapshot tag>"
   ```
2. Update the table above.

Old snapshots stay available until disk pressure forces cleanup —
each is only ~2 MB, so we can keep many.
