# How to honestly test the model

A guide for verifying: is the new model actually better than the old one, or are we fooling ourselves.

---

## Main principle

**Models can only be compared under identical conditions.** Any of the following makes the comparison dishonest:
- Different test sets (new data accumulated between retrains)
- Different number of entries (`proba≥0.5` for two models with different calibration = different percentiles)
- Different base rate in holdout (P@10% automatically drops at a lower base rate)
- Different TP/SL gates in backtest

---

## Typical mistakes

### Mistake 1: comparing WR at different N entries

**Bad:**
> Old model: 1251 entries, WR 7.91%
> New model: 741 entries, WR 15.4%
> "New one is 2x better!"

The new one simply filters more strictly. **0 entries would give WR=undefined and 0 losses** — that doesn't mean the model is perfect.

**Good:** take top-N with the same N and compare the number of wins on the same holdout.

### Mistake 2: comparing P@10% at different base rate

**Bad:**
> P@10%: 9.36% → 6.03%, regression!

If base rate dropped 1.43% → 0.82% (dirtier dataset) — P@10% **must** drop proportionally.

**Good:** compute **lift** = P@10% / base_rate.
- prev: 9.36% / 1.43% = **6.55×**
- new:  6.03% / 0.82% = **7.35×** (ranking is actually better)

### Mistake 3: economic_backtest with hardcoded TP/SL

`pulse_bot.ml.daily_validation` uses **TP=50% / SL=30%** — this is not the live configuration (live SL=15, max_hold=120s). Negative PnL in backtest **does not mean** the bot is losing money in paper mode.

**Good:** live `paper_trades` audit on rich for 24-48h after deploy.

### Mistake 4: different test sets

Between two retrains new tokens accumulated. `daily_report_entry_2026-04-25.json` was run on 14,899 rows, `daily_report_entry_2026-04-30.json` — on 19,755. Distribution shift makes direct metric comparison meaningless.

**Good:** run both models (`entry_model.ubj` and `entry_model.ubj.prev`) on the **same** holdout.

---

## Honest comparison protocol

### 1. Top-N on shared holdout

```python
# Load both models
new_model = xgb.Booster(); new_model.load_model("data/ml/entry_model.ubj")
prev_model = xgb.Booster(); prev_model.load_model("data/ml/entry_model.ubj.prev")

# Same holdout (last 20% by scored_at)
df = pd.read_parquet("data/ml/entry.parquet").sort_values("scored_at")
test = df.iloc[int(len(df) * 0.8):]
X = test[features].values
y = test["label"].values

new_proba = new_model.predict(xgb.DMatrix(X, feature_names=features))
prev_proba = prev_model.predict(xgb.DMatrix(X, feature_names=features))

# Compare top-N wins
for N in [50, 100, 200, 500, 1000]:
    new_wins = y[np.argsort(new_proba)[::-1][:N]].sum()
    prev_wins = y[np.argsort(prev_proba)[::-1][:N]].sum()
    print(f"N={N}: prev={prev_wins} new={new_wins}")
```

Ready-made script: `scripts/honest_topn_compare.py` (TODO — currently in `/tmp/honest_topn.py` on Mac).

### 2. AUC on shared holdout

`auc` from `meta.json` is computed on **different** test sets. Recomputing AUC on a single holdout gives an honest comparison of ranking quality.

### 3. Top-N overlap

If top-100 overlap < 50% — the models are **substantially different**, treat them as separate strategies. If > 90% — almost equivalent, the difference between them may be noise.

### 4. Lift, not P@10%

```
lift = precision_top10 / base_rate
```
Lift is invariant to different levels of "dirt" in holdout.

### 5. Live paper audit (source of truth)

Backtest with fixed TP=50/SL=30 is **synthetic**. The real check is `scripts/live_audit.py` on rich:
```bash
ssh rich
cd /home/sergey/www/gg
PYTHONPATH=. .venv/bin/python -m scripts.live_audit --hours 24
```

Metrics: realized PnL, WR on closed positions, ρ for regression model.

---

## What not to do

- **Don't draw conclusions on N<100 closed positions** — CI95 for WR at N=100 is ±10pp, easy to confuse with noise.
- **Don't trust a single backtest run** — economic_backtest is stochastic on exit simulation.
- **Don't compare models before feature_stability** — if features are unstable (don't survive 5 random seeds), AUC is random.
- **Don't publish "WR improved" without stating N** — that's the reviewer's first question.

---

## Minimum checklist before "the new model is better"

- [ ] Top-N comparison on one holdout (minimum N=100, 500)
- [ ] AUC and P@10% on one holdout (not from different meta.json)
- [ ] Lift × base_rate, not bare P@10%
- [ ] Top-100 overlap stated (for context)
- [ ] CI95 for each metric (if N is small)
- [ ] Live audit scheduled for 24-48h after deploy
- [ ] CHANGELOG entry with before/after under the same conditions
