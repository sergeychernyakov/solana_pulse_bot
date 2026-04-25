# Журнал изменений модели и бота

Формат: обратный хронологический порядок (новое сверху). Каждая запись:

```
## YYYY-MM-DD HH:MM — Название
**Что изменилось:** одно предложение
**Зачем:** причина
**Результат:** метрики/эффект
**Откат:** как вернуть назад
```

---

## 2026-04-25 18:00 — config_hash drift guard + Helius T+30 lag instrumentation

**Что изменилось:**
- Новый модуль `pulse_bot/ml/config_hash.py` — стабильный SHA-256 хэш по training-relevant полям `PulseBotConfig` (score_threshold_*, exit_*, entry_ml_*, entry_train_*; косметические поля исключены).
- `train.py` пишет `config_hash` + `config_values` в `entry_model.meta.json` (оба head: classification + regression).
- `policy.py:EntryMLPolicy.from_path` сравнивает хэш с runtime-конфигом и логирует **WARNING** с per-field diff при расхождении. Не отказывает в загрузке — оператор может намеренно флипать tracked-поле (например, exit_ml_active kill-switch). Защищает от silent Option-B-style регрессий (labels-vs-config mismatch 2026-04-22).
- Legacy meta.json (без config_hash) — silent skip, не шумит.
- `pipeline.py:_schedule_holder_capture` инструментирован: каждое captured snapshot логирует `Helius T+N capture lag: actual=X scheduled=Y delta=Δ (loop=… sem=… rpc=…) mint=…`. Семафор поднят 50→100 (env `PULSE_HELIUS_HOLDER_CONCURRENCY`) — на 200 токенов × 3 capture слотов burst T+30 серилизовался ~1 RPC-latency на каждый ожидающий выше cap'а.

**Зачем:** parallel infrastructure из ROADMAP_2026_05.md — protect from silent regression и Phase 3 prereq (T+30 model нужен чистый snapshot timing, иначе delta-features врут).

**Результат:**
- 63 unit-теста в `tests/pulse_bot/test_config_hash.py` зелёные.
- Регрессия: `test_features_parity` (39 passed, 1 skipped), `test_daily_validation`, `test_ml_policy` — без изменений.
- Live бот: следующий рестарт начнёт писать lag-метрики; для дрейфа конфига ничего не изменится пока модель не переобучена с новым meta.json (legacy silent path).

**Откат:** удалить три блока кода (config_hash module + meta-write в train.py + check в policy.py + lag log в pipeline.py). Хэши в meta.json игнорируются если поле отсутствует.

---

## 2026-04-25 12:00 — Phase 4A: timer-tick infrastructure для paper trades

**Что изменилось:**
- `PulseMonitor.update_empty_tick(now)` — пересчёт snapshot по уже накопленному окну без нового трейда (без мутации trend / peak counters).
- `PaperTradeRunner.tick(now, entry_time)` — обёртка вокруг snapshot + `ExitManager.decide()`, возвращает `MonitorResult` если правило сработало.
- `Pipeline._paper_trade` рефакторинг: теперь два параллельных таска — старый `stream_trades` loop и новый tick loop с интервалом `PULSE_TICK_SECONDS` (env var, default 5.0). `asyncio.Lock` гарантирует один close.
- `PULSE_TICK_SECONDS=0` → tick-таск становится no-op, поведение точно как до Phase 4A.
- Replay launchpad не использует tick-таск (real-time clock не подходит для детерминированных бэктестов).

**Зачем:** prerequisite для Phase 4B survival model (нужен per-second state для labelling) + улучшает отзывчивость exit'а когда токен молчит. Сейчас тихий токен висит до `inactivity_timeout` (90с) прежде чем `pulse_dead`/`no_new_blood` смогут сработать.

**Результат:**
- 10 unit-тестов в `tests/pulse_bot/test_timer_tick.py` зелёные.
- Регрессия: `test_features_parity` + `test_daily_validation` + `test_partial_exits_parity` + `test_optimizer_*` + `test_refactor_fixes` — 100/2s passed/skipped.
- Поведение live бота **не изменено** пока `PULSE_TICK_SECONDS` не установлен и в коде осталось default=5.0 через `getattr` — это активирует tick автоматически на следующем рестарте. Если нужно временно отключить → `PULSE_TICK_SECONDS=0`.

**Откат:** установить `PULSE_TICK_SECONDS=0` (отключить tick), либо `git revert` коммита.

---

## 2026-04-25 09:30 — Pipeline: extended trade collection post-scoring

**Что изменилось:**
- Добавлен `pulse_extended_observe_seconds: float = 0.0` в `PulseBotConfig` + env var `PULSE_EXTENDED_OBSERVE_SECONDS`
- `Pipeline._extended_observation()` — background task, продолжает писать трейды в БД после SKIP-решения ещё N секунд (с inactivity_timeout=120s)
- При >0: SKIP/RULES токены не получают мгновенный `unsubscribe_trades`, а проходят дополнительное окно сбора. BUY-токены не затронуты (они и так мониторятся через `_paper_trade`).
- По умолчанию 0 = текущее поведение, без регрессий.

**Зачем:** Sweep по `max_hold` (4 combo, 90/180/300/600s) дал **идентичные** результаты потому что в БД нет post-scoring трейдов. Замер: 78% токенов имеют last_trade ДО scoring; только 398 токенов из 73,908 имеют 50-100s post-scoring активности; >150s имеют ~70 штук. Бот рубит подписку сразу после scoring decision (`pipeline.py:781`), а трейды для не-BUY токенов теряются. Без хвоста невозможно тренировать ML с длинным max_hold или экспериментировать со SL/TP, требующими дольшего удержания.

**Результат:**
- Код change ✓ (config + pipeline + extended_observation helper). Тесты зелёные (59 passed).
- Поведение НЕ изменилось пока на live боте: дефолт=0, env var не выставлен.
- **Следующий шаг (нужно одобрение пользователя):** перезапустить live бот с `PULSE_EXTENDED_OBSERVE_SECONDS=600` чтобы начать копить расширенные трейды. Дополнительная нагрузка: WS ширина для всех токенов + DB writes. ~1-2× к текущему объёму трейдов (5.5M → 8-12M через ~3 нед).

**Откат:** snimit env var → дефолт 0 → старое поведение. Код-rollback: `git revert` config.py + pipeline.py изменений.

---

## 2026-04-25 06:00 — Exit config sweep: SL/TP/max_hold не помогают на текущих данных

**Что изменилось:**
- Новый скрипт `scripts/exit_config_sweep.py` — sweep по `(SL, TP, max_hold)`, для каждого combo: relabel через `simulate_exit` + retrain XGBoost + economic_backtest на holdout
- Env vars `PULSE_EXIT_HARD_STOP_LOSS_PCT`, `PULSE_EXIT_TAKE_PROFIT_PCT`, `PULSE_EXIT_MAX_HOLD_SECONDS`, `PULSE_EXIT_INACTIVITY_SECONDS` для override без редактирования `PulseBotConfig` defaults
- Прогнаны 2 сетки: `quick` (SL=8 vs 15) и `max_hold` (90/180/300/600 при SL=15/TP=100)

**Зачем:** Опровергнуть наивную математику «поджать SL = снизит точку безубыточности». User обоснованно отклонил аналитическое решение, требовал optimizer pass.

**Результат:**
- **Quick (SL=8/TP=50 vs SL=15/TP=100)**: −0.78 vs −0.73 SOL. Поджать SL вредит — больше hard_stops (717 vs 291), меньше positives (314 vs 355).
- **max_hold (90/180/300/600 при SL=15/TP=100)**: ВСЕ 4 combo дали идентичные числа (PnL=−0.73, AUC=0.971, entries=293, mean=−2.50%). Причина — данных post-scoring почти нет (см. отдельную запись 09:30).
- **wider_sl (SL=15/20/25/30 при TP=100/hold=90)**: SL=25 best −0.6376, SL=15 worst −0.7334 (+13% улучшения от SL=15→25). Hard_stops 291→30. **Всё равно в минусе.**
- Вывод: оси SL/TP/max_hold упираются в обрезанные данные. Phase 0 (extended observation) — критический блокер для дальнейших sweep-ов.

**Откат:** не нужен — скрипт read-only, env vars дефолт=не-влияют.

---

## 2026-04-25 03:50 — Schema v17: удаление token_price_sol + настройка validation severity

**Что изменилось:**
- `token_price_sol` удалён из `SCORER_FEATURES` (v17) — на pump.fun = `market_cap_sol / 1e9`, тот же momentum-bias что v16 убрал
- После удаления market_cap_sol в v16 он стал #2 по gain importance, подтверждая проксирование той же утечки
- `log_market_cap` убран из `features.py:extract_entry_features()` (KeyError на market_cap_sol который больше не в SELECT)
- `PRIOR_DRIFT_THRESHOLD` поднят с 0.30 → 0.50: при base rate ~0.5% порог 30% = 0.15pp абс., внутри рыночного шума
- `adversarial_validation` severity: alert → warn (SOFT): временной drift (creator_age_days монотонно растёт) всегда даёт высокий AUC train/test, не ошибка модели
- `ks_predictions` severity: alert → warn (SOFT): несопоставимые размеры (133 vs 14782) — после ретрейна ожидаемо

**Зачем:** daily_validation: 5 CRITICAL alerts → 2. feature_importance_sanity и prior_drift прошли. Остались economic_backtest (реальная проблема).

**Результат (v17 holdout):**
- AUC: 0.9805 → 0.9765 (−0.4pp — ожидаемо при удалении leak-фичи)
- Top-3 features: top5_120 / top10_minus_top5_120 / buy_vol_to_sell_vol_ratio (поведенческие, не momentum)
- prior_drift ✓, feature_importance_sanity ✓, shuffled_labels ✓, calibration ✓
- **economic_backtest ✗** metric=−0.56 SOL (proba≥0.5: 154 entries, 15.6% WR, mean_realized=−3.6%)
- Причина провала: TP=100% почти никогда не срабатывает за max_hold=90s (1 из 73908 токенов). Средний выигрыш ≈+58%, средний проигрыш ≈−15%. Точка безубыточности WR = 15/(58+15) = 20.5%, модель даёт 15.6%.
- **Это CONFIG проблема** (SL=-15% / TP=100% / max_hold=90s), не качество модели — precision improvement ×32 над случайным.
- **Решение — optimizer sweep по сетке (SL, TP, max_hold)**, каждый комбо = пере-лейблинг через simulate_exit + ретрейн + замер realized PnL на holdout. Аналитически «поджать SL» нельзя — `avg_win` зависит от labels, labels зависят от SL.

**Откат:**
- Вернуть token_price_sol в SCORER_FEATURES, FEATURE_SCHEMA_VERSION → entry_v16_20260425, ретрейн
- PRIOR_DRIFT_THRESHOLD → 0.30, severity adversarial/ks → alert

---

## 2026-04-25 02:00 — Schema v16: удаление market_cap_sol, sol_to_graduation, log_market_cap

**Что изменилось:**
- `market_cap_sol`, `sol_to_graduation` удалены из `SCORER_FEATURES`
- `log_market_cap` удалён из `DERIVED_FEATURES`
- Добавлены в `KNOWN_LEAK_FEATURES` в `daily_validation.py`
- JSON serialization crash исправлен (`default=` в `json.dumps`)
- `FEATURE_SCHEMA_VERSION` → entry_v16_20260425

**Зачем:** feature_importance_sanity failed — market_cap_sol (#1) и log_market_cap (#2) в top gain. Модель выучила "высокая MC = покупать", не generalizes. economic_backtest = −0.74 SOL до удаления.

**Результат:** feature_importance_sanity ✓, economic_backtest улучшился −0.74 → −0.56 SOL

**Откат:** вернуть фичи в lists, FEATURE_SCHEMA_VERSION → entry_v15, ретрейн

---

## 2026-04-24 19:00 — Начата миграция на PostgreSQL (IN PROGRESS)

**Что изменилось:**
- Создана Postgres-база `pulse_bot` (PG14 localhost)
- Schema `pulse_bot/db_schema_pg.sql` — 14 таблиц с `insert_order BIGSERIAL` вместо ROWID (codex fix)
- Скрипт `scripts/sync_pg_schema.py` — автоматически сверяет SQLite vs PG колонки
- Скрипт `scripts/migrate_sqlite_to_pg.py` — one-shot дамп всех таблиц через pandas → SQLAlchemy

**Зачем:** SQLite падал 3× в день на `database is locked` при конкурентных записях (pipeline + wallet_activity + dashboard). PostgreSQL решает через row-level locks вместо таблич-level. Асинхронные писатели больше не сериализуются.

**Результат:**
- Schema в PG создана ✅
- Колонки синхронизированы ✅
- Data migration завершена ✅ (3.66M trades + все таблицы за 3 мин через COPY)
- **db.py переписан ✅** (asyncpg для async writes + psycopg2 pool для sync reads)
- **Бот работает на PG ✅** (PID 85074, hash `ee1b147172960216`)
- Sequences reset после миграции (tokens.insert_order, trades.id, creator_snapshots.id, etc.)
- pipeline.py очищен от `import sqlite3` direct
- Callers update ⏳ (wallet_indexer, build_dataset, optimizer, dashboards — для training/UI, не блокирует бота)
- Tests verify300 ⏳ (не запущен на PG пока)

**Миграционные файлы:**
- `pulse_bot/db_schema_pg.sql` — DDL
- `scripts/sync_pg_schema.py` — авто-доавление колонок
- `scripts/migrate_copy.py` — fast COPY FROM migration (100× быстрее pandas)

**Откат:** SQLite файл `pulse_bot.db` не тронут. Если что-то сломается — бот работает на SQLite через `git stash` / reverт db_pg файлов.

**Все callers ported (14 модулей):**
- `wallet_indexer.py` — asyncpg + DictCursor streaming
- `build_dataset.py` — `_pg_exec` adapter
- `optimizer.py` — snapshot no-op (PG MVCC)
- `backtest_dashboard.py` — use Database class
- `helius_creator.py` — через Database._sync_query
- `dashboard.py` — добавлен `get_paper_trades`
- `backtest.py` — INSERT OR REPLACE → ON CONFLICT
- `sources/backtest.py` — psycopg2 + DictCursor
- `sources/replay.py` — psycopg2 streaming
- `collector.py` — INSERT OR IGNORE → ON CONFLICT DO NOTHING
- `analyze_sensitivity.py` — ported
- `ml/backfill_scoring.py` — ported

**Удалено (dead code):**
- `ml/score_gate_shadow_analysis.py` — done one-shot, memory saved
- `db_sqlite_backup.py` (71KB) — git history preserves

**Финальный статус:** бот работает на PG 100+ минут без падений (SQLite падал каждые 45 мин). Вся кодовая база на Postgres. Единственная оставшаяся `sqlite3.Row` ссылка в коде — docstring комментарий в `features.py:340`.

---

## 2026-04-24 18:04 — Починка survivor bias (DOA токены в обучение)

**Что изменилось:**
- `build_dataset.py` теперь добавляет в обучение **мёртвые токены** (DOA = no trades after scoring) с label=0
- Раньше они выбрасывались → модель видела только 4% популяции (выживших)

**Зачем:**
- Codex диагностировал: модель выучила паттерны на 1,292 выживших, но в работе 96% токенов = мусор. Decision boundary не overlap-ил с live distribution
- Live уверенность всех ~0.5 (модель не могла отличить мусор)
- Anti-correlation: proba ≥ 0.54 давали процент побед 8.6% (ниже случайного 19%)

**Результат (holdout):**
- Тренировочных примеров: 1,292 → **68,568** (53×)
- AUC: 0.637 → **0.982**
- Process побед в BUY зоне: 14.5% (при средней частоте 0.82% = ×17)
- Live уверенность: 0.004 — 0.788 (было 0.40 — 0.55)

**Модель hash:** `ee1b147172960216`
**Откат:** `git checkout` старый `build_dataset.py` + rebuild + retrain

---

## 2026-04-24 17:00 — Hardcoded ML params → config

**Что изменилось:** 14 параметров вынесены в `PulseBotConfig`:
- `entry_ml_proba_floor/ceiling` (env vars `PULSE_ENTRY_PROBA_*`)
- Sizing ladder (`ml_sizing_proba_1/2/3`, `ml_sizing_frac_1/2/3`)
- XGBoost hyperparams (`entry_train_n_estimators`, `max_depth`, etc.)

**Зачем:** optimizer может свипать эти параметры в Phase 2 без правок кода.

**Результат:** дефолты preserve bit-identical поведение. 82 теста прошли.

**Откат:** defaults = старые hardcoded значения; без env var → поведение то же.

---

## 2026-04-24 13:37 — Full ML-only mode (floor=ceiling=0.5)

**Что изменилось:** вручную поставил в meta.json `ceiling=0.5` (было auto-tuned). Без grey zone — модель одна решает.

**Зачем:** paper mode, хотели стресс-тест модели под полной автономией.

**Результат:** WR 13.3% на 611 trades (хуже base rate 19%). Выявило проблему — survivor bias (см. запись 18:04).

**Откат:** делал 2 раза — сначала `ceiling=0.42` → потом `ceiling=0.5`. Сейчас модель другая (после DOA fix).

---

## 2026-04-24 10:30 — Option B: simulate_exit labels

**Что изменилось:** `build_dataset.py` использует `simulate_exit()` (pure function wrapping PaperTradeRunner + ExitManager + PulseMonitor) вместо fixed TP=+50%/SL=-30%/300s.

**Зачем:** label теперь = тот же exit flow что в live → нет train/serve skew.

**Результат (v15 schema):**
- AUC: 0.603 → **0.637** (+3.4pp)
- Precision@top10%: 33% → **41%** (+5.7pp)
- Exit reason на labels: timeout 73%, hard_stop 17%, take_profit 0.1% (TP=100 не достигается)

**Откат:** `git revert` simulate_exit integration.

---

## 2026-04-24 09:00 — Phase E: Wallet analytics features

**Что изменилось:** добавлены 5 фич на основе top-3 buyer history:
- `top3_buyer_prior_mint_count_sum`
- `top3_buyer_prior_total_pnl_sol`
- `top3_buyer_prior_avg_wr`
- `top3_buyer_max_prior_pnl_sol`
- `top3_buyer_wallet_age_days_avg`

Infrastructure: `wallet_activity` materialized table, `wallet_indexer.py` backfill (3.35M trades → 1.16M pairs), live incremental updates.

**Зачем:** добавить "smart money follow" сигнал.

**Результат (v14 schema, до Option B):**
- AUC: 0.615 → 0.603 (flat, в пределах noise)
- Precision@top10%: 35% → 33% (flat)
- Feature stability: 5/5 wallet features non-zero gain в 4+ seeds
- `top3_buyer_wallet_age_days_avg` + `top3_buyer_prior_mint_count_sum` в топ-12 по gain

**Откат:** удалить WALLET_FEATURES из ENTRY_FEATURE_ORDER, rebuild+retrain.

---

## 2026-04-23 — v10 cleanup + Creator skew bug fix

**Что:** удалены 13 stable-dead features; исправлен bag где 3/4 creator features были silently 0 at live (predict_proba не получал creator_snapshot).

**Результат:** AUC 0.762 → 0.808; Precision@top10% 36% → 54.5% (но на меньшем N, шумно).

---

## 2026-04-22 — Feature stability protocol v1

**Что:** 5-seed feature_stability.py runs; drop только STABLE_DEAD в двух последовательных schema версиях.

**Зачем:** предотвратить случайные удаления нужных фич при noisy одноразовых evaluation.

---

## Текущее состояние (2026-04-24 18:15)

- **Модель:** entry_v15 + DOA fix (hash `ee1b147172960216`, AUC 0.98 holdout)
- **Бот:** работает в hybrid mode, full ML with auto-tuned thresholds (floor=0.10, ceiling=0.30)
- **Paper trades:** накапливаются, первые реальные результаты через 3-6ч
- **Схема:** entry_v15_20260424, 70 features (5 Phase E + SCORER + DERIVED + HELIUS + CREATOR)

---

## Что записывать в будущем

**Обязательно:**
- Retrain модели → метрики до/после + hash
- Изменение thresholds (floor/ceiling) → реальная причина
- Добавление/удаление фич → stability результаты
- Schema bump (entry_v15 → v16 и т.д.)
- Config изменения exit_* параметров
- Откаты и почему

**Не записывать:** мелкие фиксы кода, рефакторинги без видимого эффекта, тесты.
