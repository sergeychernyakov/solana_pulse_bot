# A/B эксперименты — журнал

Запись того, что тестировали в multi-config paper-A/B: гипотеза, конфиги
с параметрами, дата запуска, результаты.

Конфиги живут в `config/entry_configs.yaml`. Каждый конфиг — отдельный
бумажный портфель из одного общего WS-потока, тег `paper_trades.config_id`.
Сравнение per-config видно на дашборде («Per-config A/B breakdown»):
запрос — `SELECT config_id, COUNT(*), SUM(pnl_sol), AVG(pnl_pct),
AVG((pnl_pct>0)::int) FROM paper_trades WHERE status='closed' GROUP BY config_id`.

Формат записи:
```
## YYYY-MM-DD — Название раунда
**Гипотеза:** что проверяем
**Конфиги:** id — параметр (одна переменная на конфиг для чистой атрибуции)
**Запущено:** дата/время UTC, на каком коммите
**Критерий:** что считаем успехом
**Результат:** заполняется позже — N сделок, WR, PnL по конфигам, вывод
```

---

## 2026-05-14 — Раунд 1: entry-selectivity (p_raw) × exit-policy × scam-filters

**Гипотеза:** три независимых направления, по одной переменной на конфиг:
1. **Entry-отбор по сырой вероятности.** Калиброванная proba модели сжата
   (~0.01-0.04) — на ней A/B мёртв. Сырая (`p_raw`) разбросана ~0.2-0.47 и
   ранжирует победителей (auc_sign≈0.92). Вопрос: даёт ли требование более
   уверенного токена прирост win-rate, оправдывающий меньше сделок?
2. **Exit-политика.** Глобальный TP (~30%) и trailing (активация +50%)
   почти не срабатывают — p90 пик токенов ≈ +14%. Вопрос: ловить мелкие
   прибыли быстро (низкий TP / тугой trailing) лучше, чем держать ради
   редкого moonshot?
3. **Строгость scam-фильтров.** Вопрос: более жёсткие пороги bot/wash-
   кластеров улучшают WR достаточно, чтобы оправдать меньше сделок?

**Конфиги:**

| config_id  | направление | переменная (остальное = LIVE) |
|------------|-------------|-------------------------------|
| LIVE       | baseline    | без флоров, глобальный exit, фильтры 3/2 |
| PRAW30     | entry       | `p_raw_floor = 0.30` |
| PRAW40     | entry       | `p_raw_floor = 0.40` |
| TP10       | exit        | `exit_take_profit_pct = 10.0` |
| TRAILTIGHT | exit        | `exit_trailing_stop_activation_pct = 10.0`, `exit_trailing_stop_distance_pct = 15.0` |
| SCAMSTRICT | filters     | `bot_cluster_hard_skip_n = 2`, `wash_cluster_skip_n = 1` |

**Запущено:** 2026-05-14 17:07 UTC, коммит `c486acd`.

**Чистый старт:** 2026-05-14 21:25 UTC — `paper_trades` обнулена (TRUNCATE),
бот перезапущен. Причина: с 17:07 до ~20:23 кошелёк PumpPortal был ниже
порога 0.02 SOL → WS не отдавал поток сделок → 0 входов; плюс LIVE тащил
47 устаревших строк до-сброса. После топ-апа кошелька и обнуления все 6
конфигов стартуют с N=0 одновременно — равные условия. Отсчёт N≥100/конфиг
ведём отсюда.

**Критерий:** через N≥100 закрытых сделок на конфиг — сравнить
total_pnl_sol и WR против LIVE. Вариант «deploy-worthy», если
total_pnl_sol > LIVE и WR ≥ LIVE − 1pp. Иначе — оставить LIVE.

**Заметки:**
- LIVE сейчас прибылен (v21-эра: +3.98 SOL / 2158 сделок до сброса 13.05).
- entry_model помечена `ev_warning` (training-label EV отрицательный из-за
  забагованного симулятора — симулятор исправлен, но модель пока не
  переобучена). Это не блокирует торговлю, но держать в уме при чтении PnL.
- Реальной торговли нет — всё бумажное.

**Результат:** _<заполнить после набора N≥100 на конфиг>_

---

## 2026-05-15 — Раунд 2: добор до 20 конфигов (×exit-policy, scam-sanity, entry-фильтры, RULESONLY/NO_SURVIVAL)

**Гипотеза:** Раунд-1 продуктивные ~5 часов показали 100% выходов = `dead_token`,
все exit-параметры (`max_hold`, `inactivity`, `SL`, `TP`, `trailing`) определяют
PnL гораздо сильнее редко-срабатывающего TP/trailing. Раунд-2 расширяет до 20
конфигов: плотная сетка по exit-политике + entry-фильтры + sanity-чеки.

**Конфиги (14 новых):**

| config_id | направление | переменная (остальное = LIVE) |
|---|---|---|
| HOLD60 | exit | `exit_max_hold_seconds = 60` |
| HOLD180 | exit | `exit_max_hold_seconds = 180` |
| INACT60 | exit | `exit_inactivity_seconds = 60` |
| INACT180 | exit | `exit_inactivity_seconds = 180` |
| SL08 | exit | `exit_hard_stop_loss_pct = 8.0` |
| TP20 | exit | `exit_take_profit_pct = 20.0` |
| TRAIL_LOOSE | exit | trailing активация=20%, дистанция=30% |
| NOSCAM | sanity | `bot_cluster_hard_skip_n=999, wash_cluster_skip_n=999` (фильтры выключены) |
| REGFLOOR5 | entry | `reg_floor_pct = 5.0` (reg-модель advisory) |
| BUYERMAX10 | entry | `entry_buyer_max_n = 10` (только ранние входы) |
| SMARTONLY | entry | `require_smart_money = True` (нужен ≥1 `is_smart_money` в первых 30с) |
| TOP3PNL | entry | `require_top3_positive_pnl = True` (нужен ≥1 ранний buyer c `graduated_winrate>0.10`) |
| RULESONLY | strategy | `disable_ml_override = True` (полный bypass override-пути) |
| NO_SURVIVAL | strategy | `disable_survival_exit = True` (отключить survival-модель на выходе) |

**Запущено:** 2026-05-15 08:50:52 UTC. Коммит `<hash>`. md5 сверены по всем
файлам деплоя. `Loaded 20 entry configs ... Multi-config A/B active: 20 configs`.

**Важная архитектурная правка (одновременно с Round-2):** pre-filters
(`filter_bot_cluster`, `filter_wash_cluster`, `filter_smart_money_required`,
`filter_top3_positive_pnl`) теперь запускаются **внутри per-config лупа** в
`pipeline.py`. До этого они шарились через LIVE-DecisionService — и
SCAMSTRICT/NOSCAM по факту НЕ уважали свои пороги (это объясняло
lockstep counts SCAMSTRICT=13, TP10=13 в Round-1). Раунд-1 цифры по
SCAMSTRICT/wash в этом смысле невалидны; настоящий A/B по фильтрам
начинается с этого момента.

**Критерий:** через N≥100 закрытых сделок на конфиг — `total_pnl_sol > LIVE`
И `WR ≥ LIVE − 1pp`. При 20 конфигах multiple-comparison: Bonferroni
`p<0.05/20 = 0.0025`. Без коррекции FP-rate ≈ 64% — на цифры одного
«победителя» сразу не вестись.

**Заметки:**
- Темп сбора с Round-1 (5.17h): LIVE-семейство ~2.5 трейда/h. До N=100 на конфиг
  без entry-фильтра — ~1.7 дня. Entry-фильтры (PRAW30/BUYERMAX10/SMARTONLY/TOP3PNL)
  идут медленнее, тут до N=100 — несколько дней.
- PRAW40 пока живёт (Round-1 показал ~0.2 трейда/h). Если за следующие сутки
  не пополнится — выкинуть.
- Кошелёк PumpPortal: 0.0204 SOL — на пороге; PumpPortal Lightning подгрызает.
  Следить, своевременно сообщать.

**Результат:** _<заполнить после набора N≥100 на конфиг>_
