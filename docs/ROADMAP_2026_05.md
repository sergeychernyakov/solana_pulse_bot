# Roadmap 2026-05 — Phase 0-6 + Architecture Debt

**Drafted:** 2026-04-24
**Last updated:** 2026-04-28

---

## 🏗 Architecture Debt (new 2026-04-28 after codex review)

Codex review revealed that pipeline.py / db.py have outgrown their initial form. All 5 issues from the latest review (entry metadata, event-time watermark, T+30 wallet wiring, ML override polluted rows, helius backfill completeness) live **inside god-objects**. List of architectural tasks by priority:

### Phase A — Critical invariants & test contracts (1-2 days)
- [ ] **Mutation tests on key invariants:**
  - `hard_exits_cannot_be_blocked_by_ml` — ML override cannot block creator_dump / hard_stop / max_hold
  - `replay_cannot_see_trade_outside_event_window` — checkpoint snapshots use event-time, not arrival time
  - `incomplete_backfill_cannot_be_marked_complete` — Helius partial fetch does not land in completed_mints
- [ ] **Contract tests by level:** source parity, feature parity, entry decision parity, exit parity, checkpoint parity
- [ ] **Restart/resume semantics test** — open positions are correctly restored after restart

### Phase B — Pipeline.py decomposition (3-5 days)
- [ ] Split into 3 modules:
  - `ObservationSession` — intake + scheduling + holder capture + scoring
  - `DecisionService` — decide_entry + ML override + checkpoint hooks
  - `PaperTradeSupervisor` — open / monitor / close paper positions
- [ ] Extract `RuntimeContext` dataclass — a single container (token, collected, holder_snapshot, creator_snapshot, wallet_prior_stats) instead of scattered arguments
- [ ] Move feature flags from `os.environ.get(...)` to a `RuntimeFlags` typed config

### Phase C — db.py → bounded-context repos (2-3 days)
- [ ] Split into:
  - `TradesRepo` — ingestion + analytics
  - `ScoresRepo` — token_scores read/write
  - `PaperTradesRepo` — paper_trades CRUD
  - `WalletStatsRepo` — wallet_activity + wallet_classifications
  - `SnapshotsRepo` — holder + creator snapshots
- [ ] **Async-safe reads** on the hot path: remove `asyncio.to_thread(db._sync_query)` from pipeline.py:283/677/761
- [ ] Read-replicas: split read/write pools

### Phase D — One canonical backtest runtime (1-2 days)
- [ ] Pick `Pipeline + ReplayLaunchpad` as source of truth
- [ ] `pulse_bot/backtest.py` → delete or turn into a thin test harness
- [ ] Remove monkeypatch-style backtest path assembly in `main.py:205`

### Phase E — ModelRegistry + dataset lineage (1-2 days)
- [ ] `ModelRegistry` class with a manifest per model: schema_version, config_hash, train window, activation mode, expected features, model_health
- [ ] Dataset lineage: which backfill/enrichment state produced a particular model
- [ ] Replace `Path("data/ml/...")` with `registry.get(name)`

### Phase F — Feature hydration service (2 days)
- [ ] Move creator + holder + wallet prior + SOL price + onchain state assembly out of Pipeline
- [ ] `FeatureHydrationService` with explicit completeness flags
- [ ] Online vs Analytics schema separation: `ScoringResult` currently serves both online and warehouse

### Phase G — features.py / policy.py split (1-2 days)
- [ ] `features/__init__.py`, `features/entry.py`, `features/entry_t30.py`, `features/timing.py`, `features/exit.py`
- [ ] `policy/__init__.py`, `policy/entry.py`, `policy/exit.py` etc.
- [ ] A single shared module is outdated with 5+ models

### Phase H — Observability (1 day)
- [ ] System metrics via Prometheus / pushgateway:
  - queue depth, token processing latency
  - DB read latency, holder capture lag
  - percent incomplete snapshots
  - ML inference failure rate, parity mismatch rate
- [ ] Operational alerts vs research diagnostics — separate

### Phase I — Backpressure + idempotency (can be deferred)
- [ ] Bounded queues in MultiplexerLaunchpad (currently unbounded)
- [ ] Explicit business keys for ingestion idempotency (not "best effort dedup")

### Phase J — Typed enums (non-blocker)
- [ ] `EntryAction.BUY / SKIP / RULES / DEFER` enum instead of strings
- [ ] `EntryType.FULL / FAST / ML_OVERRIDE / T30 / TIMING` enum

### Execution order (by priority)
1. **Phase A** — tests now, before any refactors. Otherwise the regression risk is too high.
2. **Phase B** — Pipeline split. Highest cost/benefit.
3. **Phase C** — async reads (will keep biting us repeatedly).
4. **Phase D** — backtest consolidation. Cheap, drift-killer.
5. **Phase F** — Feature hydration (helps D and G).
6. **Phase E** — Registry.
7. **Phase G** — features/policy split.
8. **Phase H** — Observability.
9. The rest as needed.

**Rationale:** Initially I believed architectural debt "could wait until the model proved viability". Codex is right: **each next change keeps getting more expensive** on god-objects. We do this now in parallel with the models, not instead of them.

### Snapshot 2026-04-28 (actual progress)

```
✅ Phase A — Critical invariants  (17 mutation tests)
              tests/pulse_bot/test_critical_invariants.py
✅ Phase D — Backtest consolidation
              main.py removed dead BacktestEngine import
              pulse_bot/backtest.py marked as parity-only legacy
✅ Phase F — FeatureHydrationService  (8 tests)
              pulse_bot/feature_hydration.py
              pipeline.py pulls from one entry point at T+90 + T+30
✅ Phase B step 1 — DecisionService  (18 tests)
              pulse_bot/decision_service.py
              pipeline.py thinner by ~150 lines
              EntryDecision frozen dataclass for immutable chain state
✅ Phase B step 2 — PaperTradeSupervisor (relocated, no behavior change)
              pulse_bot/paper_trade_supervisor.py
              pipeline.py thinner by ~290 lines (now 1712 vs 2003)
✅ Phase E — ModelRegistry  (13 tests)
              pulse_bot/ml/model_registry.py
              boot summary in bot.log shows full ensemble health
✅ Phase H — Observability /metrics  (10 tests)
              pulse_bot/observability.py
              port 9100 exposes Prometheus exposition format
              counters: tokens_scored, paper_trades_opened/closed,
                        ml_override (action), ml_inference_failures,
                        parity_mismatches
              gauges:   open_paper_trades, model_health (per-model)
              histograms: holder_capture_lag, db_read_latency,
                          token_processing_latency

📊 Total: 95 tests pass on Mac+rich. Pipeline.py reduced 600+ lines.
   ML Override and entry-decision logic now isolated, testable.
```

### Deferred to focused multi-day session

These need dedicated time + thorough manual smoke testing because they
break import contracts or restructure persistence:

- **Phase B step 3 — ObservationSession**: split ``Pipeline._handle_token``
  (still ~800 lines) into intake + scoring + observation lifecycle.
  Touches the hot path; one mistake = no live trades. Approach:
  extract one closure at a time (e.g. ``_collect_trades``,
  ``_run_fast_phase``, ``_run_full_phase``) into named methods first;
  only extract to a separate class once the seams are clear.
- **Phase C — db.py → repos**: ``Database`` is 1348 lines with shared
  asyncpg + psycopg2 pools. Splitting into ``TradesRepo``,
  ``ScoresRepo``, ``PaperTradesRepo``, ``WalletStatsRepo``,
  ``SnapshotsRepo`` requires:
    1. Decide pool ownership (one shared, or per-repo).
    2. Migrate live callers gradually behind a façade.
    3. Async-safe reads (codex MAJOR finding) are the actual win;
       restructuring without that is cosmetic.
- **Phase G — features.py / policy.py split**: 971 + 1034 lines. Pure
  mechanical restructure. Each consumer imports many symbols by name
  from the modules; a package with __init__.py re-exports works but
  needs each test exercised. Defer until next adding a new model.
- **Phase I — Backpressure + idempotency**: bounded queues in
  multiplexer launchpad; explicit business keys (we partly addressed
  this in codex Issue #5 dedup-key fix).
- **Phase J — Typed enums**: ``EntryAction`` / ``EntryType``. Pure
  ergonomic improvement, no behavioral change. Lowest priority.

---


**Status v17→v18 (current):** AUC 0.96, Prec@top10% 7.97%, N=74,714 scored, base rate 0.81%.
6 ML heads ready (entry main / entry @T+30 / entry-timing / survival / SL+TP quantile heads).
Phase 0 deployed — bot is accumulating post-scoring trades.

**Main principle (from codex):** **do not stack ML heads on top of a weak base
model.** First strengthen the foundation (more data + multi-snapshot
signal), then advanced heads. ✅ All code phases done 2026-04-25; waiting on
Phase 0 data accumulation for validation.

**ML strategy — train-all + opportunistic gating** (2026-04-26):
- Train **all** models on every retrain cycle (entry / entry_t30 / entry_reg / entry_timing / exit / exit_quantile_sl,tp / survival).
- Each model **returns confidence** (proba / coverage / hazard variance / spearman).
- **Activate a model** only if its training quality clears the bar (AUC > 0.85 + P@top10% > 5×base, or quantile coverage within ±0.03 of target, etc.).
- At runtime: **per-prediction confidence gate** — a model opts in on a specific decision only at high confidence; otherwise it defers to RULES or to another model.
- Never override RULES if all ML are low-confidence.
- Ensemble pattern (not stacking) — each model is auditable independently. The pattern already works for entry_model (floor=0.1 / ceiling=0.6 / middle=RULES).
- Details: `~/.claude/projects/-Users-sergeychernyakov-www-gg/memory/project_ml_ensemble_opportunistic_gating.md`.

**Quick status:**
- ✅ Phase −1A (deploy on rich server) — DONE 2026-04-25
- ✅ Phase −1B (DB Rich↔Mac sync) — on-demand, documented in README
- 🔨 Phase −1A2 (local Solana RPC node) — agave-validator building, snapshot sync next
- ⏳ Phase −1A2-extra (Geyser gRPC plugin) — after local node stabilizes, **gRPC primary + PumpPortal WS as fallback** (not a replacement, a backup)
- ⏳ Phase −1C (latency benchmark AWS/Hetzner) — planning
- ⏳ Phase −1D (real-money trading) — blocked on positive economic_backtest
- ✅ Phase 0 (extended observation) — DEPLOYED, accumulating
- ✅ Phase 4A (timer tick) — DEPLOYED default ON
- ✅ Phase 4B (survival) — CODE READY, default OFF
- ✅ Phase 2.5 (time-aware features) — DEPLOYED schema v18
- ✅ Phase 3 (T+30 model) — CODE READY, default OFF
- ✅ Phase 5 (entry-timing) — CODE READY, default OFF
- ⏳ Phase 1 (sanity check) — blocked on N≥100 closed paper trades
- ⏳ Phase 2 (foundation retrain) — blocked on Phase 0 data
- ⏳ Phase 6 (TP quantile) — deferred to N≥3000

---

## Phase −1 — Production infrastructure (new 2026-04-25 21:00)

**Status:**
- ✅ Bot deployed on rich (192.168.3.118, Ubuntu, 125GB RAM, PG 16)
- ⏳ DB sync Rich ↔ Mac (currently a one-off dump, need live replication)
- ⏳ Latency benchmark: AWS / Hetzner / various zones for best Solana latency
- ⏳ Real money trading — transition from paper mode to actual SOL transactions

### A. DB synchronization Rich ↔ Mac (on-demand)

**Goal:** keep both DBs in sync for:
- Rich = production (bot writes there)
- Mac = dev/analysis (need a fresh snapshot for retrain, sweeps, research)

**Approach:** manual sync on request — when fresh data is needed on the Mac for retrain/sweep, pull a dump with a single command. No automation (cron is unnecessary — extra load, irregular need).

```bash
# Migrate a fresh snapshot Rich → Mac (one command):
ssh rich 'pg_dump -U sergeychernyakov -d pulse_bot -F c -Z 9' > /tmp/rich.dump \
  && pg_restore --clean --no-owner -d pulse_bot /tmp/rich.dump
# Time: ~5 min for a 3GB DB over LAN
```

**Alternatives (if on-demand stops being sufficient):**
- PostgreSQL streaming replication: Rich primary → Mac standby (≤1s lag)
- Logical replication: only the necessary tables (trades, token_scores)

### A2. Geyser gRPC plugin (after local Solana node)

**Trigger:** local Solana RPC node running stably (after Phase −1A2 setup).

**Idea:** move the live source to self-hosted streaming via the Geyser gRPC plugin (primary), with PumpPortal WS as fallback. The validator pushes ALL on-chain events (slot/account/transaction updates) to the client via gRPC.

**Advantages over PumpPortal WS:**
- **Latency**: ~1-5ms (local validator → process) vs 50-200ms (third-party WS)
- **Reliability**: no dependency on an external service
- **Coverage**: ALL events, not only pump.fun (can filter on the client side)
- **No quotas**: unlimited throughput
- **Future-proof**: scales to letsbonk and any other launchpads without separate WS

**Architecture — gRPC primary + WS fallback (NOT a replacement, but main + backup):**

We don't drop PumpPortal WS — we keep it as a **fallback**. This gives a resilience zone when:
- our validator falls behind tip (catching up after restart / network hiccup)
- the gRPC plugin crashes or streaming breaks
- we accidentally hit a bug in the custom decoder

**Logic:**
1. Live source: subscribe to gRPC + WS in parallel, dedupe by `(mint, signature)`.
2. Health probe: every 5s check `getSlot` on the local node — if it lags >2 slots behind mainnet (or last gRPC event >5s ago) → automatic fallback to WS-only.
3. Metrics `geyser_lag_slots` + `geyser_events_per_min` in logs/dashboard for monitoring.
4. Env: `PULSE_LAUNCHPAD=geyser+pumpportal` (new mode); fallback = existing `PumpFunLaunchpad` (`wss://pumpportal.fun/api/data`).

**What is needed:**
1. Install the Yellowstone Geyser plugin on our local validator (`--geyser-plugin-config <yaml>`).
2. Write a Python gRPC subscriber `pulse_bot/launchpads/geyser.py`:
   - Subscribe filter on the pump.fun program ID (`6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P`).
   - Decode trade events from `Transaction.meta.logMessages` + `tokenBalances`.
   - Implement the same `Launchpad.stream_trades` interface as PumpPortal WS.
3. Add `pulse_bot/launchpads/multiplexer.py`:
   - Merges events from gRPC (primary) and PumpPortal WS (fallback), dedupe cache on a 60s window by `(mint, signature)`.
   - Health-check + automatic primary/fallback switchover.
4. Env switch `PULSE_LAUNCHPAD=geyser+pumpportal` to migrate the bot.

**Effort:** 2-3 days (setup plugin + subscriber + multiplexer + dedup + tests + verification).

**Cost:** a little RAM/CPU on the validator + the multiplexer holds 2 connections in normal mode (minor).

**Use cases:**
- Live bot: gRPC primary → WS fallback. If the local node dies — the bot won't lose events.
- Backfill historical: via local RPC (current path).
- Geyser for new tokens, RPC for history — a combo.

### B. Latency benchmark for Solana

**Goal:** find a region with the min RTT to Solana mainnet RPC (reliability is critical for real-money trading).

**Candidates:**
- AWS: us-east-1, us-west-2, eu-west-1, ap-southeast-1
- Hetzner: nbg1 (Nuremberg), fsn1 (Falkenstein), hel1 (Helsinki), ash1 (Ashburn)
- Current: home network over WiFi → home ISP

**Metrics:**
- p50/p95/p99 RTT to mainnet.helius-rpc.com
- p50/p95/p99 for PumpPortal WS event delivery
- Cost per month

**Method:**
```bash
# Spin up 1-hour test instance per zone:
# Run: 1000 × {ping helius-rpc, send dummy POST, measure latency}
# Compare distributions
```

### C. Real-money trading deployment

**Trigger:** ML model shows positive economic_backtest on accumulated Phase 0 data (≥+0.3 SOL/week kill criterion from memory).

**What is needed:**
1. **Wallet creation** — a real keypair, initial balance ($100-500)
2. **Replace `PaperTradeRunner` with `LiveExecution`**:
   - Real Solana TX submission (jupiter / pump.fun direct)
   - Slippage protection
   - MEV protection (Helius staked connection or Jito bundles)
3. **Risk limits**:
   - Max position size (e.g. 0.05 SOL/trade)
   - Daily drawdown stop ($-50 → halt bot)
   - Max consecutive losses → pause
4. **Monitoring**:
   - Alert (Telegram?) on every fill, every error, daily PnL
   - Wallet balance reconciliation
5. **Compliance / accounting**: TX logs for tax reporting

**Do not launch without:**
- Paper trading >7 days with positive realized PnL
- ML model passed an honest economic_backtest > 0
- Latency benchmark showed stable ≤200ms RTT
- Real-money checklist signed off

---

## ML retrain + activation cadence (plan 2026-04-26)

**Context:** on 2026-04-26 retrained all 8 models on fresh data. The train-all-train-fast paradigm works (~30 min sequentially). Activation follows the memory entry `project_ml_ensemble_opportunistic_gating.md`.

**Stage 1 — Activation v0 (2026-04-26 19:35) ✅ DONE:**
- `pulse-bot.service` restarted, picked up fresh `entry_model.ubj` + `exit_model.ubj`.
- `entry_model` model_hash=3c9a34eb (new Apr 26).
- `exit_model` ACTIVATED for the first time — threshold=0.80 min_hold=15s.

**Stage 2 — Sanity + Shadow infrastructure (1-2 days):**
- 5-seed `feature_stability.py` on entry_model — early warning about regime drift / unstable features. Run **immediately after Stage 1** (launched).
- Implement **shadow logging** in `pipeline.py`: for every scored token run t30/timing/survival/quantile_sl/tp in predict-only mode, write `(model_name, mint, prediction, confidence, scored_at)` to a new `shadow_predictions` table. **Does not affect the decision** — pure data collection.
- Compare via `daily_validation.py`: shadow predictions vs realized outcomes on the same ML scoring window.

**Stage 3 — Production validation (7-14 days shadow):**
- Every day compute production metrics for each shadow model:
  - t30: AUC + P@top10% on closed paper_trades
  - timing: confusion matrix (was BUY_NOW correct in retrospect?)
  - survival: predicted remaining-life vs actual exit time correlation
  - quantile: production coverage (% of trades within predicted q25..q75 PnL band)
- Critical bar: **production metrics ≥ training metrics × 0.7** + Spearman > 0.15.

**Stage 4 — Activate passers (based on production validation):**
- For models that pass — set the env flag (PULSE_ENTRY_T30_ACTIVE / PULSE_TIMING_ACTIVE / PULSE_SURVIVAL_ACTIVE / PULSE_EXIT_REGRESSION_ACTIVE).
- For models that fail — keep shadow or off + investigate (regime drift / data leakage / overfitting to a small seed).
- Every activation = a separate CHANGELOG entry with before/after metrics.

**Don't-activate-on-train-metrics principle:** training metrics can lie (single-seed split, regime drift, overfitting). Production track record = real truth. See `feedback_provisional_vs_closed`: with N<500 positives, a single-holdout ΔAUC <2×SE = noise.

### TODO: repeat feature_stability seed run + cleanup stable_dead

**Trigger:** **5-7 days** after the 2026-04-26 retrain (i.e. ~2026-05-01..2026-05-03).

**Why:**
The 2026-04-26 comparison of `feature_stability_v18_seed5.json` (Apr 25) vs `..._apr26.json` (Apr 26) showed:
- AUC mean 0.9620 → 0.8995 (−6.2pp, 5× above noise threshold) — **real regime drift**
- AUC stdev ×6 (0.001 → 0.006)
- 19 status flips
- 8 features flipped unstable → stable_dead: `buy_volume_sol_at_30`, `creator_median_peak_mc_sol`, `fast_buy_count`, `first_buy_sol`, `gap_create_to_first_trade`, `hc_30`, `top10_30`, `top5_30` (all T+30 family)

**Per memory `project_feature_stability_protocol`:** drop `stable_dead` only if the same feature is dead **in TWO sequential schema versions**. We have only one version so far → wait for confirmation from the second run.

**Steps:**
1. **2026-05-01..2026-05-03:** run `feature_stability.py --n-seeds 5 --data data/ml/entry.parquet --out data/ml/feature_stability_v18_seed5_round3.json` (after a new retrain on the accumulated data).
2. Compare `_apr26.json` vs `_round3.json` via `/tmp/compare_stability.py`.
3. If the same 8 features end up in `stable_dead` again → **remove from the feature schema** (bump v18 → v19), retrain, validate prod metrics.
4. If they flip back to unstable → noise, leave them alone.
5. In parallel: review the shadow_predictions data (accumulated over the week) — production-validation gate for t30/timing/survival/quantile activations.

**Cost:** ~10 min wall-time for the 5-seed stability run + ~30 min retrain if cleanup triggers.

---

## Phase 0 — Extending the data horizon (BLOCKER for all ML iterations)

**Trigger:** ASAP, before moving to Phase 1+.

**Problem (discovered 2026-04-25):**
Only ~440 of 73,908 tokens (0.6%) in the DB have post-scoring trades. The bot calls `unsubscribe_trades(mint)` right after a SKIP decision (`pipeline.py:781`), and for 99% of tokens trades are truncated at `observe_seconds=90s`. This blocks:
- Sweep over `max_hold > 90s` (gives identical numbers — no data)
- Training models with a long holding period (winners don't have time to grow within 90s)
- Experiments with looser SL (if a token dips and recovers in 200s — we don't see it)

**What is done (code ready, awaiting deploy):**
1. `pulse_extended_observe_seconds: float = 0.0` in `PulseBotConfig`
2. Env var `PULSE_EXTENDED_OBSERVE_SECONDS`
3. `Pipeline._extended_observation()` background task
4. CHANGELOG updated, tests green

**What needs to be done (requires approval):**
1. Restart the live bot with `PULSE_EXTENDED_OBSERVE_SECONDS=600` (10 minutes post-scoring)
2. Wait 2-3 weeks → a long-tailed dataset will accumulate (~5-15M new trades)
3. Only after that does a Phase 2 sweep over `max_hold` / wider SL make sense

**Cost:**
- WS traffic: ×1.5-2 (we keep listening to those 22% of tokens with post-scoring activity)
- DB writes: same factor
- Disk: 5.5M trades → 8-12M in 3 weeks
- All three acceptable

**Rollback:** remove the env var → old behavior.

---

## Phase 1 — Sanity check

**Trigger:** N_closed ≥ 100 paper trades (approximately 2026-04-28)

**What we do:** compare actual WR of closed paper_trades vs the labeled Prec@top10%=40.7%.

| Real WR | Verdict |
|---|---|
| 30-50% | System is honest, continue |
| < 25% | Distribution shift — revert to val-tuned thresholds, investigate |
| > 50% | Sampling bias in labels vs live — reassess |

---

## Phase 2 — Foundation retrain + exit sweep

**Trigger:** N_closed ≥ 500 (approximately 2026-05-05)

**What we do:**
1. Rebuild `entry.parquet` with the new labels
2. Retrain classifier — expecting AUC 0.637 → 0.67-0.70
3. Feature stability 5 seeds — drop STABLE_DEAD (candidates from v15:
   `top3_buyer_prior_avg_wr`, `top3_buyer_max_prior_pnl_sol`)
4. Platt recalibration
5. **Optimizer sweep** with a new axis:
   - `exit_inactivity_seconds`: [30, 45, 60, 90, 120] ← NEW
   - `exit_max_hold_seconds`: [60, 90, 120, 180]
   - `exit_hard_stop_loss_pct`: [8, 10, 12, 15]

**Apply the winning config** if OOS PnL gain > +0.3 SOL/week.

---

## Phase 2.5 — Time-aware features in main model

**Trigger:** after Phase 2 retrain (~2026-05-06)

**Idea:** don't proliferate separate @T+30, @T+45, @T+60 models — instead, extend the feature vector
of the **main** model with snapshots at multiple timestamps.

**Features:**
- `unique_buyers@30`, `unique_buyers@60`, `unique_buyers@90`
- `buy_rate@30`, `buy_rate@60`, `buy_rate@90`
- `top1_holder@30`, `top1_holder@60`, `top1_holder@90`
- **Delta features:** `Δ_top1_30_to_60`, `Δ_buy_rate_60_to_90` (acceleration/deceleration)
- `hc_velocity_early` vs `hc_velocity_late` (two segments)

**Pros:**
- One model, one calibration
- The model **itself** learns the token's "evolution"
- 70 features → 200-250 features, but the same 1292 labels. With N≥3000 it handles it comfortably

**Cons:**
- Doesn't give an early decision (the bot still waits for T+90 to get @90 features)
- Phase 3 @T+30 model remains — a separate model is needed for a fast decision

**Work:** 2-3 days. Add snapshot collection @T+60 (currently only @T+30 and @T+90 via Helius; buy_rate/buy_count — recompute from trades). Extend `extract_entry_features` + `build_dataset`.

---

## Phase 3 — @T+30 dual-snapshot model

**Trigger:** N_closed ≥ 1000 + Helius T+30 snapshot infra ready
(approximately 2026-05-15)

**Codex priority #1 after foundation.** Doubles label volume/day + a second
independent signal to combine with T+90.

**Prerequisites (can start in parallel now):**
- Clean Helius snapshot @T+30s (currently lagging)
- `build_dataset_t30.py` — features visible by T+30

**Pipeline after deployment:**
```
T+30s: Model_30 scores token
  ├─ proba > 0.75 → BUY immediately (don't wait 90s)
  ├─ proba < 0.15 → SKIP immediately (free up the slot)
  └─ middle → wait for T+90s, Model_90 as today
```

**Expected gain:**
- Earlier entry (5-10th buyer instead of 20-25th)
- Faster rejection of obvious scams → more slots
- 2× labels per day

---

## Phase 4 — Timer tick + survival max_hold

**Trigger:** Phase 3 stable (approximately 2026-05-25)

### Part A — Infrastructure

Refactor `Pipeline._paper_trade`:
- Background timer tick every **5-10 sec** for active paper trades
- `PulseMonitor.update_empty_tick(now)` — recompute rates when there are no new trades
- `ExitManager` is invoked on timer ticks (not only on trade events)
- Unit tests: timer-tick invariants, no regressions in existing tests

### Part B — Survival model

- **Cox proportional hazards** or **discrete-time hazard** (NOT regression)
- Duration — censored data (right-truncated by our own exits)
- Target: time from scored_at to pulse_dead / no_new_blood signal
- Replace `exit_max_hold_seconds=90` with `min(predicted_life, 180s)`

**Prerequisite (Part A is a blocker for Part B):** the model needs per-second
state for labelling.

---

## Phase 5 — Entry-timing classifier

**Trigger:** Phase 4 stable (approximately 2026-06-05)

Every 15 sec starting from T+15s → classifier: `WAIT_MORE / BUY_NOW / SKIP`.

Supervised (not RL) — per-snapshot labels generated via simulate_exit future.
Solves the "21st buyer" problem when the signal matures before T+90s.

---

## Phase 6 DEFERRED — TP quantile head

**Trigger:** N_closed ≥ 3000 AND tail ≥ 100 tokens at +100% PnL
(approximately 2026-06-20+)

**Why deferred (codex veto):** on current data only ~10-15 tokens in the
moonshot tail; q=0.75 quantile regression will **systematically underestimate** the peaks.
The bot will be selling 2× runners at 0.8×. Re-visit when tail coverage is adequate.

---

## Phase 7 — Tighten ML gates after data collection (new 2026-04-29)

**Trigger:** all of the conditions met:
- `EXTENDED_OBSERVE` has accumulated ≥ 7 days of data with full-window trade streams
- Helius backfill of historical mints finished (≥ 5000 graduated mints in DB)
- Validator caught up to mainnet — backfill goes via local RPC
- Model retrain completed on new labels
- Health checks: ρ ≥ 0.20 on live data (validation + first 200 live trades)
- economic_backtest: EV per trade ≥ +0.5% on the new model

**Context (recorded here so we don't forget, 2026-04-29 discussion):**

The current `ml_override` setup `PULSE_ENTRY_PROBA_CEILING=0.15` is aggressive:
- 100% of paper-trades go through ML_OVERRIDE (rules never trigger BUY)
- WR=5%, PnL=-4 SOL/48h on paper (paper, not real money)
- Live ρ=±0.01 — the model ranks randomly in production
- Wins (+107%, +379%) arrive **despite** the reg-model's forecast (which predicted -3.5%, -2.4%)

**This is bad for earnings BUT good for data collection:**
- 1055 diverse closes → exit models have training material
- Random sample tokens → bootstrap data, representative dataset
- 51 winners among the losers → exit models learn from winners

So **for now we don't tighten**, we accumulate. After the Phase 7 trigger — we tighten.

### What to tighten (after trigger):

1. **Raise the ml_override ceiling** `PULSE_ENTRY_PROBA_CEILING=0.15 → 0.50`
   - Effect: override only fires at high model confidence
   - Forecast: 5-10 trades/day instead of 100+, WR ↑

2. **Reg-floor positive** `PULSE_ENTRY_REG_FLOOR_PCT=-10.0 → 0.0`
   - Only trades with a positive PnL forecast
   - Forecast: cuts off ~30% of the worst ml_override candidates

3. **Double-SKIP guard** (requires code in `decision_service.py`):
   ```
   if rules.fast == "WAIT/SKIP" AND rules.full == "SKIP":
       require ml_proba ≥ 0.70 AND reg_pnl ≥ +5%
   ```
   - Effect: when **both** rules systems agree "don't take it", the override requires **double** confidence
   - Forecast: leaves only tokens where the model **clearly** sees what the rules don't

4. **Sizing ladder by reg-prediction:**
   - `pred_pnl ≥ +20%`: 1.5× standard size
   - `+5% ≤ pred_pnl < +20%`: 1.0× (standard)
   - `0 ≤ pred_pnl < +5%`: 0.5× (defense)
   - Do ONLY once live ρ stabilizes ≥ 0.20 (otherwise sizing amplifies noise)

### Rollback:
Revert env vars in .env, restart bot, return to the Phase 6 baseline.

### Estimate:
- Code changes (double-skip guard): 1-2 hours
- Tests: 1 hour
- .env update + restart: 5 min
- Live verification: 24 hours

---

## KILLED items (per codex review)

- ❌ **TP quantile head first** — too little data, systematic bias (see Phase 6)
- ❌ **Smooth position sizing** (linear in proba) — cosmetic, at AUC 0.637 won't give a measurable lift
- ❌ **Dynamic observe_seconds via RL** — Phase 3 solves the same task supervised
- ❌ **ML-control execution constraints** (max_entry_buyer_number, min_sol_volume_hard,
  creator_blacklist) — physics/compliance, not predictions

---

## Parallel infrastructure (can start now)

1. **Clean Helius T+30 snapshot flow** — Phase 3 prerequisite
2. **`config_hash` in entry_model.meta.json** — WARN at startup if runtime config != training
   config (protect from silent Option B-style regression)
3. **Dashboard widget:** WR last 100 paper_trades + ML override BUY:SKIP ratio —
   daily monitoring kill criteria
4. **Simulate_exit vectorization** — 15min for 60k rows too slow, vectorize

---

## Kill criteria (any phase)

Actions if triggered:
- **Realized PnL < +0.3 SOL/week** (7-day rolling) → pause + investigate
- **WR < 20%** over 50+ trades → regression, revert last change
- **ML override BUY:SKIP ratio drift >2×** → data drift, retrain
- **ML vs rules underperform >5% for 3 consecutive days** → flip `PULSE_POLICY=rules`
  (see `memory/project_ml_rollback_trigger.md`)

---

## Timeline visual

> **Note on N≥X (graduated):** a "graduated mint" (mc≥85 SOL) happens ~1-3/day, ~0.5% rate. Using the graduated count as a retrain trigger is a **bad idea** (label imbalance). The real label source = `paper_trades` (PnL outcomes). The `N≥500/1000/3000` numbers below are informational rough timing for scale, **not data gates**. Retrains can be launched whenever it makes sense given the available `paper_trades` + features.

```
2026-04-24 ═══════ NOW ═══════
            │  v15 full-ML running (paper mode)
            │  T+30 infra being prepared in parallel
            │
2026-04-28 ─── N_paper≥100 ─── Phase 1 (sanity check)
            │
2026-05-05 ─── N_paper≥500 ─── Phase 2 (retrain + sweep)
            │                     ↑ expecting AUC 0.67+
            │
2026-05-15 ─── N_paper≥1000 ─── Phase 3 (@T+30 model)
            │                     ↑ foundation strengthened
            │
2026-05-20 ─── Phase 3 stable ─── Phase 4 prep (timer tick)
            │
2026-05-25 ─── Timer tick done ─── Phase 4 (survival max_hold)
            │
2026-06-05 ─── Phase 4 stable ─── Phase 5 (entry-timing classifier)
            │
2026-06-20+ ── N_paper≥3000 + tail ─── Phase 6 (TP quantile, formerly Priority A)
```

(`N_paper` = closed paper_trades with realized PnL; not graduated mints.)

---

## Tracking

- Tasks #137 (Phase 1), #138 (Phase 2), #139 (Phase 3), #140 (Phase 4),
  #141 (Phase 5), #142 (Phase 6 deferred), #143 (parallel infra)
- Memory: `project_roadmap_2026_05.md` (this file in short form)
- Kill watchdog: Task #136 — watch the full ML-only trial until 2026-05-08
