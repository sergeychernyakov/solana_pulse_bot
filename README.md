# Solana Pulse Bot

> **🟢 Status (2026-05-07): paper-bot профитный.** N=161 закрытых трейдов за 19ч с момента включения survival confidence-gate (2026-05-06 14:30 UTC):
> - **PnL: +2.78 SOL** (≈+3.5 SOL/день pace)
> - WR: **18.6 %** (30 wins / 131 losses)
> - Avg PnL/trade: +0.017 SOL
> - Top-5 winners (TYLEE +780 %, RWAFY +383 %, BFS +364 %, MAUA +347 %, INCOME +333 %) — 80 % всей прибыли. Стратегия = "lottery-ticket farming" + tail capture.
> - 8/8 моделей загружены (`status=ok`); survival эффективно в shadow-режиме через `PULSE_SURVIVAL_MIN_CONFIDENCE=0.50` — гейт фильтрует low-confidence kills (см. CHANGELOG 2026-05-06 14:30).
> - **Live execution не подключён** — это paper PnL, real money требует отдельной валидации (slippage 1-3 % на pump.fun bonding curve).

Бот-наблюдатель для мемкоин-лончпадов на Solana (pump.fun, letsbonk). Наблюдает за новыми токенами, оценивает органичность интереса, принимает решение о покупке. Двухфазный скоринг: быстрый вход (T+5s) + полный анализ (T+90s). **82 ML-фич на токен** (схема v21), 80+ конфигурируемых параметров, 8 ML голов (entry main / entry @T+30 / entry regression / entry-timing / survival / SL+TP+max_hold quantile heads).

Бэктест использует тот же код что и live бот — **100% совпадение решений** (верифицировано на 300+ токенах).

## Архитектура (v18, 2026-04-25)

```
Pipeline (один код для live и backtest):

  Token CREATE (WS или replay)
    │
  Main loop (sequential, deterministic):
    ├─ insert_token + upsert_creator + snapshot creator stats
    │
  Parallel task (per token):
    ├─ T+5s   Fast filter            → FAST_BUY / WAIT
    ├─ T+15s  ─┐
    ├─ T+30s   │ Phase 5 entry-timing checkpoints (3-class WAIT/BUY/SKIP)
    ├─ T+30s  ←─ Helius snapshot + Phase 3 T+30 model (раннее BUY/SKIP/DEFER)
    ├─ T+45s   │
    ├─ T+60s  ←─ Helius snapshot (для top1@60 интерполяции)
    ├─ T+75s   │
    ├─ T+90s  ─┘ Main entry model (XGBoost binary + regression head)
    │            schema entry_v18 — 79 фич, time-aware @30/@60/@90 + дельты
    ├─ T+120s ←─ Helius snapshot (top1_120, hc_120 для time-aware фич)
    │
    ├─ BUY → paper_trade с ExitManager (rules + survival + quantile heads)
    │       Timer tick каждые 5s + survival predict каждые 10s
    │
    └─ SKIP → extended observation N сек (PULSE_EXTENDED_OBSERVE_SECONDS)
              продолжаем сохранять trades для длинных ML labels
```

Ключевые принципы:
- **Все обновления состояния в main loop** (последовательно), скоринг в parallel tasks (с замороженным snapshot). 100% детерминизм при параллелизме.
- **Все ML модели опциональны** — каждая включается отдельным env var, default OFF. Бот без ML работает на rules.
- **Schema версионирование** — модели проверяют `feature_schema_version` при загрузке, отказываются работать с несовместимым датасетом.
- **Config drift guard** — `config_hash` в meta.json, runtime config-mismatch → WARNING на старте.

## Структура проекта

```
pulse_bot/
├── models.py              # Token, Trade, ScoringResult (~80 fields), CreatorStats
├── config.py              # PulseBotConfig — 80+ параметров (env-overridable)
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

## Команды

```bash
# Live мониторинг — подключается к Pump.fun WS, скорит токены
python main.py monitor

# Бэктест — replay собранных данных через тот же Pipeline код
python main.py backtest

# Верификация — доказывает что backtest = live (должно быть 100%)
python main.py verify

# Grid search — перебор параметров
python main.py optimize

# Качество кода (pre-commit hook)
./qa

# Интеграционный тест: 300+ токенов, live → backtest 100% match (обязательный)
./verify300

# Dashboards
streamlit run pulse_bot/dashboard.py --server.port 8501           # live
streamlit run pulse_bot/backtest_dashboard.py --server.port 8502  # backtest results
```

## Backtest = Live (100% match) — обязательное условие

Бэктест использует тот же `Pipeline` код что и live бот. Разница только в источнике данных:

| | Live | Backtest |
|---|---|---|
| Launchpad | PumpFunLaunchpad (WebSocket) | ReplayLaunchpad (PostgreSQL) |
| Pipeline | тот же код | тот же код |
| Scorer | тот же код | тот же код |
| token_scores.source | 'live' | 'backtest' |

**Обязательный тест перед любым изменением:**
```bash
./verify300   # 15 мин live → backtest → 100% match на 300+ токенах
```

Быстрая верификация (2 мин):
```bash
python main.py monitor    # собрать данные (2+ мин)
python main.py verify     # replay + сравнить
```

Гарантии детерминизма:
- `insert_token` + `upsert_creator` — в main loop (последовательно)
- Creator snapshot — локальная переменная, не shared state (нет race conditions)
- Replay загружает ровно те же trade IDs что видел live (fast_trade_ids, full_trade_ids)
- Replay использует creator_reason из live scores (exact snapshot)
- FastFilter возвращает WAIT при 0 трейдов (не фейковый FAST_BUY)
- 12 unit тестов + интеграционный verify300

## Двухфазный скоринг

| Фаза | Время | Решение | Когда покупаем |
|------|-------|---------|---------------|
| Fast | 5 сек | FAST_BUY / WAIT | entry_mode = fast или both |
| Full | 45 сек | BUY / BORDERLINE / SKIP | entry_mode = full или both |

`entry_mode` задаётся в конфиге. Default: `fast`.

## 62 метрики на токен

Все записываются в SQLite для анализа и backtesting:

**Trade patterns:** buy_count, sell_count, unique_buyers, buy_volume_sol, buy_diversity, avg/median/std buy size, top3 concentration, repeat buyers, buy velocity trend, sell pressure, ...

**Bonding curve:** curve_progress_pct, curve_velocity, curve_acceleration, sol_to_graduation, market_cap_sol

**Token metadata:** name_length, symbol_length, has_uri, is_all_caps, has_numbers

**Timing:** hour_utc, creator_tokens_today, gap_create_to_first_trade

**P&L:** pnl_5th/10th/20th/50th/100th_pct — P&L если бы вошли на N-м покупателе

## Конфигурация

80+ параметров в `pulse_bot/config.py`:

- Fast phase: observe_seconds, min_buys, min_volume, max_sell_ratio, scoring weights
- Full phase: observe_seconds, score thresholds, buyer/volume/curve weights
- Execution: fee, slippage model, buy amount
- Pulse monitor: window_size, dead/weak buy rate, trend threshold
- Exit rules: stop loss, max hold, partial sells, moonbag
- Portfolio: initial balance, max positions

Все параметры конфигурируемые для grid search optimizer.

## Production deployment — Rich server (192.168.3.118)

> **ВАЖНО — где что запускается:**
> - **`pulse_bot monitor` (живой бот)** — **только на rich**, через systemd. Никогда не запускать на Mac.
> - **Mac** — только разработка: backtest, optimizer sweep, ML train, analytics.
> - **Dashboards (live + backtest)** — на rich, доступны по http://192.168.3.118:8501/8502.
> - **Backfill** и **Solana validator** — тоже на rich, через systemd.

```bash
# SSH alias настроен в ~/.ssh/config: Host rich → 192.168.3.118 user=sergey
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
systemctl --user restart pulse-bot.service       # после изменений в .env / коде
systemctl --user stop pulse-bot.service

# Unit file: ~/.config/systemd/user/pulse-bot.service
# Логи: ~/www/gg/logs/bot.log

# DB on rich: PG 16, user=sergeychernyakov password=pulsebot
PGPASSWORD=pulsebot psql -U sergeychernyakov -d pulse_bot -h localhost
```

**Sync БД между Rich (production) и Mac (dev) — on-demand:**

Rich = source of truth (там бот пишет 24/7). Mac забирает свежий snapshot когда нужен для retrain/sweep/анализа. Cron не настраиваем — синк по требованию.

```bash
# Rich → Mac (для свежих данных перед retrain/sweep, ~5 мин для 3GB БД):
ssh rich 'pg_dump -U sergeychernyakov -d pulse_bot -F c -Z 9' > /tmp/rich.dump \
  && pg_restore --clean --no-owner -d pulse_bot /tmp/rich.dump

# Mac → Rich (только при первом deploy или восстановлении):
pg_dump -d pulse_bot -F c -Z 9 -f /tmp/dump.dump
scp /tmp/dump.dump rich:/tmp/
ssh rich 'pg_restore -U sergeychernyakov -d pulse_bot /tmp/dump.dump'
```

## Быстрый старт (dev на Mac)

> Mac — **только dev**: backtest / optimizer / ML retrain / dashboards. Живой `monitor` запускать только на rich (см. выше).

```bash
git clone <repo-url>
cd gg
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
pip install xgboost psycopg2-binary asyncpg  # ML + Postgres deps (не в requirements.txt)

# ВАЖНО: всегда активировать venv перед запуском
source .venv/bin/activate

# Backtest на свежем dump из rich
python main.py backtest

# Dashboard для просмотра данных
streamlit run pulse_bot/dashboard.py --server.port 8501
```

### Переменные окружения

```bash
# Infrastructure
export PULSE_PG_DSN="postgresql://sergeychernyakov@localhost/pulse_bot"
export HELIUS_API_KEY="..."
export PULSE_POLICY="hybrid"          # hybrid | rules

# Phase 0 — extended trade collection post-scoring (КРИТИЧНО для ML labels)
export PULSE_EXTENDED_OBSERVE_SECONDS=600    # 0 = старое (обрыв на T+90s); 600 = +10мин

# Phase 4A — timer tick (default 5s, 0 = откл.)
export PULSE_TICK_SECONDS=5

# Phase 3 — T+30 dual-snapshot model (default OFF)
export PULSE_ENTRY_T30_ACTIVE=0       # =1 включить T+30 hook (раннее BUY/SKIP/DEFER)

# Phase 4B — survival model в tick loop (default OFF)
export PULSE_SURVIVAL_ACTIVE=0        # =1 включить predicted_remaining_life

# Phase 5 — entry-timing 15s checkpoints (default OFF)
export PULSE_TIMING_ACTIVE=0          # =1 включить 3-class WAIT/BUY/SKIP

# Exit ML
export PULSE_EXIT_ML_ACTIVE=1         # 0 = pure rules
export PULSE_EXIT_REGRESSION_ACTIVE=1  # quantile SL/TP heads (active 2026-04-23)

# Entry ML thresholds (override val-tuned defaults from meta.json)
export PULSE_ENTRY_PROBA_FLOOR=0.5    # ниже = SKIP
export PULSE_ENTRY_PROBA_CEILING=0.5  # выше = BUY (ML-only при floor=ceiling)

# Exit config (для sweep-скриптов)
export PULSE_EXIT_HARD_STOP_LOSS_PCT=15
export PULSE_EXIT_TAKE_PROFIT_PCT=100
export PULSE_EXIT_MAX_HOLD_SECONDS=90
export PULSE_EXIT_INACTIVITY_SECONDS=120

# Helius RPC concurrency (для T+30/60/120 captures)
export PULSE_HELIUS_HOLDER_CONCURRENCY=100
```

**Текущий рекомендуемый запуск (для накопления Phase 0 данных):**
```bash
PULSE_EXTENDED_OBSERVE_SECONDS=600 \
PULSE_TICK_SECONDS=5 \
.venv/bin/python main.py monitor
```

### Запуск тестов

```bash
# ОБЯЗАТЕЛЬНО: использовать python из .venv, не системный
source .venv/bin/activate
pytest tests/pulse_bot/ -q

# или без активации:
.venv/bin/python -m pytest tests/pulse_bot/ -q

# Интеграционный (15 мин, 300+ токенов, backtest=live match):
./verify300
```

## ML Models — 6 голов (state @ 2026-04-25)

| Голова | Файл | Статус | AUC | Когда работает | Env switch |
|---|---|---|---|---|---|
| **Entry main** (binary) | `entry_model.ubj` v18 | ACTIVE | 0.96 | T+90s | always (v17 fallback если нет) |
| **Entry regression** | `entry_model_reg.ubj` | ACTIVE (advisory) | — (Spearman 0.28) | T+90s | `PULSE_REGRESSION_ENTRY=1` |
| **Entry @T+30** | `entry_model_t30.ubj` v1 | OFF | 0.94 | T+30s | `PULSE_ENTRY_T30_ACTIVE=1` |
| **Entry-timing** (3-class) | `entry_timing_model.ubj` v1 | OFF | — | T+15/30/45/60/75s | `PULSE_TIMING_ACTIVE=1` |
| **Survival** (hazard) | `survival_model.ubj` v1 | OFF | — | tick loop ×10s | `PULSE_SURVIVAL_ACTIVE=1` |
| **Exit binary** | удалён 2026-04-25 | — | — | — | — |
| **Exit quantile SL** | `exit_quantile_sl.ubj` | ACTIVE | — | dynamic SL | `PULSE_EXIT_REGRESSION_ACTIVE=1` |
| **Exit quantile TP** | `exit_quantile_tp.ubj` | ACTIVE | — | dynamic TP | `PULSE_EXIT_REGRESSION_ACTIVE=1` |

### Текущие метрики на holdout (2026-04-25, schema v18)

```
Main entry model:
  AUC                  0.96   95% CI [0.95, 0.97]
  Precision@top-10%    7.97%   95% CI [6.3%, 10.0%]
  BUY zone (ceiling=0.6) WR=8.7%, ×11 base rate (0.81%)

Economic backtest:    NEGATIVE   PnL=−1.96 SOL @ proba=0.5 threshold
                      (модель не профитна на ОБРЕЗАННЫХ 90s данных —
                      см. Phase 0 в ROADMAP, ждём 2-3 недели данных)
```

### Phase status (см. `docs/ROADMAP_2026_05.md`)

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

⏳ Заблокировано до пользовательского решения:
   Активация Phase 3/4B/5 хуков на live боте
```

### Exit ML architecture (после удаления exit_model 2026-04-25)

После удаления бинарной exit модели (AUC 0.55, near-random) exit логика работает так:

1. **Hard rules first (immutable safety floor):**
   `creator_dump`, `pulse_dead`, `trend_dying`, `sell_pressure`, `buy_rate_drop`, `no_new_blood`, `whale_exit`, `near_graduation`, `hard_stop (−15%)`, `take_profit (+100%)`, `trailing_stop`, `timeout (90s)`

2. **Quantile heads (active, dynamic SL/TP):**
   - `exit_quantile_sl.ubj` (q=0.25) — tightens SL для high-risk токенов
   - `exit_quantile_tp.ubj` (q=0.75) — loosens TP когда модель ожидает большой gain

3. **Survival model (Phase 4B, default OFF, активируется через `PULSE_SURVIVAL_ACTIVE=1`):**
   Замена бинарной exit модели. Предсказывает `predicted_remaining_life`, форсит exit когда life < 30s.

Отсортировано по **gain importance** (последний v18 retrain, 2026-04-25).

| Feature | Смысл | Gain |
|---|---|---|
| `top5_120` | % у топ-5 холдеров на T+120s | 10544 |
| `buy_volume_sol_at_90` | Объём SOL за 90с (= total) | 8098 |
| `delta_unique_buyers_30_to_60` | Δ уникальных покупателей T+30→T+60 | 6710 |
| `hc_120` | Holders count на T+120 | 6082 |
| `fast_buy_count` | Покупок в первые 5 сек | 4893 |
| `top10_minus_top5_120` | Концентрация средней зоны (#6-10) | 4372 |
| `buy_vol_to_sell_vol_ratio` | Имбаланс по объёму | 2690 |
| `top1_120` | % у #1 холдера на T+120 | 2333 |
| `curve_velocity` | SOL/сек в bonding curve | 1823 |
| `delta_top1_30_to_60` | Δ концентрации T+30→T+60 | 1639 |
| `sol_price_usd` | SOL цена (market regime) | 1596 |
| `fast_buy_rate` | buys/sec первые 5 сек | 1376 |
| `hour_cos`, `hour_sin` | Cyclical encoding часа UTC | ~1300 |

### Removed features (clean-up history)

| Feature | Когда | Почему |
|---|---|---|
| `name_length`, `symbol_length`, `is_all_caps`, `has_numbers` | 2026-04-23 | H7 name patterns — 0 signal |
| `market_cap_sol`, `sol_to_graduation`, `log_market_cap` | 2026-04-25 (v16) | Momentum-bias leak, dominated #1-2 gain, валил economic_backtest |
| `token_price_sol` | 2026-04-25 (v17) | = market_cap_sol/1e9 на pump.fun (constant supply) — та же утечка |
| `fast_score`, `total_score` | 2026-04-22 | Circular dependency на rules которые ML заменяет |

### История iteration'ов (entry main model)

| Дата | Schema | AUC | Prec@top10% | Economic backtest | Комментарий |
|---|---|---|---|---|---|
| 2026-04-22 | v15 | 0.74-0.82 | 43-50% | −0.29 SOL | Pre-DOA, N=528 train rows |
| 2026-04-24 | v15+DOA | 0.98 | 41% | −0.56 SOL | DOA fix, N=47997, base rate 0.82% |
| 2026-04-25 | v17 | 0.98 | 8% | −0.56 SOL | Removed `market_cap_sol`, `sol_to_graduation`, `log_market_cap`, `token_price_sol` (momentum-bias leak) |
| 2026-04-25 | v18 | 0.96 | 8% | −1.96 SOL | Phase 2.5 time-aware (+13 фич): @30/@60/@90 + дельты. **Регрессия** ожидается по ROADMAP — модель overfit на noisy labels (90s обрезка) |

**Целевая метрика для прибыли:** economic_backtest > +0.3 SOL/week. **Блокер:** Phase 0 data accumulation. Все текущие модели обучены на 90с-обрезанных labels — winners не успевают вырасти до +50/+100%, поэтому любая селективность даёт −EV. После накопления Phase 0 данных (2-3 нед) labels станут честными → re-train + sweep на честной EV.

### Правила feature management

1. **N rows / feature ≥ 20** — нарушение → overfit (см. v18 регрессия выше)
2. **Добавлять по 1-2 за раз** и мерить до/после
3. **Feature stability protocol** перед удалением: `feature_stability.py` с 5 seeds; убирать только `STABLE_DEAD` в TWO sequential schema versions
4. **Известные leak-фичи** (KNOWN_LEAK_FEATURES в `daily_validation.py`): `market_cap_sol`, `mc_at_scoring`, `sol_to_graduation`, `v_sol_in_bonding_curve`, `v_tokens_in_bonding_curve`, `log_market_cap`, `token_price_sol`. Появление в top-10 gain = регрессия

## Документация

- [Roadmap 2026-05](./docs/ROADMAP_2026_05.md) — Phase 0-6 plan + status
- [CHANGELOG](./docs/CHANGELOG.md) — журнал всех ML / behavior изменений
- [Python Style Guide](./PYTHON_STYLE_GUIDE.md)
- [Agent Instructions](./AGENTS.md)

## Автор

**Sergey Chernyakov** — Telegram: [@AIBotsTech](https://t.me/AIBotsTech)
