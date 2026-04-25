# Roadmap 2026-05 — Phase 0-6

**Дата составления:** 2026-04-24
**Последнее обновление:** 2026-04-25 13:30
**Status v17→v18 (текущее):** AUC 0.96, Prec@top10% 7.97%, N=74,714 scored, base rate 0.81%.
6 ML голов готовы (entry main / entry @T+30 / entry-timing / survival / SL+TP quantile heads).
Phase 0 deployed — бот накапливает post-scoring трейды.

**Главный принцип (от кодекса):** **не добавлять ML-головы на слабую базовую
модель.** Сначала усилить foundation (больше данных + multi-snapshot
signal), потом advanced heads. ✅ Все код-фазы завершены 2026-04-25; ждём
Phase 0 data accumulation для validation.

**Quick status:**
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

```
2026-04-24 ═══════ СЕЙЧАС ═══════
            │  v15 full-ML работает (paper mode)
            │  Infra для T+30 готовим параллельно
            │
2026-04-28 ─── N≥100 closed ─── Phase 1 (sanity check)
            │
2026-05-05 ─── N≥500 closed ─── Phase 2 (retrain + sweep)
            │                     ↑ ожидаем AUC 0.67+
            │
2026-05-15 ─── N≥1000 ─────── Phase 3 (@T+30 model)
            │                     ↑ foundation strengthened
            │
2026-05-20 ─── Phase 3 stable ─── Phase 4 prep (timer tick)
            │
2026-05-25 ─── Timer tick done ─── Phase 4 (survival max_hold)
            │
2026-06-05 ─── Phase 4 stable ─── Phase 5 (entry-timing classifier)
            │
2026-06-20+ ── N≥3000 + tail ─── Phase 6 (TP quantile, ранее Priority A)
```

---

## Tracking

- Tasks #137 (Phase 1), #138 (Phase 2), #139 (Phase 3), #140 (Phase 4),
  #141 (Phase 5), #142 (Phase 6 deferred), #143 (parallel infra)
- Memory: `project_roadmap_2026_05.md` (этот файл в короткой форме)
- Kill watchdog: Task #136 — watch full ML-only trial до 2026-05-08
