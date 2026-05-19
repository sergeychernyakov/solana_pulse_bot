# Solana Pulse Bot

> **🔴 Status (2026-05-19): project closed.** Repository archived, active development halted, live bot stopped. Historical content preserved as reference.

Observer bot for Solana memecoin launchpads (pump.fun, letsbonk). Watches new tokens, evaluates how organic the interest is, decides whether to buy. Two-phase scoring: fast entry (T+5s) + full analysis (T+90s). **82 ML features per token** (schema v21), 80+ configurable parameters, 8 ML heads (entry main / entry @T+30 / entry regression / entry-timing / survival / SL+TP+max_hold quantile heads).

Backtest uses the same code as the live bot — **100% decision match** (verified on 300+ tokens).

## Architecture (v18, 2026-04-25)

```
Pipeline (single code path for live and backtest):

  Token CREATE (WS or replay)
    │
  Main loop (sequential, deterministic):
    ├─ insert_token + upsert_creator + snapshot creator stats
    │
  Parallel task (per token):
    ├─ T+5s   Fast filter            → FAST_BUY / WAIT
    ├─ T+15s  ─┐
    ├─ T+30s   │ Phase 5 entry-timing checkpoints (3-class WAIT/BUY/SKIP)
    ├─ T+30s  ←─ Helius snapshot + Phase 3 T+30 model (early BUY/SKIP/DEFER)
    ├─ T+45s   │
    ├─ T+60s  ←─ Helius snapshot (for top1@60 interpolation)
    ├─ T+75s   │
    ├─ T+90s  ─┘ Main entry model (XGBoost binary + regression head)
    │            schema entry_v18 — 79 features, time-aware @30/@60/@90 + deltas
    ├─ T+120s ←─ Helius snapshot (top1_120, hc_120 for time-aware features)
    │
    ├─ BUY → paper_trade with ExitManager (rules + survival + quantile heads)
    │       Timer tick every 5s + survival predict every 10s
    │
    └─ SKIP → extended observation N sec (PULSE_EXTENDED_OBSERVE_SECONDS)
              keep saving trades for long-horizon ML labels
```

Key principles:
- **All state updates happen in the main loop** (sequentially), scoring runs in parallel tasks (with a frozen snapshot). 100% determinism under parallelism.
- **All ML models are optional** — each is enabled by a separate env var, default OFF. Without ML the bot runs on rules.
- **Schema versioning** — models check `feature_schema_version` at load time and refuse to run against an incompatible dataset.
- **Config drift guard** — `config_hash` in meta.json, runtime config-mismatch → WARNING on startup.

## Project layout

```
pulse_bot/
├── models.py              # Token, Trade, ScoringResult (~80 fields), CreatorStats
├── config.py              # PulseBotConfig — 80+ parameters (env-overridable)
├── db.py                  # PostgreSQL — asyncpg + psycopg2 pools
├── pipeline.py            # Pipeline — live + replay + paper_trade + tick loop
├── core.py                # PaperTradeRunner — common entry/exit replay logic
├── backtest.py            # BacktestEngine
├── optimizer.py           # Grid search (rules-based exit sweeps)
├── dashboard.py           # Streamlit live monitoring
├── backtest_dashboard.py  # Streamlit optimization results
│
├── ml/                    # 6 ML heads + supporting infra
│   ├── features.py            # ENTRY_FEATURE_ORDER (v18, 79 features) + T+30 subset
│   ├── build_dataset.py       # main entry dataset + simulate_exit_batch
│   ├── build_dataset_t30.py   # T+30 subset dataset (Phase 3)
│   ├── train.py               # train_entry / train_entry_t30 / train_exit / quantile heads
│   ├── policy.py              # EntryMLPolicy + EntryT30Policy + ExitMLPolicy + Quantile loaders
│   ├── simulate_exit.py       # Pure-function exit replay (used for labels + sweeps)
│   ├── survival.py            # Phase 4B discrete-time hazard model
│   ├── entry_timing.py        # Phase 5 3-class WAIT/BUY/SKIP @ 15s checkpoints
│   ├── daily_validation.py    # 9 honesty checks (shuffled labels, prior drift, economic_backtest, etc.)
│   ├── feature_stability.py   # 5-seed stability protocol for feature pruning
│   ├── config_hash.py         # train-time config snapshot + drift WARN
│   ├── calibration_check.py
│   ├── label_noise_floor.py
│   ├── backfill_scoring.py
│   ├── weekly_retrain.py
│   └── wallet_indexer.py      # top-3 buyer prior PnL features
│
├── sources/
│   ├── backtest.py            # BacktestSource — PostgreSQL replay
│   └── replay.py              # ReplayLaunchpad
│
├── launchpads/
│   ├── pumpfun.py             # PumpPortal WS subscription
│   └── letsbonk.py
│
├── filters/
│   ├── fast.py                # FastFilter — T+5s decision
│   ├── observation.py         # ObservationFilter — T+90s full analysis
│   ├── metrics.py             # MetricsCalculator — time-truncated stats @30/@60/@90
│   ├── creator.py
│   └── scorer.py
│
└── pulse/
    ├── monitor.py             # PulseMonitor — sliding window + update_empty_tick
    └── exit_manager.py        # ExitManager — rules cascade + ML escalation

scripts/
├── exit_config_sweep.py       # SL/TP/max_hold sweep with relabel+retrain per combo
├── migrate_copy.py            # SQLite → PG bulk migration (one-shot, done)
├── sync_pg_schema.py
└── run_daily_validation.sh
```

## Commands

```bash
# Live monitoring — connects to Pump.fun WS, scores tokens
python main.py monitor

# Backtest — replay collected data through the same Pipeline code
python main.py backtest

# Verification — proves backtest = live (must be 100%)
python main.py verify

# Grid search — parameter sweep
python main.py optimize

# Code quality (pre-commit hook)
./qa

# Integration test: 300+ tokens, live → backtest 100% match (mandatory)
./verify300

# Dashboards
streamlit run pulse_bot/dashboard.py --server.port 8501           # live
streamlit run pulse_bot/backtest_dashboard.py --server.port 8502  # backtest results
```

## Backtest = Live (100% match) — mandatory invariant

Backtest uses the same `Pipeline` code as the live bot. The only difference is the data source:

| | Live | Backtest |
|---|---|---|
| Launchpad | PumpFunLaunchpad (WebSocket) | ReplayLaunchpad (PostgreSQL) |
| Pipeline | same code | same code |
| Scorer | same code | same code |
| token_scores.source | 'live' | 'backtest' |

**Mandatory test before any change:**
```bash
./verify300   # 15 min live → backtest → 100% match on 300+ tokens
```

Quick verification (2 min):
```bash
python main.py monitor    # collect data (2+ min)
python main.py verify     # replay + compare
```

Determinism guarantees:
- `insert_token` + `upsert_creator` — in the main loop (sequentially)
- Creator snapshot — local variable, not shared state (no race conditions)
- Replay loads exactly the same trade IDs the live run saw (fast_trade_ids, full_trade_ids)
- Replay uses creator_reason from live scores (exact snapshot)
- FastFilter returns WAIT on 0 trades (no fake FAST_BUY)
- 12 unit tests + the verify300 integration test

## Two-phase scoring

| Phase | Time | Decision | When we buy |
|------|-------|---------|---------------|
| Fast | 5 sec | FAST_BUY / WAIT | entry_mode = fast or both |
| Full | 45 sec | BUY / BORDERLINE / SKIP | entry_mode = full or both |

`entry_mode` is set in the config. Default: `fast`.

## 62 metrics per token

All are written to SQLite for analysis and backtesting:

**Trade patterns:** buy_count, sell_count, unique_buyers, buy_volume_sol, buy_diversity, avg/median/std buy size, top3 concentration, repeat buyers, buy velocity trend, sell pressure, ...

**Bonding curve:** curve_progress_pct, curve_velocity, curve_acceleration, sol_to_graduation, market_cap_sol

**Token metadata:** name_length, symbol_length, has_uri, is_all_caps, has_numbers

**Timing:** hour_utc, creator_tokens_today, gap_create_to_first_trade

**P&L:** pnl_5th/10th/20th/50th/100th_pct — P&L had we entered on the N-th buyer

## Configuration

80+ parameters in `pulse_bot/config.py`:

- Fast phase: observe_seconds, min_buys, min_volume, max_sell_ratio, scoring weights
- Full phase: observe_seconds, score thresholds, buyer/volume/curve weights
- Execution: fee, slippage model, buy amount
- Pulse monitor: window_size, dead/weak buy rate, trend threshold
- Exit rules: stop loss, max hold, partial sells, moonbag
- Portfolio: initial balance, max positions

All parameters are configurable for the grid-search optimizer.

## Production deployment — Rich server (192.168.3.118)

> **IMPORTANT — where things run:**
> - **`pulse_bot monitor` (live bot)** — **rich only**, via systemd. Never run on Mac.
> - **Mac** — dev only: backtest, optimizer sweep, ML train, analytics.
> - **Dashboards (live + backtest)** — on rich, reachable at http://192.168.3.118:8501/8502.
> - **Backfill** and **Solana validator** — also on rich, via systemd.

```bash
# SSH alias configured in ~/.ssh/config: Host rich → 192.168.3.118 user=sergey
ssh rich

# Bot status (systemd user units)
systemctl --user status pulse-bot.service
systemctl --user status backfill.service
systemctl --user status solana-validator.service
systemctl --user status pulse-dashboard.service
systemctl --user status pulse-backtest-dashboard.service
tail -f ~/www/gg/logs/bot.log

# Dashboards (live data, network-accessible):
# http://192.168.3.118:8501  — main live monitoring
# http://192.168.3.118:8502  — backtest / optimizer results

# Bot lifecycle
systemctl --user start pulse-bot.service
systemctl --user restart pulse-bot.service       # after .env / code changes
systemctl --user stop pulse-bot.service

# Unit file: ~/.config/systemd/user/pulse-bot.service
# Logs: ~/www/gg/logs/bot.log

# DB on rich: PG 16, user=sergeychernyakov password=pulsebot
PGPASSWORD=pulsebot psql -U sergeychernyakov -d pulse_bot -h localhost
```

**Sync DB between Rich (production) and Mac (dev) — on-demand:**

Rich = source of truth (bot writes 24/7 there). Mac pulls a fresh snapshot when one is needed for retrain/sweep/analysis. No cron — sync on demand.

```bash
# Rich → Mac (fresh data before retrain/sweep, ~5 min for a 3 GB DB):
ssh rich 'pg_dump -U sergeychernyakov -d pulse_bot -F c -Z 9' > /tmp/rich.dump \
  && pg_restore --clean --no-owner -d pulse_bot /tmp/rich.dump

# Mac → Rich (only for initial deploy or recovery):
pg_dump -d pulse_bot -F c -Z 9 -f /tmp/dump.dump
scp /tmp/dump.dump rich:/tmp/
ssh rich 'pg_restore -U sergeychernyakov -d pulse_bot /tmp/dump.dump'
```

## Quick start (Mac dev)

> Mac is **dev only**: backtest / optimizer / ML retrain / dashboards. The live `monitor` runs only on rich (see above).

```bash
git clone <repo-url>
cd gg
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
pip install xgboost psycopg2-binary asyncpg  # ML + Postgres deps (not in requirements.txt)

# IMPORTANT: always activate venv before running
source .venv/bin/activate

# Backtest against a fresh dump from rich
python main.py backtest

# Dashboard for viewing data
streamlit run pulse_bot/dashboard.py --server.port 8501
```

### Environment variables

```bash
# Infrastructure
export PULSE_PG_DSN="postgresql://sergeychernyakov@localhost/pulse_bot"
export HELIUS_API_KEY="..."
export PULSE_POLICY="hybrid"          # hybrid | rules

# Phase 0 — extended trade collection post-scoring (CRITICAL for ML labels)
export PULSE_EXTENDED_OBSERVE_SECONDS=600    # 0 = old (cut off at T+90s); 600 = +10min

# Phase 4A — timer tick (default 5s, 0 = off)
export PULSE_TICK_SECONDS=5

# Phase 3 — T+30 dual-snapshot model (default OFF)
export PULSE_ENTRY_T30_ACTIVE=0       # =1 enables T+30 hook (early BUY/SKIP/DEFER)

# Phase 4B — survival model in tick loop (default OFF)
export PULSE_SURVIVAL_ACTIVE=0        # =1 enables predicted_remaining_life

# Phase 5 — entry-timing 15s checkpoints (default OFF)
export PULSE_TIMING_ACTIVE=0          # =1 enables 3-class WAIT/BUY/SKIP

# Exit ML
export PULSE_EXIT_ML_ACTIVE=1         # 0 = pure rules
export PULSE_EXIT_REGRESSION_ACTIVE=1  # quantile SL/TP heads (active 2026-04-23)

# Entry ML thresholds (override val-tuned defaults from meta.json)
export PULSE_ENTRY_PROBA_FLOOR=0.5    # below = SKIP
export PULSE_ENTRY_PROBA_CEILING=0.5  # above = BUY (ML-only when floor=ceiling)

# Exit config (for sweep scripts)
export PULSE_EXIT_HARD_STOP_LOSS_PCT=15
export PULSE_EXIT_TAKE_PROFIT_PCT=100
export PULSE_EXIT_MAX_HOLD_SECONDS=90
export PULSE_EXIT_INACTIVITY_SECONDS=120

# Helius RPC concurrency (for T+30/60/120 captures)
export PULSE_HELIUS_HOLDER_CONCURRENCY=100
```

**Current recommended launch (for Phase 0 data accumulation):**
```bash
PULSE_EXTENDED_OBSERVE_SECONDS=600 \
PULSE_TICK_SECONDS=5 \
.venv/bin/python main.py monitor
```

### Running tests

```bash
# MANDATORY: use python from .venv, not system python
source .venv/bin/activate
pytest tests/pulse_bot/ -q

# or without activating:
.venv/bin/python -m pytest tests/pulse_bot/ -q

# Integration test (15 min, 300+ tokens, backtest=live match):
./verify300
```

## ML Models — 6 heads (state @ 2026-04-25)

| Head | File | Status | AUC | When it fires | Env switch |
|---|---|---|---|---|---|
| **Entry main** (binary) | `entry_model.ubj` v18 | ACTIVE | 0.96 | T+90s | always (v17 fallback if missing) |
| **Entry regression** | `entry_model_reg.ubj` | ACTIVE (advisory) | — (Spearman 0.28) | T+90s | `PULSE_REGRESSION_ENTRY=1` |
| **Entry @T+30** | `entry_model_t30.ubj` v1 | OFF | 0.94 | T+30s | `PULSE_ENTRY_T30_ACTIVE=1` |
| **Entry-timing** (3-class) | `entry_timing_model.ubj` v1 | OFF | — | T+15/30/45/60/75s | `PULSE_TIMING_ACTIVE=1` |
| **Survival** (hazard) | `survival_model.ubj` v1 | OFF | — | tick loop ×10s | `PULSE_SURVIVAL_ACTIVE=1` |
| **Exit binary** | removed 2026-04-25 | — | — | — | — |
| **Exit quantile SL** | `exit_quantile_sl.ubj` | ACTIVE | — | dynamic SL | `PULSE_EXIT_REGRESSION_ACTIVE=1` |
| **Exit quantile TP** | `exit_quantile_tp.ubj` | ACTIVE | — | dynamic TP | `PULSE_EXIT_REGRESSION_ACTIVE=1` |

### Current holdout metrics (2026-04-25, schema v18)

```
Main entry model:
  AUC                  0.96   95% CI [0.95, 0.97]
  Precision@top-10%    7.97%   95% CI [6.3%, 10.0%]
  BUY zone (ceiling=0.6) WR=8.7%, ×11 base rate (0.81%)

Economic backtest:    NEGATIVE   PnL=−1.96 SOL @ proba=0.5 threshold
                      (model is not profitable on TRUNCATED 90s data —
                      see Phase 0 in ROADMAP, waiting 2-3 weeks of data)
```

### Phase status (see `docs/ROADMAP_2026_05.md`)

```
✅ Phase 0   extended observation             [DEPLOYED, accumulating data]
✅ Phase 4A  timer tick infra                  [DEPLOYED, default ON]
✅ Phase 4B  survival model                    [CODE READY, default OFF]
✅ Phase 2.5 time-aware features (v18)         [DEPLOYED, schema v18 default]
✅ Phase 3   @T+30 dual-snapshot model         [CODE READY, default OFF]
✅ Phase 5   entry-timing classifier           [CODE READY, default OFF]
✅ Infra     config_hash drift guard           [DEPLOYED]
✅ Infra     simulate_exit_batch (2.5× speed)  [DEPLOYED]
✅ Infra     Helius T+30 lag instrumentation   [DEPLOYED, sem 50→100]

⏳ Blocked on Phase 0 data accumulation (2-3 weeks):
   Phase 1   sanity check (N≥100 closed)
   Phase 2   foundation retrain + exit sweep (N≥500)
   Phase 6   TP quantile head (N≥3000 + tail)

⏳ Blocked on operator decision:
   Activation of Phase 3/4B/5 hooks on the live bot
```

### Exit ML architecture (after removal of exit_model on 2026-04-25)

After dropping the binary exit model (AUC 0.55, near-random), exit logic works like this:

1. **Hard rules first (immutable safety floor):**
   `creator_dump`, `pulse_dead`, `trend_dying`, `sell_pressure`, `buy_rate_drop`, `no_new_blood`, `whale_exit`, `near_graduation`, `hard_stop (−15%)`, `take_profit (+100%)`, `trailing_stop`, `timeout (90s)`

2. **Quantile heads (active, dynamic SL/TP):**
   - `exit_quantile_sl.ubj` (q=0.25) — tightens SL for high-risk tokens
   - `exit_quantile_tp.ubj` (q=0.75) — loosens TP when the model expects a large gain

3. **Survival model (Phase 4B, default OFF, activated via `PULSE_SURVIVAL_ACTIVE=1`):**
   Replacement for the binary exit model. Predicts `predicted_remaining_life`, forces exit when life < 30s.

Sorted by **gain importance** (latest v18 retrain, 2026-04-25).

| Feature | Meaning | Gain |
|---|---|---|
| `top5_120` | % held by top-5 holders at T+120s | 10544 |
| `buy_volume_sol_at_90` | SOL volume in first 90s (= total) | 8098 |
| `delta_unique_buyers_30_to_60` | Δ unique buyers T+30→T+60 | 6710 |
| `hc_120` | Holders count at T+120 | 6082 |
| `fast_buy_count` | Buys in first 5 sec | 4893 |
| `top10_minus_top5_120` | Middle-zone concentration (#6-10) | 4372 |
| `buy_vol_to_sell_vol_ratio` | Volume imbalance | 2690 |
| `top1_120` | % held by #1 holder at T+120 | 2333 |
| `curve_velocity` | SOL/sec into bonding curve | 1823 |
| `delta_top1_30_to_60` | Δ concentration T+30→T+60 | 1639 |
| `sol_price_usd` | SOL price (market regime) | 1596 |
| `fast_buy_rate` | buys/sec for first 5 sec | 1376 |
| `hour_cos`, `hour_sin` | Cyclical encoding of UTC hour | ~1300 |

### Removed features (clean-up history)

| Feature | When | Why |
|---|---|---|
| `name_length`, `symbol_length`, `is_all_caps`, `has_numbers` | 2026-04-23 | H7 name patterns — 0 signal |
| `market_cap_sol`, `sol_to_graduation`, `log_market_cap` | 2026-04-25 (v16) | Momentum-bias leak, dominated #1-2 gain, killed economic_backtest |
| `token_price_sol` | 2026-04-25 (v17) | = market_cap_sol/1e9 on pump.fun (constant supply) — same leak |
| `fast_score`, `total_score` | 2026-04-22 | Circular dependency on the rules ML is replacing |

### Iteration history (entry main model)

| Date | Schema | AUC | Prec@top10% | Economic backtest | Comment |
|---|---|---|---|---|---|
| 2026-04-22 | v15 | 0.74-0.82 | 43-50% | −0.29 SOL | Pre-DOA, N=528 train rows |
| 2026-04-24 | v15+DOA | 0.98 | 41% | −0.56 SOL | DOA fix, N=47997, base rate 0.82% |
| 2026-04-25 | v17 | 0.98 | 8% | −0.56 SOL | Removed `market_cap_sol`, `sol_to_graduation`, `log_market_cap`, `token_price_sol` (momentum-bias leak) |
| 2026-04-25 | v18 | 0.96 | 8% | −1.96 SOL | Phase 2.5 time-aware (+13 features): @30/@60/@90 + deltas. **Regression** expected per ROADMAP — model overfits on noisy labels (90s cutoff) |

**Profit target metric:** economic_backtest > +0.3 SOL/week. **Blocker:** Phase 0 data accumulation. All current models are trained on labels truncated at 90s — winners don't have time to grow to +50/+100%, so any selectivity gives −EV. Once Phase 0 data accumulates (2-3 weeks), labels become honest → re-train + sweep on honest EV.

### Feature management rules

1. **N rows / feature ≥ 20** — violation → overfit (see the v18 regression above)
2. **Add 1-2 at a time** and measure before/after
3. **Feature stability protocol** before removing: `feature_stability.py` with 5 seeds; remove only `STABLE_DEAD` across TWO sequential schema versions
4. **Known leak features** (KNOWN_LEAK_FEATURES in `daily_validation.py`): `market_cap_sol`, `mc_at_scoring`, `sol_to_graduation`, `v_sol_in_bonding_curve`, `v_tokens_in_bonding_curve`, `log_market_cap`, `token_price_sol`. Appearance in top-10 gain = regression

## Documentation

- [Roadmap 2026-05](./docs/ROADMAP_2026_05.md) — Phase 0-6 plan + status
- [CHANGELOG](./docs/CHANGELOG.md) — journal of all ML / behavior changes

## Author

**Sergey Chernyakov** — Telegram: [@imhotepus](https://t.me/imhotepus)

---

**Keywords:** Solana, pump.fun, pumpfun, memecoin, memecoin trading bot,
Solana bot, Solana trading bot, crypto trading bot, algorithmic trading,
sniping bot, sniper bot, pump.fun bot, pump.fun sniper, bonding curve,
PumpPortal, Helius, Yellowstone Geyser, gRPC, WebSocket, machine learning,
ML trading, XGBoost, survival analysis, quantile regression, entry timing,
exit policy, A/B testing, paper trading, backtest, walk-forward,
feature engineering, train/serve skew, model skill gate, EV-based
threshold, schema versioning, scam wallet detection, wallet classification,
sniper detection, smart money, holder concentration, top-N concentration,
PostgreSQL, psycopg2, asyncpg, Streamlit dashboard, systemd, Python 3.12,
solders, anchorpy, on-chain execution, Solana RPC, getSignaturesForAddress,
logsSubscribe, Solana validator, mainnet-beta.
