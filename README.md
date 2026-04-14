# Solana Pulse Bot v3

Бот-наблюдатель для мемкоин-лончпадов на Solana. Не снайпер — не гонится за первым блоком. Наблюдает 30–90 секунд за новым токеном, оценивает органичность интереса, входит 3–5м покупателем. Выходит не по фиксированному TP/SL, а по пульсу активности — когда покупатели иссякают.

Асимметричная ставка: теряешь мало (bonding curve почти не сдвинулась), выигрываешь кратно (если токен полетел).

Один код — два режима: бэктест на исторических данных и боевая торговля.

## Архитектура

Event-driven: Commands (намерения) отделены от Events (факты). Время абстрагировано через Clock — бэктест и live используют один код. Состояние персистентно — бот восстанавливается после падения.

```
SOURCE (BacktestSource | LiveSource)
  │ TokenEvent
PRECHECK FILTERS (authority, creator, blacklist)
  │ (passed)
OBSERVATION FILTERS (30-90 сек: buyers, volume, diversity, bundled buy)
  │ SignalEvent (score > threshold)
PORTFOLIO (can_open? reserve() balance)
  │ PlaceBuyCommand
EXECUTION WORKER (SimulatedExecution | LiveExecution, bounded concurrency)
  │ FillEvent
PORTFOLIO (commit() position, persist to SQLite)
  │
PULSE MONITOR (stream-based, per-mint task)
  │ PlaceSellCommand → Execution Worker → FillEvent → Portfolio
```

## Структура проекта

```
pulse_bot/
├── __init__.py
├── models.py              # Dataclasses: Token, Trade, Position, PulseSnapshot, TradeResult
├── events.py              # Event ABC + TokenEvent, SignalEvent, FillEvent, PulseEvent
├── commands.py            # Command ABC + PlaceBuyCommand, PlaceSellCommand
├── clock.py               # Clock ABC + RealClock + SimulatedClock
├── config.py              # Config dataclass + LaunchpadConfig + пресеты
├── db.py                  # SQLite: схема, CRUD, event_log, state persistence
├── portfolio.py           # Portfolio: reserve/commit/release, persist, restore
├── execution.py           # ExecutionHandler ABC + SimulatedExecution + LiveExecution + Worker
├── engine.py              # PulseBot: event loop + execution worker + bounded concurrency
├── collector.py           # Bitquery → SQLite (сбор данных для бэктеста)
├── report.py              # P&L отчёт, метрики, топы/лузеры
├── main.py                # CLI: collect / backtest / paper / live
│
├── sources/               # Источники данных
│   ├── base.py            # DataSource ABC (stream_new_tokens, stream_trades)
│   ├── backtest.py        # BacktestSource — SQLite + SimulatedClock
│   └── live.py            # LiveSource — WebSocket + RPC + RealClock
│
├── launchpads/            # Адаптеры лончпадов
│   ├── base.py            # Launchpad ABC (ws, parse, tx, curve, graduation, antiscam)
│   └── pumpfun.py         # PumpFun — bonding curve, tx, ws, ws_subscribe_trades
│
├── filters/               # Двухфазная фильтрация + скоринг
│   ├── base.py            # Filter ABC + FilterResult
│   ├── authority.py       # AuthorityFilter [precheck]
│   ├── creator.py         # CreatorFilter [precheck]
│   ├── bundled_buy.py     # BundledBuyFilter [observation]
│   ├── observation.py     # ObservationFilter [observation]
│   └── scorer.py          # Scorer — precheck_filters + observation_filters
│
└── pulse/                 # Пульсовый мониторинг
    ├── monitor.py         # PulseMonitor — stream-based, last_seen курсор
    └── exit_manager.py    # ExitManager — частичные выходы, moonbag, стопы
```

## Модули

| Модуль | Назначение |
|--------|-----------|
| **Clock** | Абстракция времени. `RealClock` для live, `SimulatedClock` для мгновенного бэктеста |
| **Sources** | `BacktestSource` (SQLite + SimulatedClock), `LiveSource` (WebSocket + RPC). stream_trades — без polling |
| **Launchpads** | Адаптеры лончпадов с ws, curve, graduation, antiscam. PumpFun сейчас, остальные потом |
| **Filters + Scorer** | Двухфазная фильтрация: precheck (мгновенная) + observation (после наблюдения) |
| **Portfolio** | Reserve/commit/release баланса. Персистентность в SQLite. Восстановление при рестарте |
| **Execution** | `ExecutionWorker` (bounded concurrency), `SimulatedExecution` / `LiveExecution`. Команды buy/sell с явными количествами |
| **Pulse Monitor** | Stream-based мониторинг, каждый трейд ровно раз. Тренд (rising/stable/declining) |
| **Exit Manager** | Жёсткие (creator dump, pulse dead, whale exit) и частичные (profit, weak pulse) выходы, moon bag 10% |
| **Engine** | `PulseBot` — event loop + execution worker + bounded concurrency через Semaphore |

### Лончпады

| Платформа | Bonding Curve | Graduation |
|-----------|---------------|------------|
| Pump.fun | Экспоненциальная | 85 SOL → PumpSwap |
| LetsBonk | Экспоненциальная | → Raydium |
| Believe | Динамическая (анти-снайпер) | → Jupiter |
| LaunchLab | Линейная / Экспоненциальная / Логарифмическая | → Raydium |

## Стек технологий

| Что | Чем |
|-----|-----|
| Язык | Python 3.10+ |
| Async | asyncio + aiohttp + websockets |
| Solana SDK | solders (keypair, tx) + solana-py (RPC) |
| БД | SQLite (sqlite3, без ORM) |
| Конфиг | dataclass с пресетами (Config) |
| Логирование | structlog (JSON логи) |
| RPC | Helius free tier |
| Исторические данные | Bitquery GraphQL |

## Быстрый старт

### Требования

- Python 3.10+
- Helius API ключ (бесплатный tier)
- Solana кошелёк для бота (отдельный, не основной)

### Установка

```bash
git clone <repo-url>
cd gg
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### Запуск

```bash
# 1. Собрать исторические данные
python main.py collect --days 7

# 2. Бэктест с дефолтными настройками
python main.py backtest

# 3. Бэктест с агрессивным пресетом
python main.py backtest --preset aggressive

# 4. Grid search оптимизация
python main.py optimize

# 5. Paper trading (всё реально кроме транзакций)
python main.py paper

# 6. Боевой режим
python main.py live
```

## Режимы работы

Разница между режимами — две строки: `source` и `execution`.

| Режим | Source | Execution |
|-------|--------|-----------|
| `backtest` | BacktestSource (SQLite) | SimulatedExecution |
| `paper` | LiveSource (WebSocket) | SimulatedExecution |
| `live` | LiveSource (WebSocket) | LiveExecution |

**Backtest** — прогон стратегии на исторических данных из Bitquery. Grid search параметров.

**Paper trading** — реальные данные с лончпадов, виртуальные сделки. LiveSource пишет ВСЕ данные в SQLite — завтра прогоняешь вчерашний день через BacktestSource с новыми порогами.

**Live** — реальные транзакции на mainnet. Отдельный кошелёк, закидывать ровно столько сколько готов потерять.

## Конфигурация

Config — dataclass с пресетами:

| Пресет | observe_seconds | score_threshold | pulse_dead | hard_stop |
|--------|----------------|-----------------|------------|-----------|
| CONSERVATIVE | 60 | 30 | 0.15 | 0.30 |
| MODERATE | 45 | 20 | 0.10 | 0.50 |
| AGGRESSIVE | 30 | 10 | 0.05 | 0.70 |

Основные секции: бюджет, наблюдение, фильтры создателя, пульс, выходы, лончпад, инфра.

## Хранение данных

SQLite с таблицами:

- `tokens` — все обнаруженные токены с метаданными
- `trades` — все сделки по токенам (для бэктеста и наблюдения)
- `creators` — кеш создателей (досье, graduation rate, блеклист)
- `positions` — открытые позиции (восстанавливаются при рестарте)
- `fills` — все исполненные сделки с command_id и correlation_id
- `reservations` — pending buy orders (защита от race conditions)
- `balance` — текущий баланс и резервации
- `execution_attempts` — попытки исполнения (для отладки live)
- `event_log` — лог ВСЕХ событий с event_id и correlation_id

## Метрики успеха

Бот прибыльный если за неделю:

- **Win rate > 35%** (при асимметричной ставке достаточно)
- **Average win > 2x average loss**
- **Profit factor > 1.5**
- **Max drawdown < 50% депозита**
- **Токенов отфильтровано > 95%**
- **Slippage реальный vs модельный < 2%**

## Безопасность

- Отдельный кошелёк только для бота, никогда не основной
- Приватный ключ — из файла, не хардкод
- RPC ключ — через конфиг, не в коде
- Начальный депозит: 0.15 SOL (~$20)

## Документация

- [Техническая спецификация v3](./docs/SOLANA_PULSE_BOT_SPEC.md) — полное описание архитектуры, модулей, кода и алгоритмов
- [Python Style Guide](./PYTHON_STYLE_GUIDE.md) — стандарты кода

## Автор

**Sergey Chernyakov**

Telegram: [@AIBotsTech](https://t.me/AIBotsTech)
