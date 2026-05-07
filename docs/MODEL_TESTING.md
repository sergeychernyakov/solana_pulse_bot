# Как честно тестировать модель

Гайд по проверке: новая модель действительно лучше старой, или мы себя обманываем.

---

## Главный принцип

**Сравнивать модели можно только при одинаковых условиях.** Любое из этого делает сравнение нечестным:
- Разные test sets (накопились новые данные между retrain'ами)
- Разное число entries (`proba≥0.5` у двух моделей с разной калибровкой = разные перцентили)
- Разный base rate в holdout (P@10% автоматически падает при меньшей base rate)
- Разные TP/SL gates в backtest

---

## Типичные ошибки

### Ошибка 1: сравнение WR при разном N entries

**Плохо:**
> Старая модель: 1251 entries, WR 7.91%
> Новая модель: 741 entries, WR 15.4%
> "Новая в 2 раза лучше!"

Новая просто строже отбирает. **0 entries дали бы WR=undefined и 0 потерь** — это не значит что модель идеальная.

**Хорошо:** взять top-N с одинаковым N и сравнить количество wins на одном holdout.

### Ошибка 2: сравнение P@10% при разном base rate

**Плохо:**
> P@10%: 9.36% → 6.03%, регрессия!

Если base rate упал 1.43% → 0.82% (более грязный датасет) — P@10% **обязан** упасть пропорционально.

**Хорошо:** считать **lift** = P@10% / base_rate.
- prev: 9.36% / 1.43% = **6.55×**
- new:  6.03% / 0.82% = **7.35×** (на самом деле ranking лучше)

### Ошибка 3: economic_backtest с захардкоженными TP/SL

`pulse_bot.ml.daily_validation` использует **TP=50% / SL=30%** — это не live конфигурация (live SL=15, max_hold=120s). Минусовый PnL в backtest **не означает** что бот теряет деньги в paper-режиме.

**Хорошо:** живой `paper_trades` audit на rich за 24-48h после деплоя.

### Ошибка 4: разные test sets

Между двумя retrain'ами накопились новые токены. `daily_report_entry_2026-04-25.json` гонялся на 14,899 строк, `daily_report_entry_2026-04-30.json` — на 19,755. Distribution shift делает прямое сравнение метрик бессмысленным.

**Хорошо:** прогнать обе модели (`entry_model.ubj` и `entry_model.ubj.prev`) на **одном** holdout.

---

## Честный протокол сравнения

### 1. Top-N на shared holdout

```python
# Загрузить обе модели
new_model = xgb.Booster(); new_model.load_model("data/ml/entry_model.ubj")
prev_model = xgb.Booster(); prev_model.load_model("data/ml/entry_model.ubj.prev")

# Один и тот же holdout (последние 20% по scored_at)
df = pd.read_parquet("data/ml/entry.parquet").sort_values("scored_at")
test = df.iloc[int(len(df) * 0.8):]
X = test[features].values
y = test["label"].values

new_proba = new_model.predict(xgb.DMatrix(X, feature_names=features))
prev_proba = prev_model.predict(xgb.DMatrix(X, feature_names=features))

# Сравнить top-N wins
for N in [50, 100, 200, 500, 1000]:
    new_wins = y[np.argsort(new_proba)[::-1][:N]].sum()
    prev_wins = y[np.argsort(prev_proba)[::-1][:N]].sum()
    print(f"N={N}: prev={prev_wins} new={new_wins}")
```

Готовый скрипт: `scripts/honest_topn_compare.py` (TODO — пока в `/tmp/honest_topn.py` на Mac).

### 2. AUC на shared holdout

`auc` из `meta.json` посчитан на **разных** test sets. Считать AUC заново на одном holdout — даёт честное сравнение ranking quality.

### 3. Top-N overlap

Если top-100 overlap < 50% — модели **существенно разные**, рассматривать как отдельные стратегии. Если > 90% — почти эквивалент, разница между ними может быть шумом.

### 4. Lift, не P@10%

```
lift = precision_top10 / base_rate
```
Lift инвариантен к разному уровню "грязи" в holdout.

### 5. Live paper audit (источник истины)

Backtest с фиксированными TP=50/SL=30 — это **синтетика**. Реальная проверка — `scripts/live_audit.py` на rich:
```bash
ssh rich
cd /home/sergey/www/gg
PYTHONPATH=. .venv/bin/python -m scripts.live_audit --hours 24
```

Метрики: реализованный PnL, WR на закрытых позициях, ρ для regression модели.

---

## Что не делать

- **Не делать выводов на N<100 закрытых позициях** — CI95 для WR при N=100 это ±10пп, легко спутать с шумом.
- **Не доверять одному backtest run** — economic_backtest стохастичен на симуляции exit'ов.
- **Не сравнивать модели до feature_stability** — если фичи нестабильны (не выживают 5 random seeds), AUC случаен.
- **Не публиковать "WR улучшился" без указания N** — это первый вопрос ревьюера.

---

## Минимальный чек-лист перед "новая модель лучше"

- [ ] Top-N comparison на одном holdout (минимум N=100, 500)
- [ ] AUC и P@10% на одном holdout (не из разных meta.json)
- [ ] Lift × base_rate, не голый P@10%
- [ ] Top-100 overlap указан (для контекста)
- [ ] CI95 для каждой метрики (если N мал)
- [ ] Live audit запланирован на 24-48h после деплоя
- [ ] CHANGELOG entry с before/after на тех же условиях
