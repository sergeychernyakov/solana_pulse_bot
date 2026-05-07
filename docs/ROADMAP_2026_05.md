# Roadmap 2026-05 — Phase 0-6 + Architecture Debt

**Дата составления:** 2026-04-24
**Последнее обновление:** 2026-04-28

---

## 🏗 Architecture Debt (новое 2026-04-28 после codex review)

Codex review вскрыл что pipeline.py / db.py переросли свою первую форму. Все 5 issues последнего ревью (entry metadata, event-time watermark, T+30 wallet wiring, ML override polluted rows, helius backfill completeness) — **в god-object'ах**. Список архитектурных задач по приоритетам:

### Phase A — Critical invariants & test contracts (1-2 дня)
- [ ] **Mutation tests на ключевые инварианты:**
  - `hard_exits_cannot_be_blocked_by_ml` — ML override не может блокировать creator_dump / hard_stop / max_hold
  - `replay_cannot_see_trade_outside_event_window` — checkpoint snapshots используют event-time, не arrival time
  - `incomplete_backfill_cannot_be_marked_complete` — Helius partial fetch не попадает в completed_mints
- [ ] **Contract tests по уровням:** source parity, feature parity, entry decision parity, exit parity, checkpoint parity
- [ ] **Restart/resume semantics test** — открытые позиции корректно восстанавливаются после рестарта

### Phase B — Pipeline.py decomposition (3-5 дней)
- [ ] Разбить на 3 модуля:
  - `ObservationSession` — intake + scheduling + holder capture + scoring
  - `DecisionService` — decide_entry + ML override + checkpoint hooks
  - `PaperTradeSupervisor` — open / monitor / close paper positions
- [ ] Извлечь `RuntimeContext` dataclass — единый контейнер (token, collected, holder_snapshot, creator_snapshot, wallet_prior_stats) вместо россыпи аргументов
- [ ] Перевести feature flags из `os.environ.get(...)` в `RuntimeFlags` typed config

### Phase C — db.py → bounded-context repos (2-3 дня)
- [ ] Разделить:
  - `TradesRepo` — ingestion + analytics
  - `ScoresRepo` — token_scores read/write
  - `PaperTradesRepo` — paper_trades CRUD
  - `WalletStatsRepo` — wallet_activity + wallet_classifications
  - `SnapshotsRepo` — holder + creator snapshots
- [ ] **Async-safe reads** в hot path: убрать `asyncio.to_thread(db._sync_query)` из pipeline.py:283/677/761
- [ ] Read-replicas: разделить read/write пулы

### Phase D — One canonical backtest runtime (1-2 дня)
- [ ] Выбрать `Pipeline + ReplayLaunchpad` как source of truth
- [ ] `pulse_bot/backtest.py` → удалить или превратить в тонкий test harness
- [ ] Убрать monkeypatch-style сборку backtest path в `main.py:205`

### Phase E — ModelRegistry + dataset lineage (1-2 дня)
- [ ] `ModelRegistry` класс с manifest на каждую модель: schema_version, config_hash, train window, activation mode, expected features, model_health
- [ ] Dataset lineage: какой backfill/enrichment state породил конкретную модель
- [ ] Заменить `Path("data/ml/...")` на `registry.get(name)`

### Phase F — Feature hydration service (2 дня)
- [ ] Вынести из Pipeline сборку creator + holder + wallet prior + SOL price + onchain state
- [ ] `FeatureHydrationService` с явными completeness flags
- [ ] Online vs Analytics schema разделение: `ScoringResult` сейчас служит и для online, и для warehouse

### Phase G — features.py / policy.py split (1-2 дня)
- [ ] `features/__init__.py`, `features/entry.py`, `features/entry_t30.py`, `features/timing.py`, `features/exit.py`
- [ ] `policy/__init__.py`, `policy/entry.py`, `policy/exit.py` etc.
- [ ] Один shared module устарел при 5+ моделях

### Phase H — Observability (1 день)
- [ ] System metrics через Prometheus / pushgateway:
  - queue depth, token processing latency
  - DB read latency, holder capture lag
  - percent incomplete snapshots
  - ML inference failure rate, parity mismatch rate
- [ ] Operational alerts vs research diagnostics — разделить

### Phase I — Backpressure + idempotency (можно отложить)
- [ ] Bounded queues в MultiplexerLaunchpad (сейчас unbounded)
- [ ] Explicit business keys для ingestion idempotency (не "best effort dedup")

### Phase J — Typed enums (нонблокер)
- [ ] `EntryAction.BUY / SKIP / RULES / DEFER` enum вместо строк
- [ ] `EntryType.FULL / FAST / ML_OVERRIDE / T30 / TIMING` enum

### Порядок исполнения (по приоритету)
1. **Phase A** — тесты сейчас, до любых рефакторов. Иначе риск регрессии слишком высок.
2. **Phase B** — Pipeline split. Самый дорогой по бенефитам.
3. **Phase C** — async reads (много раз нас уже укусит).
4. **Phase D** — backtest consolidation. Дешёвый, drift-killer.
5. **Phase F** — Feature hydration (помогает D и G).
6. **Phase E** — Registry.
7. **Phase G** — features/policy split.
8. **Phase H** — Observability.
9. Остальное по необходимости.

**Обоснование:** В исходный момент я считал что архитектурный долг "подождёт пока модель не докажет жизнеспособность". Codex прав: **каждое следующее изменение всё дороже** на god-object'ах. Делаем сейчас параллельно с моделями, не вместо.

### Snapshot 2026-04-28 (фактическое выполнение)

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


**Status v17→v18 (текущее):** AUC 0.96, Prec@top10% 7.97%, N=74,714 scored, base rate 0.81%.
6 ML голов готовы (entry main / entry @T+30 / entry-timing / survival / SL+TP quantile heads).
Phase 0 deployed — бот накапливает post-scoring трейды.

**Главный принцип (от кодекса):** **не добавлять ML-головы на слабую базовую
модель.** Сначала усилить foundation (больше данных + multi-snapshot
signal), потом advanced heads. ✅ Все код-фазы завершены 2026-04-25; ждём
Phase 0 data accumulation для validation.

**ML-стратегия — train-all + opportunistic gating** (2026-04-26):
- Тренируем **все** модели при каждом retrain cycle (entry / entry_t30 / entry_reg / entry_timing / exit / exit_quantile_sl,tp / survival).
- Каждая модель **возвращает confidence** (proba / coverage / hazard variance / spearman).
- **Активируем модель** только если её training quality прошла bar (AUC > 0.85 + P@top10% > 5×base, или quantile coverage в пределах ±0.03 от target, и т.д.).
- В runtime: **per-prediction confidence gate** — модель opt-in'ит на конкретное решение только при высокой уверенности; иначе deferral на RULES или другую модель.
- Никогда не override RULES если все ML на low-confidence.
- Ensemble pattern (не stacking) — каждая модель аудит-able отдельно. Pattern уже работает на entry_model (floor=0.1 / ceiling=0.6 / middle=RULES).
- Подробности: `~/.claude/projects/-Users-sergeychernyakov-www-gg/memory/project_ml_ensemble_opportunistic_gating.md`.

**Quick status:**
- ✅ Phase −1A (deploy on rich server) — DONE 2026-04-25
- ✅ Phase −1B (DB Rich↔Mac sync) — on-demand, документировано в README
- 🔨 Phase −1A2 (local Solana RPC node) — agave-validator билдится, snapshot sync next
- ⏳ Phase −1A2-extra (Geyser gRPC plugin) — после стабилизации local node, **gRPC primary + PumpPortal WS как fallback** (не замена, а резерв)
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

## Phase −1 — Production infrastructure (новое 2026-04-25 21:00)

**Статус:**
- ✅ Bot deployed на rich (192.168.3.118, Ubuntu, 125GB RAM, PG 16)
- ⏳ DB sync Rich ↔ Mac (сейчас одноразовый dump, нужна live replication)
- ⏳ Latency benchmark: AWS / Hetzner / разные зоны для best Solana latency
- ⏳ Real money trading — переход с paper mode на actual SOL transactions

### A. DB synchronization Rich ↔ Mac (on-demand)

**Задача:** держать обе БД в синке для:
- Rich = production (бот пишет туда)
- Mac = dev/analysis (нужен свежий snapshot для retrain, sweeps, исследований)

**Подход:** ручная синхронизация по запросу — когда на Mac нужны свежие данные для retrain/sweep, забираем dump одной командой. Никакой автоматики (cron не нужен — лишняя нагрузка, нерегулярная потребность).

```bash
# Мигрировать свежий snapshot Rich → Mac (одна команда):
ssh rich 'pg_dump -U sergeychernyakov -d pulse_bot -F c -Z 9' > /tmp/rich.dump \
  && pg_restore --clean --no-owner -d pulse_bot /tmp/rich.dump
# Время: ~5 мин для 3GB БД через LAN
```

**Альтернативы (если on-demand перестанет хватать):**
- PostgreSQL streaming replication: Rich primary → Mac standby (≤1s лаг)
- Logical replication: только нужные таблицы (trades, token_scores)

### A2. Geyser gRPC plugin (после local Solana node)

**Trigger:** local Solana RPC node работает стабильно (после Phase −1A2 setup).

**Идея:** перевести live source на самохост-стриминг через Geyser gRPC plugin (primary), PumpPortal WS — как fallback. Validator передаёт ВСЕ on-chain события (slot/account/transaction updates) клиенту через gRPC.

**Преимущества над PumpPortal WS:**
- **Latency**: ~1-5ms (local validator → process) vs 50-200ms (third-party WS)
- **Reliability**: нет зависимости от внешнего сервиса
- **Coverage**: ВСЕ события, не только pump.fun (можно фильтровать на стороне клиента)
- **No quotas**: unlimited throughput
- **Future-proof**: масштабируется на letsbonk и любые другие launchpads без отдельных WS

**Архитектура — gRPC primary + WS fallback (НЕ замена, а основной + резерв):**

Не выкидываем PumpPortal WS — оставляем как **fallback**. Это даёт зону устойчивости когда:
- наш validator отстаёт от tip (catching up после restart / network hiccup)
- gRPC plugin падает или streaming рвётся
- мы случайно ловим bug в кастомном decoder'е

**Логика:**
1. Live source: subscribe gRPC + WS параллельно, dedupe по `(mint, signature)`.
2. Health probe: каждые 5s проверяем `getSlot` локального ноды — если отстаёт >2 slots от mainnet (или последний gRPC event >5s назад) → автоматический fallback на WS-only.
3. Метрика `geyser_lag_slots` + `geyser_events_per_min` в logs/dashboard для мониторинга.
4. Env: `PULSE_LAUNCHPAD=geyser+pumpportal` (новый mode); fallback = существующий `PumpFunLaunchpad` (`wss://pumpportal.fun/api/data`).

**Что нужно:**
1. Установить Yellowstone Geyser plugin на наш local validator (`--geyser-plugin-config <yaml>`).
2. Написать Python gRPC subscriber `pulse_bot/launchpads/geyser.py`:
   - Subscribe filter на pump.fun program ID (`6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P`).
   - Decode trade events из `Transaction.meta.logMessages` + `tokenBalances`.
   - Реализовать тот же интерфейс `Launchpad.stream_trades` что PumpPortal WS.
3. Добавить `pulse_bot/launchpads/multiplexer.py`:
   - Объединяет события из gRPC (primary) и PumpPortal WS (fallback), dedupe-кэш на 60s window по `(mint, signature)`.
   - Health-check + автопереключение primary/fallback.
4. Env switch `PULSE_LAUNCHPAD=geyser+pumpportal` для миграции бота.

**Effort:** 2-3 дня (setup plugin + subscriber + multiplexer + dedup + tests + verification).

**Цена:** немного RAM/CPU на validator + multiplexer держит 2 коннекта в обычном режиме (минор).

**Применение:**
- Live bot: gRPC primary → WS fallback. Если local node умрёт — бот не потеряет события.
- Backfill historical: через local RPC (текущий путь).
- Геозер для new tokens, RPC для history — комбо.

### B. Latency benchmark для Solana

**Цель:** найти регион с min RTT до Solana mainnet RPC (для real-money trading надёжность критична).

**Кандидаты:**
- AWS: us-east-1, us-west-2, eu-west-1, ap-southeast-1
- Hetzner: nbg1 (Nuremberg), fsn1 (Falkenstein), hel1 (Helsinki), ash1 (Ashburn)
- Текущий: home network через WiFi → home ISP

**Метрики:**
- p50/p95/p99 RTT до mainnet.helius-rpc.com
- p50/p95/p99 для PumpPortal WS event delivery
- Cost per month

**Метод:**
```bash
# Spin up 1-hour test instance per zone:
# Run: 1000 × {ping helius-rpc, send dummy POST, measure latency}
# Compare distributions
```

### C. Real-money trading deployment

**Trigger:** ML модель показывает positive economic_backtest на накопленных Phase 0 данных (≥+0.3 SOL/week kill criterion из memory).

**Что нужно:**
1. **Wallet creation** — реальный keypair, начальный balance ($100-500)
2. **Replace `PaperTradeRunner` с `LiveExecution`**:
   - Real Solana TX submission (jupiter / pump.fun direct)
   - Slippage protection
   - MEV protection (Helius staked connection или Jito bundles)
3. **Risk limits**:
   - Max position size (e.g. 0.05 SOL/trade)
   - Daily drawdown stop ($-50 → halt bot)
   - Max consecutive losses → pause
4. **Monitoring**:
   - Alert (Telegram?) на every fill, every error, daily PnL
   - Wallet balance reconciliation
5. **Compliance / accounting**: TX logs для tax reporting

**Не запускать без:**
- Paper trading >7 days с positive realized PnL
- ML модель прошла honest economic_backtest > 0
- Latency benchmark показал стабильное ≤200ms RTT
- Real-money checklist signed off

---

## ML retrain + activation cadence (план 2026-04-26)

**Контекст:** 2026-04-26 retrained все 8 моделей на свежих данных. Train-all-train-fast paradigm работает (~30 мин последовательно). Активация по принципу memory `project_ml_ensemble_opportunistic_gating.md`.

**Этап 1 — Активация v0 (2026-04-26 19:35) ✅ DONE:**
- `pulse-bot.service` рестартован, подхватил свежие `entry_model.ubj` + `exit_model.ubj`.
- `entry_model` model_hash=3c9a34eb (новая Apr 26).
- `exit_model` ACTIVATED впервые — threshold=0.80 min_hold=15s.

**Этап 2 — Sanity + Shadow infrastructure (1-2 дня):**
- 5-seed `feature_stability.py` на entry_model — раннее предупреждение о regime drift / unstable features. Выполнить **сразу после Этапа 1** (запущено).
- Реализовать **shadow logging** в `pipeline.py`: для каждого scored token прогонять t30/timing/survival/quantile_sl/tp в режиме predict-only, писать `(model_name, mint, prediction, confidence, scored_at)` в новую таблицу `shadow_predictions`. **Не влияет на decision** — только сбор данных.
- Сравнивать через `daily_validation.py`: shadow predictions vs realized outcomes на той же ML scoring window.

**Этап 3 — Production validation (7-14 дней shadow):**
- Каждый день рассчитывать production metrics для каждой shadow модели:
  - t30: AUC + P@top10% на закрытых paper_trades
  - timing: confusion matrix (was BUY_NOW correct in retrospect?)
  - survival: predicted remaining-life vs actual exit time correlation
  - quantile: production coverage (% of trades within predicted q25..q75 PnL band)
- Critical bar: **production metrics ≥ training metrics × 0.7** + Spearman > 0.15.

**Этап 4 — Активация прошедших (по факту production validation):**
- Тех моделей которые прошли — выставить env флаг (PULSE_ENTRY_T30_ACTIVE / PULSE_TIMING_ACTIVE / PULSE_SURVIVAL_ACTIVE / PULSE_EXIT_REGRESSION_ACTIVE).
- Тех что не прошли — оставить shadow или off + investigate (regime drift / data leakage / overfitting к маленькому seed).
- Каждая активация = отдельный CHANGELOG entry с before/after metrics.

**Принцип не activate-on-train-metrics:** training metrics могут лгать (single-seed split, regime drift, overfitting). Production track record = реальная истина. См. `feedback_provisional_vs_closed`: при N<500 positives single-holdout ΔAUC <2×SE = шум.

### TODO: повторный feature_stability seed run + cleanup stable_dead

**Trigger:** через **5-7 дней** после 2026-04-26 retrain (т.е. ~2026-05-01..2026-05-03).

**Why:**
2026-04-26 сравнение `feature_stability_v18_seed5.json` (Apr 25) vs `..._apr26.json` (Apr 26) показало:
- AUC mean 0.9620 → 0.8995 (−6.2pp, 5× выше noise threshold) — **реальный regime drift**
- AUC stdev ×6 (0.001 → 0.006)
- 19 status flips
- 8 фичей флипнулись unstable → stable_dead: `buy_volume_sol_at_30`, `creator_median_peak_mc_sol`, `fast_buy_count`, `first_buy_sol`, `gap_create_to_first_trade`, `hc_30`, `top10_30`, `top5_30` (все T+30 family)

**Per memory `project_feature_stability_protocol`:** удалять `stable_dead` только если **в TWO sequential schema versions** одна и та же фича dead. Сейчас одна версия → ждём подтверждения второго run'а.

**Шаги:**
1. **2026-05-01..2026-05-03:** запустить `feature_stability.py --n-seeds 5 --data data/ml/entry.parquet --out data/ml/feature_stability_v18_seed5_round3.json` (после нового retrain'а на накопленных данных).
2. Сравнить `_apr26.json` vs `_round3.json` через `/tmp/compare_stability.py`.
3. Если те же 8 фичей в `stable_dead` повторно → **удалить из feature schema** (bump v18 → v19), retrain, validate prod metrics.
4. Если флипают обратно к unstable → шум, ничего не трогаем.
5. Параллельно: посмотреть на shadow_predictions данные (накопленные за неделю) — production-validation gate для t30/timing/survival/quantile активаций.

**Cost:** ~10 мин wall-time на 5-seed stability run + ~30 мин retrain если cleanup срабатывает.

---

## Phase 0 — Расширение горизонта данных (БЛОКЕР для всех ML-итераций)

**Trigger:** ASAP, до перехода к Phase 1+.

**Проблема (выявлено 2026-04-25):**
В БД post-scoring трейды есть только у ~440 токенов из 73,908 (0.6%). Бот вызывает `unsubscribe_trades(mint)` сразу после SKIP-решения (`pipeline.py:781`), и для 99% токенов трейды обрезаются на `observe_seconds=90s`. Это блокирует:
- Sweep по `max_hold > 90s` (даёт идентичные числа — данных нет)
- Тренировку моделей с длинным holding period (winners не успевают вырасти за 90с)
- Эксперименты с looser SL (если токен дипует и восстанавливается за 200с — мы этого не видим)

**Что сделано (код готов, ждёт деплой):**
1. `pulse_extended_observe_seconds: float = 0.0` в `PulseBotConfig`
2. Env var `PULSE_EXTENDED_OBSERVE_SECONDS`
3. `Pipeline._extended_observation()` background task
4. CHANGELOG обновлён, тесты зелёные

**Что нужно сделать (требует одобрения):**
1. Перезапустить live бот с `PULSE_EXTENDED_OBSERVE_SECONDS=600` (10 минут post-scoring)
2. Подождать 2-3 недели → накопится датасет с длинным хвостом (~5-15M новых трейдов)
3. Только после этого имеет смысл Phase 2 sweep по `max_hold` / wider SL

**Цена:**
- WS трафик: ×1.5-2 (продолжаем слушать тех 22% токенов где есть post-scoring активность)
- DB writes: тот же фактор
- Disk: 5.5M trades → 8-12M через 3 недели
- Все три приемлемы

**Откат:** убрать env var → старое поведение.

---

## Phase 1 — Sanity check

**Trigger:** N_closed ≥ 100 paper trades (ориентировочно 2026-04-28)

**Что делаем:** сравнить реальный WR закрытых paper_trades vs labeled Prec@top10%=40.7%.

| Real WR | Вердикт |
|---|---|
| 30-50% | Система честная, продолжаем |
| < 25% | Distribution shift — вернуть val-tuned thresholds, investigate |
| > 50% | Sampling bias в labels vs live — пересмотреть |

---

## Phase 2 — Foundation retrain + exit sweep

**Trigger:** N_closed ≥ 500 (ориентировочно 2026-05-05)

**Что делаем:**
1. Rebuild `entry.parquet` с новыми labels
2. Retrain classifier — ожидаем AUC 0.637 → 0.67-0.70
3. Feature stability 5 seeds — дропнуть STABLE_DEAD (кандидаты с v15:
   `top3_buyer_prior_avg_wr`, `top3_buyer_max_prior_pnl_sol`)
4. Platt recalibration
5. **Optimizer sweep** с новой осью:
   - `exit_inactivity_seconds`: [30, 45, 60, 90, 120] ← NEW
   - `exit_max_hold_seconds`: [60, 90, 120, 180]
   - `exit_hard_stop_loss_pct`: [8, 10, 12, 15]

**Апплаим winning config** если OOS PnL gain > +0.3 SOL/week.

---

## Phase 2.5 — Time-aware features in main model

**Trigger:** после Phase 2 retrain (~2026-05-06)

**Идея:** не плодить отдельные модели @T+30, @T+45, @T+60 — а расширить feature vector
**главной** модели снимками на нескольких таймштампах.

**Фичи:**
- `unique_buyers@30`, `unique_buyers@60`, `unique_buyers@90`
- `buy_rate@30`, `buy_rate@60`, `buy_rate@90`
- `top1_holder@30`, `top1_holder@60`, `top1_holder@90`
- **Delta-фичи:** `Δ_top1_30_to_60`, `Δ_buy_rate_60_to_90` (акселерация/декселерация)
- `hc_velocity_early` vs `hc_velocity_late` (два участка)

**Плюсы:**
- Одна модель, одна калибровка
- Модель **сама** учит "эволюцию" токена
- 70 фич → 200-250 фич, но те же 1292 labels. При N≥3000 спокойно тянет

**Минусы:**
- Не даёт раннего решения (бот всё равно ждёт T+90 чтобы получить @90 фичи)
- Phase 3 @T+30 model остаётся — для быстрого решения нужна отдельная модель

**Работа:** 2-3 дня. Добавить snapshot collection @T+60 (сейчас только @T+30 и @T+90 via Helius; buy_rate/buy_count — пересчитать из trades). Extend `extract_entry_features` + `build_dataset`.

---

## Phase 3 — @T+30 dual-snapshot model

**Trigger:** N_closed ≥ 1000 + Helius T+30 snapshot infra ready
(ориентировочно 2026-05-15)

**Codex priority #1 после foundation.** Удваивает label volume/день + второй
независимый сигнал для combine с T+90.

**Prerequisites (можно начинать параллельно сейчас):**
- Clean Helius snapshot @T+30s (сейчас с лагом)
- `build_dataset_t30.py` — фичи видимые к T+30

**Pipeline после deployment:**
```
T+30s: Model_30 оценивает token
  ├─ proba > 0.75 → BUY сразу (не ждём 90с)
  ├─ proba < 0.15 → SKIP сразу (освобождаем слот)
  └─ middle → ждём T+90s, Model_90 как сейчас
```

**Ожидаемый gain:**
- Раньше заходим (5-10-й покупатель вместо 20-25-го)
- Быстрее бракуем явные скамы → больше слотов
- 2× labels в день

---

## Phase 4 — Timer tick + survival max_hold

**Trigger:** Phase 3 stable (ориентировочно 2026-05-25)

### Часть A — Infrastructure

Refactor `Pipeline._paper_trade`:
- Background timer tick каждые **5-10 сек** для активных paper trades
- `PulseMonitor.update_empty_tick(now)` — пересчёт rates при отсутствии новых трейдов
- `ExitManager` вызывается на timer tick'ах (не только на trade events)
- Unit tests: timer-tick invariants, no regressions in existing tests

### Часть B — Survival model

- **Cox proportional hazards** или **discrete-time hazard** (НЕ regression)
- Duration — censored data (right-truncated нашими же exits)
- Target: время от scored_at до pulse_dead / no_new_blood signal
- Replace `exit_max_hold_seconds=90` с `min(predicted_life, 180s)`

**Prerequisite (Часть A blocker для Части B):** модель нуждается в per-second
state для labelling.

---

## Phase 5 — Entry-timing classifier

**Trigger:** Phase 4 stable (ориентировочно 2026-06-05)

Каждые 15 сек начиная с T+15s → classifier: `WAIT_MORE / BUY_NOW / SKIP`.

Supervised (не RL) — per-snapshot labels generated via simulate_exit future.
Решает "21-й покупатель" problem когда signal созрел раньше T+90s.

---

## Phase 6 DEFERRED — TP quantile head

**Trigger:** N_closed ≥ 3000 AND tail ≥ 100 tokens at +100% PnL
(ориентировочно 2026-06-20+)

**Почему отложено (codex veto):** на текущих данных только ~10-15 токенов в
moonshot tail; q=0.75 quantile regression **систематически занизит** пики.
Бот будет продавать 2× runners на 0.8×. Re-visit когда tail coverage adequate.

---

## Phase 7 — Tighten ML gates после data collection (новое 2026-04-29)

**Trigger:** все из условий выполнены:
- `EXTENDED_OBSERVE` накопил ≥ 7 дней данных с full-window trade streams
- Helius backfill historical mints закончен (≥ 5000 graduated mints в БД)
- Validator догнал mainnet — backfill используем через local RPC
- Retrain моделей завершён на новых лейблах
- Health checks: ρ ≥ 0.20 на live данных (validation + first 200 live trades)
- economic_backtest: EV per trade ≥ +0.5% на новой модели

**Контекст (фиксируется тут чтобы не забыть, дискуссия 2026-04-29):**

Текущий `ml_override` setup `PULSE_ENTRY_PROBA_CEILING=0.15` агрессивный:
- 100% paper-trades идут через ML_OVERRIDE (rules никогда не triggers BUY)
- WR=5%, PnL=-4 SOL/48h на бумаге (paper, не реальные деньги)
- Live ρ=±0.01 — модель в production ranking'ует случайно
- Win'ы (+107%, +379%) приходят **вопреки** прогнозу reg-модели (она прогнозировала -3.5%, -2.4%)

**Это плохо для зарабатывания НО хорошо для сбора данных:**
- 1055 разнообразных закрытий → exit-модели имеют тренировочный материал
- Random sample tokens → bootstrap data, представительный датасет
- 51 winner среди loser'ов → exit-модели учатся на победителях

Поэтому **сейчас не зажимаем**, накапливаем. После Phase 7 trigger — зажимаем.

### Что зажать (после trigger):

1. **Поднять ml_override ceiling** `PULSE_ENTRY_PROBA_CEILING=0.15 → 0.50`
   - Эффект: override работает только при высокой уверенности модели
   - Прогноз: сделок 5-10/день вместо 100+, WR ↑

2. **Reg-floor positive** `PULSE_ENTRY_REG_FLOOR_PCT=-10.0 → 0.0`
   - Только trades с положительным прогнозом PnL
   - Прогноз: отсекает ~30% худших ml_override candidates

3. **Double-SKIP guard** (требует код в `decision_service.py`):
   ```
   if rules.fast == "WAIT/SKIP" AND rules.full == "SKIP":
       require ml_proba ≥ 0.70 AND reg_pnl ≥ +5%
   ```
   - Эффект: когда **обе** rules-системы согласны "не бери", переопределение требует **двойной** уверенности
   - Прогноз: оставит только tokens где модель **явно** видит то, что rules не видят

4. **Sizing ladder by reg-prediction:**
   - `pred_pnl ≥ +20%`: 1.5× standard size
   - `+5% ≤ pred_pnl < +20%`: 1.0× (стандарт)
   - `0 ≤ pred_pnl < +5%`: 0.5× (защита)
   - Делать ТОЛЬКО когда live ρ установится ≥ 0.20 (иначе sizing усиливает шум)

### Откат:
Revert env vars в .env, restart bot, вернёмся к Phase 6 baseline.

### Estimate:
- Code changes (double-skip guard): 1-2 часа
- Tests: 1 час
- .env update + restart: 5 мин
- Live verification: 24 часа

---

## KILLED items (по критике кодекса)

- ❌ **TP quantile head первым** — данных мало, систематический bias (см. Phase 6)
- ❌ **Smooth position sizing** (линейная от proba) — cosmetic, на AUC 0.637 не даст measurable lift
- ❌ **Dynamic observe_seconds через RL** — Phase 3 решает ту же задачу supervised
- ❌ **ML-control execution constraints** (max_entry_buyer_number, min_sol_volume_hard,
  creator_blacklist) — физика/compliance, не predictions

---

## Parallel infrastructure (можно начинать сейчас)

1. **Clean Helius T+30 snapshot flow** — Phase 3 prerequisite
2. **`config_hash` в entry_model.meta.json** — WARN at startup if runtime config != training
   config (protect from silent Option B-style regression)
3. **Dashboard widget:** WR last 100 paper_trades + ML override BUY:SKIP ratio —
   daily monitoring kill criteria
4. **Simulate_exit vectorization** — 15min for 60k rows too slow, vectorize

---

## Kill criteria (любая phase)

Actions if triggered:
- **Realized PnL < +0.3 SOL/week** (7-day rolling) → pause + investigate
- **WR < 20%** над 50+ трейдами → regression, revert last change
- **ML override BUY:SKIP ratio drift >2×** → data drift, retrain
- **ML vs rules underperform >5% for 3 consecutive days** → flip `PULSE_POLICY=rules`
  (см. `memory/project_ml_rollback_trigger.md`)

---

## Timeline visual

> **Внимание про N≥X (graduated):** "graduated mint" (mc≥85 SOL) случается ~1-3/день, ~0.5% rate. Использовать число graduated как trigger для retrain — **плохая идея** (label imbalance). Реальный label source = `paper_trades` (PnL outcomes). Числа `N≥500/1000/3000` ниже — informational rough timing для масштабов, **не data-gates**. Retrain можно запускать когда есть смысл по доступным `paper_trades` + features.

```
2026-04-24 ═══════ СЕЙЧАС ═══════
            │  v15 full-ML работает (paper mode)
            │  Infra для T+30 готовим параллельно
            │
2026-04-28 ─── N_paper≥100 ─── Phase 1 (sanity check)
            │
2026-05-05 ─── N_paper≥500 ─── Phase 2 (retrain + sweep)
            │                     ↑ ожидаем AUC 0.67+
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
2026-06-20+ ── N_paper≥3000 + tail ─── Phase 6 (TP quantile, ранее Priority A)
```

(`N_paper` = closed paper_trades с realized PnL; не graduated mints.)

---

## Tracking

- Tasks #137 (Phase 1), #138 (Phase 2), #139 (Phase 3), #140 (Phase 4),
  #141 (Phase 5), #142 (Phase 6 deferred), #143 (parallel infra)
- Memory: `project_roadmap_2026_05.md` (этот файл в короткой форме)
- Kill watchdog: Task #136 — watch full ML-only trial до 2026-05-08
