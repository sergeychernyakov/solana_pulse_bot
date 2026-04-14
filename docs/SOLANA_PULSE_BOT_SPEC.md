# Solana Pulse Bot v3 — Техническая спецификация

## Концепция

Бот-наблюдатель для мемкоин-лончпадов на Solana. Не снайпер — не гонится за первым блоком. Наблюдает 30-90 секунд за новым токеном, оценивает органичность интереса, входит 3м-5м покупателем. Выходит не по фиксированному TP/SL, а по пульсу активности — когда покупатели иссякают.

Ассиметричная ставка: теряешь мало (bonding curve почти не сдвинулась), выигрываешь кратно (если токен полетел).

Один код — два режима: бэктест на исторических данных и боевая торговля.


---

## Архитектура

Event-driven архитектура: все модули общаются через типизированные события. Commands (намерения) отделены от Events (факты). Время абстрагировано через Clock — бэктест и live используют один и тот же код. Состояние персистентно — бот восстанавливается после падения.

```
                    ┌───────────┐
                    │  SOURCE   │ BacktestSource | LiveSource
                    │ (DataSource)
                    └─────┬─────┘
                          │ TokenEvent
                    ┌─────▼──────────┐
                    │ PRECHECK       │ Authority, Creator, Blacklist
                    │ FILTERS        │ hard_reject (без наблюдения)
                    └─────┬──────────┘
                          │ (passed)
                    ┌─────▼──────────┐
                    │ OBSERVATION    │ 30-90 сек сбор метрик
                    │ FILTERS        │ buyers, volume, diversity, bundled
                    └─────┬──────────┘
                          │ SignalEvent (score > threshold)
                    ┌─────▼──────────┐
                    │ PORTFOLIO      │ can_open? reserve() → PlaceBuyCommand
                    └─────┬──────────┘
                          │ PlaceBuyCommand
                    ┌─────▼──────────┐
                    │ EXECUTION      │ SimulatedExecution | LiveExecution
                    │ WORKER         │ (отдельная task, bounded concurrency)
                    └─────┬──────────┘
                          │ FillEvent
                    ┌─────▼──────────┐
                    │ PORTFOLIO      │ commit() position, update balance
                    │                │ persist to SQLite
                    └─────┬──────────┘
                          │
                    ┌─────▼──────────┐
                    │ PULSE MONITOR  │ stream-based (no polling)
                    │ per-mint task  │ тренд пульса, частичные выходы
                    └─────┬──────────┘
                          │ PlaceSellCommand
                          │ → Execution Worker → FillEvent → Portfolio
                          │
              ┌───────────┴───────────┐
              │  EVENT BUS            │
              │  asyncio.Queue        │
              │  + Clock (Real|Sim)   │
              │  Все события логируются│
              │  с event_id +         │
              │  correlation_id       │
              └───────────────────────┘
```


---

## Файловая структура

```
pulse_bot/
├── __init__.py
├── models.py              # Все dataclass: Token, Trade, Position, PulseSnapshot, TradeResult
├── events.py              # Event ABC + TokenEvent, SignalEvent, FillEvent, PulseEvent
├── commands.py            # Command ABC + PlaceBuyCommand, PlaceSellCommand (намерения)
├── clock.py               # Clock ABC + RealClock + SimulatedClock
├── config.py              # Config dataclass + LaunchpadConfig + пресеты
├── db.py                  # SQLite: схема, CRUD, event_log, state persistence
├── portfolio.py           # Portfolio: баланс, позиции, reserve/commit/release
├── execution.py           # ExecutionHandler ABC + SimulatedExecution + LiveExecution
├── engine.py              # PulseBot: главный event loop + event bus
├── collector.py           # Bitquery → SQLite (сбор данных для бэктеста)
├── report.py              # P&L отчёт, метрики, топы/лузеры
├── main.py                # CLI: collect / backtest / paper / live
├── requirements.txt
├── README.md
│
├── sources/               # Источники данных
│   ├── __init__.py
│   ├── base.py            # DataSource ABC
│   ├── backtest.py        # BacktestSource(DataSource) — SQLite + SimulatedClock
│   └── live.py            # LiveSource(DataSource) — WebSocket + RPC + RealClock
│
├── launchpads/            # Адаптеры лончпадов
│   ├── __init__.py
│   ├── base.py            # Launchpad ABC (ws, parse, build_tx, curve, graduation)
│   └── pumpfun.py         # PumpFun(Launchpad) — bonding curve, tx, ws
│                          # Потом: letsbonk.py, believe.py, launchlab.py
│                          # Каждый адаптер может быть "толстым" — свои эвристики
│
├── filters/               # Фильтры (разделены на фазы)
│   ├── __init__.py
│   ├── base.py            # Filter ABC + FilterResult
│   ├── authority.py       # AuthorityFilter — mint/freeze, Token-2022 [precheck]
│   ├── creator.py         # CreatorFilter — история, возраст, блеклист [precheck]
│   ├── bundled_buy.py     # BundledBuyFilter — общий источник SOL [observation]
│   ├── observation.py     # ObservationFilter — buyers, volume SOL, diversity [observation]
│   └── scorer.py          # Scorer — двухфазный: precheck_filters + observation_filters
│
└── pulse/                 # Пульсовый мониторинг
    ├── __init__.py
    ├── monitor.py         # PulseMonitor — stream-based, last_seen_tx курсор
    └── exit_manager.py    # ExitManager — частичные выходы, moonbag, стопы
```

30 файлов, 4 пакета. Каждый пакет — отдельная зона ответственности. Новый лончпад = новый файл в `launchpads/`, но файл может быть толстым — свои формулы кривой, graduation logic, анти-скам эвристики.


---

## Модуль 1: EVENTS + COMMANDS — Типизированные сообщения

Сердце системы. **Events** — факты (что произошло). **Commands** — намерения (что нужно сделать). Разделение важно: Command может быть отклонён, Event — уже случился.

Каждое сообщение имеет `event_id` (уникальный) и `correlation_id` (связь с исходным TokenEvent для всей цепочки решений).

```python
# events.py — факты (что произошло)

from dataclasses import dataclass, field
from abc import ABC
import uuid


@dataclass
class Event(ABC):
    timestamp: float
    event_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    correlation_id: str = ""    # ID исходного TokenEvent — вся цепочка решений


@dataclass
class TokenEvent(Event):
    """Новый токен обнаружен на лончпаде."""
    token: Token
    launchpad: str              # 'pumpfun' | 'letsbonk' | ...

    def __post_init__(self):
        if not self.correlation_id:
            self.correlation_id = self.event_id  # Корень цепочки


@dataclass
class FilterEvent(Event):
    """Результат фильтрации (для лога, даже если отброшен)."""
    token: Token
    phase: str                  # 'precheck' | 'observation'
    passed: bool
    hard_rejected: bool
    score: int
    reasons: list[str]          # ["authority_ok +0", "creator_score +15", ...]


@dataclass
class SignalEvent(Event):
    """Сигнал на покупку — прошёл все фильтры, score > threshold."""
    token: Token
    score: int
    reasons: list[str]
    observation_data: dict      # Метрики наблюдения: unique_buyers, total_sol_in и т.д.


@dataclass
class FillEvent(Event):
    """Исполненная сделка — реальная или симулированная."""
    mint: str
    symbol: str
    side: str                   # 'buy' | 'sell'
    price: float
    quote_amount_sol: float     # Сколько SOL потрачено (buy) или получено (sell)
    base_amount_tokens: float   # Сколько токенов получено (buy) или продано (sell)
    fee_sol: float
    slippage_pct: float
    tx_sig: str | None          # None в бэктесте
    command_id: str = ""        # ID команды, которую исполнили


@dataclass
class PulseEvent(Event):
    """Обновление пульса позиции."""
    mint: str
    snapshot: 'PulseSnapshot'
    action: str                 # 'hold' | 'sell_partial' | 'sell_all'
    reason: str                 # 'pulse_dead' | 'creator_dump' | 'strong_profit' | ...
    sell_pct: float             # 0.0 (hold) | 0.3 (partial) | 1.0 (all)
```

```python
# commands.py — намерения (что нужно сделать)

from dataclasses import dataclass, field
from abc import ABC
import uuid


@dataclass
class Command(ABC):
    timestamp: float
    command_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    correlation_id: str = ""


@dataclass
class PlaceBuyCommand(Command):
    """Намерение купить — Portfolio зарезервировал средства."""
    mint: str
    symbol: str
    quote_amount_sol: float     # Сколько SOL потратить
    reason: str                 # 'signal_score_35'
    reservation_id: str = ""    # ID резервации в Portfolio


@dataclass
class PlaceSellCommand(Command):
    """Намерение продать — Portfolio рассчитал количество токенов."""
    mint: str
    symbol: str
    base_amount_tokens: float   # Сколько токенов продать (явно!)
    reason: str                 # 'pulse_dead' | 'partial_profit' | ...
```

Каждый Event и Command логируются в SQLite (`event_log`) с `event_id` и `correlation_id`. Бэктест может переиграть любую цепочку решений, отследив correlation_id от TokenEvent до финального FillEvent.


---

## Модуль 2: CLOCK — Абстракция времени

Ключевой модуль для тезиса "один код — два режима". Все модули получают время и sleep через Clock, а не через `time.time()` / `asyncio.sleep()` напрямую.

```python
# clock.py (корень пакета)

from abc import ABC, abstractmethod


class Clock(ABC):
    """Абстракция времени. RealClock для live, SimulatedClock для бэктеста."""

    @abstractmethod
    def now(self) -> float:
        """Текущее время (unix timestamp)."""
        ...

    @abstractmethod
    async def sleep(self, seconds: float) -> None:
        """Ожидание. В бэктесте — мгновенное, в live — реальное."""
        ...


class RealClock(Clock):
    """Живое время. Для paper и live режимов."""

    def now(self) -> float:
        return time.time()

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(seconds)


class SimulatedClock(Clock):
    """Симулированное время. Для бэктеста — детерминированный replay."""

    def __init__(self, start_ts: float):
        self._current_ts = start_ts

    def now(self) -> float:
        return self._current_ts

    async def sleep(self, seconds: float) -> None:
        self._current_ts += seconds
        # Мгновенный return — бэктест не ждёт реальные 45 секунд

    def advance_to(self, ts: float) -> None:
        """Перемотать время до конкретного момента (для replay по данным)."""
        self._current_ts = max(self._current_ts, ts)
```

**Правило:** ни один модуль не вызывает `time.time()` или `asyncio.sleep()` напрямую. Только `self.clock.now()` и `await self.clock.sleep()`. Это делает бэктест детерминированным и мгновенным.


---

## Модуль 3: SOURCES — Источники данных

Абстракция источника данных. Бэктест и прод — один интерфейс.

```python
# sources/base.py + sources/backtest.py + sources/live.py

class DataSource(ABC):
    """Абстрактный источник данных. Бэктест читает SQLite, прод — WebSocket."""

    clock: Clock               # Все источники используют Clock

    @abstractmethod
    async def stream_new_tokens(self) -> AsyncIterator[TokenEvent]:
        """Поток новых токенов."""
        ...

    @abstractmethod
    async def stream_trades(self, mint: str) -> AsyncIterator[Trade]:
        """Поток сделок токена (stream, не polling!)."""
        ...

    @abstractmethod
    async def get_trades(self, mint: str, from_ts: float, to_ts: float) -> list[Trade]:
        """Сделки токена за период (для observation window)."""
        ...

    @abstractmethod
    async def get_wallet_history(self, wallet: str, limit: int = 10) -> list[Transaction]:
        """Последние транзакции кошелька (для bundled buy detection)."""
        ...

    @abstractmethod
    async def get_token_info(self, mint: str) -> TokenInfo:
        """Mint/freeze authority, program type."""
        ...


class BacktestSource(DataSource):
    """Читает из SQLite. Эмулирует поток событий в хронологическом порядке."""

    def __init__(self, db_path: str, clock: SimulatedClock, start_ts: float, end_ts: float):
        self.db_path = db_path
        self.clock = clock          # SimulatedClock — время из данных
        self.start_ts = start_ts
        self.end_ts = end_ts

    async def stream_new_tokens(self) -> AsyncIterator[TokenEvent]:
        for token in iter_tokens_by_time(self.db_path, self.start_ts, self.end_ts):
            self.clock.advance_to(token.created_at)  # Перематываем время
            yield TokenEvent(timestamp=self.clock.now(), token=token, launchpad='pumpfun')

    async def stream_trades(self, mint: str) -> AsyncIterator[Trade]:
        """Replay трейдов из SQLite в хронологическом порядке."""
        for trade in iter_trades_by_mint(self.db_path, mint):
            self.clock.advance_to(trade.timestamp)
            yield trade

    async def get_trades(self, mint, from_ts, to_ts) -> list[Trade]:
        return get_trades_window(self.db_path, mint, from_ts, to_ts)

    # ... остальные методы из SQLite


class LiveSource(DataSource):
    """WebSocket + RPC. Пишет каждый ивент в SQLite для будущих бэктестов."""

    def __init__(self, launchpad: 'Launchpad', clock: RealClock, rpc_url: str, db_path: str):
        self.launchpad = launchpad
        self.clock = clock          # RealClock — реальное время
        self.rpc_url = rpc_url
        self.db_path = db_path      # Записываем всё для реплея

    async def stream_new_tokens(self) -> AsyncIterator[TokenEvent]:
        async for raw in self.launchpad.ws_subscribe():
            token = self.launchpad.parse_create_event(raw)
            insert_token(self.db_path, token)       # Сохраняем для бэктеста
            yield TokenEvent(timestamp=self.clock.now(), token=token, launchpad=self.launchpad.name)

    async def stream_trades(self, mint: str) -> AsyncIterator[Trade]:
        """Подписка на WebSocket трейды — реальный stream."""
        async for raw in self.launchpad.ws_subscribe_trades(mint):
            trade = self.launchpad.parse_trade_event(raw)
            insert_trade(self.db_path, trade)       # Сохраняем для бэктеста
            yield trade

    async def get_trades(self, mint, from_ts, to_ts) -> list[Trade]:
        # Сначала проверяем кеш в SQLite, потом RPC
        ...
```

Ключевое: `LiveSource` пишет ВСЕ данные в SQLite. Завтра прогоняешь вчерашний день через `BacktestSource` с новыми порогами. Бесплатный бэктест из реальных данных.


---

## Модуль 4: LAUNCHPAD — Адаптеры лончпадов

```python
# launchpads/base.py + launchpads/pumpfun.py

class Launchpad(ABC):
    """Абстракция лончпада. Сейчас PumpFun, потом LetsBonk, Believe, LaunchLab."""

    name: str
    program_id: str
    fee_pct: float

    @abstractmethod
    async def ws_subscribe(self) -> AsyncIterator[dict]:
        """WebSocket подписка на новые токены."""
        ...

    @abstractmethod
    async def ws_subscribe_trades(self, mint: str) -> AsyncIterator[dict]:
        """WebSocket подписка на трейды конкретного токена (stream!)."""
        ...

    @abstractmethod
    def parse_create_event(self, raw: dict) -> Token:
        """Парсинг сырого события в Token."""
        ...

    @abstractmethod
    def parse_trade_event(self, raw: dict) -> Trade:
        """Парсинг сырого события в Trade."""
        ...

    @abstractmethod
    async def build_buy_tx(self, mint: str, amount_sol: float, keypair) -> Transaction:
        """Сформировать транзакцию покупки на bonding curve."""
        ...

    @abstractmethod
    async def build_sell_tx(self, mint: str, amount_tokens: float, keypair) -> Transaction:
        """Сформировать транзакцию продажи на bonding curve."""
        ...

    @abstractmethod
    def calculate_slippage(self, amount_sol: float, curve_state: dict) -> float:
        """Рассчитать slippage по формуле bonding curve."""
        ...

    @abstractmethod
    def get_graduation_threshold(self) -> float:
        """Порог graduation в SOL (специфичен для лончпада)."""
        ...

    @abstractmethod
    def get_antiscam_checks(self, token: Token, trades: list[Trade]) -> list[str]:
        """Специфичные для лончпада анти-скам проверки. Возвращает список warning reasons."""
        ...


class PumpFun(Launchpad):
    name = "pumpfun"
    program_id = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
    fee_pct = 0.01
    ws_url = "wss://pumpportal.fun/api/data"

    # Bonding curve: y = 1073000191 - 32190005730/(30+x)
    # x = SOL вложено, y = токенов получено
    # Экспоненциальная кривая — ранний вход дёшев

    async def ws_subscribe(self):
        async with websockets.connect(self.ws_url) as ws:
            await ws.send(json.dumps({"method": "subscribeNewToken"}))
            while True:
                msg = json.loads(await ws.recv())
                if msg.get("txType") == "create":
                    yield msg

    def parse_create_event(self, raw: dict) -> Token:
        return Token(
            mint=raw["mint"],
            name=raw.get("name", ""),
            symbol=raw.get("symbol", ""),
            creator=raw.get("traderPublicKey", ""),
            created_at=raw.get("timestamp", time.time()),
            uri=raw.get("uri", ""),
            launchpad="pumpfun",
        )

    # ... build_buy_tx, build_sell_tx, calculate_slippage


# Потом:
# class LetsBonk(Launchpad): ...
# class Believe(Launchpad): ...
```


---

## Модуль 5: FILTERS + SCORER — Двухфазная фильтрация и скоринг

Фильтры разделены на две фазы:
- **Precheck** (до наблюдения): authority, creator, blacklist — мгновенные, отсекают 90% мусора
- **Observation** (после 30-90 сек наблюдения): buyers, volume, diversity, bundled buy — требуют данных

```python
# filters/base.py + filters/scorer.py + filters/*.py

@dataclass
class FilterResult:
    score: int                  # Вклад в общий скор
    hard_reject: bool           # True = мгновенный отказ, score неважен
    reason: str                 # Человекочитаемая причина


class Filter(ABC):
    """Абстрактный фильтр. Каждый фильтр = отдельный класс, отдельный вес."""

    name: str
    weight: int                 # Макс. вклад в score (0 для hard-reject фильтров)
    enabled: bool = True

    @abstractmethod
    async def check(self, token: Token, context: dict) -> FilterResult:
        ...


class AuthorityFilter(Filter):
    """HARD REJECT: mint/freeze authority не отозван или Token-2022."""
    name = "authority"
    weight = 0

    async def check(self, token, context) -> FilterResult:
        info = context.get('token_info')
        if not info:
            return FilterResult(0, True, "no_token_info")
        if info.mint_authority is not None:
            return FilterResult(0, True, "mint_authority_active")
        if info.freeze_authority is not None:
            return FilterResult(0, True, "freeze_authority_active")
        if info.program == TOKEN_2022_PROGRAM:
            return FilterResult(0, True, "token_2022")
        return FilterResult(0, False, "authority_ok")


class CreatorFilter(Filter):
    """Мягкий скоринг по истории создателя + hard reject для блеклиста."""
    name = "creator"
    weight = 15

    async def check(self, token, context) -> FilterResult:
        stats = context.get('creator_stats')

        # Hard: блеклист
        if stats and stats.blacklisted:
            return FilterResult(0, True, "creator_blacklisted")

        # Hard: кошелёк моложе 24ч
        if stats and stats.wallet_age_hours < self.cfg.min_creator_wallet_age_hours:
            return FilterResult(0, True, f"wallet_age_{stats.wallet_age_hours:.0f}h")

        # Soft: серийный скамер (>50 токенов, <1% graduation)
        if stats and stats.total_tokens > 50:
            if stats.graduation_rate < self.cfg.min_creator_graduation_rate:
                return FilterResult(-20, False, f"serial_scammer_{stats.graduation_rate:.3f}")
            if stats.graduation_rate > 0.05:
                return FilterResult(self.weight, False, f"serial_winner_{stats.graduation_rate:.3f}")

        return FilterResult(0, False, "creator_unknown")


class BundledBuyFilter(Filter):
    """HARD REJECT: первые покупатели получили SOL из одного источника."""
    name = "bundled_buy"
    weight = 0

    async def check(self, token, context) -> FilterResult:
        early_buyers = context.get('early_buyers', [])
        if len(early_buyers) < 3:
            return FilterResult(0, False, "too_few_buyers")

        # Проверяем источник SOL для первых 5 покупателей
        funding_sources = {}
        for buyer_wallet in early_buyers[:5]:
            history = await self.source.get_wallet_history(buyer_wallet, limit=10)
            for tx in history:
                if tx.is_sol_transfer_in and tx.age_hours < 24:
                    src = tx.from_wallet
                    funding_sources.setdefault(src, []).append(buyer_wallet)

        # 3+ покупателя из одного источника = bundled
        for src, buyers in funding_sources.items():
            if len(buyers) >= 3:
                return FilterResult(0, True, f"bundled_from_{src[:8]}")

        return FilterResult(0, False, "no_bundling")


class ObservationFilter(Filter):
    """Мягкий скоринг по метрикам наблюдения (45 сек окно)."""
    name = "observation"
    weight = 50  # Суммарный макс из подметрик ниже

    async def check(self, token, context) -> FilterResult:
        trades = context.get('observation_trades', [])
        score = 0
        reasons = []

        buys = [t for t in trades if t.side == 'buy']
        sells = [t for t in trades if t.side == 'sell']

        # --- Уникальные покупатели ---
        unique_buyers = set(t.wallet for t in buys)
        if len(unique_buyers) >= 10:
            score += 10; reasons.append(f"buyers_10+")
        elif len(unique_buyers) >= 5:
            score += 15; reasons.append(f"buyers_{len(unique_buyers)}")
        else:
            score -= 10; reasons.append(f"buyers_low_{len(unique_buyers)}")

        # --- Volume в SOL (не количество сделок!) ---
        total_sol = sum(t.amount_sol for t in buys)
        if total_sol > 2.0:
            score += 15; reasons.append(f"volume_{total_sol:.1f}sol")
        elif total_sol > 0.5:
            score += 5; reasons.append(f"volume_{total_sol:.1f}sol")
        else:
            score -= 5; reasons.append(f"volume_low_{total_sol:.2f}sol")

        # --- Разнообразие сумм (антибот) ---
        amounts = [round(t.amount_sol, 4) for t in buys]
        unique_amounts = len(set(amounts))
        if unique_amounts >= 4:
            score += 10; reasons.append(f"diverse_amounts")
        elif unique_amounts < 2 and len(amounts) > 3:
            score -= 15; reasons.append(f"uniform_amounts_bot")

        # --- Скорость кривой ---
        if trades:
            elapsed = trades[-1].timestamp - trades[0].timestamp
            curve = context.get('curve_progress', 0)
            if elapsed > 0 and curve > 0:
                speed = curve / elapsed
                if speed > 0.5:
                    score += 10; reasons.append(f"fast_curve")

        # --- Нет ранних продаж создателя ---
        creator_sells = [t for t in sells if t.is_creator]
        if creator_sells:
            return FilterResult(0, True, "creator_selling_early")

        # --- Один кит > 50% объёма ---
        if buys:
            max_buy = max(t.amount_sol for t in buys)
            if total_sol > 0 and max_buy / total_sol > 0.5:
                score -= 20; reasons.append(f"whale_{max_buy:.2f}sol")

        # --- Curve range ---
        curve = context.get('curve_progress', 0)
        if curve > 40:
            score -= 15; reasons.append(f"late_entry_{curve:.0f}pct")

        # --- Sell pressure ---
        if len(sells) > len(buys) * 0.5 and len(buys) > 3:
            score -= 10; reasons.append(f"sell_pressure")

        return FilterResult(score, False, " | ".join(reasons))


class Scorer:
    """Двухфазная фильтрация. Precheck → Observation (если прошёл precheck)."""

    def __init__(
        self,
        precheck_filters: list[Filter],     # authority, creator, blacklist
        observation_filters: list[Filter],   # bundled_buy, observation
        clock: Clock,
        threshold: int = 20,
    ):
        self.precheck = [f for f in precheck_filters if f.enabled]
        self.observation = [f for f in observation_filters if f.enabled]
        self.clock = clock
        self.threshold = threshold

    async def precheck(self, token: Token, context: dict) -> FilterEvent:
        """Фаза 1: мгновенная фильтрация (<50мс). До наблюдения."""
        return await self._run_filters(self.precheck, token, context, phase='precheck')

    async def evaluate(self, token: Token, context: dict) -> FilterEvent:
        """Фаза 2: полная оценка после наблюдения. Включает precheck + observation."""
        return await self._run_filters(
            self.precheck + self.observation, token, context, phase='observation',
        )

    async def _run_filters(
        self, filters: list[Filter], token: Token, context: dict, phase: str,
    ) -> FilterEvent:
        total_score = 0
        all_reasons = []

        for f in filters:
            result = await f.check(token, context)
            all_reasons.append(f"{f.name}: {result.reason} ({result.score:+d})")

            if result.hard_reject:
                return FilterEvent(
                    timestamp=self.clock.now(), token=token,
                    phase=phase, passed=False, hard_rejected=True,
                    score=0, reasons=all_reasons,
                )

            total_score += result.score

        passed = total_score >= self.threshold
        return FilterEvent(
            timestamp=self.clock.now(), token=token,
            phase=phase, passed=passed, hard_rejected=False,
            score=total_score, reasons=all_reasons,
        )
```


---

## Модуль 6: PULSE — Пульсовый мониторинг + ExitManager

**Важно:** PulseMonitor получает трейды через stream (`source.stream_trades(mint)`), а не через polling каждые 3 секунды. Это исключает дублирование трейдов и ложные сигналы. Каждый трейд обрабатывается ровно один раз.

```python
# pulse/monitor.py + pulse/exit_manager.py

@dataclass
class PulseSnapshot:
    buy_rate: float             # Доля покупок в окне (0.0 - 1.0)
    sell_rate: float
    new_wallet_rate: float      # Доля новых кошельков среди покупателей
    avg_buy_size_sol: float     # Средний размер покупки в SOL
    total_sol_in: float         # Объём покупок в SOL за окно
    creator_selling: bool
    whale_exit: bool            # Продажа > 1 SOL

    # ТРЕНД (сравнение с предыдущим окном)
    buy_rate_trend: str         # 'rising' | 'stable' | 'declining'
    buy_size_trend: str         # 'rising' | 'stable' | 'declining'
    trend_declining_count: int  # Сколько окон подряд declining
    trend_dying: bool           # declining >= 2 окна подряд

    curve_progress: float       # % заполнения bonding curve


class PulseMonitor:
    """Скользящее окно событий с трендовым анализом."""

    def __init__(self, window_size: int = 20, min_events: int = 5):
        self.window: deque[Trade] = deque(maxlen=window_size)
        self.seen_wallets: set[str] = set()
        self.prev_buy_rate: float | None = None
        self.prev_avg_buy_size: float | None = None
        self.trend_declining_count: int = 0

    def update(self, trade: Trade) -> PulseSnapshot | None:
        self.window.append(trade)
        if len(self.window) < self.min_events:
            return None

        buys = [t for t in self.window if t.side == 'buy']
        sells = [t for t in self.window if t.side == 'sell']

        buy_rate = len(buys) / len(self.window)
        sell_rate = len(sells) / len(self.window)

        # Новые кошельки
        new_wallets = set()
        for t in buys:
            if t.wallet not in self.seen_wallets:
                new_wallets.add(t.wallet)
        self.seen_wallets.update(t.wallet for t in self.window)
        new_wallet_rate = len(new_wallets) / max(len(buys), 1)

        # Средний размер в SOL
        avg_buy = sum(t.amount_sol for t in buys) / max(len(buys), 1)
        total_sol = sum(t.amount_sol for t in buys)

        # Тренд
        buy_rate_trend = self._trend(buy_rate, self.prev_buy_rate)
        buy_size_trend = self._trend(avg_buy, self.prev_avg_buy_size)

        if buy_rate_trend == 'declining':
            self.trend_declining_count += 1
        else:
            self.trend_declining_count = 0

        self.prev_buy_rate = buy_rate
        self.prev_avg_buy_size = avg_buy

        return PulseSnapshot(
            buy_rate=buy_rate,
            sell_rate=sell_rate,
            new_wallet_rate=new_wallet_rate,
            avg_buy_size_sol=avg_buy,
            total_sol_in=total_sol,
            creator_selling=any(t.is_creator and t.side == 'sell' for t in self.window),
            whale_exit=any(t.amount_sol > 1.0 and t.side == 'sell' for t in sells),
            buy_rate_trend=buy_rate_trend,
            buy_size_trend=buy_size_trend,
            trend_declining_count=self.trend_declining_count,
            trend_dying=(self.trend_declining_count >= 2),
            curve_progress=self.window[-1].curve_progress_pct if self.window else 0,
        )

    def _trend(self, current: float, previous: float | None) -> str:
        if previous is None:
            return 'stable'
        diff = (current - previous) / max(previous, 0.001)
        if diff > 0.15:
            return 'rising'
        if diff < -0.15:
            return 'declining'
        return 'stable'


class ExitManager:
    """Решает когда и сколько продавать на основе пульса."""

    def __init__(self, cfg: 'Config'):
        self.cfg = cfg
        self.remaining_pct: float = 1.0
        self.partial_exit_count: int = 0
        self.has_taken_profit: bool = False

    def decide(self, pulse: PulseSnapshot, pnl_pct: float, elapsed_sec: float) -> PulseEvent:

        # === ЖЁСТКИЕ ВЫХОДЫ — 100% ===

        if pulse.creator_selling:
            return self._sell_all("creator_dump")

        if pulse.buy_rate < self.cfg.pulse_dead_buy_rate:
            return self._sell_all("pulse_dead")

        if pulse.trend_dying:
            return self._sell_all("trend_dying")

        if pulse.sell_rate > pulse.buy_rate * self.cfg.pulse_sell_pressure_ratio:
            return self._sell_all("sell_pressure")

        if pulse.new_wallet_rate == 0 and len([...]) >= self.cfg.pulse_no_new_wallets_events:
            return self._sell_all("no_new_blood")

        if pulse.whale_exit:
            return self._sell_all("whale_exit")

        if pulse.curve_progress > self.cfg.pulse_near_graduation_pct:
            return self._sell_all("near_graduation")

        if pnl_pct < -self.cfg.hard_stop_loss_pct * 100:
            return self._sell_all("hard_sl")

        if elapsed_sec > self.cfg.max_hold_seconds:
            return self._sell_all("timeout")

        # === ЧАСТИЧНЫЕ ВЫХОДЫ ===

        available = self.remaining_pct - self.cfg.moonbag_pct
        if available <= 0.01:
            return self._hold()

        # Сильный профит → зафиксировать 30%
        if pnl_pct > self.cfg.partial_sell_profit_threshold * 100 and not self.has_taken_profit:
            sell_pct = min(self.cfg.partial_sell_on_profit_pct, available)
            self.has_taken_profit = True
            return self._sell_partial(sell_pct, "strong_profit")

        # Пульс слабеет + профит → продать 50%
        if pulse.buy_rate < self.cfg.pulse_weak_buy_rate and pnl_pct > 50:
            sell_pct = min(self.cfg.partial_sell_on_weak_pulse_pct, available)
            return self._sell_partial(sell_pct, "weak_pulse_profit")

        return self._hold()

    def _sell_all(self, reason: str) -> PulseEvent:
        pct = self.remaining_pct
        self.remaining_pct = 0
        return PulseEvent(
            timestamp=time.time(), mint="", snapshot=None,
            action="sell_all", reason=reason, sell_pct=pct,
        )

    def _sell_partial(self, pct: float, reason: str) -> PulseEvent:
        self.remaining_pct -= pct
        self.partial_exit_count += 1
        return PulseEvent(
            timestamp=time.time(), mint="", snapshot=None,
            action="sell_partial", reason=reason, sell_pct=pct,
        )

    def _hold(self) -> PulseEvent:
        return PulseEvent(
            timestamp=time.time(), mint="", snapshot=None,
            action="hold", reason="pulse_ok", sell_pct=0.0,
        )
```


---

## Модуль 7: PORTFOLIO — Управление позициями и балансом

Отдельный модуль для учёта баланса, позиций, ордеров. Не размазано по engine.

**Ключевые отличия от v2:**
- **Reserve/commit/release** — при создании buy-команды средства резервируются, при fill — коммитятся, при fail — возвращаются. Защита от race conditions при параллельных SignalEvent.
- **Персистентность** — каждое изменение состояния пишется в SQLite. При рестарте Portfolio восстанавливается из БД.
- **Явные количества** — buy команда содержит `quote_amount_sol`, sell команда содержит `base_amount_tokens`. Никаких неявных пересчётов.

```python
# portfolio.py (корень пакета)

class Portfolio:
    def __init__(self, db: 'Database', clock: Clock, initial_balance: float,
                 max_positions: int, buy_amount: float):
        self.db = db
        self.clock = clock
        self.balance: float = initial_balance
        self.reserved: float = 0.0              # Зарезервировано под pending buy orders
        self.positions: dict[str, Position] = {}
        self.history: list[TradeResult] = []
        self.max_positions = max_positions
        self.buy_amount = buy_amount

    @classmethod
    def restore(cls, db: 'Database', clock: Clock, cfg: 'Config') -> 'Portfolio':
        """Восстановление из SQLite после рестарта. Читает open positions, pending orders, balance."""
        portfolio = cls(db, clock, cfg.initial_balance_sol, cfg.max_open_positions, cfg.buy_amount_sol)
        portfolio.positions = db.load_open_positions()
        portfolio.balance = db.load_balance()
        portfolio.reserved = db.load_reserved()
        portfolio.history = db.load_trade_history()
        return portfolio

    @property
    def available_balance(self) -> float:
        """Доступный баланс = общий - зарезервированный."""
        return self.balance - self.reserved

    def can_open(self) -> bool:
        """Есть бюджет и слот под новую позицию?"""
        return (
            len(self.positions) < self.max_positions
            and self.available_balance >= self.buy_amount
        )

    def create_buy_command(self, signal: SignalEvent) -> PlaceBuyCommand | None:
        """SignalEvent → PlaceBuyCommand. Резервирует средства атомарно."""
        if not self.can_open():
            return None
        if signal.token.mint in self.positions:
            return None

        # Reserve — деньги заблокированы до fill или fail
        reservation_id = uuid.uuid4().hex[:12]
        self.reserved += self.buy_amount
        self.db.save_reservation(reservation_id, self.buy_amount)

        return PlaceBuyCommand(
            timestamp=self.clock.now(),
            correlation_id=signal.correlation_id,
            mint=signal.token.mint,
            symbol=signal.token.symbol,
            quote_amount_sol=self.buy_amount,
            reason=f"signal_score_{signal.score}",
            reservation_id=reservation_id,
        )

    def on_buy_fill(self, fill: FillEvent, reservation_id: str):
        """Commit: обновить баланс и открыть позицию после покупки."""
        # Release reservation, deduct actual cost
        self.reserved -= self.buy_amount
        self.balance -= (fill.quote_amount_sol + fill.fee_sol)

        pos = Position(
            mint=fill.mint,
            symbol=fill.symbol,
            entry_price=fill.price,
            entry_time=fill.timestamp,
            amount_sol=fill.quote_amount_sol,
            amount_tokens=fill.base_amount_tokens,
            remaining_pct=1.0,
            total_received_sol=0.0,
        )
        self.positions[fill.mint] = pos

        # Persist
        self.db.save_position(pos)
        self.db.save_fill(fill)
        self.db.delete_reservation(reservation_id)
        self.db.save_balance(self.balance, self.reserved)

    def on_buy_fail(self, reservation_id: str):
        """Release: вернуть зарезервированные средства при ошибке исполнения."""
        self.reserved -= self.buy_amount
        self.db.delete_reservation(reservation_id)
        self.db.save_balance(self.balance, self.reserved)

    def create_sell_command(self, pulse_event: PulseEvent) -> PlaceSellCommand | None:
        """PulseEvent → PlaceSellCommand с явным количеством токенов."""
        pos = self.positions.get(pulse_event.mint)
        if not pos:
            return None
        sell_tokens = pos.amount_tokens * pulse_event.sell_pct * pos.remaining_pct
        return PlaceSellCommand(
            timestamp=self.clock.now(),
            correlation_id=pulse_event.correlation_id,
            mint=pulse_event.mint,
            symbol=pos.symbol,
            base_amount_tokens=sell_tokens,     # Явно! Не amount_sol
            reason=pulse_event.reason,
        )

    def on_sell_fill(self, fill: FillEvent):
        """Обновить баланс и позицию после продажи."""
        pos = self.positions.get(fill.mint)
        if not pos:
            return
        received = fill.quote_amount_sol - fill.fee_sol
        self.balance += received
        pos.total_received_sol += received
        sold_fraction = fill.base_amount_tokens / pos.amount_tokens
        pos.remaining_pct -= sold_fraction

        # Persist
        self.db.save_fill(fill)
        self.db.save_balance(self.balance, self.reserved)

        if pos.remaining_pct <= 0.01:
            result = TradeResult(
                mint=pos.mint, symbol=pos.symbol,
                entry_price=pos.entry_price, exit_price=fill.price,
                amount_sol=pos.amount_sol,
                pnl_sol=pos.total_received_sol - pos.amount_sol,
                pnl_pct=(pos.total_received_sol / pos.amount_sol - 1) * 100,
                entry_time=pos.entry_time, exit_time=fill.timestamp,
                hold_seconds=fill.timestamp - pos.entry_time,
                exit_reason=fill.command_id,
                partial_exits=0,
            )
            self.history.append(result)
            self.db.close_position(pos.mint, result)
            del self.positions[fill.mint]
        else:
            self.db.update_position(pos)
```


---

## Модуль 8: EXECUTION — Исполнение ордеров

**Ключевые отличия от v2:**
- Принимает `Command` (PlaceBuyCommand / PlaceSellCommand), а не OrderEvent
- Buy использует `quote_amount_sol`, sell использует `base_amount_tokens` — явные поля, без путаницы
- Execution Worker — отдельная `asyncio.Task` с `Semaphore`, не блокирует event processor
- Каждая попытка исполнения записывается в SQLite (`execution_attempts`)

```python
# execution.py (корень пакета)

class ExecutionHandler(ABC):
    @abstractmethod
    async def execute(self, command: Command) -> FillEvent:
        ...


class SimulatedExecution(ExecutionHandler):
    """Бэктест: исполняет мгновенно + модель slippage по bonding curve."""

    def __init__(self, source: DataSource, launchpad: Launchpad, clock: Clock):
        self.source = source
        self.launchpad = launchpad
        self.clock = clock

    async def execute(self, command: Command) -> FillEvent:
        if isinstance(command, PlaceBuyCommand):
            return await self._simulate_buy(command)
        elif isinstance(command, PlaceSellCommand):
            return await self._simulate_sell(command)

    async def _simulate_buy(self, cmd: PlaceBuyCommand) -> FillEvent:
        trades = await self.source.get_trades(cmd.mint, cmd.timestamp - 5, cmd.timestamp)
        price = trades[-1].price_sol if trades else 0
        slippage = self.launchpad.calculate_slippage(cmd.quote_amount_sol, ...)
        fill_price = price * (1 + slippage)
        fee = cmd.quote_amount_sol * self.launchpad.fee_pct
        tokens = cmd.quote_amount_sol / fill_price if fill_price > 0 else 0

        return FillEvent(
            timestamp=self.clock.now(),
            correlation_id=cmd.correlation_id,
            mint=cmd.mint, symbol=cmd.symbol, side='buy',
            price=fill_price,
            quote_amount_sol=cmd.quote_amount_sol,
            base_amount_tokens=tokens,
            fee_sol=fee, slippage_pct=slippage * 100,
            tx_sig=None, command_id=cmd.command_id,
        )

    async def _simulate_sell(self, cmd: PlaceSellCommand) -> FillEvent:
        trades = await self.source.get_trades(cmd.mint, cmd.timestamp - 5, cmd.timestamp)
        price = trades[-1].price_sol if trades else 0
        slippage = self.launchpad.calculate_slippage(cmd.base_amount_tokens * price, ...)
        fill_price = price * (1 - slippage)
        received_sol = cmd.base_amount_tokens * fill_price
        fee = received_sol * self.launchpad.fee_pct

        return FillEvent(
            timestamp=self.clock.now(),
            correlation_id=cmd.correlation_id,
            mint=cmd.mint, symbol=cmd.symbol, side='sell',
            price=fill_price,
            quote_amount_sol=received_sol,
            base_amount_tokens=cmd.base_amount_tokens,
            fee_sol=fee, slippage_pct=slippage * 100,
            tx_sig=None, command_id=cmd.command_id,
        )


class LiveExecution(ExecutionHandler):
    """Прод: реальная Solana транзакция."""

    def __init__(self, launchpad: Launchpad, clock: Clock, rpc_client, keypair, db: 'Database'):
        self.launchpad = launchpad
        self.clock = clock
        self.rpc = rpc_client
        self.keypair = keypair
        self.db = db

    async def execute(self, command: Command) -> FillEvent:
        if isinstance(command, PlaceBuyCommand):
            tx = await self.launchpad.build_buy_tx(
                command.mint, command.quote_amount_sol, self.keypair,
            )
        elif isinstance(command, PlaceSellCommand):
            tx = await self.launchpad.build_sell_tx(
                command.mint, command.base_amount_tokens, self.keypair,
            )

        # Отправка с retry, каждая попытка логируется
        for attempt in range(2):  # Max 2 попытки (не 3 — бережём время)
            self.db.log_execution_attempt(command.command_id, attempt)
            try:
                sig = await self.rpc.send_transaction(tx)
                result = await self.rpc.confirm_transaction(sig, timeout=30)
                break
            except Exception as e:
                if attempt == 1:
                    raise

        return FillEvent(
            timestamp=self.clock.now(),
            correlation_id=command.correlation_id,
            mint=command.mint, symbol=command.symbol,
            side='buy' if isinstance(command, PlaceBuyCommand) else 'sell',
            price=...,      # Из результата tx
            quote_amount_sol=...,
            base_amount_tokens=...,
            fee_sol=..., slippage_pct=...,
            tx_sig=str(sig), command_id=command.command_id,
        )


class ExecutionWorker:
    """Отдельный worker для исполнения команд. Не блокирует event processor."""

    def __init__(self, handler: ExecutionHandler, max_concurrent: int = 2):
        self.handler = handler
        self._semaphore = asyncio.Semaphore(max_concurrent)
        self._queue: asyncio.Queue[Command] = asyncio.Queue()

    async def submit(self, command: Command) -> None:
        """Положить команду в очередь исполнения."""
        await self._queue.put(command)

    async def run(self, on_fill: Callable, on_fail: Callable) -> None:
        """Основной цикл worker'а. Запускается как отдельная asyncio.Task."""
        while True:
            command = await self._queue.get()
            async with self._semaphore:
                try:
                    fill = await self.handler.execute(command)
                    await on_fill(fill, command)
                except Exception as e:
                    await on_fail(command, e)
```


---

## Модуль 9: ENGINE — Главный цикл

Event-driven цикл. Все модули общаются через `asyncio.Queue`.

**Ключевые отличия от v2:**
- Execution вынесен в отдельный `ExecutionWorker` — не блокирует event processor
- Token handler ограничен `Semaphore` — bounded concurrency, не взрывает память
- Pulse monitor использует `stream_trades()` — stream вместо polling, каждый трейд ровно раз
- Все time-зависимые операции через `self.clock` — бэктест детерминированный

```python
# engine.py (корень пакета)

class PulseBot:
    def __init__(
        self,
        source: DataSource,
        launchpad: Launchpad,
        scorer: Scorer,
        portfolio: Portfolio,
        exec_worker: ExecutionWorker,
        clock: Clock,
        config: Config,
        db: 'Database',
    ):
        self.source = source
        self.launchpad = launchpad
        self.scorer = scorer
        self.portfolio = portfolio
        self.exec_worker = exec_worker
        self.clock = clock
        self.cfg = config
        self.db = db
        self.event_queue: asyncio.Queue = asyncio.Queue()
        self._token_semaphore = asyncio.Semaphore(10)  # Max 10 токенов одновременно

    async def run(self):
        """Запуск: слушатель + обработчик + execution worker параллельно."""
        await asyncio.gather(
            self._token_listener(),
            self._event_processor(),
            self.exec_worker.run(
                on_fill=self._on_fill,
                on_fail=self._on_execution_fail,
            ),
        )

    async def _token_listener(self):
        """Слушает новые токены → кидает в очередь."""
        async for token_event in self.source.stream_new_tokens():
            self.db.log_event(token_event)
            await self.event_queue.put(token_event)

    async def _event_processor(self):
        """Обрабатывает события. НЕ исполняет ордера — это делает ExecutionWorker."""
        while True:
            event = await self.event_queue.get()
            self.db.log_event(event)

            if isinstance(event, TokenEvent):
                # Bounded concurrency через semaphore
                asyncio.create_task(self._handle_token_bounded(event))

            elif isinstance(event, SignalEvent):
                command = self.portfolio.create_buy_command(event)
                if command:
                    await self.exec_worker.submit(command)  # Не блокирует!

            elif isinstance(event, PulseEvent):
                if event.action in ('sell_all', 'sell_partial'):
                    command = self.portfolio.create_sell_command(event)
                    if command:
                        await self.exec_worker.submit(command)

    async def _on_fill(self, fill: FillEvent, command: Command):
        """Callback от ExecutionWorker при успешном исполнении."""
        self.db.log_event(fill)
        if fill.side == 'buy':
            self.portfolio.on_buy_fill(fill, command.reservation_id)
            asyncio.create_task(self._monitor_pulse(fill.mint, fill.correlation_id))
        else:
            self.portfolio.on_sell_fill(fill)

    async def _on_execution_fail(self, command: Command, error: Exception):
        """Callback от ExecutionWorker при ошибке — возвращаем резервацию."""
        if isinstance(command, PlaceBuyCommand):
            self.portfolio.on_buy_fail(command.reservation_id)
        structlog.get_logger().error("execution_failed", command=command, error=str(error))

    async def _handle_token_bounded(self, token_event: TokenEvent):
        """Обработка токена с bounded concurrency."""
        async with self._token_semaphore:
            await self._handle_token(token_event)

    async def _handle_token(self, token_event: TokenEvent):
        """Обработка нового токена: precheck → наблюдение → полная оценка → signal."""
        token = token_event.token
        corr_id = token_event.correlation_id

        # Собираем контекст для precheck фильтров
        context = {}
        context['token_info'] = await self.source.get_token_info(token.mint)
        context['creator_stats'] = self.db.get_creator_stats(token.creator)

        # Фаза 1: Precheck (мгновенная, <50мс)
        precheck_result = await self.scorer.precheck(token, context)
        precheck_result.correlation_id = corr_id
        await self.event_queue.put(precheck_result)
        if precheck_result.hard_rejected:
            return

        # Наблюдение (30-90 сек) — через Clock, не asyncio.sleep!
        await self.clock.sleep(self.cfg.observe_seconds)

        observation_trades = await self.source.get_trades(
            token.mint,
            token.created_at,
            token.created_at + self.cfg.observe_seconds,
        )
        context['observation_trades'] = observation_trades
        context['early_buyers'] = list(set(
            t.wallet for t in observation_trades if t.side == 'buy'
        ))[:5]
        if observation_trades:
            context['curve_progress'] = observation_trades[-1].curve_progress_pct

        # Фаза 2: Полная фильтрация (precheck + observation)
        full_result = await self.scorer.evaluate(token, context)
        full_result.correlation_id = corr_id
        await self.event_queue.put(full_result)

        if full_result.passed:
            await self.event_queue.put(SignalEvent(
                timestamp=self.clock.now(),
                correlation_id=corr_id,
                token=token,
                score=full_result.score,
                reasons=full_result.reasons,
                observation_data={
                    'unique_buyers': len(context['early_buyers']),
                    'total_sol': sum(t.amount_sol for t in observation_trades if t.side == 'buy'),
                },
            ))

    async def _monitor_pulse(self, mint: str, correlation_id: str):
        """Мониторинг пульса: stream-based, каждый трейд ровно один раз."""
        pos = self.portfolio.positions.get(mint)
        if not pos:
            return

        monitor = PulseMonitor(
            window_size=self.cfg.pulse_window_size,
            min_events=self.cfg.pulse_min_events,
        )
        exit_mgr = ExitManager(self.cfg)

        # Stream — не polling! Каждый трейд обрабатывается ровно один раз.
        async for trade in self.source.stream_trades(mint):
            if mint not in self.portfolio.positions:
                break

            snapshot = monitor.update(trade)
            if not snapshot:
                continue

            pnl_pct = (trade.price_sol / pos.entry_price - 1) * 100
            elapsed = self.clock.now() - pos.entry_time

            pulse_event = exit_mgr.decide(snapshot, pnl_pct, elapsed)
            pulse_event.mint = mint
            pulse_event.snapshot = snapshot
            pulse_event.correlation_id = correlation_id

            if pulse_event.action != 'hold':
                await self.event_queue.put(pulse_event)
                if pulse_event.action == 'sell_all':
                    return
```


---

## Модуль 10: CONFIG

```python
# config.py (корень пакета)

@dataclass
class LaunchpadConfig:
    name: str
    program_id: str
    ws_url: str
    fee_pct: float = 0.01
    curve_type: str = "exponential"     # exponential | linear | logarithmic

PUMPFUN_CONFIG = LaunchpadConfig(
    name="pumpfun",
    program_id="6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P",
    ws_url="wss://pumpportal.fun/api/data",
    fee_pct=0.01,
    curve_type="exponential",
)

@dataclass
class Config:
    # === БЮДЖЕТ ===
    initial_balance_sol: float = 0.15
    buy_amount_sol: float = 0.03
    max_open_positions: int = 3

    # === НАБЛЮДЕНИЕ ===
    observe_seconds: int = 45
    score_threshold: int = 20

    # === ФИЛЬТРЫ СОЗДАТЕЛЯ ===
    min_creator_wallet_age_hours: int = 24
    min_creator_graduation_rate: float = 0.01
    max_creator_dead_tokens: int = 10
    require_socials: bool = False

    # === ПУЛЬС ===
    pulse_window_size: int = 20
    pulse_min_events: int = 5
    pulse_dead_buy_rate: float = 0.10
    pulse_weak_buy_rate: float = 0.30
    pulse_sell_pressure_ratio: float = 2.0
    pulse_no_new_wallets_events: int = 5
    pulse_near_graduation_pct: float = 70.0

    # === ВЫХОДЫ ===
    partial_sell_on_weak_pulse_pct: float = 0.50
    partial_sell_on_profit_pct: float = 0.30
    partial_sell_profit_threshold: float = 2.0
    moonbag_pct: float = 0.10
    hard_stop_loss_pct: float = 0.50
    max_hold_seconds: int = 7200

    # === ЛОНЧПАД ===
    launchpad: LaunchpadConfig = field(default_factory=lambda: PUMPFUN_CONFIG)

    # === ИНФРА ===
    rpc_url: str = ""
    db_path: str = "pulse_bot.db"
    log_level: str = "INFO"

# Пресеты
CONSERVATIVE = Config(observe_seconds=60, score_threshold=30, pulse_dead_buy_rate=0.15, hard_stop_loss_pct=0.30)
MODERATE = Config()
AGGRESSIVE = Config(observe_seconds=30, score_threshold=10, pulse_dead_buy_rate=0.05, hard_stop_loss_pct=0.70)
```


---

## SQLite схема

**Отличия от v2:** добавлены `positions`, `fills`, `reservations`, `execution_attempts` для персистентного состояния. Бот восстанавливается после рестарта. Event log расширен `event_id` и `correlation_id`.

```sql
-- Токены (данные для бэктеста)
CREATE TABLE tokens (
    mint TEXT PRIMARY KEY,
    name TEXT, symbol TEXT, creator TEXT,
    created_at REAL, uri TEXT, launchpad TEXT,
    mint_authority_revoked INTEGER,
    freeze_authority_revoked INTEGER,
    graduated INTEGER DEFAULT 0
);

-- Сделки (данные для бэктеста и observation)
CREATE TABLE trades (
    tx_sig TEXT PRIMARY KEY,
    mint TEXT, wallet TEXT, side TEXT,
    amount_sol REAL, amount_tokens REAL,
    price_sol REAL, curve_progress_pct REAL,
    timestamp REAL, is_creator INTEGER
);

-- Кеш создателей
CREATE TABLE creators (
    wallet TEXT PRIMARY KEY,
    total_tokens INTEGER, graduated_tokens INTEGER,
    graduation_rate REAL, wallet_age_hours REAL,
    blacklisted INTEGER DEFAULT 0
);

-- === ПЕРСИСТЕНТНОЕ СОСТОЯНИЕ (новое в v3) ===

-- Открытые позиции (восстанавливаются при рестарте)
CREATE TABLE positions (
    mint TEXT PRIMARY KEY,
    symbol TEXT,
    entry_price REAL,
    entry_time REAL,
    amount_sol REAL,
    amount_tokens REAL,
    remaining_pct REAL DEFAULT 1.0,
    total_received_sol REAL DEFAULT 0.0,
    correlation_id TEXT,
    status TEXT DEFAULT 'open'        -- 'open' | 'closed'
);

-- Все исполненные сделки
CREATE TABLE fills (
    fill_id TEXT PRIMARY KEY,         -- event_id из FillEvent
    mint TEXT, symbol TEXT, side TEXT,
    price REAL,
    quote_amount_sol REAL,
    base_amount_tokens REAL,
    fee_sol REAL, slippage_pct REAL,
    tx_sig TEXT,
    command_id TEXT,                   -- ID команды
    correlation_id TEXT,              -- Цепочка от TokenEvent
    timestamp REAL
);

-- Резервации баланса (pending buy orders)
CREATE TABLE reservations (
    reservation_id TEXT PRIMARY KEY,
    amount_sol REAL,
    created_at REAL
);

-- Попытки исполнения (для отладки live)
CREATE TABLE execution_attempts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    command_id TEXT,
    attempt INTEGER,
    status TEXT,                       -- 'pending' | 'success' | 'failed'
    error TEXT,
    timestamp REAL
);

-- Баланс (восстанавливается при рестарте)
CREATE TABLE balance (
    id INTEGER PRIMARY KEY CHECK (id = 1),  -- Только одна строка
    balance_sol REAL,
    reserved_sol REAL,
    updated_at REAL
);

-- Лог ВСЕХ событий (для реплея и отладки)
CREATE TABLE event_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT,                     -- Уникальный ID события
    correlation_id TEXT,               -- Цепочка от TokenEvent
    event_type TEXT,                   -- 'token' | 'filter' | 'signal' | 'command' | 'fill' | 'pulse'
    data TEXT,                         -- JSON сериализованный Event/Command
    timestamp REAL
);

-- Индексы
CREATE INDEX idx_trades_mint_ts ON trades(mint, timestamp);
CREATE INDEX idx_tokens_created ON tokens(created_at);
CREATE INDEX idx_events_ts ON event_log(timestamp);
CREATE INDEX idx_events_corr ON event_log(correlation_id);
CREATE INDEX idx_fills_mint ON fills(mint, timestamp);
CREATE INDEX idx_positions_status ON positions(status);
```


---

## CLI и режимы

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

Разница между режимами — Clock, Source, Execution:

```python
if mode == 'backtest':
    clock = SimulatedClock(start_ts=start)
    source = BacktestSource(db_path, clock, start, end)
    execution = SimulatedExecution(source, launchpad, clock)
elif mode in ('live', 'paper'):
    clock = RealClock()
    source = LiveSource(launchpad, clock, rpc_url, db_path)
    execution = (
        LiveExecution(launchpad, clock, rpc, keypair, db)
        if mode == 'live'
        else SimulatedExecution(source, launchpad, clock)
    )

# Portfolio восстанавливается из SQLite при рестарте
portfolio = Portfolio.restore(db, clock, cfg) if mode != 'backtest' else Portfolio(db, clock, ...)

exec_worker = ExecutionWorker(execution, max_concurrent=2)
bot = PulseBot(source=source, exec_worker=exec_worker, clock=clock, portfolio=portfolio, ...)
asyncio.run(bot.run())
```


---

## План разработки

```
НЕДЕЛЯ 1 — ФУНДАМЕНТ:
  Вечер 1: models.py + events.py + commands.py + config.py
           Все dataclass: Token, Trade, Position, PulseSnapshot, TradeResult
           Events: TokenEvent, FilterEvent, SignalEvent, FillEvent, PulseEvent
           Commands: PlaceBuyCommand, PlaceSellCommand
           Config + LaunchpadConfig + пресеты

  Вечер 2: clock.py + db.py + sources/base.py + sources/backtest.py
           Clock ABC + RealClock + SimulatedClock
           SQLite схема (tokens, trades, creators, positions, fills, event_log, balance)
           DataSource ABC + BacktestSource (чтение из SQLite + SimulatedClock)

  Вечер 3: launchpads/base.py + launchpads/pumpfun.py
           Launchpad ABC (ws, parse, build_tx, curve, graduation, antiscam)
           PumpFun (парсинг create/trade, bonding curve формула, ws_subscribe_trades)

  Вечер 4: collector.py
           Bitquery GraphQL → SQLite (токены + сделки за 7 дней)
           Запуск сбора данных

НЕДЕЛЯ 2 — МОЗГИ:
  Вечер 1: filters/base.py + filters/authority.py + filters/creator.py
           Filter ABC + FilterResult + AuthorityFilter + CreatorFilter

  Вечер 2: filters/bundled_buy.py + filters/observation.py + filters/scorer.py
           BundledBuyFilter + ObservationFilter (volume SOL, unique buyers, diversity)
           Scorer (двухфазный: precheck_filters + observation_filters)

  Вечер 3: pulse/monitor.py + pulse/exit_manager.py
           PulseMonitor (stream-based, last_seen курсор, тренд declining/rising/stable)
           ExitManager (частичные выходы + moonbag)

  Вечер 4: portfolio.py + execution.py
           Portfolio (reserve/commit/release, персистентность, restore)
           ExecutionHandler ABC + SimulatedExecution + ExecutionWorker

НЕДЕЛЯ 3 — СБОРКА:
  Вечер 1: engine.py
           PulseBot (event loop, execution worker, bounded concurrency)
           Склейка всех модулей через Clock

  Вечер 2: report.py + main.py
           P&L отчёт (win rate, profit factor, drawdown, exit reasons)
           CLI: collect / backtest / paper / live

  Вечер 3: Первый бэктест
           Прогон на данных за 7 дней. Анализ результатов.
           Подкрутка порогов скоринга.

  Вечер 4: Grid search
           Перебор: observe_seconds × score_threshold × pulse_dead × hard_sl
           Найти лучшую комбинацию параметров.

НЕДЕЛЯ 4 — ПРОД:
  Вечер 1: sources/live.py
           WebSocket подключение к Pump.fun + stream_trades
           Запись всех данных в SQLite (для будущих бэктестов)

  Вечер 2: execution.py (LiveExecution)
           Реальные buy/sell транзакции на devnet
           Тест на devnet (фейковые SOL)

  Вечер 3: Paper trading mainnet
           LiveSource + SimulatedExecution + Portfolio.restore()
           Реальные данные, виртуальные сделки, восстановление при рестарте

  Вечер 4: Боевой режим
           Live с 0.15 SOL (~$20)
           Мониторинг, алерты

ПОТОМ:
  Неделя 5: LetsBonk (launchpads/letsbonk.py — свои эвристики, graduation)
  Неделя 6: Believe (launchpads/believe.py — динамическая кривая, анти-снайпер)
  Неделя 7: Smart Wallets модуль (отдельный Source или доп. listener)
  Неделя 8: Telegram алерты (уведомления о сделках)
```


---

## Метрики успеха

Бот прибыльный если за неделю:

- **Win rate > 35%** (при ассиметричной ставке этого достаточно)
- **Average win > 2× average loss** (ассиметрия)
- **Profit factor > 1.5** (gross profit / gross loss)
- **Max drawdown < 50% депозита**
- **Токенов отфильтровано > 95%** (покупаем < 5% увиденного)
- **Slippage реальный vs модельный < 2%** (валидация SimulatedExecution)

Если метрики не выполняются после 2 недель paper trading — менять пороги или стратегию. Не лить живые деньги в убыточную систему.
