# A/B experiments — log

Record of what was tested in multi-config paper-A/B: hypothesis, configs
with parameters, launch date, results.

Configs live in `config/entry_configs.yaml`. Each config is a separate
paper portfolio from one shared WS stream, tagged `paper_trades.config_id`.
Per-config comparison is visible on the dashboard ("Per-config A/B breakdown"):
query — `SELECT config_id, COUNT(*), SUM(pnl_sol), AVG(pnl_pct),
AVG((pnl_pct>0)::int) FROM paper_trades WHERE status='closed' GROUP BY config_id`.

Entry format:
```
## YYYY-MM-DD — Round title
**Hypothesis:** what we are checking
**Configs:** id — parameter (one variable per config for clean attribution)
**Launched:** date/time UTC, on which commit
**Criterion:** what we count as success
**Result:** filled in later — N trades, WR, PnL per config, conclusion
```

---

## 2026-05-14 — Round 1: entry-selectivity (p_raw) × exit-policy × scam-filters

**Hypothesis:** three independent directions, one variable per config:
1. **Entry selection by raw probability.** Calibrated model proba is compressed
   (~0.01-0.04) — A/B is dead on it. Raw (`p_raw`) is spread ~0.2-0.47 and
   ranks winners (auc_sign≈0.92). Question: does requiring a more
   confident token give a win-rate uplift that justifies fewer trades?
2. **Exit policy.** Global TP (~30%) and trailing (activation +50%)
   almost never trigger — p90 token peak ≈ +14%. Question: is catching small
   profits quickly (low TP / tight trailing) better than holding for a
   rare moonshot?
3. **Scam-filter strictness.** Question: do stricter bot/wash-cluster
   thresholds improve WR enough to justify fewer trades?

**Configs:**

| config_id  | direction | variable (rest = LIVE) |
|------------|-------------|-------------------------------|
| LIVE       | baseline    | no floors, global exit, filters 3/2 |
| PRAW30     | entry       | `p_raw_floor = 0.30` |
| PRAW40     | entry       | `p_raw_floor = 0.40` |
| TP10       | exit        | `exit_take_profit_pct = 10.0` |
| TRAILTIGHT | exit        | `exit_trailing_stop_activation_pct = 10.0`, `exit_trailing_stop_distance_pct = 15.0` |
| SCAMSTRICT | filters     | `bot_cluster_hard_skip_n = 2`, `wash_cluster_skip_n = 1` |

**Launched:** 2026-05-14 17:07 UTC, commit `c486acd`.

**Clean start:** 2026-05-14 21:25 UTC — `paper_trades` truncated (TRUNCATE),
bot restarted. Reason: from 17:07 to ~20:23 the PumpPortal wallet was below
the 0.02 SOL threshold → WS did not deliver the trade stream → 0 entries; plus LIVE was carrying
47 stale pre-reset rows. After topping up the wallet and truncating, all 6
configs start with N=0 simultaneously — equal conditions. The N≥100/config
count starts from here.

**Criterion:** after N≥100 closed trades per config — compare
total_pnl_sol and WR against LIVE. "Deploy-worthy" variant if
total_pnl_sol > LIVE and WR ≥ LIVE − 1pp. Otherwise — keep LIVE.

**Notes:**
- LIVE is currently profitable (v21 era: +3.98 SOL / 2158 trades before the 13.05 reset).
- entry_model is flagged `ev_warning` (training-label EV is negative due to
  a buggy simulator — the simulator is fixed, but the model has not yet been
  retrained). This does not block trading, but keep in mind when reading PnL.
- No real trading — everything is paper.

**Result:** _<fill in after reaching N≥100 per config>_

---

## 2026-05-15 — Round 2: top-up to 20 configs (×exit-policy, scam-sanity, entry-filters, RULESONLY/NO_SURVIVAL)

**Hypothesis:** Round-1's productive ~5 hours showed 100% exits = `dead_token`,
all exit parameters (`max_hold`, `inactivity`, `SL`, `TP`, `trailing`) determine
PnL much more strongly than the rarely-triggering TP/trailing. Round-2 expands to 20
configs: dense grid over exit-policy + entry-filters + sanity-checks.

**Configs (14 new):**

| config_id | direction | variable (rest = LIVE) |
|---|---|---|
| HOLD60 | exit | `exit_max_hold_seconds = 60` |
| HOLD180 | exit | `exit_max_hold_seconds = 180` |
| INACT60 | exit | `exit_inactivity_seconds = 60` |
| INACT180 | exit | `exit_inactivity_seconds = 180` |
| SL08 | exit | `exit_hard_stop_loss_pct = 8.0` |
| TP20 | exit | `exit_take_profit_pct = 20.0` |
| TRAIL_LOOSE | exit | trailing activation=20%, distance=30% |
| NOSCAM | sanity | `bot_cluster_hard_skip_n=999, wash_cluster_skip_n=999` (filters off) |
| REGFLOOR5 | entry | `reg_floor_pct = 5.0` (reg-model advisory) |
| REGFLOOR_MINUS5 | entry | `reg_floor_pct = -5.0` (soft, added 2026-05-18 18:52 UTC) |
| REGFLOOR_MINUS10 | entry | `reg_floor_pct = -10.0` (most permissive, added 2026-05-18 18:52 UTC) |
| REGFLOOR0 | entry | `reg_floor_pct = 0.0` (only non-negative reg-forecast) |
| REGFLOOR10 | entry | `reg_floor_pct = 10.0` (sanity, very few entries) |
| BUYERMAX10 | entry | `entry_buyer_max_n = 10` (only early entries) |
| SMARTONLY | entry | `require_smart_money = True` (need ≥1 `is_smart_money` in first 30s) |
| TOP3PNL | entry | `require_top3_positive_pnl = True` (need ≥1 early buyer with `graduated_winrate>0.10`) |
| RULESONLY | strategy | `disable_ml_override = True` (full bypass of override-path) |
| NO_SURVIVAL | strategy | `disable_survival_exit = True` (disable survival-model on exit) |

**Launched:** 2026-05-15 08:50:52 UTC. Commit `<hash>`. md5 verified across all
deploy files. `Loaded 20 entry configs ... Multi-config A/B active: 20 configs`.

**Update 2026-05-18 18:52 UTC:** added REGFLOOR_MINUS5/MINUS10 (soft
floor variants for mapping the reg-model sensitivity curve). After restart
the bot loads 24 configs (`Multi-config A/B active: 24 configs`). New
configs start with N=0; the rest continue from ~2 trades (last
restart was at 18:44, feed is alive, wallet > 0.02 SOL).

**Important architectural fix (simultaneous with Round-2):** pre-filters
(`filter_bot_cluster`, `filter_wash_cluster`, `filter_smart_money_required`,
`filter_top3_positive_pnl`) now run **inside the per-config loop** in
`pipeline.py`. Before this they were shared via the LIVE-DecisionService — and
SCAMSTRICT/NOSCAM in fact did NOT respect their thresholds (this explained
the lockstep counts SCAMSTRICT=13, TP10=13 in Round-1). Round-1 numbers for
SCAMSTRICT/wash are invalid in this sense; the real A/B on filters
starts from this point.

**Criterion:** after N≥100 closed trades per config — `total_pnl_sol > LIVE`
AND `WR ≥ LIVE − 1pp`. With 20 configs multiple-comparison: Bonferroni
`p<0.05/20 = 0.0025`. Without correction FP-rate ≈ 64% — do not get carried away
by a single "winner's" numbers immediately.

**Notes:**
- Collection pace from Round-1 (5.17h): LIVE family ~2.5 trades/h. To N=100 per config
  without entry-filter — ~1.7 days. Entry-filters (PRAW30/BUYERMAX10/SMARTONLY/TOP3PNL)
  are slower, here to N=100 — several days.
- PRAW40 is still alive so far (Round-1 showed ~0.2 trades/h). If it does not gain trades
  over the next 24h — drop it.
- PumpPortal wallet: 0.0204 SOL — at the threshold; PumpPortal Lightning is nibbling at it.
  Monitor, report in time.

**Result:** _<fill in after reaching N≥100 per config>_
