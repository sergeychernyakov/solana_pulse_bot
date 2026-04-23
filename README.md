# Solana Pulse Bot

Бот-наблюдатель для мемкоин-лончпадов на Solana. Наблюдает за новыми токенами, оценивает органичность интереса, принимает решение о покупке. Двухфазный скоринг: быстрый вход (5 сек) + полный анализ (45 сек). 62 метрики на токен, 80+ конфигурируемых параметров.

Бэктест использует тот же код что и live бот — **100% совпадение решений** (верифицировано на 178 токенах).

## Архитектура

```
Pipeline (один код для live и backtest):

  Token CREATE (WS или replay)
    │
  Main loop (sequential, deterministic):
    ├─ insert_token
    ├─ upsert_creator
    ├─ snapshot creator stats
    │
  Parallel task (per token):
    ├─ Phase 1: Fast (5s) → FAST_BUY / WAIT
    ├─ Phase 2: Full (45s) → BUY / SKIP / BORDERLINE
    ├─ Score with 62 metrics + creator snapshot
    └─ Store to token_scores (source='live' or 'backtest')
```

Ключевой принцип: **все обновления состояния в main loop (последовательно), скоринг в parallel tasks (с замороженным snapshot)**. Это даёт 100% детерминизм при сохранении параллелизма.

## Структура проекта

```
pulse_bot/
├── __init__.py
├── models.py              # Token, Trade, ScoringResult (62 fields), CreatorStats
├── config.py              # PulseBotConfig — 80+ параметров для backtesting
├── db.py                  # SQLite с WAL mode, token_scores (source: live/backtest)
├── pipeline.py            # Pipeline — единый код для live и replay
├── clock.py               # Clock ABC + RealClock + SimulatedClock
├── portfolio.py           # Balance, positions, P&L tracking
├── execution.py           # SimulatedExecution — slippage model on bonding curve
├── backtest.py            # BacktestEngine — full cycle replay
├── optimizer.py           # Grid search over parameter combinations
├── dashboard.py           # Streamlit: live token monitoring
├── backtest_dashboard.py  # Streamlit: optimization results
│
├── sources/
│   ├── backtest.py        # BacktestSource — SQLite replay
│   └── replay.py          # ReplayLaunchpad — same Launchpad interface as live WS
│
├── launchpads/
│   ├── base.py            # Launchpad ABC
│   └── pumpfun.py         # PumpFun WS: create events, trade streaming
│
├── filters/
│   ├── base.py            # Filter ABC
│   ├── fast.py            # FastFilter — 5 sec entry decision
│   ├── observation.py     # ObservationFilter — full 45 sec analysis
│   ├── metrics.py         # MetricsCalculator — computes all 62 metrics
│   ├── creator.py         # CreatorFilter — creator history
│   └── scorer.py          # Scorer — aggregates filters, produces decision
│
└── pulse/
    ├── monitor.py         # PulseMonitor — sliding window, trend detection
    └── exit_manager.py    # ExitManager — 10 configurable exit rules
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
| Launchpad | PumpFunLaunchpad (WebSocket) | ReplayLaunchpad (SQLite) |
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

## Быстрый старт

```bash
git clone <repo-url>
cd gg
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# Запустить мониторинг
python main.py monitor

# В другом терминале — dashboard
streamlit run pulse_bot/dashboard.py --server.port 8501
```

## ML Models — Цели и параметры

### Главные цели

| Модель | Главная метрика | Текущее | **Цель** |
|---|---|---|---|
| **Entry** (binary XGBoost + regression head) | **Precision@top-10%** — из топ-10% предсказаний сколько реально winners | **54.5% (binary) / 60% (regression)** | **>60%** (чтобы покрыть fees+slippage) |
| **Exit** (binary XGBoost + quantile heads shadow) | **AUC on exit-now signal** + realized override PnL | AUC 0.72, entry_ml_proba #1 feature | AUC>0.75, net PnL uplift vs rules-only |

Base rate (winner без модели) = **14%** — regression head уже в 4× лучше случайного.

### Exit ML architecture (v2, 2026-04-23, codex-reviewed)

Exit делает 4-way gated decision через `ExitMLPolicy.decide_with_confidence`:

| Условие | Action | Что делает |
|---|---|---|
| `proba ≥ 0.80` (`sell_ceiling`) | **SELL_ALL** | Форсит полный выход (escalates hold) |
| `0.55 ≤ proba < 0.80` | **SELL_PARTIAL** | Частичный выход, размер по proba |
| `0.20 ≤ proba < 0.55` | **RULES** | Defer to rule-based cascade |
| `proba < 0.20` AND `pnl ≥ -5%` | **HOLD_HARD** | Блок **только** `weak_pulse_profit` |

**Sizing ladder** (hardcoded priors, codex E5 — не val-tune на том же N=1686):
- `0.55-0.65` → 30%
- `0.65-0.75` → 50%
- `0.75-0.80` → 70%
- `≥ 0.80` → 100% (SELL_ALL path)

**Invariants (safety floor — immutable, always checked first):**

`creator_dump`, `pulse_dead`, `trend_dying`, `sell_pressure`, `buy_rate_drop`, `no_new_blood`, `whale_exit`, `near_graduation`, `hard_stop (-15%)`, `take_profit (+100%)`, `trailing_stop (+50%/-50%)`, `timeout (90s)` — эти hard rules проверяются до ML gating. `HOLD_HARD` **не может** их блокировать. `strong_profit` partial (>+200%) тоже immutable.

**Cross-model coupling — ВРЕМЕННО ОТКЛЮЧЕНО (exit_v3, 2026-04-23):**

`entry_ml_proba` был включён как фича в exit model (v2, 2026-04-23) и давал #1 gain. Но при попытке активировать regression entry:
- Classifier + NaN: exit AUC 0.6227 ✅
- Regression + NaN: exit AUC 0.48 ❌ (regression predictions с spearman 0.28 на N=661 — слишком шумные для coupling)

Принято решение убрать cross-feature **до** стабилизации regression. Live сейчас работает:
- Entry: **regression** (precision@top10% 60%, Avg PnL@top10% +8.92%)
- Exit: AUC 0.5472 — независимая модель, без cross-feature (−7pp от v2, но decoupled от regression noise)
- Tandem: entry precision +5.5pp перевешивает exit AUC −7pp (exit всё равно имеет safety floor на immutable rules)

**TODO — восстановить cross-feature когда:**
- N_entry ≥ 2000 AND regression spearman_rho ≥ 0.45, ИЛИ
- Выбрать путь classifier-only для cross-feature (regression entry активна, но в exit передаётся proba от classifier)

При restore — вернуть `entry_ml_proba` в `EXIT_FEATURE_ORDER`, схема bump → v4, reinstate `_precompute_entry_probas` в build_dataset, cross-model hash gate в `ExitMLPolicy.from_path`, и `test_entry_proba_train_serve_parity`.

**Quantile regression heads** (E3, shadow only, `PULSE_EXIT_REGRESSION_ACTIVE=0` по умолчанию): `exit_quantile_sl.ubj` (q=0.25) для SL-tightening и `exit_quantile_tp.ubj` (q=0.75) для TP-loosening. Активация требует 2 недели shadow + paired bootstrap 500 resamples показывает что bucket бьёт фиксированный порог на ≥1σ.

**Env var rollback (3-layer disable):**
- `PULSE_EXIT_ML_ACTIVE=0` — полностью откл. ML exit override
- `PULSE_EXIT_ML_HOLD_HARD=0` — откл. только HOLD_HARD блок
- `PULSE_EXIT_REGRESSION_ACTIVE=0` — откл. dynamic SL/TP (default)

**Observability:** `ExitManager.ml_counters` отслеживает `ml_override_count`, `ml_partial_count`, `ml_hold_hard_count`. Каждый ML-driven exit пишет reason `ml_exit_trigger` / `ml_partial_trigger` / `ml_hold_hard_blocked_weak_pulse` в `paper_trades`.

### Active features (31 — use by model)

Отсортировано по **gain importance** (сколько деревья их реально используют).

| Feature | Смысл | Gain | Статус |
|---|---|---|---|
| `avg_buy_sol` | Средний размер покупки | 43.1 | ✅ Top |
| `buy_volume_sol` | Общий объём SOL вложено | 42.4 | ✅ Top |
| `fast_volume_sol` | Объём первые 5 сек | 24.4 | ✅ Top |
| `top3_buyer_pct` | % у топ-3 покупателей | 24.2 | ✅ Top |
| `max_buy_sol` | Самая крупная покупка | 21.6 | ✅ Top |
| `unique_buyers` | Уникальных покупателей | 20.3 | ✅ Top |
| `creator_median_peak_mc_sol` | Средний пик MC у прошлых токенов creator'а | 20.2 | ✅ Top |
| `top1_30` | % у #1 холдера на T+30s (Helius) | 18.9 | ✅ Top |
| `fast_sell_ratio` | Доля продаж в первые 5 сек | 18.7 | ✅ Top |
| `top1_delta` | Изменение концентрации T+30→T+120 | 18.2 | ✅ Top |
| `sell_pressure` | sell_count / buy_count | ~17 | ✅ Medium |
| `total_score` | Hand-tuned scorer output | ~15 | ⚠️ Circular (output rules we replace) |
| `fast_score` | Hand-tuned fast scorer output | ~14 | ⚠️ Circular |
| `curve_velocity` | SOL/сек в bonding curve | ~13 | ✅ Medium |
| `curve_acceleration` | Ускорение curve | ~13 | ✅ Medium |
| `first_buy_sol` | Размер первой покупки | ~12 | ✅ Medium |
| `repeat_buyer_count` | Сколько кошельков купили >1 раз | ~12 | ✅ Medium |
| `buy_size_trend` | Ratio avg 2-й половины / 1-й | ~12 | ✅ Medium |
| `buy_velocity_trend` | Ratio rate 2-й половины / 1-й | ~12 | ✅ Medium |
| `time_to_first_buy` | Сек от создания до 1-й покупки | 11.9 | 🔻 Low |
| `pnl_at_fast_entry_pct` | PnL% если бы вошли на T+5s | 10.4 | 🔻 Low |
| `creator_age_days` | Возраст кошелька creator'а | 9.7 | 🔻 Low |
| `buy_diversity` | Сколько разных размеров покупок | 8.7 | 🔻 Low |
| `hc_30` | Количество холдеров T+30s | 8.4 | 🔻 Low |
| `concurrent_observations` | Tokens параллельно бот смотрит | 6.8 | 🔻 Low |
| `top5_30` | % у топ-5 холдеров T+30s | 5.6 | 🔻 Low |
| `tokens_last_5min` | Активность рынка | ~5 | 🔻 Low |
| `top1_120`, `top5_120`, `top5_delta` | Helius T+120 snapshots | 3-5 | 🔻 Low |
| `buy_count` | Число покупок | 2.9 | 🔻 Low |
| `sell_count` | Число продаж | 2.8 | 🔻 Low |

### Dead features (23 — в schema но не используется ни одним деревом)

**Gain = 0**, безопасно удалить без потери качества. Duplicate-сигналы которые модель игнорирует в пользу других.

| Feature | Смысл | Почему dead |
|---|---|---|
| `hour_sin` / `hour_cos` | Час UTC (cyclical encoding) | Время дня не даёт signal |
| `has_uri` | Есть ли metadata URI | ~100% токенов имеют → 0 variance |
| `helius_snapshot_complete` | 1 если Helius snapshot получен | Duplicate с hc_30 |
| `fast_buy_count` | Покупок в первые 5 сек | Duplicate с buy_count |
| `fast_unique_buyers` | Уникальных в 5 сек | Duplicate с unique_buyers |
| `median_buy_sol` | Медианная покупка | Duplicate с avg_buy_sol |
| `sell_volume_sol` | Объём продаж | Модель выбрала buy_volume |
| `first_half_buy_rate`, `second_half_buy_rate` | Rate по половинам | Raw halves уже в buy_velocity_trend |
| `avg_first_half_buy_sol`, `avg_second_half_buy_sol` | Средний чек по половинам | Duplicate |
| `creator_tokens_today` | Сколько токенов dev сегодня | 90% имеют 1-5 → слабый variance |
| `creator_inter_token_interval_sec` | Интервал между запусками | Duplicate с age |
| `creator_total_prior_tokens` | Всего токенов у dev'а | Correlated с age |
| `curve_progress_at_t30/t60/t90` (3) | Форма curve в разных точках | Добавлены 2026-04-23, signal не дали |
| `time_gap_median_first20` | Интервалы между первыми 20 trades | Добавлена 2026-04-23, signal не дала |
| `buy_volume_first10s` | Объём SOL в первые 10 сек | Duplicate с buy_volume_sol |
| `unique_buyers_first30s`, `unique_buyers_last30s` | Уникальные в окнах | Duplicate с unique_buyers |
| `full_trade_count` | Общее число trades | Duplicate (buy_count+sell_count) |

### Candidate features (не добавлены — wait for data или нужна инфра)

| Feature | Почему не добавили | Effort | Condition для активации |
|---|---|---|---|
| `launchpad` (pumpfun / letsbonk / other) | 99.6% pumpfun → 0 variance | 30 мин | Летsbonk накопит ~500+ labeled |
| `sol_price_usd` | Capture с 2026-04-22, мало данных | 1ч | 5-7 дней накопления |
| `sol_price_1h_change_pct` | Нужен SOL price history | 2ч | После sol_price accumulation |
| `mint_authority_revoked` | 30/30 pump.fun auto-revoke → 0 variance | 0 (ready) | При non-pumpfun launchpad |
| `freeze_authority_revoked` | То же | 0 (ready) | При non-pumpfun launchpad |
| `buyers_new_wallet_pct` | Helius wallet age per buyer | 4-6ч | При N ≥ 2000 |
| `buyers_avg_sol_balance` | Helius balance fetch | 4ч | При N ≥ 2000 |
| `top_buyer_prior_pnl_sum` | Wallet-level PnL индексация | 40-80ч | Долгосрочно |
| `bundled_buy_fraction` | Tx signature parsing (coord-bot) | 15-25ч | Codex flagged as fragile |
| `pumpfun_tokens_per_minute_now` | Market heat indicator | 1ч | Future |
| `tokens_graduated_last_hour` | Graduation heat | 2ч | Future |
| `creator_active_tokens_now` | Co-trading context (multi-token dev) | 4ч | Future |

### Removed features (пробовали, убрали)

| Feature | Why removed |
|---|---|
| `name_length`, `symbol_length`, `is_all_caps`, `has_numbers` | H7 name patterns — 0 signal, removed 2026-04-23 |

### История iteration'ов

| Дата | AUC | Precision@top-10% | Комментарий |
|---|---|---|---|
| Initial binary | 0.735 | — | 38 features baseline |
| +label v2 (fees в labels) | 0.822 | 43% | **Big win** — honest label |
| +has_uri, market context | 0.740 | 43% | Marginal |
| Remove circular fast/total_score | 0.724 | — | Codex ablation check |
| Retroactive backfill (+19 labeled) | 0.777 | 43% | More data, slight lift |
| +pnl_at_fast_entry_pct + trade_counts | **0.818** | **50%** | **Big lift** — trade partition matters |
| +7 cheap features (curve/time gaps) | 0.801 | **36%** | ❌ **Overfit at N=661** — все 7 dead |

### Правила feature management

1. **N rows / feature ≥ 20** — нарушили когда добавили 7 cheap features (8 rows/feature) → overfit
2. **Добавлять по 1-2 за раз** и мерить до/после
3. **Ablation test** обязателен если features > 30 при N < 1500
4. **Dead features удаляем** — нет смысла в schema overhead
5. **Candidate при 0 variance** держать в infrastructure, не добавлять в `ENTRY_FEATURE_ORDER` пока variance не появится

## Документация

- [Техническая спецификация](./docs/SOLANA_PULSE_BOT_SPEC.md)
- [Python Style Guide](./PYTHON_STYLE_GUIDE.md)

## Автор

**Sergey Chernyakov** — Telegram: [@AIBotsTech](https://t.me/AIBotsTech)
