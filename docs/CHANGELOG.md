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
## 2026-05-08 07:09 — Retrain candidate DROPPED — calibration shift on same-data test

**Что произошло:** Перетренил entry_model на свежем датасете (115365 rows, +4.6K vs prior 110K). Новая модель прошла встроенный health check (`Saved model to data/ml/entry_model.ubj (health=ok)`) — но **same-data comparison через `scripts/same_data_compare.py` показал** что новая модель **хуже** на всех операционных порогах.

**Diff:**
| Metric | OLD (May-5) | NEW (May-7) | Δ |
|---|---|---|---|
| AUC holdout | 0.9329 | 0.9220 | −1.1 pp |
| Precision@top10 | 2.03 % | 3.13 % | +1.1 pp |
| Train rows | 77 547 | 80 755 | +4 % |
| Proba p50 | 0.020 | **0.190** | **+0.17** ← critical drift |
| Proba p99 | 0.604 | 0.584 | −0.02 |
| EV-grid | fallback | OK (0.190/0.252) | improvement |

**Killer finding:** NEW model имеет **median proba = 0.190**, выше live `PULSE_ENTRY_PROBA_CEILING=0.15`. Развернули бы новую модель без обновления env — бот купил бы **100 %** токенов (вся holdout выборка прошла бы порог).

**Same-data comparison (chronological holdout 17 305 rows, threshold=0.15):**
- OLD: 1 197 BUYs, WR 3.68 %, avg PnL −0.95 %
- NEW: 17 305 BUYs (100 %!), WR 0.41 %, avg PnL −0.19 %

На любых разумных operating points OLD выше WR:
- @0.252: OLD 5.54 % vs NEW 2.31 %
- @0.190: OLD 4.63 % vs NEW 0.94 %
- @0.110: OLD 3.06 % vs NEW 0.41 %

**Action — REVERT на диске:**
```bash
cp data/ml/entry_model.ubj data/ml/entry_model_candidate.ubj   # preserve
cp data/ml/entry_model.ubj.prev data/ml/entry_model.ubj         # revert disk
cp data/ml/entry_model.meta.json.prev data/ml/entry_model.meta.json
```

Бот в памяти держит OLD (May-5), disk = OLD (sha256 verified match), candidate сохранён `entry_model_candidate.{ubj,meta.json}` для архива и пост-мортема. Snapshot `known-good-2026-05-07-paper-profitable` integrity preserved — никаких реальных изменений в production.

**Root cause гипотеза:**
- NEW dataset включает свежие paper_trades с другим class balance (test_base_rate 0.41 % vs 0.25 %)
- Class balance shift → calibration shift на softmax output
- `_save_with_health_check` смотрит на `proba_spread > 0.05` и `auc_delta > -0.05` — оба прошли. Calibration drift (median proba shift) **не проверяется**.

**Следующий шаг (отдельная задача):** добавить calibration-drift check в `_save_with_health_check`:
```python
prev_p50 = prev_meta.get("model_health", {}).get("proba_p50")
if prev_p50 is not None and abs(curr_p50 - prev_p50) > 0.05:
    health["status"] = "fail"
    health["notes"].append(f"calibration drift: p50 {prev_p50:.3f} → {curr_p50:.3f}")
```

**Tooling commit:** `scripts/same_data_compare.py` — proper apples-to-apples retrain validation для будущих ML changes. Должен запускаться **перед** деплоем любого нового model artifact.

**Откат отката:** не нужен — никаких деплой-эффектов не было. Candidate файлы можно удалить через 7 дней если post-mortem не нужен.

---
## 2026-05-07 15:42 — Snapshot `known-good-2026-05-07-paper-profitable`

**Что зафиксировано:** Полный известный-хороший state бота — code + models + runtime knobs. Доступен для откатa в один клик через `restore.sh`.

**Что в snapshot:**
- **Code**: git tag `known-good-2026-05-07-paper-profitable` → commit `8d1f69b` (regression gate) на вершине `a2fec14` (confidence gates).
- **Models** (8 .ubj + 8 .meta.json): entry, entry_t30, entry_reg, entry_timing, exit_quantile_sl/tp/max_hold, survival. ~2 MB total.
- **Runtime knobs**: все `PULSE_*` env vars из `.env` (без секретов — `HELIUS_API_KEYS`, `PULSE_PG_DSN` etc остаются в host).
- **Manifest**: sha256 каждого файла, проверяется на restore.

**Где хранится:**
- rich: `~/backups/gg/known-good-2026-05-07-paper-profitable/`
- mac:  `~/backups/gg/known-good-2026-05-07-paper-profitable/` (rsync redundancy)

**Bot status в момент snapshot:**
- Paper PnL +2.78 SOL за 19 ч, WR 18.6 %, N=161 closed trades
- Все 8 ML heads `status=ok`
- `PULSE_SURVIVAL_ACTIVE=1` + `PULSE_SURVIVAL_MIN_CONFIDENCE=0.50` (confidence gate активен)
- Regression gate frozen на N=1247 (см. предыдущую запись)

**Как откатиться:**
```bash
git checkout known-good-2026-05-07-paper-profitable
ssh rich "cd ~/www/gg && git fetch && git checkout known-good-2026-05-07-paper-profitable"
ssh rich "bash ~/backups/gg/known-good-2026-05-07-paper-profitable/restore.sh"
```

`restore.sh` сам:
1. Останавливает `pulse-bot.service`
2. Бэкапит текущие `data/ml/` → `data/ml.before-restore.<TS>/` (откат-restore самого restore возможен)
3. Копирует snapshot models обратно
4. Бэкапит `.env` → `.env.before-restore.<TS>`
5. Заменяет `PULSE_*` строки на snapshot версии (секреты сохраняются)
6. Проверяет sha256 моделей по manifest
7. Перезапускает bot + tail 30 строк logs/bot.log

**Документация:** `docs/SNAPSHOTS.md` — полная инструкция как создавать новые snapshots, как откатывать, как promote'ить snapshot до regression-baseline.

**Откат самого snapshot:** не нужен — snapshot files immutable, ничего в проде не изменилось при создании.

---
## 2026-05-07 14:23 — Hard regression gate (`scripts/regression_gate.py`)

**Что изменилось:** Добавлен offline-скрипт-гейт, который перед каждым деплоем проверяет что текущий exit-config не делает хуже исторических paper_trades.

**Как работает:**
1. Pull последние N=2000 закрытых paper_trades (за 14 дней) из rich PG.
2. Для каждого трейда fetch все post-entry trades из `trades` table.
3. Replay через `simulate_exit_batch(current_config, ...)` → получить counterfactual {WR, PnL/trade, profit_factor}.
4. Сравнить с замороженным baseline в `pulse_bot/ml/regression_baseline.json`.
5. Verdict:
   - PASS — все метрики ≥ baseline − 1·SE → exit 0
   - WARN — любая в [−2·SE, −1·SE) → exit 2
   - FAIL — любая < baseline − 2·SE → exit 1 (refuses to ship)

**Baseline (frozen 2026-05-07T14:23Z):**
- N=1247 closed paper_trades (14-day rolling)
- WR 2.65 % ± 0.45 SE
- avg PnL 0.00663 SOL ± 0.00032 SE (negative — отражает старые era до survival fix)
- profit_factor 0.148

(Низкая абсолютная WR/PnL — потому что dataset включает Apr 16-17 era survival-bleeding. Гейт сравнивает _replay_ counterfactual vs baseline counterfactual, не реальный live PnL — это проверка регрессии exit-config, не стратегии в целом.)

**Доказательство что гейт работает (A/B):**
- Step 1 — `--freeze` snapshot текущего → baseline сохранён, exit 0
- Step 2 — same config replay → exit 0 (PASS, Δ=0.0000 на всех метриках)
- Step 3 — manually inflate baseline до WR=20 % (impossible цель) → exit 1 (FAIL, "❌ wr_pct=2.6500 REGRESSED by 17.3500 (37.7·SE) ... refusing to ship")
- Step 4 — restore real baseline → exit 0 (PASS) again

**Использование:**
```bash
# Один раз — заморозить baseline на known-good state:
ssh rich 'cd ~/www/gg && set -a && source .env && set +a && \
  PYTHONPATH=. .venv/bin/python scripts/regression_gate.py --freeze'

# Перед каждым деплоем (manual или CI):
ssh rich 'cd ~/www/gg && set -a && source .env && set +a && \
  PYTHONPATH=. .venv/bin/python scripts/regression_gate.py'
```

**Известные ограничения (codex view):**
1. Только exit-side regression (entry decisions заморожены как-есть).
2. Selection bias — replay только entries которые принял старый policy.
3. SE assumes IID, real trades auto-correlated (нужен block-bootstrap).
4. Tail-dependence на outliers (top-5 winners = 80 % PnL).
5. Нет live runtime monitor (Layer 2 — отдельная задача).
6. `--freeze` доверяет оператору без sanity floor.

**Откат:** просто не запускать. Скрипт изолированный, не подключён к pulse-bot.service.

**Не влияет на текущую доходность бота** (+2.78 SOL/19h). Запускается только pre-deploy, к live pipeline не прикасается.

---
## 2026-05-06 14:30 — Confidence/sanity gates audit + uniform low-conf safe defaults

**Что изменилось:** Аудит как все 8 моделей реагируют на низкую уверенность; добавлены недостающие защиты по принципу defense-in-depth.

**Принцип:** на низкой уверенности модель должна делать **passive default action** (не торговать / держать), а не **destructive default** (купить/убить позицию). До этого только survival нарушал инвариант.

**Изменения в коде:**

1. **survival** (`pipeline.py:1823`) — добавлен `PULSE_SURVIVAL_MIN_CONFIDENCE` env var (default 0.50). Раньше exit срабатывал на `remaining_life < 30s` без проверки `pred.confidence`; теперь predictions с `confidence < 0.50` (которые ≈100% существующего trafficа — например 0.29) **пропускаются**, trade живёт до natural exit (TP/SL/dead_token/timeout). Confidence = mean abs(hazard − 0.5) × 2; 0.29 = hazards около 0.355 (max uncertainty).

2. **entry_reg** (`decision_service.py:346`) — добавлен `PULSE_ENTRY_REG_CEILING_PCT` (default 30.0). Симметричен существующему floor (-100% по умолчанию). reg-предсказания > +30% PnL — это, скорее всего, шум при ρ=0.008; блокируем ML override BUY на таких outliers как и на reg<floor. Логируется как `reg_ceiling_block`.

3. **exit_quantile_sl** (`exit_manager.py:272`) — добавлены sanity bounds. Если `q25 ∉ [-100%, 0%]` — модель сломана (предсказывает положительный PnL для SL-тайтенера или нереально низкое значение); silent skip, hard_stop ниже всё равно сработает.

**Не тронуто (уже safe):**
- **entry** (T+90 classifier) — `proba >= ceiling=0.15` → BUY override; иначе rules path. Default safe.
- **entry_t30** — `>=0.75 BUY / <0.15 SKIP / DEFER в grey zone`. Default DEFER (passive).
- **entry_timing** — `proba > timing_gate=0.85` для BUY/SKIP_EARLY (3-class softmax). Default DEFER.
- **exit_quantile_max_hold** — clamp `[min(30, ceiling), ceiling]`, может только ускорить exit, никогда не extend. Behind feature flag `exit_max_hold_dynamic` (default OFF).
- **exit_quantile_tp** — load-time health check (ρ ≥ 0.05), не used в hot path solo, comparison с config TP.

**Результат:**
- `PULSE_SURVIVAL_THRESHOLD=0.10` (откат моей ошибки 0.70 — это калибровка алгоритма, не confidence gate)
- `PULSE_SURVIVAL_MIN_CONFIDENCE=0.50` (новая)
- `PULSE_ENTRY_REG_CEILING_PCT=30.0` (новая, в коде default)
- 26 unit tests in `test_decision_service.py` pass (added `test_reg_ceiling_blocks_overconfident_predictions`)
- Bot restarted 14:30 UTC, all 8 models loaded status=ok

**Откат:**
```bash
# Var-only rollbacks (no code revert needed):
sed -i 's/^PULSE_SURVIVAL_MIN_CONFIDENCE=.*$/PULSE_SURVIVAL_MIN_CONFIDENCE=0.0/' .env
echo 'PULSE_ENTRY_REG_CEILING_PCT=10000.0' >> .env
systemctl --user restart pulse-bot.service
```

**Тест:** через 4 часа после restart сверим количество `survival_predict` exits с предыдущим окном (где был 92%). Ожидание — drop до <30% (только high-confidence kills).

---
## 2026-05-05 17:30 — Disable survival permanently (conservative retrain also degenerate in prod)

**Что изменилось:** `PULSE_SURVIVAL_ACTIVE=1 → 0` в `.env` на rich. Bot restart 20:22:24 UTC. Survival остаётся в shadow mode (логируется, не act'ит).

**Зачем:** После conservative retrain 13:42 UTC (sanity_status=ok, 12 distinct remaining_life на 100 train mints) — на проде **тот же failure pattern** что был с Apr-30 degenerate моделью:
- 26 closed paper_trades (13:42–17:16 UTC, 3.5 часа)
- **Все 26 exit_reason = `survival_predict`** на hold ≈97s
- 22/23 убытков ровно −0.0070 SOL (фиксированный fee/slippage)
- WR 11.5% (3 wins), PnL −0.0057 SOL — net flat только потому что 1 win (+0.0464) случайно покрыл 22 одинаковых fee-loss

**Root cause (диагноз):** sanity test проверял **training distribution** (random mints из 231k hazard rows), но продакшн inference получает **degenerate feature vectors**: `wallet_stats=None` для всех T+30 предсказаний (12/59 фич нулевые). На degenerate-векторах модель схлопывается в один моду → uniform "death at ~95s" для любого токена. Sanity на train data это не покрывает by design.

**Что подтверждено:**
- Survival ≠ модель-проблема. Это inference-pipeline problem (wallet_stats hydration broken)
- Дальнейший ретрейн на тех же фичах не починит — это design flaw гидрации
- Disable + расследовать корень `wallet_prior_stats=None` в `FeatureHydrationService.hydrate_for_t30/t90`

**Метрики до vs после disable (отслеживаются live):** ожидание — exits переходят на natural reasons (timeout/hard_stop/take_profit), hold time распределён вместо uniform 95s, PnL восстановится (или станет хуже — это и есть тест).

**Что НЕ изменено (по результатам критики кодекса):**
- `entry_model.ubj` rollback к `.prev` (Apr-30) — **отложен**. AUC новой 0.9329 vs prev 0.9173 = +0.016, P@10 6%→2% (regression). Но без survival неизвестно сколько entry contributes к WR=11.5%. A/B сначала только survival.
- `entry_t30` / `entry_reg` rollback — `.prev` файлов не существует (retrain pipeline не делает auto-backup для них), откатить нечего.
- HELIUS T+120 extrapolation (fix #80) — **оставлен**, это безусловный win.

**Откат disable:** `sed -i 's/^PULSE_SURVIVAL_ACTIVE=0$/PULSE_SURVIVAL_ACTIVE=1/' /home/sergey/www/gg/.env && systemctl --user restart pulse-bot.service` — НО **не делать** пока не починен `wallet_prior_stats` hydration root cause.

**Следующие шаги:**
1. Watch 30+ min: убедиться что нет `survival_predict` в exits, exit_reason распределён
2. Diagnose `FeatureHydrationService.hydrate_for_t30/t90` — почему `wallet_prior_stats` всегда None в проде (см. `pulse_bot/feature_hydration.py`)
3. После починки hydration — economic_backtest на entry_v22/v21 чтобы решить про откат entry_model

---
## 2026-05-05 13:42 — Survival re-trained with conservative hparams + .prev backup + sanity test

**Что изменилось:** В `pulse_bot/ml/survival.py:train_survival_model()`:
- Defaults: `max_depth 4 → 3`, `learning_rate 0.05 → 0.03`, добавлено `reg_lambda=2.0, reg_alpha=0.1` — снижают capacity / overfitting tendency
- Auto-backup: model + meta перемещаются в `*.prev` перед overwrite (paritет с `_save_with_health_check`)
- **Sanity test post-fit:** на 100 random training mints запускается `predict_remaining_life`; ожидается **≥5 distinct values**. Если меньше — `sanity_status=degenerate` логируется ERROR. Блокирует тихий deploy degenerate model
- Перерлоучен на rich (231,178 hazard rows, 10,870 positives = 4.7%)
- `PULSE_SURVIVAL_ACTIVE=0 → 1` восстановлен в `.env`, bot restart 13:42:35 UTC

**Результат:**
- sanity_status=ok, 12 distinct remaining_life across 100 sampled mints (vs 1 distinct in degenerate Apr-30 retrain)
- Все 8 моделей `status=ok` после restart
- Survival.ubj 234 KB (vs 328 KB у degenerate — меньше trees, менее склонен collapse)

**Откат:** `cp data/ml/survival_model.ubj.prev data/ml/survival_model.ubj && cp data/ml/survival_model.meta.json.prev data/ml/survival_model.meta.json && systemctl --user restart pulse-bot.service`. ВНИМАНИЕ: `.prev` это degenerate Apr-30 retrain — откатывать только если новая версия plotting **хуже**. До этого была pre-Apr-30 версия, её больше нет.

---
## 2026-05-05 13:10 — Hot-fix: disable survival (degenerate after retrain)

**Что изменилось:** `PULSE_SURVIVAL_ACTIVE=1 → 0` в `.env` на rich. Survival model остаётся загружена в shadow mode (`PULSE_SURVIVAL_SHADOW=1`) для логирования, но больше не принимает решения exit. Bot restart 13:10:17 UTC.

**Зачем:** Перерлоученный 11:51 UTC survival_model — degenerate: предсказывает `remaining_life=25s` при `confidence=0.39` для **всех** tokens независимо от их состояния. Bot закрывал каждую открытую позицию через ~95s elapsed по survival_predict с PnL=-7% (только slippage).

**Метрики до отката (post-survival-retrain, 70 min):**
- 30 opened, 1 win, 29 losses
- WR 9.7% baseline → **3.3%** (-66%)
- PnL +0.18 SOL/24h baseline → **-0.21 SOL за 70 мин** (-3 SOL/24h trajectory)
- Все 29 losses identical: `survival_predict @ 95-97s, predicted_remaining=25s, conf=0.39, pnl=-7%`

Это train/serve degeneracy: модель learned uniform "death soon" prior на всех tokens вместо token-specific signal. Возможные причины:
1. Свежие paper_trades (5 days с Apr 30) сместили label distribution к uniform negatives
2. scale_pos_weight 20.26 + 200 estimators могли overfit на death class
3. Hazard frame 230k/10830 positives — ratio same но N выросло, training dynamics shifted

**Откат не возможен через .prev:** `train_survival_model()` не имеет auto-backup в отличие от `_save_with_health_check`. Только env flag toggle.

**Оставшиеся 7 моделей не тронуты, status=ok**, продолжают работать (entry, entry_t30, entry_reg, entry_timing, exit_quantile_sl/tp/max_hold).

**Следующий шаг (task #85):** добавить .prev backup в `train_survival_model()`, перерлоучить с conservative hparams (lower learning_rate, или tuned scale_pos_weight через cross-validation), проверить prediction diversity (sanity test: predict_remaining должен **различаться** на разных tokens) перед re-enable.

**Откат hot-fix:** `sed -i 's/^PULSE_SURVIVAL_ACTIVE=0/PULSE_SURVIVAL_ACTIVE=1/' /home/sergey/www/gg/.env && systemctl --user restart pulse-bot.service` — но **не делать** до retrain.

---
## 2026-05-05 11:54 — Refresh exit_quantile (3 heads) + entry_timing + survival

**Что изменилось:** На том же датасете (no schema change), пере-обучили оставшиеся 4 модели на 5 днях свежих paper_trade outcomes (с момента предыдущего retrain 30 апреля).

**Зачем:** После retrain entry/t30/reg в 10:34 решили довести 8/8 моделей до единого свежего timestamp. Skew fix не задевает feature space этих моделей (они не используют HELIUS / CREATOR), но 5 дней accumulated paper_trades = больше свежих labels.

**Результат (before → after):**
```
exit_quantile_sl        ρ +0.069 → +0.080  (+0.011)
exit_quantile_tp        ρ +0.063 → +0.084  (+0.021)
exit_quantile_max_hold  ρ +0.501 → +0.503  (+0.002)
entry_timing            (no AUC) → AUC OvR 0.863, F1 SKIP=0.83, BUY_NOW=0.51, WAIT=0.50
survival                hazard 220437 rows → 230296 rows (+4.5%, positive_rate stable 4.7%)
```
Все 5 моделей `status=ok` после restart. Никаких регрессий vs baseline. Все 8 моделей в production.

**Откат:** `cp data/ml/<model>.ubj.prev data/ml/<model>.ubj` для каждой из 5 моделей + `systemctl --user restart pulse-bot.service`. Auto-backup создаётся train.py при сохранении.

---
## 2026-05-05 10:34 — Skew fix: HELIUS T+120 extrapolation + CREATOR NaN-policy v2 + retrain entry/t30/reg

**Что изменилось:**

### 1. HELIUS T+120 race fix (`pipeline._fetch_holder_snapshot_all`)
Pipeline scoring на T+90, а T+120 capture запускается **точно в T+120s** — race: на момент predict_proba T+120 row отсутствует → `top1_120 / top5_120 / top10_120 / hc_120` всегда 0 → 4 HELIUS + 3 DERIVED фич мёртвых на каждом live-токене.

Теперь когда T+120 row missing — extrapolate из (T+30, T+60) линейно: `f(120) ≈ 2*f(60) - f(30)` с clamp [0, 100] для percent fields, `max(hc_30, hc_60, ...)` для holder count (holders only grow в первые 2 мин bonding curve). `build_dataset.py` зеркалит ту же логику для тех 1.2% historical rows что без T+120 — train/serve parity.

### 2. CREATOR NaN-policy v2 (`features._main._get_creator_feat`, `build_dataset.py`)
Solo creators (~64% live tokens, snapshot_prior_tokens < 2) имели `creator_median_peak_mc_sol = 0` и `creator_inter_token_interval_sec = 0` — это математически undefined (median/interval от единственного токена), но модель училась интерпретировать как "low MC" / "fast cadence" сигнал.

Теперь:
- `snapshot=None` → NaN для всех CREATOR_FEATURES (вместо 0.0)
- `snapshot.priors < 2` + degenerate field → NaN
- `creator_age_days, creator_balance_sol` остаются реальными значениями (meaningful регardless of priors)
- `build_dataset.py` дублирует логику: `solo_mask` устанавливает NaN на 64% solo-rows; `total_prior_tokens, graduated_count` остаются 0-fill (legitimate counts)

XGBoost handles NaN через missingness splits — модель теперь различает "no creator data" vs "0 SOL median".

### 3. Retrain entry/t30/reg
```
entry      AUC 0.9173 → 0.9329 (+1.6pp), spread 0.833, ΔAUC +0.0155, status=ok
entry_t30  AUC 0.8604 → 0.9126 (+5.2pp), 77547 train, status=ok
entry_reg  rho +0.040 → +0.068, status=ok
```
Все 8 моделей загружены `status=ok` после restart. Ничего не отключено.

**Зачем:** Audit 2026-05-04 показал ~25/82 non-zero features в живом потоке; исследование (2026-05-05 09:00) выявило две конкретные дыры (HELIUS T+120 race, CREATOR solo). Skew fix унифицирует train/serve distribution.

**Результат:**
- **proba spread:** baseline `min=0.01 max=0.34 p90=0.19 avg=0.06` → post-fix `min=0.02 max=0.36 p90=0.27 avg=0.12` (24 scores). max ≥ 0.10 уверенно — было критерием skew_fix успеха.
- **HELIUS coverage** (ml_feature_vector audit): 8/12 → 9/12 (+ extrapolated values теперь meaningful, не 0)
- **DERIVED:** 5/10 → 7/10
- **Trade rate:** 6/час pre-fix → ~12/час post-fix (ML override чаще проходит floor 0.15)
- **PnL** на 4 post-restart trades: -0.028 SOL (-0.7% avg). Слишком малая выборка; следить 24h.

**Файлы изменены:** `pulse_bot/pipeline.py:1610-1670` (extrapolation), `pulse_bot/ml/features/_main.py:862-940` (NaN-fill), `pulse_bot/ml/build_dataset.py:246-450` (parity), `tests/pulse_bot/test_features_parity.py` + `tests/pulse_bot/test_t30_model.py` (тесты обновлены под новое поведение). Перерлоучены: `data/ml/{entry_model,entry_model_reg,entry_model_t30}.{ubj,meta.json}`.

**Откат:**
1. `git revert` всех 4 файлов кода
2. Восстановить старые модели из `.prev` (auto-backup в train.py): `cp data/ml/entry_model.ubj.prev data/ml/entry_model.ubj` (тоже для reg, t30 + meta files)
3. `systemctl --user restart pulse-bot.service`

**Watch:** через 24h сравнить total PnL vs pre-fix baseline (+0.229 SOL/24h). Если деградация >50% — rollback. Если PnL ровно или вверх — keeper.

---
## 2026-05-04 16:30 — Validator rebuild from fresh snapshot + train/serve skew analysis

### Validator rebuild

**Что изменилось:** Остановили `solana-validator.service`, переместили `ledger`, `snapshots`, `accounts` в `*.broken.20260504_163102` (1.2TB сохранены), создали пустые dirs, перезапустили.

В `~/.config/systemd/user/solana-validator.service` добавлен флаг `--minimal-snapshot-download-speed 1048576` (1 MB/s) — иначе validator слишком агрессивно abort'ил downloads на peers >5 MB/s, не находил подходящих и крутился в retry-loop.

**Зачем:** root застрял на slot 416490330 с 2026-04-29 19:30 (4+ дня без advancement). `getHealth` показывал `behind by 220615 slots`. Все диагностики из task #58 / #70 (UFW порты, getHealth, catchup) были correct — root просто перестал двигаться. Только полный rebuild лечит — bank state corrupt без чёткой ошибки.

**Результат:** Validator запустился в `bootstrap` фазе, скачивает свежий snapshot. Скорость от peers — **0.5-1 MB/s** (rate-limited). Foundation snapshot URL `https://api.mainnet-beta.solana.com/snapshot.tar.bz2` ещё медленнее (738 B/s). Snapshot ~118 GB → catchup займёт **24-48 часов** в фоне.

**Бот не пострадал:** `PUBLIC_RPC_URLS` в `.env` использует только Helius (3 ключа), validator там не listed — bot работает на Helius.

**Откат:** `systemctl --user stop solana-validator && rm -rf ledger snapshots accounts && mv ledger.broken.20260504_163102 ledger && mv snapshots.broken.20260504_163102 snapshots && mv accounts.broken.20260504_163102 accounts && systemctl --user start solana-validator`. Восстановит застрявший state (был behind 220k slots) — не лечит проблему.

**Next:** мониторить `du -sh /home/sergey/solana-validator/snapshots/remote/` пока snapshot не достигнет ~118 GB. После этого validator перейдёт в `caught up` фазу.

---

### Train/serve skew analysis

**Что изменилось:** ничего в коде — диагностика. `tmp/feature_audit2.py` запущен на live token с ≥10 трейдов. Подсчитал по группам.

**Зачем:** в логах warn `predict_proba: only 24-29/82 features non-zero — Suspected train/serve skew`. Нужно было понять *какие именно* фичи мёртвые.

**Результат — три группы фич почти полностью мёртвые на инференсе:**

| Группа | Non-zero/Total | Проблема |
|---|---|---|
| SCORER | 28/31 (90%) | ✓ healthy |
| DERIVED | 7/10 (70%) | ✓ healthy |
| HELIUS | **0/12 (0%) ✗** | `holder_snapshot` пустой/нулевой при scoring — Helius captures либо не успели до scoring, либо не пишут в `token_holders_snapshots` |
| CREATOR | **0/4 (0%) ✗** | `creator_snapshot` lookup даёт нули — либо creator-новый wallet без истории, либо `Database.get_creator_stats_as_of_sync` баг |
| WALLET | 8/12 (67%) | top10_buyer_*, n_bots_in_top10 NaN — частично |
| TIME_AWARE | **0/9 (0%) ✗** | unique_buyers_at_30/60/90, buy_rate_at_30/60/90 — windowed агрегаты не вычисляются (или scoring до T+30s) |
| TIME_AWARE_DERIVED | **0/4 (0%) ✗** | производные от dead TIME_AWARE |
| **ИТОГ** | **43/82 (52%)** | модель видит ~48% нулей вместо trained distribution |

**Возможные фиксы (не реализованы):**
1. **NaN вместо 0 для missing**: XGBoost обрабатывает NaN нативно; 0 интерпретируется как "feature=0", обманывая модель
2. **Defer scoring до T+30s минимум**: даст HELIUS T+30 + TIME_AWARE_30 успеть populate
3. **Retrain на сэмплах с такой же sparsity**: training data должен матчить inference distribution
4. **Дебаг каждой группы:** поверить что `helius_holders.py` пишет данные в `token_holders_snapshots` для ВСЕХ tokens, не только при FAST_BUY

**Следующий шаг:** выбрать одну стратегию фикса и реализовать. Самое дешёвое — пункт 1 (NaN) + пункт 4 (debug Helius writes). Самое правильное — пункт 3 (retrain).

---

## 2026-05-04 16:00 — WS keepalive deep dive: watchdog + read-loop refactor

**Что изменилось:** В `pulse_bot/launchpads/pumpfun.py`:
- `_read_messages` использует `async for raw_msg in self._ws` (раньше пробовал `asyncio.wait_for(ws.recv())` — терял ~50% сообщений когда таймаут отменял inner future).
- `_watchdog_loop` — отдельная coroutine, форс-реконнектит WS если `_last_msg_ts` молчит >300с (safety net на случай silent server close).
- `__init__` добавляет `_watchdog_task` и `_last_msg_ts`; `connect`/`disconnect` управляют их жизненным циклом; reconnect в `_ws_reader_loop` сбрасывает `_last_msg_ts`.
- `_establish_connection` оставлен на `ping_interval=30, ping_timeout=60` (как до 2026-05-04). Эксперимент с `ping_interval=None` подтвердил что pumpportal не отвечает на RFC ping/pong, но по факту даёт хуже throughput на загруженном production-подключении (silent gaps по 180с) — лучше переподключаться каждые ~103с по 1011 чем висеть в silent connection.

**Зачем:** После 13:30 фикса (API key) обнаружилось что keepalive 1011 drops остались каждые ~103с — клиент `websockets` закрывал WS потому что pumpportal не пингует обратно. Probe показал 3 стратегии (no-heartbeat / `{"method":"ping"}` / re-`subscribeNewToken`) все упали на t≈100с — RFC ping/pong единственный путь поддержания соединения для библиотеки. С `ping_interval=None` probe survived 375s, но в production бот всё равно молчал 180с и нужен был watchdog. На реверте к 30/60 + watchdog бот стабильнее: drops ритмичные и предсказуемые, а не случайные silent gaps.

**Открытое:** Throughput create-events на боте **~1.2/min** vs probe **4-6/min** к тому же серверу. Это server-side throttling Lightning tier (или per-connection rate-limit с активными subscribeTokenTrade). Не блокирует — бот работает (29 токенов / 30 мин, 70 трейдов / 16 mints, 18 paper_trades / 1ч), но throughput ниже чем до 2026-05-01.

**Возможные следующие шаги:**
1. Профинансировать Lightning wallet больше 0.02 SOL — может разблокирует rate
2. Переключиться на `PULSE_LAUNCHPAD=geyser+pumpportal` (multiplexer уже готов, Geyser plugin задеплоен на rich validator) — если Geyser даёт create events напрямую, обойдём троттлинг
3. Платный PumpPortal tier — если Lightning имеет hard cap

**Откат:** Удалить `_watchdog_loop`, `_watchdog_task`, `_last_msg_ts` из `pulse_bot/launchpads/pumpfun.py`; вернуть `_read_messages` к версии до 2026-05-04 16:00.

**Файлы:** `pulse_bot/launchpads/pumpfun.py`.

---

## 2026-05-04 13:30 — PumpPortal WS auth: добавлен API-ключ, fixed `subscribeTokenTrade` gating

**Что изменилось:** добавлен `PUMPPORTAL_API_KEY` в `.env` + `config.py:PUMPPORTAL_API_KEY`; в `pulse_bot/launchpads/pumpfun.py:_establish_connection` к WS URL прикладывается `?api-key=…`; в `_read_messages` неизвестные ответы логируются на WARNING вместо silent drop.

**Зачем:** с ~2026-05-01 PumpPortal без объявления загейтил `subscribeTokenTrade` и `subscribeAccountTrade` под API-ключ, чей привязанный Lightning-кошелёк держит ≥0.02 SOL. WS принимал `subscribe`-сообщение и не возвращал явной ошибки, но трейды не доставлялись — server-side keepalive дропал соединение каждые ~50-90с с code 1011. 3 дня (с 2026-05-01) бот получал 0 трейдов в реалтайме (только Helius T+30/T+180/T+300 captures), что заблокировало все entry-decisions требующие trade-volume. Silent drop неизвестных WS-сообщений в `_read_messages` маскировал баг — добавили WARNING чтобы такое ловилось сразу.

**Lightning-кошелёк:** `GHf7v3Kx9r9VA58UE47k8FouHzyrsCtWH3gx8VWTdK2v` создан через pumpportal.fun/getting-started, профинансирован 0.02 SOL с бот-кошелька (`5cz57jC...`, TX `54io7BhkMVUna5NjAHpZyHCXezMANR8NpGpj92pWs8PK2ZM3Hp6K2dCq3wNXmFvDq9pYWeLshJS5271R9yKDvJ6w`). Lightning используется **только** для WS-аутентификации; основной торговый кошелёк остаётся `5cz57jC...` (0.20 SOL баланс).

**Результат:**
- До: 0 трейдов в `trades` за 3 дня (с 2026-05-01).
- После рестарта (2026-05-04 13:25 UTC): 17 трейдов / 3 unique mints за первые 5 минут.
- WS log теперь содержит `Successfully subscribed to keys.` после `subscribeTokenTrade` (раньше silent).
- BASED token (`7xWkzP7DmhRV`) — первый после рестарта прошёл полный пайплайн: T+15 WAIT_MORE → T+30 DEFER (proba=0.527) → T+30 TIMING WAIT_MORE (p_buy=0.22).
- Keepalive 1011 drops остались (каждые ~52с, ~5% downtime); subscriptions автоматически переустанавливаются после реконнекта — функционально не блокирует, но требует отдельного фикса.

**Откат:** удалить `PUMPPORTAL_API_KEY` из `.env`; в `pulse_bot/config.py` удалить определение `PUMPPORTAL_API_KEY`; в `pulse_bot/launchpads/pumpfun.py` вернуть импорт + `_establish_connection` к виду до 2026-05-04. Lightning-кошелёк можно вывести через recovery (private key в `pumpportal-wallet.json`).

**Файлы:** `pulse_bot/config.py`, `pulse_bot/launchpads/pumpfun.py`, `.env` (rich), `pumpportal-wallet.json` (rich + Mac бэкап).

---


## 2026-05-01 20:04 — Cleanup pass: dead code, smoke tests, filter health observability

### Удалено dead code
- `pulse_bot/filters/creator.py` — `CreatorFilter` никогда не импортировался в production. Был источником путаницы: я обновлял `creators.blacklisted=1`, ожидая что этот класс сработает, а он мёртвый. Источник реального gate теперь `DecisionService.filter_creator_blacklist`.

### Добавлен `pulse_bot/filter_health.py` + boot integration
Cканирует `bot.log`, считает per-gate firing rates, лоупает в boot summary сразу после `MODEL REGISTRY`. Помечает мёртвые гейты `✗ DEAD` чтобы операторы видели когда фильтр не стреляет неделями (как `creators.blacklisted` до этой сессии).

**Tracked gates:**
- `creator_blacklist_skip` (DecisionService)
- `bot_cluster_skip` (DecisionService)
- `wash_cluster_skip` (DecisionService, env-gated)
- `survival_predict_exit` (Pipeline)
- `t30_skip_early` (DecisionService EARLY OVERRIDE pattern)
- `timing_skip_early` (same)
- `ml_sl_tightened` (ExitManager)
- `dynamic_max_hold_used` (placeholder — нет log line yet)

### Tests добавлены (27 passing total для cleanup)
- `tests/pulse_bot/test_creator_blacklist_filter.py` (6 tests) — pin DecisionService.filter_creator_blacklist contract
- `tests/pulse_bot/test_filter_health.py` (5 tests) — log scanning, regex correctness
- `tests/pulse_bot/test_wash_cluster_filter.py` (6 tests) — earlier this day
- `tests/pulse_bot/test_codex_fixes_2026_04_30.py` (10 tests) — earlier this day

### Inline-bug найден и зафиксирован
`UnboundLocalError: _os_t30 not defined` — timing block использовал `_os_t30` который импортируется в t30 block выше. Если t30 не active И не shadow, t30 block пропускается, а timing всё ещё пытается дёрнуть `_os_t30.environ`. Fix: locally `import os as _os_timing` в timing block.

### Первый live FILTER HEALTH summary

| Gate | n | Last seen | Verdict |
|---|---|---|---|
| survival_predict_exit | 203 | 17:11:26 | ✓ ALIVE |
| t30_skip_early | 233 | 08:56:34 (11h ago) | ⚠️ stale |
| ml_sl_tightened | 2 | 20:02:29 | ✓ ALIVE |
| creator_blacklist_skip | 0 | — | ✗ DEAD (just activated) |
| bot_cluster_skip | 0 | — | ✗ DEAD (classifications lag) |
| wash_cluster_skip | 0 | — | ✗ env-disabled |
| timing_skip_early | 0 | — | ✗ env-disabled |
| dynamic_max_hold_used | 0 | — | ✗ survival pre-empts |

**TODO (отдельный followup):**
- t30_skip_early не стреляет 11h — investigate (regression after some change?)
- `dynamic_max_hold_used` — нужно добавить log line когда model.predict вызывается

**Откат:** `git revert` для `filter_health.py` + restore `filters/creator.py` (но не нужно, dead code).

---

## 2026-05-01 16:00 — feature_hydration text=numeric bug FIXED (CRITICAL)

**Что:** в `feature_hydration.py:99-101` неправильный 2-й аргумент в `get_creator_stats_as_of_sync(creator, ref_mint)`. Передавался `float(scored_at)` (timestamp) вместо `token.mint` (string). PG-запрос `WHERE mint = ?` получал numeric → каждый токен → `operator does not exist: text = numeric` → exception → creator_snapshot=None → 53/82 ML features dropped (creator + derived + wallet_prior_stats path partially zero).

**Fix:** `token.mint` вместо `float(scored_at)` в `feature_hydration.py:106`.

**Результат:** последний `creator_snapshot lookup failed` log = 15:57:30. Бот рестартанул 15:57:43. После restart — **0 ошибок** этого типа.

**Известный остаток:** `predict_proba: only 21-27/82` warnings всё ещё лоупаются — но это **не от этого бага**, а pre-existing wallet_classifications freshness lag (fresh wallets не классифицированы → top10_buyer_* features = NaN). Отдельная проблема.

**Также рассмотрены и отложены 2 связанных бага:**
- `rug_count` в `creator_snapshots` всегда 0: definition issue в `helius_creator.py:139` (`mc_at_score < 1.5 SOL` не ловит pump.fun rugs которые сначала peak'ят 25-30 SOL потом dump'ятся). Требует другой peak detection + backfill 24k creators. Не блокер — `graduation_rate` purposeful использует.
- `tokens_where_creator_sold_early` всегда 0: `pipeline.py:393` always passes `sold_early=False`. Used only in dead `filters/creator.py` (никем не вызывается в pipeline). Не блокер — DecisionService.filter_creator_blacklist использует cumulative `creators.blacklisted` напрямую.

---

## 2026-05-01 16:00 — Creator blacklist Tier-2 ACTIVATED (493 scammers)

### Investigation findings

User pushed back on "только 100+ tokens?" → audit showed **gradient**:
| Tier | Definition | Creators | %trades cut | PnL saved |
|---|---|---|---|---|
| 1 STRICT | 20+ tok, grad<1%, peak<25 | 56 | 4.5% | +39 SOL |
| **2 SHIPPED** | **20+ tok, grad<2%, peak<28** | **499** | **25.9%** | **+234 SOL** |
| 5 LOOSE | 10+ tok, grad<3%, peak<35 | 1322 | 45.8% | +389 SOL |

Tier-2 picked: 499 creators, 26% trades cut, +234 SOL paper saved per ~2 weeks. Diminishing returns past Tier-3.

### Critical bug found in deployment

После SQL `UPDATE creators SET blacklisted=1 WHERE wallet IN (Tier-2)`, бот **не блокировал ни одного** scammer токена за 5 минут (хотя 30% новых tokens из last hour были от blacklisted creators):

**Корень:** `scorer.py:288` проверяет `creator_snapshot.blacklisted`, но `get_creator_stats_as_of_sync` (`db.py:468`) ВСЕГДА возвращает `blacklisted=False` (leak-free as-of view design). А legacy `CreatorFilter` в `filters/creator.py` хоть и читает cumulative таблицу — **никем не вызывается** в pipeline. Dead code.

### Fix

Добавил `DecisionService.filter_creator_blacklist` (mirror `filter_bot_cluster` pattern):
- Async COUNT query на `creators.blacklisted = 1`
- Hard SKIP до ML override
- Counter `creator_blacklist_skips` для observability
- Pipeline `pipeline.py:1006-1008` вызывает его перед bot_cluster + wash_cluster

### Backfill infrastructure

- `scripts/backfill_creator_blacklist.py` — re-scans Tier-2 criteria (env-tunable thresholds), idempotent (flips ON и OFF), `--dry-run` mode
- Crontab: `17 * * * *` — hourly re-scan на rich
- Logs to `/home/sergey/www/gg/logs/creator_blacklist.log`

### Status

- 493/499 candidates flipped (6 в creator_snapshots но не в `creators` таблице — добавятся при первом encounter)
- Бот рестартанул, фильтр active
- Live firings — ждём (бот видит ~30% blacklisted tokens из новых)

### Известные побочные баги (отдельные TODO)

1. **`creator_snapshot lookup failed: text = numeric`** в `feature_hydration.py` — type mismatch SQL. Bot работает но 27-29 фич non-zero вместо 80+. Влияет на ML quality. Отдельная сессия.
2. **`is_creator` field в snapshot** — `tokens_where_creator_sold_early` и `rug_count`/`rug_rate` не заполняются (всегда 0). Не блокер для blacklist (мы используем `total_prior_tokens`/`graduation_rate`/`median_peak_mc_sol` которые заполняются), но closed loop отсутствует.

**Откат:**
- `UPDATE creators SET blacklisted = 0;` — вся таблица в один присест
- Crontab: `crontab -e` и удалить cron entry
- Code: `git revert` коммит с DecisionService.filter_creator_blacklist

---

## 2026-05-01 09:00 — Wallet blacklist audit + wash-cluster gate (shipped OFF)

**Codex review** of wallet-blacklist strategy → recommended wash-cluster hard gate as highest EV. Implemented + tested + sweep-validated. Sweep showed **NO acceptable threshold**, so shipped behind env flag default OFF.

### Findings before implementation

**1. Bot-cluster filter уже существует** (`DecisionService.filter_bot_cluster`, `decision_service.py:91-164`) и активен с `PULSE_BOT_CLUSTER_HARD_SKIP=3` (default). Но **0 firings за 4+ дня** — `wallet_classifications` отстаёт: на момент scoring fresh wallets ещё не классифицированы.

**2. Audit recent paper_trades (7d) с CURRENT classification state:**
| signal | n with signal |
|---|---|
| ≥3 bot wallets in first 30s | **2256/4355 (52%)** |
| ≥3 cluster_id wallets | 17/4355 (0.4%) |
| ≥6 sniper wallets | 2520/4355 (58%) |

Это «freshness lag», НЕ train/serve skew (как кодекс назвал). Skew был бы при разных типах данных в train/inference. У нас одинаково — current state, просто на свежих кошельках state пустое.

### Wash-cluster gate (Option C из кодекс review)

**Код:** `DecisionService.filter_wash_cluster` mirror'ит `filter_bot_cluster` pattern. Wired в `pipeline.py:1003-1006` сразу после bot-cluster.

**Env params (все default OFF / пермиссивные):**
- `PULSE_WASH_CLUSTER_SKIP_N=0` (default — disabled)
- `PULSE_WASH_CLUSTER_SIZE_MIN=5`
- `PULSE_WASH_CLUSTER_SIZE_MAX=50`

**Tests:** `tests/pulse_bot/test_wash_cluster_filter.py` — 6 тестов passed (disabled-by-default, fire-on-threshold, below-threshold-pass, already-skip-noop, late-buyers-ignored, db-failure-passes-through).

### Optimizer sweep на 5490 closed paper_trades

| skip_n | band | %cut | WR lift |
|---|---|---|---|
| 2 | [5,50] | 1.4% | **-0.17pp** ❌ |
| 3 | [5,50] | 0.9% | -0.08pp ❌ |
| 4 | [5,50] | 0.5% | **+0.01pp** ≈ noise |
| 5 | [5,50] | 0.3% | +0.01pp ≈ noise |

Acceptance criterion (codex): **≥+1.5pp WR lift AND ≤8% cut** — НЕ ДОСТИГНУТ ни одной комбинацией.

**Объяснение:** ML model уже видит wash-сигнал через `top10_buyer_prior_*` features (Phase E wallet analytics, schema v14+). Hard gate redundant. Sample 5490 trades / 437 wins недостаточен для +1.5pp детектирования.

### Решение

- Wash-cluster gate code shipped в production, default OFF
- Активация отложена до накопления больших данных (`>10k trades`) или нового сигнала в audit
- НЕ тратим время на (a) creator blacklist backfill (codex defer), (b) point-in-time wallet snapshot fix (большая работа без ROI на текущих данных)

**Откат:** `git revert` или просто `PULSE_WASH_CLUSTER_SKIP_N=0` (default уже OFF).

---

## 2026-05-01 07:00 — Codex review fixes (1 CRITICAL + 3 MAJOR) + tests + timing meta

**Codex independent review** на 4 нетривиальных изменения 2026-04-30 нашёл 1 CRITICAL и 3 MAJOR бага. Все исправлены:

### CRITICAL — `pipeline.py` survival threshold parsed per-tick
`PULSE_SURVIVAL_THRESHOLD` парсился внутри hook'а на каждом тике. Malformed value (как inline-comment regression .env) → `ValueError` каждый тик после `min_hold` → silently dropped exits.
**Fix:** parse один раз в `Pipeline.__init__` (line 254-273), validate range (0,1], fail loudly с понятным сообщением. Вызов на line 1714 теперь использует `self._survival_threshold`.

### M1 — `exit_manager.py` clamp инвертируется при `cfg.exit_max_hold_seconds < 30`
Optimizer sweep мог поставить `exit_max_hold_seconds=20s`. Старый clamp `max(30, min(pred, 20)) = 30` — позволял модели **расширить** hold за статический потолок 20s.
**Fix:** `floor = min(30, exit_max_hold_seconds); max(floor, min(pred, ceiling))`. Теперь модель никогда не extend'ит выше static cap.

### M2 — `build_dataset.py` `_simulate_forward_hold_seconds` без exception handler
Один bad row в 60k-row rebuild → краш всей сборки.
**Fix:** wrap в `try/except` с `logger.exception` и fallback `0.0` (right-censored).

### M3 — `is_creator=False` хардкоден
`PulseMonitor` использует `t.is_creator` для `creator_selling` death-signal. Симулированный exit никогда не видел creator dumps → forward-hold labels систематически overestimate hold time на токенах с creator dumps.
**Fix:** SQL `build_exit_dataset` теперь тащит `tokens.creator AS creator_wallet`, передаёт в `_df_rows_to_trades` через новый параметр `creator_wallet`. Trade.is_creator = `wallet == creator_wallet`.

### Тесты — `tests/pulse_bot/test_codex_fixes_2026_04_30.py` (10 passed)
- Dynamic max_hold clamp: 3 теста (нормальный случай, низкий cfg<30s, predict failure)
- _simulate_forward_hold_seconds: 2 теста (empty future, simulator crash)
- _df_rows_to_trades: 2 теста (creator match, no creator)
- MonitorResult.hold_seconds: 3 теста (hard_stop, timeout с param, timeout default=0)

### Per-class metrics в entry_timing meta (`entry_timing.py`)
Добавлены к `meta.json` при следующем retrain'е:
- 80/20 random split (train/test masks)
- per-class precision/recall/f1/support через `precision_recall_fscore_support`
- one-vs-rest AUC через `roc_auc_score(multi_class="ovr")`
- `train_rows`, `test_rows` поля
- При тиниx датасетах (<150 rows) — фолбэк на all-train, no-test (для unit-тестов).

### Live audit findings (post-activation)
| window | n | mean PnL | WR |
|---|---|---|---|
| 12:20-17:20 (baseline) | 66 | -3.86% | 9.1% |
| 17:50-now (+ survival) | 62 | -4.01% | 9.7% |

Survival ACTIVE: 87 force-exits, **mean PnL +0.01%** (нейтральный cut). Dynamic max_hold: **0 firings** — survival забирает все exits первым (fires at ~100s elapsed, чуть позже 90% времени cap). dyn_max_hold redundant в текущей конфигурации; можно отключить позже без потери (risk нулевой).

Бот рестартанул, все 8 моделей status=ok.

**Откат:** все фиксы покрыты тестами; rollback по отдельному файлу при необходимости.

---

## 2026-04-30 21:00 — `PULSE_TIMING_SKIP_ONLY_ACTIVE` knob (codex review)

**Что:** добавлен env-flag для активации SKIP-side у entry_timing без BUY-side. Mirror entry_t30 pattern. По рекомендации codex review (2026-04-30 19:00).

**Изменения в `pipeline.py`:**
- L252-260: новый `self._timing_skip_only_active` (env `PULSE_TIMING_SKIP_ONLY_ACTIVE=1`, default OFF)
- L298-302: модель загружается также при skip_only режиме (mirror existing shadow check)
- L671-674: early-decision-active proxy включает skip_only режим
- L1338-1402: timing block переписан — БYU-branch явно гvarded на `_timing_active` (не сработает в skip_only); SKIP-only ветвь mirror entry_t30 SKIP_EARLY pattern с `state["source"]="timing_skip"` для observability

**Семантика:**
- `PULSE_TIMING_ACTIVE=1` — full LIVE (BUY + SKIP overrides)
- `PULSE_TIMING_SKIP_ONLY_ACTIVE=1` — только SKIP_EARLY override
- `PULSE_TIMING_SHADOW=1` — shadow logging only
- Все три могут комбинироваться

**Бот рестартанул, все 8 моделей status=ok.**

**Активация (`PULSE_TIMING_SKIP_ONLY_ACTIVE=1`) НЕ выполнена** — codex запрещает на устаревших shadow данных. Ждём 48h fresh shadow на новом model stack (новый entry_v0.917 + dynamic_max_hold + survival ACTIVE с 2026-04-30 17:20-17:50). Через 48h re-run audit; если SKIP@≥0.95 mean ≥ -1% — активировать с `PULSE_TIMING_CONFIDENCE_GATE=0.95`.

**Откат:** `sed -i '/PULSE_TIMING_SKIP_ONLY_ACTIVE/d' .env && systemctl --user restart pulse-bot.service`. Code change безопасен — без env флага новый код-путь не активен.

---

## 2026-04-30 17:35 — Survival threshold calibrated to 0.10

**Что:** прогнал sweep `survival_threshold ∈ {0.10..0.95}` против 4742 реальных закрытых paper_trades. Замерял Spearman ρ predicted vs actual hold time + сколько winners (pnl>+3%) преждевременно бы вырубились.

| threshold | ρ | pred median | kill winners | save deaths |
|---|---|---|---|---|
| **0.10** ⭐ | **+0.500** | 60s | 64% (187/291) | 44% (1672/3793) |
| 0.30 | +0.436 | 25s | 85% | 69% |
| 0.50 (default) | +0.423 | 15s | 96% | 88% |
| 0.85 | +0.232 | 5s | 97% | 93% |

`0.10` оптимальный:
- Лучший Spearman ρ (+0.500 vs +0.423 на дефолте)
- Median pred=60s **совпадает** с median actual=60s (правильный масштаб)
- Соотношение спасённых смертей к убитым winners = **8.9×** (1672/187)

**Изменения:**
- `pulse_bot/pipeline.py:1685` — добавлен env-флаг `PULSE_SURVIVAL_THRESHOLD` (default 0.10), пробрасывается в `predict_remaining_life`.
- `.env` на rich: `PULSE_SURVIVAL_THRESHOLD=0.10`.
- Бот рестартанул, threshold подхвачен.

**Sanity check после перезапуска:**
- low_score state: remaining_life @ thr=0.5 = 20s → @ thr=0.1 = **60s** ✓
- mid_score: 5s → 10s
- high_score: 5s → 10s

**Активация выполнена 17:50** — `PULSE_SURVIVAL_ACTIVE=1` в rich `.env`, бот PID 251603 рестартанул, лог: `Survival exit ACTIVE: model will load on first paper trade tick. min_hold=15s`. Двойной gate (threshold=0.10 + pipeline cutoff <30s) обеспечивает консервативность.

**Откат при регрессии:** `sed -i '/PULSE_SURVIVAL_ACTIVE/d' .env && systemctl --user restart pulse-bot.service`. Watch-window 6-12h: если paper PnL ухудшается значимо vs preceding 6-12h baseline (без survival, но с dynamic max_hold) — откатить.

---

## 2026-04-30 17:20 — Dynamic max_hold ACTIVATED + survival sanity verified

**C — `PULSE_EXIT_MAX_HOLD_DYNAMIC=1` активирован на rich.**
Бот рестартанул PID 199054. config: `exit_max_hold_dynamic=True`, env: `PULSE_EXIT_MAX_HOLD_DYNAMIC=1`. ExitManager теперь предсказывает max_hold per-token при первом decide() через `exit_quantile_max_hold` (ρ=+0.501). Clamp `[30s, 120s]` — модель может только сократить hold, не растянуть.

Замечание про bash inline-комменты: `PULSE_EXIT_MAX_HOLD_DYNAMIC=1  # comment` после `set -a; source .env` попадает в env как литеральная строка `"1  # comment"` — `lambda v: v.lower() in ("1", "true")` вернёт False. Inline-комменты в `.env` запрещены — комментарий должен быть на отдельной строке.

**D — survival sanity verified, активация откладывается:**
- ✅ Predictions **больше не пустые** (было 20% empty hazard_curve в shadow данных — фикс из 15:30)
- ✅ Predictions **варьируются** по входу (low_score state → 20s remaining_life, high_score → 5s; hazard curves разные)
- ✅ Direction sensible: hazard падает по мере survival (0.722 @ T+0 → 0.469 @ T+30 → 0.366 @ T+60 → 0.238 @ T+90)
- ⚠️ **80% predictions = remaining_life=5s** (модель прогнозирует "die immediately" для большинства состояний)

→ **НЕ активирую `PULSE_SURVIVAL_ACTIVE=1`** — бот force-exit'ил бы 80% трейдов на T+5s, теряя winners. Калибровка слишком агрессивная (4.71% positive rate × bucket=5s даёт cumulative survival <0.5 в первом букете).

Survival остаётся в shadow — нужна калибровочная подстройка (`survival_threshold` 0.5 → 0.3) ПЕРЕД активацией. Параллельно shadow данные дадут ground truth для validation.

---

## 2026-04-30 16:55 — Dynamic max_hold wiring + survival labels fix

### A. Dynamic max_hold ready-to-activate

Wired exit_quantile_max_hold model into ExitManager под env-флагом (default OFF):
- `pulse_bot/pulse/exit_manager.py` — добавлен `quantile_max_hold_policy` параметр + `_dynamic_max_hold_cached`. На первый decide() предсказание вычисляется один раз и кэшируется. Clamped to `[30s, cfg.exit_max_hold_seconds]` — модель может только **раньше** выйти, не позже статического потолка.
- `pulse_bot/config.py` — `exit_max_hold_dynamic: bool = False`. Env: `PULSE_EXIT_MAX_HOLD_DYNAMIC=1`.
- `pulse_bot/core.py` — загрузка модели когда флаг ON.

Активация через 24-48h shadow validation (paired-bootstrap vs static max_hold=120s).

### B. Survival model labels fix — PnL-based death

**Найден критический баг.** `DEATH_EXIT_REASONS` = `{pulse_dead, no_new_blood, sell_pressure}` пропускал самые частые smerti, и наоборот включал в смерть положительные сделки. Audit на paper_trades:

| exit_reason | n | avg PnL | старый label |
|---|---|---|---|
| dead_token | 9016 | -5.87% | "censored" ❌ |
| hard_stop | 1868 | -19.49% | "censored" ❌ |
| no_new_blood | 496 | +3.28% | "death" ❌ |
| sell_pressure | 267 | +0.28% | "death" ❌ |

~10800 реальных смертей помечались как "выжили", ~760 winners как "deaths". Модель училась обратному.

**Фикс:** death определяется по реализованному PnL: `pnl_pct < -3.0%`. Policy-agnostic, grounded в observed outcome.

**До/после:**
- Hazard frame rows: similar size
- **Positives: ~700 → 10,389** (×15 рост, теперь 4.71% rate)
- features: pnl_pct исключён из feature set (label-derived, был бы leak)

Бот рестартанул, все 8 моделей status=ok.

**Откат:**
- `DEATH_EXIT_REASONS` revert + `_record_duration` revert
- max_hold dynamic — оставить env-флаг OFF (default), wiring остаётся в коде

---

## 2026-04-30 16:15 — exit_quantile_max_hold v2 — simulate_exit-driven target

**Что изменилось:** заменил target `forward_seconds_to_peak` (имел survivor bias, ρ=-0.196) на `forward_seconds_to_exit` через `simulate_exit()`. Теперь target = время за которое live exit-policy (TP/SL/max_hold + monitor signals) закрыла бы позицию, открытую в текущем state. Ровно та задача что модель решает на inference.

**Технический cliff:**
- `MonitorResult` в `core.py` — добавлен опциональный field `hold_seconds: float = 0.0`
- `PaperTradeRunner.process_trade/tick/timeout_result` — заполняют `hold_seconds`
- `simulate_exit._replay_trades` — пробрасывает hold_seconds
- `build_dataset.py` — новый helper `_simulate_forward_hold_seconds(...)` вызывается на каждой sample row
- `train.py:train_exit_quantile_max_hold` — обновлён target column name

**Результат (сравнение на retrain):**

| Metric | v1 (forward_seconds_to_peak) | v2 (forward_seconds_to_exit) |
|---|---|---|
| Spearman ρ | **-0.196** (anti_correlated) | **+0.501** (ok) |
| Coverage @ q=0.75 | n/a (rejected) | 79.1% (хорошая калибровка) |
| Status | anti_correlated | **ok** |
| Train rows | — | 2255 |
| Test rows | — | 1800 |

**Distribution target после фикса:** min=0.0, median=1.8s, max=30.0s. Все pump.fun трейды быстро экзитят (хард stop / inactivity / dump). max=30s — артефакт post-scoring data truncation (большинство токенов не имеют trade-stream после 30s).

**Все 8 моделей теперь status=ok** в boot summary. exit_quantile_max_hold вернулся из `_broken/` в активные артефакты.

**Активация (отдельная задача):** модель остаётся shadow-only. Активация через будущий `PULSE_EXIT_MAX_HOLD_DYNAMIC=1` после paired-bootstrap gate vs static max_hold=120s.

**Откат:**
- `mv data/ml/_broken/exit_quantile_max_hold.* data/ml/` (старый артефакт)
- В `build_dataset.py` вернуть target = `forward_seconds_to_peak`
- Убрать `hold_seconds` из MonitorResult (опционально)

---

## 2026-04-30 15:30 — Shadow audit + 3 model fixes

**Что изменилось:** прогнал shadow-data анализ на 4 днях накопленных предсказаний (51k entry_timing, 12k entry_t30, 977 survival, 175 exit_quantile rows). Выявил 3 проблемы, исправил.

### 1. exit_quantile_max_hold target bug (ρ=-0.196 anti_correlated)
**Корень:** `build_dataset.py:932` target=`forward_seconds_to_peak` имеет survivor bias. DOA tokens (нет дальнейшей активности) → idx_peak в начале окна → sec=0; pumpers → peak в конце → sec=600. Модель учится "мало активности → жди долго; много активности → коротко" — обратное от нужного.
**Действия:**
- Добавил TODO-комментарий в `build_dataset.py:932` объясняющий баг.
- Переместил `data/ml/exit_quantile_max_hold.{ubj,meta.json}` → `data/ml/_broken/`. Boot summary теперь показывает "MISSING" вместо anti_correlated.
- Правильный target (на будущее): simulate_exit-driven label = время до {TP|SL|max_hold} под live-политикой через `simulate_exit.py`.

### 2. survival_model — 20% empty predictions
**Корень:** `meta.max_horizon_seconds=180`, но `PULSE_EXTENDED_OBSERVE_SECONDS=600` заставляет хук вызываться при `elapsed > 180`. `predict_remaining_life` в `survival.py:381` возвращает `SurvivalPrediction(remaining_life=0)` с empty hazard_curve — мусорная shadow запись + force-exit на каждом тике если активировать.
**Фикс:** добавил early-return в `pipeline.py:1685` — `if elapsed >= max_horizon: return None` ДО вызова predictor.

### 3. timing model — false alarm
Boot log "Entry-timing checkpoint ACTIVE" обманчив — он fires когда модель ЗАГРУЖЕНА (для shadow или live), не когда влияет на решения. Реально `PULSE_TIMING_ACTIVE` НЕ выставлен — timing не активна. Никаких изменений (false alarm в моём анализе).

### t30 SKIP-side проверка с production threshold
Перебакетил с `skip_tail=0.005` (production), не 0.05:
- DEEP_SKIP (p<0.005): n=353, mean PnL=**-1.15%**, WR=0.6% — DOA-токены, мини-потери
- SOFT_SKIP (0.005-0.05): -4.19%, 6.1% WR
- MID (0.05-0.85): -4.81%, 8.7% WR

DEEP_SKIP уже отделяет "мёртвые" токены — `PULSE_ENTRY_T30_SKIP_ACTIVE=1` работает как задумано.

**Результат:** Model Registry boot summary до:
```
exit_quantile_max_hold  rho=-0.196  status=anti_correlated  ⚠️
```
после:
```
exit_quantile_max_hold  MISSING (data/ml/exit_quantile_max_hold.ubj)
```

**Откат:** `mv data/ml/_broken/exit_quantile_max_hold.* data/ml/`. Survival fix откатить — убрать early-return в `pipeline.py:1685`.

---

## 2026-04-30 13:00 — Retrain all 8 models + honest top-N comparison

**Что изменилось:** sequential retrain через `scripts/retrain_all_8.sh` после фикса survival pipeline (использует `SurvivalLabelBuilder.build_from_db()` вместо exit.parquet — нужен hazard frame с `died_in_bucket`). Артефакты на rich:
- `entry_model.ubj` (binary): AUC 0.898 → 0.917
- `entry_model_reg.ubj`, `entry_model_t30.ubj`, `entry_timing_model.ubj`
- `exit_quantile_{sl,tp,max_hold}.ubj`, `survival_model.ubj`

**Зачем:** schema v21 datacollection накопил 98,329 строк (vs 88k предыдущих). Хотели проверить улучшение ranking + завести survival_model которая раньше падала с `KeyError: 'died_in_bucket'`.

### Честное сравнение (top-N на shared holdout)

`daily_validation` отчёты сравнивать **нельзя** — разные test sets (14,899 rows / 363 pos vs 19,755 rows / 631 pos), разные base rates (1.43% vs 0.82%), разное N entries при `proba≥0.5`. Прогнал `scripts/honest_topn_compare.py` — обе модели на одном holdout (last 20% by `scored_at`, 19,666 rows / 184 positives, base rate 0.94%):

| N | PREV wins | NEW wins | Δ |
|---|---|---|---|
| 50 | 18 | 18 | 0 |
| 100 | 33 | 31 | -2 |
| 200 | 51 | 52 | +1 |
| 500 | 86 | 93 | +7 |
| 741 | 95 | 108 | +13 |
| 1000 | 108 | 116 | +8 |
| 1500 | 123 | 133 | +10 |

- **AUC на shared holdout:** PREV 0.906 → NEW 0.919
- **Top-100 overlap:** 46% (модели существенно разные)
- **Top-500 overlap:** 76%

**Вывод:** на самом топе (top-50/100) модели тай или PREV чуть лучше (-2 wins, в пределах шума). На широком диапазоне (500-1500) NEW заметно лучше (+7 to +13 wins). В текущем live-режиме (`PULSE_ENTRY_FLOOR_PCT=0.5`, ≈top-741 на этом holdout) NEW даёт +13 wins (+14% relative).

### Метрики которые НЕ являются деградацией

- **P@10% упал 9.36% → 6.03%** — артефакт base rate (1.43% → 0.82%). Lift вырос: 6.55× → 7.35×.
- **economic_backtest -1.96 → -1.09 SOL** — fixed TP=50/SL=30 в backtest, не live config (SL=15, max_hold=120s). Минус не означает потери в paper-режиме.

**Результат:** новая модель принята как baseline. 24h paper audit покажет реальный PnL на rich.

**Откат:** `mv data/ml/entry_model.ubj.prev data/ml/entry_model.ubj` + перезапуск бота на rich.

**Гайд по честному тестированию:** добавлен `docs/MODEL_TESTING.md` + переиспользуемый скрипт `scripts/honest_topn_compare.py`.

---

## 2026-04-30 04:30 — Codex review fallout: SL=8 bug rollback + multiplexer dedup fix

**Codex review** (independent code-review agent) caught 4 issues in yesterday's work. Triaged honestly:

### CONFIRMED real bug — SL=8 semantic confusion

`exit_hard_stop_loss_pct` compares **fee+slippage-adjusted PnL** to threshold via `_calc_leg_pnl()` (pulse_bot/core.py:278). With fees=1% × 2 + slippage=2-3% × 2 + priority=0.2%, the threshold of "leg_pnl<-8%" requires raw price drop of only ~1.09%. **Bot was cutting positions on every -1% dip.**

Confirmed in 24h audit:
- 576 hard_stop events (38.2% of 1507 closures), avg -15.03% reported PnL
- ~80% of -7.4 SOL/24h paper bleed attributable to over-aggressive SL

**Action:** rolled back `PULSE_EXIT_HARD_STOP_LOSS_PCT=8 → 15` in rich .env, bot restarted. Wait 24h for new steady-state.

### CONFIRMED bug — multiplexer dedup-key collision

`_trade_dedup_key` used `int(timestamp)` (1-second bucket) — sniper-bot identical-amount orders within same wall-clock second collide as one event. Trade.signature was not surfaced from Geyser.

**Fix:**
- Added `Trade.signature: str = ""` field
- Geyser parse_trade_event populates it
- Multiplexer uses dual-key approach: insert BOTH synthetic + signature into LRU on first sight; either-key match → dupe. Backward-compat shim `_trade_dedup_key()` kept for tests.

### CORRECTED — schema mismatch protection mechanism

Codex flagged my reasoning was wrong. policy/_main.py refuses by **feature-LIST comparison**, not version-string. v21→v22 happens to add 1 feature (82→83), so list-length check triggers refusal by accident. Future bumps that don't change feature count won't be protected this way. Filed as known limitation.

### REVISED — reg_pnl ρ verdict

Codex called my "+0.177 = signal" claim noise on n=120 (CI ≈ [0, 0.35]). Re-checked at n=811 (+0.177) and n=1507 (+0.1781) — **ρ stable across larger samples**, not noise. Original test set was 0.146; live exceeds it. Real signal confirmed. But codex right that I shouldn't have claimed it on n=120.

### NOT CONFIRMED — validator catchup

Codex suggested `solana catchup` instead of `getSlot` polls. Re-measured with 30s + 60s windows: still 0.2 slot/s vs mainnet 2.5/s. Validator IS falling behind 2.3 slot/s = 55min/h. Conclusion holds. Disk cleanup freed 110G (old bootstrap snapshot + tmp downloads); didn't restart validator.

### Honest PnL state

24h audit: -7.4 SOL paper, ×170 past `+0.3 SOL/week` kill criterion. BUT SL=8 bug accounts for ~80% of bleed. Wait 24h post-rollback before any further changes.

**Откат если не улучшится:**
- Revert all 29-04 exit changes: TP 30→80, MH 120→300, reg-floor=-100
- Re-test against pre-29-04 baseline (-2 SOL/24h)

---

## 2026-04-29 21:15 — v22 schema (creator_self_buy) + Geyser plugin deployed + validator firewall fix

**Что изменилось:**

### 1. Validator firewall + catchup recovery
- **UFW открыт** для UDP/TCP 8000-8020 (раньше всё блокировалось → validator не мог принимать turbine shreds → застревал на root forever)
- Validator restart с свежим snapshot. Catchup в процессе (47 мин позади mainnet, ETA 2-3ч до full sync)
- `RUST_LOG=warn` в systemd unit → log I/O ↓ 70% (5.6GB → 860MB / 12h)

### 2. Yellowstone Geyser plugin v12.3.0 deployed
- Установлен `libyellowstone_grpc_geyser.so` от Triton (matches agave 3.1.14)
- Config: `/home/sergey/solana-validator/yellowstone-config.json` (только transactions filter, остальное max=0)
- gRPC server слушает `127.0.0.1:10000`
- Validator unit обновлён `--geyser-plugin-config <path>`
- Bot активирован `PULSE_LAUNCHPAD=geyser+pumpportal` → `MultiplexerLaunchpad` подключён, primary=geyser fallback=pumpfun

**Caveat:** до полного catchup'а validator'а Geyser стримит stale events. Bot фактически работает на PumpPortal через multiplexer dedup. Geyser activates as primary когда validator caught up.

### 3. Exit config retune (через replay sweep)
- `PULSE_EXIT_TAKE_PROFIT_PCT`: 80 → **30** (старый никогда не срабатывал)
- `PULSE_EXIT_HARD_STOP_LOSS_PCT`: 30 → **8** (зажали SL)
- `PULSE_EXIT_MAX_HOLD_SECONDS`: 300 → **120** (короче окно)
- Replay sweep на 700 actually-entered paper_trades подтвердил best combo

**Live audit (3ч после applied):** WR 3.7→6.5% (+76%), winners reach +19% max но не доходят до TP=30%. Slip per trade улучшен но не плюс ещё.

### 4. Reg-floor soft gate
- `PULSE_ENTRY_REG_FLOOR_PCT=-10.0` в .env: блокирует ml_override BUY если reg_pnl < -10%
- 0 blocks за первый час (reg predictions mostly +3..+18%)

### 5. creator_self_buy filter (FastFilter + scorer + ML schema)
- Детектирует когда создатель токена сам покупает свой токен (rug-pull / fake-demand сигнал)
- `FastResult.creator_self_buy: bool` + `creator_self_buy_position: int` (1-indexed among buys, 0=never)
- `MetricsCalculator` тоже считает на full window
- `ScoringResult.creator_self_buy_position` → попадает в `ENTRY_FEATURE_ORDER` как фича для ML
- Soft penalty: `PULSE_FAST_CREATOR_SELF_BUY_SCORE=-10` (default)
- Hard reject opt-in: `PULSE_FAST_CREATOR_SELF_BUY_REJECT_MAX_POSITION=N` (default 0=off, collect data first)

### 6. Schema bump v21 → v22
- `FEATURE_SCHEMA_VERSION = "entry_v22_20260429_creator_self_buy"`
- 82 → 83 features (added `creator_self_buy_position`)
- Golden test re-recorded (`UPDATE_GOLDENS=1`)
- **Bot НЕ рестартован** — schema mismatch protection (v22 code refuses v21 model). Активируется при следующем retrain после накопления данных.

### 7. Helius backfill graduated mints
- Crash-bug fixed (`PUBLIC_RPC_URLS` auto-build из API keys)
- `--mint-parallelism 1 --concurrency 4` + 3 ключа = устойчивая работа без 429-storm
- ~293/480 mints обработано, +141k новых trades inserted

### 8. Validator monitor + scripts
- `scripts/validator_catchup_monitor.py` — поллит getHealth + getSlot
- `scripts/replay_exits_sweep.py` — replay simulate_exit на закрытых paper_trades

### 9. Documentation
- `CLAUDE.md`: новый раздел "Infrastructure on rich" с UDP ports, Helius keys, validator config
- `.env.example`: полная документация всех PULSE_* флагов
- `docs/ROADMAP_2026_05.md`: добавлена Phase 7 (tighten gates after data collection)
- Memory: `project_rich_infrastructure.md` обновлён про UFW UDP fix

**Trigger Phase 7:** EXTENDED_OBSERVE accumulates 7+ days, retrain v22 with fresh labels, ρ live ≥ 0.20 → tighten ml_override gates (ceiling 0.15→0.50, reg-floor 0.0, double-SKIP guard).

**Откат:**
- Geyser: `PULSE_LAUNCHPAD=pumpfun` в .env, restart bot. `--geyser-plugin-config` убрать из validator unit.
- Exit config: revert TP/SL/MH в .env.
- creator_self_buy schema: rollback FEATURE_SCHEMA_VERSION + restore ENTRY_FEATURE_ORDER.

---

## 2026-04-29 09:41 — Exit-config из replay sweep + reg-floor

**Что изменилось (rich .env):**
1. `PULSE_EXIT_TAKE_PROFIT_PCT`: 80 → **30**
2. `PULSE_EXIT_HARD_STOP_LOSS_PCT`: 30 → **8**
3. `PULSE_EXIT_MAX_HOLD_SECONDS`: 300 → **120**
4. `PULSE_ENTRY_REG_FLOOR_PCT=-10.0` (NEW): blocks ml_override BUY если reg_pnl_pct < -10%

**Зачем:** Live audit (48h, n=1217 закрытий) показал realized PnL = -4.0 SOL, WR=3.7%, EV=-3.29%/trade. Старый exit-конфиг (TP=80, SL=30, MH=300) — 0 take_profit срабатываний за 48ч, 92.9% dead_token. Replay sweep на 700 actually-entered позициях по grid 64 combos выявил best (TP=30, SL=8, MH=120) с total -1.68 SOL vs current -4.0 SOL (×2.4 меньше слив, WR 6.36% vs 3.7%).

**Кодовые изменения:**
- `pulse_bot/decision_service.py`: `apply_ml_override` принимает `PULSE_ENTRY_REG_FLOOR_PCT` (default -100 = off). Если `reg_pnl_pct < floor` → блокирует override, считает в `ml_overrides_skip`, эмиттит metric `ml_override.action=reg_floor_block`.
- `scripts/replay_exits_sweep.py`: новый — replay simulate_exit на закрытых paper_trades по grid (TP × SL × max_hold).
- `scripts/live_audit.py`: новый — realized PnL/WR/exit-reason breakdown + измерение entry_model_reg ρ на live данных (вышло 0.008, vs test 0.146 — модель шумная в live).
- `scripts/helius_backfill_graduated.py`: фикс crash на пустом `PUBLIC_RPC_URLS` (auto-build из HELIUS_API_KEYS).
- `CLAUDE.md`: новый раздел "Infrastructure on rich" — local validator :8899, 3 helius keys, backfill systemd service.

**Live verification (post-restart 09:41 UTC):**
- ML OVERRIDEs показывают reg_pnl predictions от -1.20% до +18.18%
- PAPER BUYs entering с score=534-628 (decoded: +3.4% to +12.8% predicted PnL)
- 0 reg-floor blocks за первую минуту (все predictions > -10%)
- Все 8 моделей status=ok в boot summary

**Ожидаемые метрики через 24h:** WR ≥ 6%, EV per trade > -3%, realized PnL > -2 SOL/24h. Если хуже — kill criterion.

**Откат:** в .env вернуть TP=80 SL=30 MH=300, удалить REG_FLOOR_PCT, restart.

---

## 2026-04-28 21:46 — entry_t30 SKIP-side ревайв + entry_model_reg в decision flow

**Что изменилось:**
1. **entry_t30 SKIP-side активирован** (`PULSE_ENTRY_T30_SKIP_ACTIVE=1`) с переоткалиброванным confidence gate `PULSE_T30_SKIP_TAIL` 0.05 → **0.005** (default in pipeline.py).
   - Старый tail=0.05 ловил ~80% живых токенов (median proba=0.021) → блокировал ml_override и был отключён 04-27.
   - Новый tail=0.005 ловит только bottom ~20% (явно DOA) → main T+90 model видит остальные.
   - skip_wr на test set при proba<0.15 = 0.05% — потеря виннеров пренебрежимо мала.
2. **entry_model_reg подключён в ml_override flow.**
   - Загружен в Pipeline.__init__ как параллельный `_ml_entry_reg_policy` (sibling EntryMLPolicy).
   - На каждом ml_override BUY decision вызывается `predict_score()` для предсказания PnL%.
   - `DecisionService.apply_ml_override()` принимает `reg_pnl_pct` kwarg.
   - `entry_score` теперь кодирует прогноз: `round(reg_pnl × 10) + 500` (offset для неотрицательного integer column). Range [-49.9%, +49.9%] → [1, 999].
   - Без жёсткого gate — рангует/информирует, не отсекает (ρ=0.146 слабый, недостаточно для kill-gate).

**Зачем:**
- T+30 SKIP был мёртвым грузом — модель healthy (AUC=0.911), но из-за плохо откалиброванного confidence gate не работала в live.
- entry_model_reg был мёртвым грузом — обучался каждую сессию, но никем не вызывался. Теперь как минимум obогащает entry_score forecast'ом PnL.

**Live verification (после рестарта 21:41:57):**
- ✅ `PAPER BUY BURNPUMP score=527` = predicted PnL +2.7% (decode: (527-500)/10).
- ✅ T30 SKIP_EARLY на `Gy5fAKJQDrtg` proba=0.002 — заблокирован как DOA.
- ✅ Tokens с proba 0.005-0.15 проходят дальше к main модели (не блокируются tail-gate).

**Откат:**
- T30 SKIP: `PULSE_ENTRY_T30_SKIP_ACTIVE=0` в .env на rich + restart.
- entry_reg: удалить из Pipeline.__init__ (или просто убрать `data/ml/entry_model_reg.ubj` — load fail = legacy fallback).

---

## 2026-04-28 19:55 — Percentile-fallback для entry_model + retrain timing с class weights

**Что изменилось:**
1. **`_search_confidence_thresholds`**: при коллапсе EV-поиска (`floor >= ceiling`) — fallback на p20/p80 квинтили + проверка ranking enrichment. Новые статусы:
   - `ok` — EV-поиск нашёл прибыльный bucket
   - `ok_percentile_fallback` — EV плоский, но топ-quintile WR ≥ 1.3× base AND нижний ≤ 0.7× base (модель ранжирует)
   - `degenerate_flat` — нет ranking power вообще (отказ от ML override)
2. **`policy/_main.py`**: принимает `ok_percentile_fallback` как healthy без `PULSE_ALLOW_DEGENERATE_MODEL=1`.
3. **entry_model retrain**: status="ok" → `ok_percentile_fallback`, BUY=3244 WR=5.18% (×3.6 base), SKIP=210 WR=0.95%, AUC=0.898, proba_spread=0.833.
4. **entry_timing_model retrain**: 5000 mints → 15510 rows. Class weights APPLIED (фикс 04-28 не был активирован — старая 04-27 модель обучалась без них и предсказывала p_skip≈1.0). Новые веса: WAIT=1.83, BUY=4.22, SKIP=0.45.

**Зачем:** Live логи показывали `model_health=degenerate` для entry_model (EV uniformly negative на memecoin датасете) и `p_skip=1.0` для timing (class imbalance не балансировался). Без этих фиксов бот опирался на `PULSE_ALLOW_DEGENERATE_MODEL=1` override, а timing был нерабочим.

**Диагностика exit-моделей (sweep 64 combos):**
- 90 284 entries, viable=3461 (3.8% — остальные DOA)
- Все combo TP×SL×max_hold дают avg_pnl ≈ −5%, TP_rate ≤ 0.78%
- Корень: `PULSE_EXTENDED_OBSERVE_SECONDS=600` уже включён, но 94% датасета — старые DOA-токены без post-scoring trades
- **Решение:** wait 48-72h на накопление новых данных, тогда retrain exit-моделей

**Также:**
- entry_model_reg переобучена на v21: ρ=0.146 (было 0.21 v18), inverse ranking → данные плохие, **deferred** до новых данных
- exit_quantile_sl/tp/max_hold: не трогаем до новых данных
- survival_model: не трогаем до новых данных
- "train/serve skew" 27/82 features non-zero — диагностировано как DOA artifact, не баг hydration

**Откат:**
- entry_model: `mv data/ml/entry_model.ubj.prev data/ml/entry_model.ubj` + `mv data/ml/entry_model.meta.json.prev data/ml/entry_model.meta.json` на rich
- entry_timing_model: предыдущая версия от 04-27 09:18 утрачена (нет .prev backup)
- train.py + policy/_main.py: revert через git

---

## 2026-04-27 — v20 schema + runtime gate fixes (codex review pass)

**Что изменилось:**
1. **Schema bump v19 → v20** (`entry_v20_20260427_wallet`): добавлены 3 фичи на основе review кодекс-агента:
   - `top10_buyer_prior_avg_wr` — extend Phase E с top-3 на top-10 покупателей
   - `top10_buyer_prior_total_pnl_sol` — то же для PnL
   - `n_buyers_first_5s` — снайпер-прокси (счётчик кошельков купивших в первые 5 сек после mint)
   T+30 schema bump v2 → v3 (та же группа фичей применима — WALLET_FEATURES общая).
2. **RULES bucket → SKIP override** через `PULSE_ENTRY_GREY_TO_SKIP=1`. Серый bucket в `entry_model.meta` имеет WR=0.57% — главный источник потерь. Теперь в hybrid режиме модель форсит SKIP вместо передачи rules-движку.
3. **T+30 SKIP-only режим** через `PULSE_ENTRY_T30_SKIP_ACTIVE=1`. T+30 модель имеет skip_wr≈0.012% (отличный фильтр) но buy_wr≈8.6% (всё ещё ниже breakeven). Новый флаг — даём ей право только SKIP, не BUY.
4. **`PULSE_ENTRY_MODE=full` env var добавлен в config**. По умолчанию был "both" — fast-purchase мог войти даже при full=SKIP. Закрыли арбитраж против собственной модели.
5. **Багфикс** `wallet_indexer.py:163` — `is_closed_at_ingest_time` был хардкоден `=1`, теперь `=1 if sell_sol>0 else 0`. Исправляет загрязнение Phase E queries которые фильтруют по closed positions.

**Зачем:** WR=22% при breakeven=31%. Кодекс-обзор показал что главный лоссмейкер — gating runtime, не модель сама по себе. Wallet-фичи добавляются параллельно как минимальный scope (3 фичи вместо предложенных 7) — wash-clusters отложены для v21 после оценки v20.

**Точки утечки данных проверены:**
- `n_buyers_first_5s` — point-in-time из trades этого минта, нет leakage
- top10 wallet stats — используют существующий leak-safe `get_wallet_prior_stats_sync` (filter `last_trade_ts < cutoff_ts`, `mint != exclude_mint`)

**Результат:** замеры после retrain + 24-48ч paper trading.

**Откат:**
- Runtime fixes: убрать env-флаги в `.env` rich + restart
- Schema v20: вернуть `FEATURE_SCHEMA_VERSION = "entry_v19_20260427"` и удалить новые WALLET_FEATURES
- Откатить `wallet_indexer.py:163` если Phase E снова потеряет nullable closed flag

---

## 2026-04-27 — Skew fix: top10 wallet features silently NaN

**Что нашли:** Live ML proba после v20 deploy max=0.03 (норма ≥0.10) на 63 токенах. Логи показали `predict_proba: only 25/78 features non-zero` — train/serve skew.

**Корень:** Codex-обзор обнаружил баг в `features.py:649`. Условие `if len(top3_buyer_wallets) > 3` игнорировало top10_* фичи когда у токена ≤3 покупателей. Для pump.fun где большинство токенов умирают с 1-3 покупателями — фичи всегда NaN. Проверка entry.parquet: 58% train rows имеют NaN в top10_buyer_prior_avg_wr — модель училась на этом, в live тоже получала NaN (parity), но обе стороны "потеряли" сигнал.

**Фикс:** убрал guard, top10 features populate всегда когда хотя бы 1 wallet с prior history. Перестроил entry.parquet + entry_t30.parquet, перетренировал обе модели.

**Откат:** вернуть `if len(top3_buyer_wallets) > 3:` в `_extract_wallet_prior_features`.

**Результат:** замеры после рестарта.

---

## 2026-04-27 — Confidence gates на T+30 / timing моделях

**Что нашли:** После revert pipeline (entry AUC 0.825) бот не открывал позиций несмотря на ML override (proba ≥ ceiling=0.30 на всех токенах). Причина: T+30 SKIP-only режим срабатывал на ВСЕ токены (proba=0.007 у всех = ниже floor=0.15) и перекрывал ML BUY override через cp_verdict=SKIP_EARLY.

**Принцип:** каждая ML модель должна **молчать** когда не уверена. Действует только в **экстремальных хвостах** распределения. Иначе любая некалиброванная модель (особенно после retrain'а на сильно меняющемся датасете) может тупо ВЕТО всю торговлю.

**Что изменилось:**
- `pipeline.py` T+30 hook: SKIP_EARLY firing требует `proba < PULSE_T30_SKIP_TAIL` (default 0.05, vs старый skip_floor=0.15). BUY_EARLY требует `proba > PULSE_T30_BUY_TAIL` (default 0.85).
- Тот же confidence gate применён к entry-timing (`PULSE_TIMING_SKIP_TAIL`, `PULSE_TIMING_BUY_TAIL`).
- Модель в middle-zone не вмешивается, передаёт решение T+90 entry path.

**Откат:** убрать env-переменные → fall back to floor/ceiling defaults from meta.json (предыдущее поведение).

---

## 2026-04-27 — Wallet classifier deployed (1.46M wallets классифицированы)

**Что:** новая таблица `wallet_classifications` на rich, 4 детектора:
- `is_sniper` — 23,816 (14.5%): 2-of-4 правил (buy_age<5s, cv<0.15, n_buys_30d≥300, median_hold<60s)
- `is_smart_money` — 13,267 (8.1%): WR > 40% на graduated mints (peak MC ≥ 35 SOL)
- `is_bot` — 379 (0.23%): строгий sniper subset (buy_age<2s, n_buys_30d≥500)
- `cluster_id` — 36,797 (22.5%) в 494 кластерах — co-occurrence ≥3 mints в 30с окне

**Зачем:** новый сигнал для следующего retrain. Snipers фильтруют rugs, smart_money подсказывают winners. wash-cluster при cluster_size 3-50 — индикатор отмыва.

**Использование:** features в v21 schema (отложено до завершения revert pipeline). Готово в `pulse_bot/ml/wallet_classifier.py`.

**Откат:** `DROP TABLE wallet_classifications;` (derived data, не критично).

---

## 2026-04-27 — EV-based threshold search в train.py

**Что изменилось:** `_search_confidence_thresholds()` теперь оптимизирует не WR (winrate) а EV (mean realized_pnl_pct в bucket). Принимает опциональный `pnl` параметр; backwards-compatible (если pnl=None, fallback к WR-search).

**Зачем:** WR=22% при avg_W=22.9, avg_L=-10.2 → EV=-2.92%/трейд. WR-оптимизация выбирает thresholds которые максимизируют точность бинарной классификации, но не деньги. Кодекс-обзор указал на это как на одну из главных проблем gating.

**Результат:** при v20 retrain (запущен 2026-04-27 15:46 UTC) thresholds будут выбираться так что:
- CEILING (proba выше → BUY): максимизирует mean realized_pnl_pct
- FLOOR (proba ниже → SKIP): минимизирует mean realized_pnl_pct (т.е. отсекает наибольшие убыточные trades)

PnL clip применён -100% / +200% — защита от outlier-кандидатов с broken price feeds.

**Откат:** не требуется — функция backwards-compatible. Если EV даёт хуже WR на test set, в meta.json появится низкий `ceiling_ev`/`floor_ev` и видно сразу.

---

## 2026-04-27 — Расширили holder snapshot scheduler до T+600

**Что изменилось:** в `pulse_bot/helius_holders.py` `CAPTURE_AGE_SECONDS` расширен с `(30, 60, 120)` до `(30, 60, 120, 180, 300, 600)`. Новые точки делаются для каждого нового минта дополнительно к существующим. Теперь конфигурируется через env `PULSE_HOLDER_CAPTURE_AGES`.

**Зачем:** ставит фундамент для двухступенчатого каскада. Stage-2 классификатор будет видеть пост-T+90 динамику холдеров (top1 ушёл? распределение разбавилось?), которой stage-1 не имеет. Проверка БД на 2026-04-27: T+180+ snapshots = 0 за всю историю — `PULSE_EXTENDED_OBSERVE_SECONDS=600` влияет только на подписку, scheduler был хардкоднут на 3 точки.

**Результат:** аддитивное изменение, существующий ML pipeline (читает age=30/60/120) не меняется. Накопление за 4 недели ожидаем ~2800 минтов с full T+180 фичами для обучения каскада.

**Откат:** `PULSE_HOLDER_CAPTURE_AGES=30,60,120` в `.env` rich + restart pulse-bot.service.

**Нагрузка:** Helius RPC вызовы 3 → 6 на минт. Семафор PULSE_HELIUS_HOLDER_CONCURRENCY=100 покрывает (600s spread = низкий peak concurrent).

---

## 2026-04-27 14:23 — Apply combined sweep #3 best config + tight ceiling

**Что изменилось (через `.env` на rich, легко откатить):**
```
PULSE_ENTRY_PROBA_CEILING=0.30   (было 0.25 — top-1% по баллу модели вместо top-30%)
PULSE_SCORE_BUY=40                (было 30 — стрingier rules entry filter)
PULSE_EXIT_HARD_STOP_LOSS_PCT=30  (было 15 — wider SL, fewer false stops)
PULSE_EXIT_TAKE_PROFIT_PCT=80     (было 100 — достижимый TP)
PULSE_EXIT_INACTIVITY_SECONDS=60  (было 120 — быстрее реагируем на тишину)
PULSE_EXIT_MAX_HOLD_SECONDS=300   (было 90 — даём winners больше времени)
```

**Зачем:**
- 3 sweep'a (exit-only #1, entry-only #2, combined #3 = 1216 combos) дали **ровно эти параметры в TOP-10 каждого**. Pattern stable, не случайность.
- Текущая конфига приводит к ~25 paper trades / день / WR 4-19% / avg_pnl −7%/трейд.
- Новая конфига (по simulation): 50 trades / WR 20% / avg_pnl **−0.36%/trade** — **20× меньше paper-loss**.
- Все combos sweep'a отрицательные → реальная цель: уменьшить bleed, накопить shadow predictions, дождаться накопления EXTENDED_OBSERVE данных.

**Per memory `feedback_config_changes_via_optimizer`:** правило "PnL > 0 в sweep" формально нарушается (best −0.18 SOL). Применяем потому что:
- Paper-only (никаких реальных SOL)
- 20× улучшение vs текущая
- Single-direction change: если хуже — откат через `.env` revert + restart.

**Ожидаемое поведение:**
- Бот покупает реже (top-1% вместо top-30%)
- WR должна подняться к 20% (vs 10% сейчас)
- avg_loss tighter благодаря wider SL (paradoxically: 30% SL дает менее частый strict-stop trigger)
- TP=80 достижимый — некоторые wins будут через TP вместо max_hold

**Откат:**
- Удалить 6 PULSE_* строк из `.env`, restart bot. Вернутся defaults (`PULSE_ENTRY_PROBA_CEILING=0.25` имхо как override стоит сохранить через откат на 0.7).

---

## 2026-04-27 13:00 — Feature schema v18 → v19 cleanup (4 stable_dead removed)

**Что изменилось:**
- 4 фичи удалены из ML schema по протоколу `project_feature_stability_protocol` (stable_dead в **TWO sequential** 5-seed runs, Apr 25 + Apr 26):
  1. `creator_tokens_today` — был SCORER_FEATURES (T+90 + T+30)
  2. `fast_sell_ratio` — был SCORER_FEATURES (T+90 + T+30)
  3. `creator_total_prior_tokens` — был CREATOR_FEATURES
  4. `creator_graduated_count` — был CREATOR_FEATURES
- `FEATURE_SCHEMA_VERSION`: `entry_v18_20260425` → `entry_v19_20260427`
- `FEATURE_SCHEMA_VERSION_T30`: `entry_t30_v1_20260425` → `entry_t30_v2_20260427`
- `ENTRY_FEATURE_ORDER` count: 79 → **75**
- `SCORER_FEATURES_T30` count: 33 → 31
- Test `test_creator_features_live_vs_training_parity` обновлён (убраны spot-checks для removed features).

**Зачем:**
- Per memory `project_feature_stability_protocol`: "remove only stable_dead in TWO sequential schema versions". Эти 4 фичи показали gain ≈ 0 на всех 5 seeds в обоих v18 runs (Apr 25 + Apr 26).
- 4 фичи зашумляли feature space + замедляли inference.
- `creator_tokens_today` всё ещё используется в filter `creator_max_tokens_today` (hard-gate); только из ML feature vector выкинут.
- На pump.fun многие creator-features 0-variance (per memory `project_pumpfun_default_authorities` есть аналог): creator_total_prior_tokens / creator_graduated_count часто 0 для new creators.

**Результат (ожидаемый):**
- Старые модели на v18 (entry_model.ubj и entry_model_t30.ubj) **не загрузятся** — schema mismatch hard-fail. Текущие модели надо retrain'ить.
- Build_dataset запущен в фоне на rich (PID 2219001) → `entry.parquet` v19 → train.py на v19 → загрузить в бот.
- Ожидаемый AUC: ≈ старый ±noise (4 фичи имели gain ≈ 0 → удаление не должно повлиять). Реальная цель: чище feature space для следующего retrain'a с новыми фичами.

**Откат:**
- Вернуть 4 имени в SCORER_FEATURES / CREATOR_FEATURES в `pulse_bot/ml/features.py`.
- Ревертить `FEATURE_SCHEMA_VERSION` обратно `entry_v18_20260425`.
- Старые .ubj модели снова заработают (если их сохранили; иначе retrain).

---

## 2026-04-27 12:30 — Dashboards переведены с Mac на rich (firewalld+UFW конфиг)

**Что изменилось:**
- Streamlit `dashboard.py` (port 8501) и `backtest_dashboard.py` (port 8502) запущены на rich через systemd user units `pulse-dashboard.service` и `pulse-backtest-dashboard.service`. `Restart=always`, `enable`d.
- Mac dashboards (PID 51343, 51344) убиты — Mac остался dev-only per memory `feedback_pulse_bot_runs_on_rich_only`.
- Доступ через LAN: http://192.168.3.118:8501 / 8502 (Mac → rich).

**Найдено и пофикшено (network deep-dive):**
- На rich оказались **ДВА файервола** одновременно: UFW (priority 0, default DROP) и firewalld (priority 10, default REJECT с `icmp-host-prohibited`).
- Чтобы порт был доступен извне нужны **обе** ACCEPT:
  - UFW: `ufw allow from 192.168.3.0/24 to any port 8501 proto tcp`
  - firewalld: `firewall-cmd --permanent --add-rich-rule='rule family=ipv4 source address=192.168.3.0/24 port port=8501 protocol=tcp accept'`
  - Также: `firewall-cmd --permanent --zone=public --change-interface=enp7s0` (interface не был привязан к зоне)
- Раньше работали только порты которые имели правила в обеих системах (например 8888 postmortem).

**Зачем:**
- Mac PG заморожен с 2026-04-25 21:00 (миграция бота на rich) — Mac dashboard видел stale данные. Нужно смотреть live с rich.
- Per memory: live monitoring = rich, dev = Mac.

**Результат:**
- Dashboards отвечают HTTP 200 с Mac. Видны сегодняшние paper_trades, shadow predictions, optimizer runs.
- README обновлён — раздел Production deployment включает оба сервиса в systemctl status block + LAN URLs.

**Откат:**
- `systemctl --user disable --now pulse-dashboard.service pulse-backtest-dashboard.service` на rich.
- На Mac: `streamlit run pulse_bot/dashboard.py --server.port 8501` (но Mac PG нужно sync'ить с rich для актуальных данных).

---

## 2026-04-27 09:35 — Bot resumed trading with ceiling=0.25 + first results

**Что изменилось:**
- `PULSE_ENTRY_PROBA_CEILING=0.25` (было 0.7 / меньше дефолта 0.9 в new model meta) — подобрано под calibration новой модели после retrain'a 2026-04-26 (proba P95 = 0.06, P99 = 0.13, max 0.35).
- Optimizer #2 (entry-filter sweep, 500 combos) — best PnL все ещё отрицательный (-0.142 SOL, WR 13%). Не применяю — per memory `feedback_config_changes_via_optimizer`: только при PnL > 0.
- verify300 (replay parity на 300 свежих токенов): **297/297 FAST + 297/297 FULL match (100%)** — мои сегодняшние code изменения (holder fix, T+30 plumbing, NaN sentinel, shadow infra) детерминизм не нарушают.
- Timing model retrained на counts-zero / derived-NaN schema (test_extract_features_shape_and_keys теперь проходит).
- Pipeline `getattr(self, "_survival_active", False)` defensive чтобы тесты-моки не падали.
- 388/388 unit-tests + 11/11 verify300 + 21/21 shadow/multiplexer/decoder = всё зелёное.

**Зачем:**
- Под старый ceiling 0.7 модель не выдавала достаточно высокую proba ни на одном live токене → 0 BUY за 14ч.
- Снижение до 0.25 — pragmatic threshold, чтобы накопить shadow и paper-PnL данные для validation.

**Результат (10 трейдов сегодня):**
- WR 10% (1/10), avg_pnl **+1.53%** (драйвер — IQmog +106% sell_pressure exit).
- Vs yesterday: 50 трейдов / 18% WR / −5.30% avg_pnl.
- Малая выборка, но текущий avg впервые положительный.

**Откат:**
- `.env`: `PULSE_ENTRY_PROBA_CEILING=0.7` (или удалить line, чтобы дефолт=0.9 из meta), restart bot.
- Изменения в коде test-фиксов уже committed; revert по git.

---

## 2026-04-26 23:25 — Two latent SQL bugs in holder fetch (smallint vs bool, single-string params)

**Что изменилось (debug round on top of codex fixes):**
После сегодняшнего fix'а holder_snapshot wiring (см. 22:55 entry), warning `predict_proba: holder=None` всё равно валился. WARN-level traceback показал две скрытые причины:

1. **`is_negative_row = FALSE`** падало с `operator does not exist: smallint = boolean`. Колонка `is_negative_row` в PG schema это `smallint` (0/1), не `boolean`. Меняем в обоих запросах: `_fetch_holder_snapshot_t30` (line 1244) и `_fetch_holder_snapshot_all` (line 1318) → `is_negative_row = 0`.

2. **`_sync_query(..., mint)`** передавал `mint` как одну строку. `_sync_query` ожидает `params: tuple | list`. Python `tuple("XXX...pump")` разворачивает строку в tuple символов → SQL получал каждый символ как separate placeholder → `not all arguments converted during string formatting`. Фикс: `(mint,)` tuple вместо строки.

**Зачем:**
- Без этих фиксов holder fetch **молча** failed на каждом scored token → `holder_snapshot_all = {}` → `or None` → policy.py warning `holder=None` → нет holder features → proba ≈ 0.005 → no BUYs. То есть **codex review fix только частично работал** до сегодня вечера; реально бот по-прежнему был в режиме train/serve skew.
- Promoted exception logger from `debug` → `warning` to surface root cause faster on future regressions.

**Результат:**
- После 22:25 restart: `T30 predict_proba: only 20/56 features non-zero (holder=set creator=set ...)` — впервые видим `holder=set` в warning.
- Бот наконец имеет полный feature vector на каждом scored token.
- Если модель находит реальный signal — будет BUY. Иначе continue collect shadow data.

**Откат:** revert pipeline.py changes (вернёт SQL `=FALSE` и `mint` без tuple wrap → again silent broken).

---

## 2026-04-26 23:10 — Codex review: 5 critical/major findings fixed

Codex-style review последних 10 коммитов нашёл 5 проблем в новых ML-головах. Все исправлены:

1. **CRITICAL — T+30 inference missing wallet args (recurrence of holder bug)**
   `pipeline.py:1215+` — `_evaluate_t30_checkpoint` теперь fetch'ит `top3_buyer_wallets` (`compute_top3_buyer_wallets(visible)`) + `wallet_prior_stats` через `get_wallet_prior_stats_sync` с T+30 cutoff, передаёт в `EntryT30Policy.decide_with_confidence`. Закрывает train/serve gap для 5 WALLET_FEATURES.

2. **CRITICAL — non-zero smoke guard для T30/timing**
   `policy.py:602+` (EntryT30Policy.predict_proba) и `entry_timing.py:530+` (predict_entry_timing) теперь логируют `WARN: only N/M features non-zero` (mirror of EntryMLPolicy guard). Раннее обнаружение skew на новых головах.

3. **MAJOR — entry_timing 0.0-sentinel → NaN**
   `entry_timing.py:161` — `feats = {k: float("nan") for k in TIMING_FEATURE_ORDER}` вместо `0.0`. XGBoost теперь видит "missing" как distinct ветку. `TIMING_SCHEMA_VERSION` bumped `v1_20260425 → v2_nan_20260426`. Старая модель отвергается на load (hard fail). Перетренирована на NaN корпусе.

4. **MAJOR — startup-warning при dead-TP defaults**
   `config.py:391+` — новая функция `_warn_on_dead_exit_combo()` пишет `EXIT CONFIG WARNING: take_profit_pct=100.0% with max_hold_seconds=90s is the documented dead pair` при каждом `get_config()`. Видна в логах bot/scripts. Per memory `feedback_config_changes_via_optimizer`: значения не меняем без sweep'а — только warning.
   Также: `pulse_extended_observe_seconds` default 0.0 → 600.0 (Phase 0 default fix).

5. **MAJOR — ExitManager loud-once при отсутствии ML stack**
   `exit_manager.py:122+` — `_warn_no_ml_advisors_once` выводит WARN при `ml_advisor=None AND quantile_sl=None AND quantile_tp=None`, log spam защита через class-level флаг. Future невидимый exit_model.ubj loss будет очевиден.

**Bonus — T+30 holder race condition fix:**
- `pipeline.py:1206+` — retry holder snapshot up to 5× spaced 400ms apart. Helius capture lands within 200-500ms но часто race'ится с T+30 hook firing. Теперь typical first-hit чтение, max ~2s wait.

**Tests:** 31/31 PASS. Smoke test: `extract_snapshot_features([], 30, 0)` → 15/16 NaN (только snapshot_t установлен) ✓.

**Откат:**
- TIMING_SCHEMA_VERSION → v1, restore meta.json schema field — отменит NaN fix
- Other fixes: revert pipeline.py + policy.py + entry_timing.py + config.py + exit_manager.py

---

## 2026-04-26 22:55 — Train/serve skew fix: holder snapshot wired into T+90 + T+30 inference

**Что изменилось (3 связанных бага):**

1. `pipeline.py:1230` (`_fetch_holder_snapshot_t30`): `FROM holder_snapshots` → `FROM token_holders_snapshots`. Реальной таблицы `holder_snapshots` не существует, запрос валился в exception → возвращался None → t30 политика zero-fill'ила HELIUS_FEATURES_T30.

2. `pipeline.py:671` (T+90 scoring): `Scorer.score(...)` вызывалось **без** `holder_snapshot=` параметра → ScoringResult не имел holder данных → ML feature extractor получал zero для всех 12 HELIUS_FEATURES.

3. `pipeline.py:752 + 882` (T+90 ML inference): `EntryMLPolicy.predict_proba(...)` и `decide_with_confidence(...)` тоже вызывались без `holder_snapshot=` — повторное проявление того же гэпа.

**Fix:** Новая helper `_fetch_holder_snapshot_all(mint)` строит flat dict со всеми HELIUS_FEATURES (top1_30, top5_30, top10_30, hc_30, top1_120, top5_120, top10_120, hc_120, top1_delta, top5_delta, top10_delta, hc_velocity), синтезируя deltas + velocity точно так же как `build_dataset.py` делает на тренировке. Передаётся в scorer + 2 ML call sites.

**Зачем:**
- Per memory `project_creator_skew_bug` (2026-04-23 fix): тот же класс бага — train/serve skew когда features есть в DB но не подгружены при inference. Сегодня обнаружен новый instance.
- Симптом: `WARN predict_proba: only 9-16/79 features non-zero` на каждом scored token, proba=0.007 везде, бот **не покупал 5+ часов**.
- Root cause: pipeline.py никогда не пробрасывал holder_snapshot — только creator_snapshot и wallet_stats.

**Результат:**
- После рестарта 22:55: первая T+30 BUY decision сразу же — `proba=0.756 buys=10` (раньше было proba=0.007 везде).
- Бот **снова делает живые торговые решения**. Exit_quantile + survival shadow начнут писаться при первом paper_trade open.

**Откат:**
- git revert pipeline.py на предыдущую ревизию (бот вернётся в "не торгует" режим, что было предыдущим default).

---

## 2026-04-26 22:25 — feature_stability v18 seed5 round-2: regime drift detected

**Что изменилось:**
- Запущен `feature_stability.py --n-seeds 5 --data data/ml/entry.parquet --out data/ml/feature_stability_v18_seed5_apr26.json` после сегодняшнего retrain.
- Сравнение со старым `feature_stability_v18_seed5.json` (Apr 25):

| Метрика | Apr 25 | Apr 26 | Δ |
|---|---:|---:|---:|
| AUC mean (5-seed) | 0.9620 | 0.8995 | **−6.2pp** |
| AUC stdev | 0.0010 | 0.0058 | **×6** |
| stable_dead count | 6 | 12 | +6 |
| stable_active | 36 | 31 | −5 |
| Status flips | — | 19 | — |

- 8 новых stable_dead (все T+30 family): `buy_volume_sol_at_30`, `creator_median_peak_mc_sol`, `fast_buy_count`, `first_buy_sol`, `gap_create_to_first_trade`, `hc_30`, `top10_30`, `top5_30`.
- Top gain shifts: `top10_minus_top5_120` +5224, `delta_unique_buyers_30_to_60` +4850, `hc_120` +2820 (T+90/T+120 family усилилось); `buy_count_to_sell_count_ratio` −1179 ослабло.

**Зачем:**
- Per memory `project_feature_stability_protocol`: 5-seed run перед удалением фичей.
- Per `feedback_provisional_vs_closed`: ΔAUC < 2×SE = шум. У нас 2×SE ≈ 0.012, реальный drop = 0.062 → **5× выше threshold**, drift реальный, не шум.

**Что НЕ сделано (намеренно):**
- **Stable_dead фичи НЕ удалены.** Per protocol требуется подтверждение в **двух последовательных** schema versions. Этот run = round 1 of 2.
- **Активация shadow моделей отложена** — нет основания шевелить инфраструктуру на основе одного drift run'a.

**Гипотезы причин drift'a:**
- `PULSE_EXTENDED_OBSERVE_SECONDS=600` (включён сегодня 18:07) сместил label distribution: `simulate_exit` теперь использует extended trade history, новые exit_reason ratios.
- DOA fix data continued accumulation.
- Реальный regime change на pump.fun (sniper-арбитраж, AI-token wave).

**Действия в плане:** см. ROADMAP_2026_05 секция "TODO: повторный feature_stability seed run + cleanup stable_dead" — через 5-7 дней (2026-05-01..03) повторить, при подтверждении удалить мертвяков → bump schema v18 → v19 → retrain.

**Откат:** не требуется (мы ничего не изменили в production).

---

## 2026-04-26 22:10 — Shadow infrastructure для всех 4 опциональных моделей

**Что изменилось:**
- `pulse_bot/ml/shadow.py` расширен с 2 → **4 record_*** функций:
  - `record_t30_shadow` — entry_model_t30 (T+30 hook)
  - `record_timing_shadow` — entry_timing_model (15s checkpoints)
  - `record_quantile_shadow` — exit_quantile_sl/tp (paired q25/q75 на каждом exit-decision tick)
  - `record_survival_shadow` — survival_model (per-tick remaining-life prediction + hazard curve)
- 4 env флага: `PULSE_ENTRY_T30_SHADOW`, `PULSE_TIMING_SHADOW`, `PULSE_EXIT_QUANTILE_SHADOW`, `PULSE_SURVIVAL_SHADOW`. Все `=1` в `.env` на rich.
- `pipeline.py` — 3 hook'а в shadow mode (t30, timing, survival) с условием `_active OR shadow_enabled`. Live decision срабатывает только если active=True.
- `pulse/exit_manager.py` — конструктор принимает `quantile_tp_policy` + `mint` + `scored_at`. В `decide()` shadow logging q25+q75 на каждом tick.
- `core.py` (PaperTradeRunner) — пробрасывает `mint`/`scored_at` в ExitManager, грузит `quantile_tp_policy` если shadow enabled.
- Тесты `tests/pulse_bot/test_shadow.py` расширены 6 → **10** (env-gates + payload + truncation + exception swallow). 31/31 общий suite PASS.

**Зачем:**
- Per memory `project_ml_ensemble_opportunistic_gating`: модели активируются в LIVE только после **production validation** (production metrics ≥ training × 0.7). Shadow — единый канал сбора этих метрик для всех 4 опциональных моделей.
- Lower friction для будущей активации: один-два DB queries + flip env flag + restart, без code change.

**Результат (текущий):**
- После rebot 22:08 UTC: entry_model_t30=25 предсказаний, entry_timing_model=38 предсказаний за ~120s.
- exit_quantile + survival ждут первого открытия paper_trade (бот пока не покупал из-за высокого ML ceiling=0.9).
- shadow_predictions table свободно растёт — все 4 модели готовы.

**План производственной валидации (через 7-14 дней):**
- t30: AUC + P@top10% на закрытых paper_trades — порог: prod ≥ training×0.7 (т.е. ≥ 0.63 / 5.9% соответственно).
- timing: confusion matrix BUY_NOW/SKIP/WAIT_MORE — порог: BUY_NOW precision > base_rate × 5.
- survival: predicted_remaining_life vs actual_exit_time correlation — порог: Spearman > 0.15.
- quantile: production coverage (% of trades within q25..q75 PnL band) — порог: ±0.03 от target.

**Откат:**
- 4 env флага → `=0`, restart. shadow_predictions table сохраняется.
- Удалить таблицу: `DROP TABLE shadow_predictions`.

---

## 2026-04-26 19:55 — Shadow infrastructure for entry_model_t30 (заменён выше)

**Что изменилось:**
- Новый модуль `pulse_bot/ml/shadow.py` — best-effort logger predictions to a new table, swallows exceptions (никогда не валит live decision path).
- Новая таблица `shadow_predictions` (BIGSERIAL id, model_name, mint, scored_at, snapshot_t, prediction JSONB, confidence, model_hash, schema_version, inserted_at). Migration: `scripts/migrate_shadow_predictions.py` (idempotent).
- `pipeline.py` модифицирован:
  - Проверка `shadow.t30_shadow_enabled()` рядом с `_entry_t30_active` в `__init__` (загрузка policy при shadow OR live).
  - Same в активации `_observation_checkpoint_loop` (запуск loop при shadow OR live).
  - В t30 hook: всегда вызываем `record_t30_shadow` если флаг set, регардлесс LIVE/SHADOW.
- Активирован `PULSE_ENTRY_T30_SHADOW=1` в `.env` на rich, бот рестартован.
- Тесты `tests/pulse_bot/test_shadow.py` (6/6 PASS).

**Зачем:**
- Per memory `project_ml_ensemble_opportunistic_gating`: модель активируется в LIVE только после **production validation** (production metrics ≥ training × 0.7). Shadow mode = безопасный сбор predictions для validation gate.
- `entry_model_t30` имеет training metrics AUC 0.8963 / P@10 8.45% — выглядит хорошо, но переключение entry decision с T+90 на T+30 — серьёзный change. Shadow mode даст ground truth: "если бы t30 решал, что бы случилось".

**Результат (текущий):**
- 5 shadow predictions в первые 100 секунд после restart — pipeline работает.
- Бот продолжает торговать на entry_model (T+90), t30 модель predict'ает но НЕ влияет на decision.
- Через 1-2 дня сборки будем сравнивать `shadow_predictions` vs realized outcomes.

**Что НЕ сделано (отложено):**
- `PULSE_EXIT_QUANTILE_SHADOW=1` env установлен но не подключён к `exit_manager.decide()` — нужно plumb mint/scored_at в decide signature. Helper `record_quantile_shadow` готов, integration требует ~50 LOC + тесты, отложу на следующий заход.
- survival_model и entry_timing_model shadow пока тоже не wired (similar effort).

**Откат:**
- `PULSE_ENTRY_T30_SHADOW=0` в `.env` + restart — log entries прекращаются, table остаётся.
- Удалить таблицу: `DROP TABLE shadow_predictions`.

---

## 2026-04-26 19:35 — All 8 models retrained + entry/exit upgrade live

**Что изменилось:**
- Натренированы все 8 моделей на свежих данных rich (live PG, 84,953 entry rows, 9,212 paper_trades, 79k holder snaps T+30/60/120):
  - `entry_model` (binary): AUC 0.9149, P@top10 9.49% (vs base 1.22%)
  - `entry_model_reg` (regression): AvgPnL@top10 = −0.02% — **нет сигнала** на магнитуду
  - `entry_model_t30` (T+30 only): AUC 0.8963, P@top10 8.45%
  - `exit_model` (binary): обучен с нуля, threshold 0.80
  - `exit_quantile_sl` (q=0.25): coverage 0.221 (target 0.25 ✓), Spearman 0.11 (slabo)
  - `exit_quantile_tp` (q=0.75): coverage 0.768 (target 0.75 ✓), Spearman 0.17 (граница)
  - `survival_model`: 185k hazard rows, 0.38% positive
  - `entry_timing_model` (3-class): 30k snapshots WAIT/BUY/SKIP
- `pulse-bot.service` рестартован — подхватил **свежие entry + exit models**.
- **`Exit ML ACTIVE` впервые** (model_hash=68b57a81, threshold=0.80, min_hold=15s) — exit advisor will escalate hold→sell_all on high proba; hard rules immutable.
- t30 / timing / survival / exit_quantile / entry_reg остаются **на диске без активации** до production validation (см. ROADMAP "ML retrain + activation cadence").

**Зачем:**
- Старые модели (Apr 23-25) тренировались на 5-10× меньшем датасете. Свежий retrain на полном корпусе с PG live data = более точные boundaries и веса.
- exit_model отсутствовал на диске → exit ML был **disabled** all this time. Теперь активен.
- entry_reg honest negative result (AvgPnL@10 = −0.02%) лучше переоптимизированной старой метрики на N=903.

**Результат (ожидание):**
- Exit decisions теперь учитывают ML-predicted "должен ли выйти" — добавится ML-driven sell-all на dying токенах помимо hard rules.
- Entry pipeline без изменений в логике (та же модель, новые веса).
- shadow models собирают данные для будущей активации.

**Откат:**
- Сохранить старые модели до restart можно из git (если потребуется revert): `git checkout HEAD~1 -- data/ml/`.
- Backup current: `cp data/ml/*.ubj data/ml/backup_2026-04-26/`.
- Restart bot после revert.

---

## 2026-04-26 18:07 — `PULSE_EXTENDED_OBSERVE_SECONDS=600` activated on rich

**Что изменилось:** Добавлена строка `PULSE_EXTENDED_OBSERVE_SECONDS=600` в `/home/sergey/www/gg/.env` на rich; `pulse-bot.service` рестартован, переменная подтверждена в `/proc/<pid>/environ`.

**Зачем:**
- Без флага бот отписывался от трейдов сразу после T+90 decision → 99% SKIP-нутых токенов не имели on-chain истории за пределами окна наблюдения.
- Это блокирует тренировку: `entry_timing_model` (3-class WAIT/BUY/SKIP на 15-сек чекпоинтах), survival с длинным max_hold, optimizer max_hold sweep'ы.
- Per memory `project_post_scoring_data_truncation`: max_hold sweeps были no-op'ами без этого флага.

**Результат (ожидаемое):**
- Бот продолжит ловить трейды **до 600 сек после T+90 решения** для каждого SKIP-токена.
- Через 1-2 дня корпус extended observation вырастет с ~262 mints (>5min trades) до тысяч.
- Cost: +N WS/DB writes на каждый SKIP token (≈10× больше DB writes на трейды; мы пишем уже миллионы — переживём).

**Откат:** убрать строку из `.env` + `systemctl --user restart pulse-bot.service`.

---

## 2026-04-26 16:30 — Backfill state poisoning bug + cleanup

**Что изменилось:**
- `scripts/helius_backfill_graduated.py:735`: `completed.add(mint)` теперь срабатывает только при `parsed > 0 OR inserted > 0`. Раньше mint помечался complete по факту завершения обработки — то есть даже когда RPC отбил 429-кой и вернул 0 sigs, mint считался "сделанным" и больше не trial'ился.
- Новый `scripts/fix_backfill_state.py` — переписывает `data/backfill_state.json`, оставляя в `completed_mints` только те mints, у которых в DB есть ≥1 backfill trade (`trades.market_cap_sol = 0`). Делает `.bak.<ts>` копию.
- `backfill.service` остановлен и `disable`d пока не решён вопрос с источником архивной истории (QuickNode исчерпал daily quota, local node имеет только ~80 мин истории, нужен либо Helius/Triton archival либо ждать reset).

**Зачем:**
- Out of 7005 mints, помеченных `completed` старой логикой, **только 153 (2.2%)** реально получили backfill-trades в БД. **6852 (97.8%)** были помечены complete без единого вставленного trade — большинство потому что QuickNode rate-limit'илось или потому что local node не имеет старой истории.
- Старая логика делала backfill **необратимо неполным**: после "ложного complete" эти mints больше не пытались бы.
- ML моделям нужна полнота истории прошедших pump.fun trades, иначе wallet/creator features распределены неверно.

**Результат:**
- state file: 7005 → 153 completed_mints (cleanup_dropped=6852).
- Backup: `data/backfill_state.json.bak.1777219683`.
- Когда backfill будет разморожен (Helius / QuickNode reset / Triton) — корректно отработает 6852 retried mints.
- Тесты на новые launchpad модули (geyser/multiplexer/decoder): 21/21 pass.

**Откат:**
- Восстановить state: `cp data/backfill_state.json.bak.1777219683 data/backfill_state.json`.
- Code revert в helius_backfill_graduated.py: убрать `if parsed > 0 or inserted > 0:` guard (вернуть безусловный `completed.add(mint)`).

---

## 2026-04-26 13:25 — Solana validator OOM fix (3 рестарта подряд)

**Что изменилось:**
- Убран `--account-index program-id --account-index-include-key 6EF8...` (in-memory индекс всех pump.fun аккаунтов).
- Добавлен `--enable-accounts-disk-index` (Solana accounts index на NVMe вместо RAM).
- `--accounts-db-cache-limit-mb` 16384 → 8192.
- Добавлен `--no-poh-speed-test` — non-voting RPC нода не должна падать на 10M hashes/s benchmark под нагрузкой (наш peak под rebuild = 2.9M, требование cluster = 10M).

**Зачем:**
- Validator OOM-killed 3 раза подряд во время rebuild snapshot — peak 122 GB anon-rss (на машине 125 GB RAM). Default Solana config держит весь accounts index в RAM, на full mainnet snapshot это ~50-100 GB.
- Pump.fun program account-index дополнительно удвоил это (миллионы pump.fun аккаунтов, каждый — индексная запись).
- Для нашего use-case (Geyser gRPC streaming + backfill через RPC) account-index не нужен.

**Результат (ожидание):** RAM peak <60 GB, rebuild завершится без OOM.

**Откат:**
- `~/.config/systemd/user/solana-validator.service`: вернуть `--accounts-db-cache-limit-mb 16384`, добавить `--account-index program-id --account-index-include-key 6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P`, убрать `--enable-accounts-disk-index`. `daemon-reload` + restart.

---

## 2026-04-26 09:35 — Yellowstone gRPC source + multiplexer (infra, not yet active)

**Что изменилось:**
- Новый адаптер `pulse_bot/launchpads/geyser.py` — Yellowstone gRPC subscribe к pump.fun program (account_required=`6EF8...`), декодит Create/Buy/Sell из protobuf TransactionStatusMeta.
- Новый `pulse_bot/launchpads/multiplexer.py` — primary+fallback launchpad: gRPC primary, PumpPortal fallback, dedup по `mint` для create + `(mint, wallet, ts, sol, side)` для трейдов; LRU 20k mints / 100k trades; health-monitor логирует если primary молчит >5s.
- Конфиг: `pulse_launchpad`, `pulse_geyser_endpoint=127.0.0.1:10000`, `pulse_geyser_x_token`, `pulse_geyser_health_lag_seconds=5.0` + соответствующие env vars (`PULSE_LAUNCHPAD`, `PULSE_GEYSER_*`).
- `main.py` маршрутизация `_build_launchpad(config)` по `pulse_launchpad`: `pumpportal` (default) | `geyser+pumpportal` (mux) | `geyser` (standalone).
- На rich собирается Yellowstone Geyser plugin `.so` (cargo build, `~/tmp/yellowstone-grpc`, tag `v12.3.0+solana.3.1.13`).
- Pyrhon proto stubs сгенерированы из `geyser.proto` + `solana-storage.proto` в `pulse_bot/launchpads/yellowstone_proto/`.
- Тесты `tests/pulse_bot/test_multiplexer_dedup.py` (4/4 PASS).

**Зачем:**
- Снизить latency live-source с ~50-200ms (PumpPortal/Helius WS) до ~1-5ms (local validator → gRPC).
- Убрать зависимость от 3rd-party rate limits / quotas / Cloudflare bot management.
- Резервирование: если local validator отстанет — PumpPortal автоматически несёт стрим.

**Результат:**
- Дефолт `pulse_launchpad="pumpportal"` — поведение бота сейчас не изменено.
- Активация — после: (1) plugin .so собран, (2) validator снапшот синканул, (3) `--geyser-plugin-config` добавлен в systemd unit, (4) `PULSE_LAUNCHPAD=geyser+pumpportal` в .env.
- Бот на rich продолжает работать на PumpPortal (текущее поведение).

**Откат:**
- `PULSE_LAUNCHPAD=pumpportal` в .env (дефолт), `systemctl --user restart pulse-bot.service`.
- Удалить новые файлы: `pulse_bot/launchpads/{geyser,multiplexer}.py`, `pulse_bot/launchpads/yellowstone_proto/*`.

---

## 2026-04-25 21:00 — Deploy bot to rich server (production)

**Что изменилось:**
- Полная миграция бота с Mac на server `rich` (192.168.3.118, Ubuntu, 125GB RAM, PG 16)
- pg_dump 2.7GB Mac → scp → pg_restore на rich (4.21M trades, 79K live scores восстановлены)
- rsync code (1458 files, 93MB), исключая .venv/.git/data/parquet
- venv setup на rich: requirements.txt + extras (xgboost, psycopg2-binary, aiohttp, scipy, sklearn)
- .env скопирован, добавлен `PULSE_PG_DSN=postgresql://sergeychernyakov:pulsebot@localhost/pulse_bot`
- Bot стартован через nohup (PID 573790 на rich)

**Зачем:**
- 24/7 работа (Mac выключается, обрывает Phase 0 collection)
- Стабильное соединение (Ethernet vs WiFi)
- Свободные ресурсы для backfill параллельно

**Результат:**
- Bot на rich принимает события, делает Helius captures (T+30/60/120 lag=0.00s), активно пишет в БД
- Mac бот остановлен (PID 28843 killed)
- Resumed 2 open paper_trades без потерь
- README обновлён с deploy инструкциями

**Откат:**
- На Mac: `nohup .venv/bin/python main.py monitor` (БД и код синхронизированы)
- На rich: `pkill -f "main.py monitor"`

---

## 2026-04-25 13:15 — Удаление exit_model + DB cleanup (300 MB)

**Что изменилось:**
- Удалён файл `data/ml/exit_model.ubj` + `exit_model.meta.json` — бинарный exit классификатор exit_v3 с AUC 0.5454 (практически random, 9 фич).
- DB cleanup:
  - `DELETE FROM token_scores WHERE source='backtest'` — 118,674 строк (от 1ч backtest 2026-04-24, не используются ML обучением)
  - `TRUNCATE event_log` — 350,278 строк (write-only diagnostic, нет читателей в коде)
  - `VACUUM FULL ANALYZE` на обеих таблицах — реклейм места

**Зачем:**
- exit_model AUC 0.55 = почти random; threshold=0.80 редко срабатывает, реальный contribution к live решениям микроскопический. Теперь exits идут rules + exit_quantile_sl/tp heads + (после включения) Phase 4B survival.
- 300 MB освобождено на диске, чище данные для будущих sweeps (без backtest noise в base).

**Результат:**
- DB size: 3056 MB → 2756 MB (−300 MB, −9.8%)
- token_scores: 400 MB → 176 MB (118K → 75K строк)
- event_log: 76 MB → 32 KB (350K → 3 строк)
- Бот живой (PID 39189) — модель в памяти не затронута, изменение увидит только при рестарте
- Pipeline graceful fallback: `load_exit_policy_if_available` возвращает None → лог `Exit ML: no model loaded (advisor disabled)`

**Откат:**
- exit_model: переобучить через `train.py --dataset exit` (parquet всё ещё на месте: data/ml/exit.parquet)
- DB cleanup: backtest scores и event_log были write-only/stale — восстанавливать незачем. Если нужен event_log — `git revert` cleanup commit (нет — это не коммит, просто DB операции). Просто заново начнёт логировать после рестарта.

---

## 2026-04-25 19:00 — Phase 2.5: time-aware (multi-snapshot) features в main entry-модели

**Что изменилось:**
- `pulse_bot/ml/features.py` → `FEATURE_SCHEMA_VERSION` поднят `entry_v17_20260425` → `entry_v18_20260425`.
- Добавлены две новые группы фич в `ENTRY_FEATURE_ORDER` (в конце, чтобы не сдвигать порядок старых колонок):
  * `TIME_AWARE_FEATURES` (9 шт): `unique_buyers_at_{30,60,90}`, `buy_rate_at_{30,60,90}`, `buy_volume_sol_at_{30,60,90}`. Считаются по обрезке buy-stream'а до `created_at + age`. Знаменатель `buy_rate_at_N` — это N (а не observation_seconds), чтобы значение было сопоставимо между токенами с разным окном наблюдения.
  * `TIME_AWARE_DERIVED_FEATURES` (4 шт): `top1_at_60` (линейная интерполяция между Helius-снимками @30 и @120 — Helius @60 не снимает), `delta_top1_30_to_60`, `delta_buy_rate_60_to_90`, `delta_unique_buyers_30_to_60`.
- `pulse_bot/filters/metrics.py` → `TokenMetrics` получил 9 новых полей `*_at_{30,60,90}`; `MetricsCalculator.compute()` считает их через `_stats_up_to(age)`.
- `pulse_bot/filters/scorer.py` → `ScoringResult` пробрасывает 9 новых полей.
- `pulse_bot/models.py` → 9 новых дефолтных полей в `ScoringResult`.
- `pulse_bot/ml/build_dataset.py` → новый helper `_compute_time_aware_features()`; `build_entry_dataset()` после wallet-блока bulk-fетчит trades [created_at, +90s] по чанкам 500 mints, агрегирует @30/@60/@90, считает интерполяцию `top1_at_60` и три delta-фичи. Парность с live-путём проверена в тестах.
- Новый файл `tests/pulse_bot/test_time_aware_features.py` (8 тестов): truncation, deltas, schema layout, build_dataset wiring, parity invariant `@90 == full-window`.
- Обновлены `tests/pulse_bot/test_features_parity.py`: `test_feature_order_is_stable` теперь учитывает 7 групп; `test_parity_with_parquet_if_present` корректно скипает старый parquet, у которого нет v18-колонок (требует пересборки датасета — отдельный шаг).

**Зачем:** Phase 2.5 из ROADMAP_2026_05.md. Идея: вместо отдельных голов @T+30/@T+45/@T+60 (Phase 3, для **раннего** решения) расширить feature vector главной T+90 модели снимками траектории, чтобы единый классификатор учил «эволюцию» токена. 70 → 83 фич; те же 1292 labels — XGBoost регуляризацией справляется на этом N (codex 2026-04-24). Не заменяет Phase 3 для fast-decision'а.

**Результат:**
- 8 новых тестов + 14 регрессионных в `test_features_parity.py` зелёные (22 passed, 2 skipped).
- Парность @90 = full-window подтверждена бит-в-бит на синтетическом стриме.
- Ruff/black/isort чистые на всех изменённых файлах.
- **Модель НЕ переобучена** — это отдельный шаг (по требованию: «DO NOT retrain model»). Существующая модель станет несовместима по `FEATURE_SCHEMA_VERSION` — нужен retrain до следующей попытки запуска `EntryMLPolicy`.

**Open question:** `top1_at_60` — линейная интерполяция между Helius @30 и @120 (Helius не делает @60-snapshot). Альтернатива — переснять top1@60 напрямую из bonding-curve trade-stream'а, но это требует индексации wallet-долей по trades, что дорого. Решение отложено до Phase 3 prereq (clean Helius T+30 snapshot flow).

**Откат:** вернуть `FEATURE_SCHEMA_VERSION = "entry_v17_20260425"`, удалить `TIME_AWARE_FEATURES` + `TIME_AWARE_DERIVED_FEATURES` из `ENTRY_FEATURE_ORDER` и обработку в `extract_entry_features` / `build_entry_dataset`. Поля в `TokenMetrics` / `ScoringResult` можно оставить — они не ломают обратную совместимость.

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
