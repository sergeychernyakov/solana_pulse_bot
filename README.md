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

# Качество кода
./qa

# Dashboards
streamlit run pulse_bot/dashboard.py --server.port 8501           # live
streamlit run pulse_bot/backtest_dashboard.py --server.port 8502  # backtest results
```

## Backtest = Live (100% match)

Бэктест использует тот же `Pipeline` код что и live бот. Разница только в источнике данных:

| | Live | Backtest |
|---|---|---|
| Launchpad | PumpFunLaunchpad (WebSocket) | ReplayLaunchpad (SQLite) |
| Pipeline | тот же код | тот же код |
| Scorer | тот же код | тот же код |
| token_scores.source | 'live' | 'backtest' |

Верификация:
```bash
python main.py monitor    # собрать данные (2+ мин)
python main.py verify     # replay + сравнить
```

Гарантии детерминизма:
- `insert_token` + `upsert_creator` — в main loop (последовательно)
- Creator snapshot замораживается ДО параллельной задачи
- Replay загружает ровно те же trade IDs что видел live (fast_trade_ids, full_trade_ids)
- FastFilter возвращает WAIT при 0 трейдов (не фейковый FAST_BUY)

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

## Документация

- [Техническая спецификация](./docs/SOLANA_PULSE_BOT_SPEC.md)
- [Python Style Guide](./PYTHON_STYLE_GUIDE.md)

## Автор

**Sergey Chernyakov** — Telegram: [@AIBotsTech](https://t.me/AIBotsTech)
