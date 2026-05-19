# Solana Pulse Bot v3 — Technical Specification

## Concept

An observer bot for memecoin launchpads on Solana. Not a sniper — does not race for the first block. Observes a new token for 30-90 seconds, evaluates the organicity of interest, enters as the 3rd-5th buyer. Exits not on a fixed TP/SL, but on activity pulse — when buyers dry up.

Asymmetric bet: lose little (bonding curve has barely moved), win multiples (if the token takes off).

One codebase — two modes: backtest on historical data and live trading.


---

## Architecture

Event-driven architecture: all modules communicate through typed events. Commands (intentions) are separated from Events (facts). Time is abstracted through Clock — backtest and live use the same code. State is persistent — the bot recovers after a crash.

```
                    ┌───────────┐
                    │  SOURCE   │ BacktestSource | LiveSource
                    │ (DataSource)
                    └─────┬─────┘
                          │ TokenEvent
                    ┌─────▼──────────┐
                    │ PRECHECK       │ Authority, Creator, Blacklist
                    │ FILTERS        │ hard_reject (no observation)
                    └─────┬──────────┘
                          │ (passed)
                    ┌─────▼──────────┐
                    │ OBSERVATION    │ 30-90 sec metric collection
                    │ FILTERS        │ buyers, volume, diversity, bundled
                    └─────┬──────────┘
                          │ SignalEvent (score > threshold)
                    ┌─────▼──────────┐
                    │ PORTFOLIO      │ can_open? reserve() → PlaceBuyCommand
                    └─────┬──────────┘
                          │ PlaceBuyCommand
                    ┌─────▼──────────┐
                    │ EXECUTION      │ SimulatedExecution | LiveExecution
                    │ WORKER         │ (separate task, bounded concurrency)
                    └─────┬──────────┘
                          │ FillEvent
                    ┌─────▼──────────┐
                    │ PORTFOLIO      │ commit() position, update balance
                    │                │ persist to SQLite
                    └─────┬──────────┘
                          │
                    ┌─────▼──────────┐
                    │ PULSE MONITOR  │ stream-based (no polling)
                    │ per-mint task  │ pulse trend, partial exits
                    └─────┬──────────┘
                          │ PlaceSellCommand
                          │ → Execution Worker → FillEvent → Portfolio
                          │
              ┌───────────┴───────────┐
              │  EVENT BUS            │
              │  asyncio.Queue        │
              │  + Clock (Real|Sim)   │
              │  All events logged    │
              │  with event_id +      │
              │  correlation_id       │
              └───────────────────────┘
```


---

## File structure

```
pulse_bot/
├── __init__.py
├── models.py              # All dataclasses: Token, Trade, Position, PulseSnapshot, TradeResult
├── events.py              # Event ABC + TokenEvent, SignalEvent, FillEvent, PulseEvent
├── commands.py            # Command ABC + PlaceBuyCommand, PlaceSellCommand (intentions)
├── clock.py               # Clock ABC + RealClock + SimulatedClock
├── config.py              # Config dataclass + LaunchpadConfig + presets
├── db.py                  # SQLite: schema, CRUD, event_log, state persistence
├── portfolio.py           # Portfolio: balance, positions, reserve/commit/release
├── execution.py           # ExecutionHandler ABC + SimulatedExecution + LiveExecution
├── engine.py              # PulseBot: main event loop + event bus
├── collector.py           # Bitquery → SQLite (data collection for backtest)
├── report.py              # P&L report, metrics, winners/losers
├── main.py                # CLI: collect / backtest / paper / live
├── requirements.txt
├── README.md
│
├── sources/               # Data sources
│   ├── __init__.py
│   ├── base.py            # DataSource ABC
│   ├── backtest.py        # BacktestSource(DataSource) — SQLite + SimulatedClock
│   └── live.py            # LiveSource(DataSource) — WebSocket + RPC + RealClock
│
├── launchpads/            # Launchpad adapters
│   ├── __init__.py
│   ├── base.py            # Launchpad ABC (ws, parse, build_tx, curve, graduation)
│   └── pumpfun.py         # PumpFun(Launchpad) — bonding curve, tx, ws
│                          # Later: letsbonk.py, believe.py, launchlab.py
│                          # Each adapter can be "thick" — its own heuristics
│
├── filters/               # Filters (split into phases)
│   ├── __init__.py
│   ├── base.py            # Filter ABC + FilterResult
│   ├── authority.py       # AuthorityFilter — mint/freeze, Token-2022 [precheck]
│   ├── creator.py         # CreatorFilter — history, age, blacklist [precheck]
│   ├── bundled_buy.py     # BundledBuyFilter — common SOL source [observation]
│   ├── observation.py     # ObservationFilter — buyers, volume SOL, diversity [observation]
│   └── scorer.py          # Scorer — two-phase: precheck_filters + observation_filters
│
└── pulse/                 # Pulse monitoring
    ├── __init__.py
    ├── monitor.py         # PulseMonitor — stream-based, last_seen_tx cursor
    └── exit_manager.py    # ExitManager — partial exits, moonbag, stops
```

30 files, 4 packages. Each package is a separate area of responsibility. A new launchpad = new file in `launchpads/`, but the file can be thick — its own curve formulas, graduation logic, anti-scam heuristics.


---

## Module 1: EVENTS + COMMANDS — Typed messages

The heart of the system. **Events** are facts (what happened). **Commands** are intentions (what needs to be done). The separation is important: a Command can be rejected, an Event has already occurred.

Each message has an `event_id` (unique) and a `correlation_id` (link to the originating TokenEvent for the entire decision chain).

```python
# events.py — facts (what happened)

from dataclasses import dataclass, field
from abc import ABC
import uuid


@dataclass
class Event(ABC):
    timestamp: float
    event_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    correlation_id: str = ""    # ID of the originating TokenEvent — full decision chain


@dataclass
class TokenEvent(Event):
    """New token detected on a launchpad."""
    token: Token
    launchpad: str              # 'pumpfun' | 'letsbonk' | ...

    def __post_init__(self):
        if not self.correlation_id:
            self.correlation_id = self.event_id  # Root of the chain


@dataclass
class FilterEvent(Event):
    """Filtering result (for the log, even if rejected)."""
    token: Token
    phase: str                  # 'precheck' | 'observation'
    passed: bool
    hard_rejected: bool
    score: int
    reasons: list[str]          # ["authority_ok +0", "creator_score +15", ...]


@dataclass
class SignalEvent(Event):
    """Buy signal — passed all filters, score > threshold."""
    token: Token
    score: int
    reasons: list[str]
    observation_data: dict      # Observation metrics: unique_buyers, total_sol_in, etc.


@dataclass
class FillEvent(Event):
    """Executed trade — real or simulated."""
    mint: str
    symbol: str
    side: str                   # 'buy' | 'sell'
    price: float
    quote_amount_sol: float     # SOL spent (buy) or received (sell)
    base_amount_tokens: float   # Tokens received (buy) or sold (sell)
    fee_sol: float
    slippage_pct: float
    tx_sig: str | None          # None in backtest
    command_id: str = ""        # ID of the command that was executed


@dataclass
class PulseEvent(Event):
    """Position pulse update."""
    mint: str
    snapshot: 'PulseSnapshot'
    action: str                 # 'hold' | 'sell_partial' | 'sell_all'
    reason: str                 # 'pulse_dead' | 'creator_dump' | 'strong_profit' | ...
    sell_pct: float             # 0.0 (hold) | 0.3 (partial) | 1.0 (all)
```

```python
# commands.py — intentions (what needs to be done)

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
    """Intention to buy — Portfolio has reserved funds."""
    mint: str
    symbol: str
    quote_amount_sol: float     # How much SOL to spend
    reason: str                 # 'signal_score_35'
    reservation_id: str = ""    # Reservation ID in Portfolio


@dataclass
class PlaceSellCommand(Command):
    """Intention to sell — Portfolio computed the token amount."""
    mint: str
    symbol: str
    base_amount_tokens: float   # How many tokens to sell (explicit!)
    reason: str                 # 'pulse_dead' | 'partial_profit' | ...
```

Every Event and Command is logged in SQLite (`event_log`) with `event_id` and `correlation_id`. The backtest can replay any decision chain by following correlation_id from TokenEvent to the final FillEvent.


---

## Module 2: CLOCK — Time abstraction

Key module for the "one codebase — two modes" thesis. All modules get time and sleep through Clock, not via `time.time()` / `asyncio.sleep()` directly.

```python
# clock.py (package root)

from abc import ABC, abstractmethod


class Clock(ABC):
    """Time abstraction. RealClock for live, SimulatedClock for backtest."""

    @abstractmethod
    def now(self) -> float:
        """Current time (unix timestamp)."""
        ...

    @abstractmethod
    async def sleep(self, seconds: float) -> None:
        """Wait. In backtest — instant, in live — real."""
        ...


class RealClock(Clock):
    """Live time. For paper and live modes."""

    def now(self) -> float:
        return time.time()

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(seconds)


class SimulatedClock(Clock):
    """Simulated time. For backtest — deterministic replay."""

    def __init__(self, start_ts: float):
        self._current_ts = start_ts

    def now(self) -> float:
        return self._current_ts

    async def sleep(self, seconds: float) -> None:
        self._current_ts += seconds
        # Instant return — backtest does not wait the real 45 seconds

    def advance_to(self, ts: float) -> None:
        """Fast-forward time to a specific moment (for data-driven replay)."""
        self._current_ts = max(self._current_ts, ts)
```

**Rule:** no module calls `time.time()` or `asyncio.sleep()` directly. Only `self.clock.now()` and `await self.clock.sleep()`. This makes the backtest deterministic and instant.


---

## Module 3: SOURCES — Data sources

Data source abstraction. Backtest and production share one interface.

```python
# sources/base.py + sources/backtest.py + sources/live.py

class DataSource(ABC):
    """Abstract data source. Backtest reads SQLite, production — WebSocket."""

    clock: Clock               # All sources use Clock

    @abstractmethod
    async def stream_new_tokens(self) -> AsyncIterator[TokenEvent]:
        """Stream of new tokens."""
        ...

    @abstractmethod
    async def stream_trades(self, mint: str) -> AsyncIterator[Trade]:
        """Stream of token trades (stream, not polling!)."""
        ...

    @abstractmethod
    async def get_trades(self, mint: str, from_ts: float, to_ts: float) -> list[Trade]:
        """Token trades for a period (for observation window)."""
        ...

    @abstractmethod
    async def get_wallet_history(self, wallet: str, limit: int = 10) -> list[Transaction]:
        """Recent wallet transactions (for bundled buy detection)."""
        ...

    @abstractmethod
    async def get_token_info(self, mint: str) -> TokenInfo:
        """Mint/freeze authority, program type."""
        ...


class BacktestSource(DataSource):
    """Reads from SQLite. Emulates the event stream in chronological order."""

    def __init__(self, db_path: str, clock: SimulatedClock, start_ts: float, end_ts: float):
        self.db_path = db_path
        self.clock = clock          # SimulatedClock — time from data
        self.start_ts = start_ts
        self.end_ts = end_ts

    async def stream_new_tokens(self) -> AsyncIterator[TokenEvent]:
        for token in iter_tokens_by_time(self.db_path, self.start_ts, self.end_ts):
            self.clock.advance_to(token.created_at)  # Fast-forward time
            yield TokenEvent(timestamp=self.clock.now(), token=token, launchpad='pumpfun')

    async def stream_trades(self, mint: str) -> AsyncIterator[Trade]:
        """Replay trades from SQLite in chronological order."""
        for trade in iter_trades_by_mint(self.db_path, mint):
            self.clock.advance_to(trade.timestamp)
            yield trade

    async def get_trades(self, mint, from_ts, to_ts) -> list[Trade]:
        return get_trades_window(self.db_path, mint, from_ts, to_ts)

    # ... other methods from SQLite


class LiveSource(DataSource):
    """WebSocket + RPC. Writes every event into SQLite for future backtests."""

    def __init__(self, launchpad: 'Launchpad', clock: RealClock, rpc_url: str, db_path: str):
        self.launchpad = launchpad
        self.clock = clock          # RealClock — real time
        self.rpc_url = rpc_url
        self.db_path = db_path      # Record everything for replay

    async def stream_new_tokens(self) -> AsyncIterator[TokenEvent]:
        async for raw in self.launchpad.ws_subscribe():
            token = self.launchpad.parse_create_event(raw)
            insert_token(self.db_path, token)       # Save for backtest
            yield TokenEvent(timestamp=self.clock.now(), token=token, launchpad=self.launchpad.name)

    async def stream_trades(self, mint: str) -> AsyncIterator[Trade]:
        """Subscribe to WebSocket trades — real stream."""
        async for raw in self.launchpad.ws_subscribe_trades(mint):
            trade = self.launchpad.parse_trade_event(raw)
            insert_trade(self.db_path, trade)       # Save for backtest
            yield trade

    async def get_trades(self, mint, from_ts, to_ts) -> list[Trade]:
        # First check the SQLite cache, then RPC
        ...
```

Key point: `LiveSource` writes ALL data into SQLite. Tomorrow you run yesterday's day through `BacktestSource` with new thresholds. Free backtest on real data.


---

## Module 4: LAUNCHPAD — Launchpad adapters

```python
# launchpads/base.py + launchpads/pumpfun.py

class Launchpad(ABC):
    """Launchpad abstraction. Currently PumpFun, later LetsBonk, Believe, LaunchLab."""

    name: str
    program_id: str
    fee_pct: float

    @abstractmethod
    async def ws_subscribe(self) -> AsyncIterator[dict]:
        """WebSocket subscription to new tokens."""
        ...

    @abstractmethod
    async def ws_subscribe_trades(self, mint: str) -> AsyncIterator[dict]:
        """WebSocket subscription to trades of a specific token (stream!)."""
        ...

    @abstractmethod
    def parse_create_event(self, raw: dict) -> Token:
        """Parse raw event into a Token."""
        ...

    @abstractmethod
    def parse_trade_event(self, raw: dict) -> Trade:
        """Parse raw event into a Trade."""
        ...

    @abstractmethod
    async def build_buy_tx(self, mint: str, amount_sol: float, keypair) -> Transaction:
        """Build a buy transaction on the bonding curve."""
        ...

    @abstractmethod
    async def build_sell_tx(self, mint: str, amount_tokens: float, keypair) -> Transaction:
        """Build a sell transaction on the bonding curve."""
        ...

    @abstractmethod
    def calculate_slippage(self, amount_sol: float, curve_state: dict) -> float:
        """Compute slippage using the bonding curve formula."""
        ...

    @abstractmethod
    def get_graduation_threshold(self) -> float:
        """Graduation threshold in SOL (launchpad-specific)."""
        ...

    @abstractmethod
    def get_antiscam_checks(self, token: Token, trades: list[Trade]) -> list[str]:
        """Launchpad-specific anti-scam checks. Returns a list of warning reasons."""
        ...


class PumpFun(Launchpad):
    name = "pumpfun"
    program_id = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
    fee_pct = 0.01
    ws_url = "wss://pumpportal.fun/api/data"

    # Bonding curve: y = 1073000191 - 32190005730/(30+x)
    # x = SOL invested, y = tokens received
    # Exponential curve — early entry is cheap

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


# Later:
# class LetsBonk(Launchpad): ...
# class Believe(Launchpad): ...
```


---

## Module 5: FILTERS + SCORER — Two-phase filtering and scoring

Filters are split into two phases:
- **Precheck** (before observation): authority, creator, blacklist — instant, filter out 90% of junk
- **Observation** (after 30-90 sec of observation): buyers, volume, diversity, bundled buy — require data

```python
# filters/base.py + filters/scorer.py + filters/*.py

@dataclass
class FilterResult:
    score: int                  # Contribution to the overall score
    hard_reject: bool           # True = instant rejection, score irrelevant
    reason: str                 # Human-readable reason


class Filter(ABC):
    """Abstract filter. Each filter = separate class, separate weight."""

    name: str
    weight: int                 # Max contribution to score (0 for hard-reject filters)
    enabled: bool = True

    @abstractmethod
    async def check(self, token: Token, context: dict) -> FilterResult:
        ...


class AuthorityFilter(Filter):
    """HARD REJECT: mint/freeze authority not revoked or Token-2022."""
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
    """Soft scoring on creator history + hard reject for blacklist."""
    name = "creator"
    weight = 15

    async def check(self, token, context) -> FilterResult:
        stats = context.get('creator_stats')

        # Hard: blacklist
        if stats and stats.blacklisted:
            return FilterResult(0, True, "creator_blacklisted")

        # Hard: wallet younger than 24h
        if stats and stats.wallet_age_hours < self.cfg.min_creator_wallet_age_hours:
            return FilterResult(0, True, f"wallet_age_{stats.wallet_age_hours:.0f}h")

        # Soft: serial scammer (>50 tokens, <1% graduation)
        if stats and stats.total_tokens > 50:
            if stats.graduation_rate < self.cfg.min_creator_graduation_rate:
                return FilterResult(-20, False, f"serial_scammer_{stats.graduation_rate:.3f}")
            if stats.graduation_rate > 0.05:
                return FilterResult(self.weight, False, f"serial_winner_{stats.graduation_rate:.3f}")

        return FilterResult(0, False, "creator_unknown")


class BundledBuyFilter(Filter):
    """HARD REJECT: early buyers received SOL from a common source."""
    name = "bundled_buy"
    weight = 0

    async def check(self, token, context) -> FilterResult:
        early_buyers = context.get('early_buyers', [])
        if len(early_buyers) < 3:
            return FilterResult(0, False, "too_few_buyers")

        # Check the SOL source for the first 5 buyers
        funding_sources = {}
        for buyer_wallet in early_buyers[:5]:
            history = await self.source.get_wallet_history(buyer_wallet, limit=10)
            for tx in history:
                if tx.is_sol_transfer_in and tx.age_hours < 24:
                    src = tx.from_wallet
                    funding_sources.setdefault(src, []).append(buyer_wallet)

        # 3+ buyers from one source = bundled
        for src, buyers in funding_sources.items():
            if len(buyers) >= 3:
                return FilterResult(0, True, f"bundled_from_{src[:8]}")

        return FilterResult(0, False, "no_bundling")


class ObservationFilter(Filter):
    """Soft scoring on observation metrics (45 sec window)."""
    name = "observation"
    weight = 50  # Total max from sub-metrics below

    async def check(self, token, context) -> FilterResult:
        trades = context.get('observation_trades', [])
        score = 0
        reasons = []

        buys = [t for t in trades if t.side == 'buy']
        sells = [t for t in trades if t.side == 'sell']

        # --- Unique buyers ---
        unique_buyers = set(t.wallet for t in buys)
        if len(unique_buyers) >= 10:
            score += 10; reasons.append(f"buyers_10+")
        elif len(unique_buyers) >= 5:
            score += 15; reasons.append(f"buyers_{len(unique_buyers)}")
        else:
            score -= 10; reasons.append(f"buyers_low_{len(unique_buyers)}")

        # --- Volume in SOL (not number of trades!) ---
        total_sol = sum(t.amount_sol for t in buys)
        if total_sol > 2.0:
            score += 15; reasons.append(f"volume_{total_sol:.1f}sol")
        elif total_sol > 0.5:
            score += 5; reasons.append(f"volume_{total_sol:.1f}sol")
        else:
            score -= 5; reasons.append(f"volume_low_{total_sol:.2f}sol")

        # --- Amount diversity (anti-bot) ---
        amounts = [round(t.amount_sol, 4) for t in buys]
        unique_amounts = len(set(amounts))
        if unique_amounts >= 4:
            score += 10; reasons.append(f"diverse_amounts")
        elif unique_amounts < 2 and len(amounts) > 3:
            score -= 15; reasons.append(f"uniform_amounts_bot")

        # --- Curve speed ---
        if trades:
            elapsed = trades[-1].timestamp - trades[0].timestamp
            curve = context.get('curve_progress', 0)
            if elapsed > 0 and curve > 0:
                speed = curve / elapsed
                if speed > 0.5:
                    score += 10; reasons.append(f"fast_curve")

        # --- No early creator sells ---
        creator_sells = [t for t in sells if t.is_creator]
        if creator_sells:
            return FilterResult(0, True, "creator_selling_early")

        # --- One whale > 50% of volume ---
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
    """Two-phase filtering. Precheck → Observation (if precheck passed)."""

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
        """Phase 1: instant filtering (<50ms). Before observation."""
        return await self._run_filters(self.precheck, token, context, phase='precheck')

    async def evaluate(self, token: Token, context: dict) -> FilterEvent:
        """Phase 2: full evaluation after observation. Includes precheck + observation."""
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

## Module 6: PULSE — Pulse monitoring + ExitManager

**Important:** PulseMonitor receives trades via stream (`source.stream_trades(mint)`), not through polling every 3 seconds. This eliminates trade duplication and false signals. Every trade is processed exactly once.

```python
# pulse/monitor.py + pulse/exit_manager.py

@dataclass
class PulseSnapshot:
    buy_rate: float             # Share of buys in the window (0.0 - 1.0)
    sell_rate: float
    new_wallet_rate: float      # Share of new wallets among buyers
    avg_buy_size_sol: float     # Average buy size in SOL
    total_sol_in: float         # Buy volume in SOL for the window
    creator_selling: bool
    whale_exit: bool            # Sell > 1 SOL

    # TREND (comparison with previous window)
    buy_rate_trend: str         # 'rising' | 'stable' | 'declining'
    buy_size_trend: str         # 'rising' | 'stable' | 'declining'
    trend_declining_count: int  # How many consecutive declining windows
    trend_dying: bool           # declining >= 2 consecutive windows

    curve_progress: float       # % of bonding curve filled


class PulseMonitor:
    """Sliding event window with trend analysis."""

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

        # New wallets
        new_wallets = set()
        for t in buys:
            if t.wallet not in self.seen_wallets:
                new_wallets.add(t.wallet)
        self.seen_wallets.update(t.wallet for t in self.window)
        new_wallet_rate = len(new_wallets) / max(len(buys), 1)

        # Average size in SOL
        avg_buy = sum(t.amount_sol for t in buys) / max(len(buys), 1)
        total_sol = sum(t.amount_sol for t in buys)

        # Trend
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
    """Decides when and how much to sell based on the pulse."""

    def __init__(self, cfg: 'Config'):
        self.cfg = cfg
        self.remaining_pct: float = 1.0
        self.partial_exit_count: int = 0
        self.has_taken_profit: bool = False

    def decide(self, pulse: PulseSnapshot, pnl_pct: float, elapsed_sec: float) -> PulseEvent:

        # === HARD EXITS — 100% ===

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

        # === PARTIAL EXITS ===

        available = self.remaining_pct - self.cfg.moonbag_pct
        if available <= 0.01:
            return self._hold()

        # Strong profit → lock in 30%
        if pnl_pct > self.cfg.partial_sell_profit_threshold * 100 and not self.has_taken_profit:
            sell_pct = min(self.cfg.partial_sell_on_profit_pct, available)
            self.has_taken_profit = True
            return self._sell_partial(sell_pct, "strong_profit")

        # Weakening pulse + profit → sell 50%
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

## Module 7: PORTFOLIO — Position and balance management

A separate module for accounting balance, positions, orders. Not spread across the engine.

**Key differences from v2:**
- **Reserve/commit/release** — when a buy command is created, funds are reserved; on fill they are committed; on fail they are returned. Protection against race conditions on parallel SignalEvent.
- **Persistence** — every state change is written to SQLite. On restart, Portfolio is restored from the DB.
- **Explicit amounts** — the buy command contains `quote_amount_sol`, the sell command contains `base_amount_tokens`. No implicit recalculations.

```python
# portfolio.py (package root)

class Portfolio:
    def __init__(self, db: 'Database', clock: Clock, initial_balance: float,
                 max_positions: int, buy_amount: float):
        self.db = db
        self.clock = clock
        self.balance: float = initial_balance
        self.reserved: float = 0.0              # Reserved for pending buy orders
        self.positions: dict[str, Position] = {}
        self.history: list[TradeResult] = []
        self.max_positions = max_positions
        self.buy_amount = buy_amount

    @classmethod
    def restore(cls, db: 'Database', clock: Clock, cfg: 'Config') -> 'Portfolio':
        """Restore from SQLite after restart. Reads open positions, pending orders, balance."""
        portfolio = cls(db, clock, cfg.initial_balance_sol, cfg.max_open_positions, cfg.buy_amount_sol)
        portfolio.positions = db.load_open_positions()
        portfolio.balance = db.load_balance()
        portfolio.reserved = db.load_reserved()
        portfolio.history = db.load_trade_history()
        return portfolio

    @property
    def available_balance(self) -> float:
        """Available balance = total - reserved."""
        return self.balance - self.reserved

    def can_open(self) -> bool:
        """Is there budget and a slot for a new position?"""
        return (
            len(self.positions) < self.max_positions
            and self.available_balance >= self.buy_amount
        )

    def create_buy_command(self, signal: SignalEvent) -> PlaceBuyCommand | None:
        """SignalEvent → PlaceBuyCommand. Reserves funds atomically."""
        if not self.can_open():
            return None
        if signal.token.mint in self.positions:
            return None

        # Reserve — money is locked until fill or fail
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
        """Commit: update balance and open position after purchase."""
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
        """Release: return reserved funds on execution error."""
        self.reserved -= self.buy_amount
        self.db.delete_reservation(reservation_id)
        self.db.save_balance(self.balance, self.reserved)

    def create_sell_command(self, pulse_event: PulseEvent) -> PlaceSellCommand | None:
        """PulseEvent → PlaceSellCommand with explicit token amount."""
        pos = self.positions.get(pulse_event.mint)
        if not pos:
            return None
        sell_tokens = pos.amount_tokens * pulse_event.sell_pct * pos.remaining_pct
        return PlaceSellCommand(
            timestamp=self.clock.now(),
            correlation_id=pulse_event.correlation_id,
            mint=pulse_event.mint,
            symbol=pos.symbol,
            base_amount_tokens=sell_tokens,     # Explicit! Not amount_sol
            reason=pulse_event.reason,
        )

    def on_sell_fill(self, fill: FillEvent):
        """Update balance and position after sale."""
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

## Module 8: EXECUTION — Order execution

**Key differences from v2:**
- Accepts `Command` (PlaceBuyCommand / PlaceSellCommand), not OrderEvent
- Buy uses `quote_amount_sol`, sell uses `base_amount_tokens` — explicit fields, no confusion
- Execution Worker — a separate `asyncio.Task` with `Semaphore`, does not block the event processor
- Every execution attempt is written to SQLite (`execution_attempts`)

```python
# execution.py (package root)

class ExecutionHandler(ABC):
    @abstractmethod
    async def execute(self, command: Command) -> FillEvent:
        ...


class SimulatedExecution(ExecutionHandler):
    """Backtest: executes instantly + bonding-curve slippage model."""

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
    """Production: real Solana transaction."""

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

        # Send with retry, every attempt is logged
        for attempt in range(2):  # Max 2 attempts (not 3 — save time)
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
            price=...,      # From tx result
            quote_amount_sol=...,
            base_amount_tokens=...,
            fee_sol=..., slippage_pct=...,
            tx_sig=str(sig), command_id=command.command_id,
        )


class ExecutionWorker:
    """Separate worker for executing commands. Does not block the event processor."""

    def __init__(self, handler: ExecutionHandler, max_concurrent: int = 2):
        self.handler = handler
        self._semaphore = asyncio.Semaphore(max_concurrent)
        self._queue: asyncio.Queue[Command] = asyncio.Queue()

    async def submit(self, command: Command) -> None:
        """Put a command into the execution queue."""
        await self._queue.put(command)

    async def run(self, on_fill: Callable, on_fail: Callable) -> None:
        """Main worker loop. Runs as a separate asyncio.Task."""
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

## Module 9: ENGINE — Main loop

Event-driven loop. All modules communicate through `asyncio.Queue`.

**Key differences from v2:**
- Execution moved out into a separate `ExecutionWorker` — does not block the event processor
- Token handler is bounded by `Semaphore` — bounded concurrency, does not blow up memory
- Pulse monitor uses `stream_trades()` — stream instead of polling, every trade exactly once
- All time-dependent operations go through `self.clock` — backtest is deterministic

```python
# engine.py (package root)

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
        self._token_semaphore = asyncio.Semaphore(10)  # Max 10 tokens concurrently

    async def run(self):
        """Startup: listener + processor + execution worker in parallel."""
        await asyncio.gather(
            self._token_listener(),
            self._event_processor(),
            self.exec_worker.run(
                on_fill=self._on_fill,
                on_fail=self._on_execution_fail,
            ),
        )

    async def _token_listener(self):
        """Listens for new tokens → puts them on the queue."""
        async for token_event in self.source.stream_new_tokens():
            self.db.log_event(token_event)
            await self.event_queue.put(token_event)

    async def _event_processor(self):
        """Processes events. Does NOT execute orders — ExecutionWorker does."""
        while True:
            event = await self.event_queue.get()
            self.db.log_event(event)

            if isinstance(event, TokenEvent):
                # Bounded concurrency via semaphore
                asyncio.create_task(self._handle_token_bounded(event))

            elif isinstance(event, SignalEvent):
                command = self.portfolio.create_buy_command(event)
                if command:
                    await self.exec_worker.submit(command)  # Non-blocking!

            elif isinstance(event, PulseEvent):
                if event.action in ('sell_all', 'sell_partial'):
                    command = self.portfolio.create_sell_command(event)
                    if command:
                        await self.exec_worker.submit(command)

    async def _on_fill(self, fill: FillEvent, command: Command):
        """Callback from ExecutionWorker on successful execution."""
        self.db.log_event(fill)
        if fill.side == 'buy':
            self.portfolio.on_buy_fill(fill, command.reservation_id)
            asyncio.create_task(self._monitor_pulse(fill.mint, fill.correlation_id))
        else:
            self.portfolio.on_sell_fill(fill)

    async def _on_execution_fail(self, command: Command, error: Exception):
        """Callback from ExecutionWorker on error — return the reservation."""
        if isinstance(command, PlaceBuyCommand):
            self.portfolio.on_buy_fail(command.reservation_id)
        structlog.get_logger().error("execution_failed", command=command, error=str(error))

    async def _handle_token_bounded(self, token_event: TokenEvent):
        """Token handling with bounded concurrency."""
        async with self._token_semaphore:
            await self._handle_token(token_event)

    async def _handle_token(self, token_event: TokenEvent):
        """Handle a new token: precheck → observation → full evaluation → signal."""
        token = token_event.token
        corr_id = token_event.correlation_id

        # Collect context for precheck filters
        context = {}
        context['token_info'] = await self.source.get_token_info(token.mint)
        context['creator_stats'] = self.db.get_creator_stats(token.creator)

        # Phase 1: Precheck (instant, <50ms)
        precheck_result = await self.scorer.precheck(token, context)
        precheck_result.correlation_id = corr_id
        await self.event_queue.put(precheck_result)
        if precheck_result.hard_rejected:
            return

        # Observation (30-90 sec) — via Clock, not asyncio.sleep!
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

        # Phase 2: Full filtering (precheck + observation)
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
        """Pulse monitoring: stream-based, every trade exactly once."""
        pos = self.portfolio.positions.get(mint)
        if not pos:
            return

        monitor = PulseMonitor(
            window_size=self.cfg.pulse_window_size,
            min_events=self.cfg.pulse_min_events,
        )
        exit_mgr = ExitManager(self.cfg)

        # Stream — not polling! Every trade is processed exactly once.
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

## Module 10: CONFIG

```python
# config.py (package root)

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
    # === BUDGET ===
    initial_balance_sol: float = 0.15
    buy_amount_sol: float = 0.03
    max_open_positions: int = 3

    # === OBSERVATION ===
    observe_seconds: int = 45
    score_threshold: int = 20

    # === CREATOR FILTERS ===
    min_creator_wallet_age_hours: int = 24
    min_creator_graduation_rate: float = 0.01
    max_creator_dead_tokens: int = 10
    require_socials: bool = False

    # === PULSE ===
    pulse_window_size: int = 20
    pulse_min_events: int = 5
    pulse_dead_buy_rate: float = 0.10
    pulse_weak_buy_rate: float = 0.30
    pulse_sell_pressure_ratio: float = 2.0
    pulse_no_new_wallets_events: int = 5
    pulse_near_graduation_pct: float = 70.0

    # === EXITS ===
    partial_sell_on_weak_pulse_pct: float = 0.50
    partial_sell_on_profit_pct: float = 0.30
    partial_sell_profit_threshold: float = 2.0
    moonbag_pct: float = 0.10
    hard_stop_loss_pct: float = 0.50
    max_hold_seconds: int = 7200

    # === LAUNCHPAD ===
    launchpad: LaunchpadConfig = field(default_factory=lambda: PUMPFUN_CONFIG)

    # === INFRA ===
    rpc_url: str = ""
    db_path: str = "pulse_bot.db"
    log_level: str = "INFO"

# Presets
CONSERVATIVE = Config(observe_seconds=60, score_threshold=30, pulse_dead_buy_rate=0.15, hard_stop_loss_pct=0.30)
MODERATE = Config()
AGGRESSIVE = Config(observe_seconds=30, score_threshold=10, pulse_dead_buy_rate=0.05, hard_stop_loss_pct=0.70)
```


---

## SQLite schema

**Differences from v2:** added `positions`, `fills`, `reservations`, `execution_attempts` for persistent state. The bot recovers after a restart. The event log is extended with `event_id` and `correlation_id`.

```sql
-- Tokens (data for backtest)
CREATE TABLE tokens (
    mint TEXT PRIMARY KEY,
    name TEXT, symbol TEXT, creator TEXT,
    created_at REAL, uri TEXT, launchpad TEXT,
    mint_authority_revoked INTEGER,
    freeze_authority_revoked INTEGER,
    graduated INTEGER DEFAULT 0
);

-- Trades (data for backtest and observation)
CREATE TABLE trades (
    tx_sig TEXT PRIMARY KEY,
    mint TEXT, wallet TEXT, side TEXT,
    amount_sol REAL, amount_tokens REAL,
    price_sol REAL, curve_progress_pct REAL,
    timestamp REAL, is_creator INTEGER
);

-- Creator cache
CREATE TABLE creators (
    wallet TEXT PRIMARY KEY,
    total_tokens INTEGER, graduated_tokens INTEGER,
    graduation_rate REAL, wallet_age_hours REAL,
    blacklisted INTEGER DEFAULT 0
);

-- === PERSISTENT STATE (new in v3) ===

-- Open positions (restored on restart)
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

-- All executed trades
CREATE TABLE fills (
    fill_id TEXT PRIMARY KEY,         -- event_id from FillEvent
    mint TEXT, symbol TEXT, side TEXT,
    price REAL,
    quote_amount_sol REAL,
    base_amount_tokens REAL,
    fee_sol REAL, slippage_pct REAL,
    tx_sig TEXT,
    command_id TEXT,                   -- Command ID
    correlation_id TEXT,              -- Chain from TokenEvent
    timestamp REAL
);

-- Balance reservations (pending buy orders)
CREATE TABLE reservations (
    reservation_id TEXT PRIMARY KEY,
    amount_sol REAL,
    created_at REAL
);

-- Execution attempts (for live debugging)
CREATE TABLE execution_attempts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    command_id TEXT,
    attempt INTEGER,
    status TEXT,                       -- 'pending' | 'success' | 'failed'
    error TEXT,
    timestamp REAL
);

-- Balance (restored on restart)
CREATE TABLE balance (
    id INTEGER PRIMARY KEY CHECK (id = 1),  -- Only one row
    balance_sol REAL,
    reserved_sol REAL,
    updated_at REAL
);

-- Log of ALL events (for replay and debugging)
CREATE TABLE event_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT,                     -- Unique event ID
    correlation_id TEXT,               -- Chain from TokenEvent
    event_type TEXT,                   -- 'token' | 'filter' | 'signal' | 'command' | 'fill' | 'pulse'
    data TEXT,                         -- JSON-serialized Event/Command
    timestamp REAL
);

-- Indexes
CREATE INDEX idx_trades_mint_ts ON trades(mint, timestamp);
CREATE INDEX idx_tokens_created ON tokens(created_at);
CREATE INDEX idx_events_ts ON event_log(timestamp);
CREATE INDEX idx_events_corr ON event_log(correlation_id);
CREATE INDEX idx_fills_mint ON fills(mint, timestamp);
CREATE INDEX idx_positions_status ON positions(status);
```


---

## CLI and modes

```bash
# 1. Collect historical data
python main.py collect --days 7

# 2. Backtest with default settings
python main.py backtest

# 3. Backtest with the aggressive preset
python main.py backtest --preset aggressive

# 4. Grid-search optimization
python main.py optimize

# 5. Paper trading (everything real except transactions)
python main.py paper

# 6. Live mode
python main.py live
```

The difference between modes is Clock, Source, Execution:

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

# Portfolio is restored from SQLite on restart
portfolio = Portfolio.restore(db, clock, cfg) if mode != 'backtest' else Portfolio(db, clock, ...)

exec_worker = ExecutionWorker(execution, max_concurrent=2)
bot = PulseBot(source=source, exec_worker=exec_worker, clock=clock, portfolio=portfolio, ...)
asyncio.run(bot.run())
```


---

## Development plan

```
WEEK 1 — FOUNDATION:
  Evening 1: models.py + events.py + commands.py + config.py
             All dataclasses: Token, Trade, Position, PulseSnapshot, TradeResult
             Events: TokenEvent, FilterEvent, SignalEvent, FillEvent, PulseEvent
             Commands: PlaceBuyCommand, PlaceSellCommand
             Config + LaunchpadConfig + presets

  Evening 2: clock.py + db.py + sources/base.py + sources/backtest.py
             Clock ABC + RealClock + SimulatedClock
             SQLite schema (tokens, trades, creators, positions, fills, event_log, balance)
             DataSource ABC + BacktestSource (read from SQLite + SimulatedClock)

  Evening 3: launchpads/base.py + launchpads/pumpfun.py
             Launchpad ABC (ws, parse, build_tx, curve, graduation, antiscam)
             PumpFun (create/trade parsing, bonding curve formula, ws_subscribe_trades)

  Evening 4: collector.py
             Bitquery GraphQL → SQLite (tokens + trades for 7 days)
             Run data collection

WEEK 2 — BRAINS:
  Evening 1: filters/base.py + filters/authority.py + filters/creator.py
             Filter ABC + FilterResult + AuthorityFilter + CreatorFilter

  Evening 2: filters/bundled_buy.py + filters/observation.py + filters/scorer.py
             BundledBuyFilter + ObservationFilter (volume SOL, unique buyers, diversity)
             Scorer (two-phase: precheck_filters + observation_filters)

  Evening 3: pulse/monitor.py + pulse/exit_manager.py
             PulseMonitor (stream-based, last_seen cursor, trend declining/rising/stable)
             ExitManager (partial exits + moonbag)

  Evening 4: portfolio.py + execution.py
             Portfolio (reserve/commit/release, persistence, restore)
             ExecutionHandler ABC + SimulatedExecution + ExecutionWorker

WEEK 3 — INTEGRATION:
  Evening 1: engine.py
             PulseBot (event loop, execution worker, bounded concurrency)
             Wiring all modules via Clock

  Evening 2: report.py + main.py
             P&L report (win rate, profit factor, drawdown, exit reasons)
             CLI: collect / backtest / paper / live

  Evening 3: First backtest
             Run on 7 days of data. Analyze results.
             Tune scoring thresholds.

  Evening 4: Grid search
             Sweep: observe_seconds × score_threshold × pulse_dead × hard_sl
             Find the best parameter combination.

WEEK 4 — PRODUCTION:
  Evening 1: sources/live.py
             WebSocket connection to Pump.fun + stream_trades
             Record all data into SQLite (for future backtests)

  Evening 2: execution.py (LiveExecution)
             Real buy/sell transactions on devnet
             Test on devnet (fake SOL)

  Evening 3: Paper trading mainnet
             LiveSource + SimulatedExecution + Portfolio.restore()
             Real data, virtual trades, recovery on restart

  Evening 4: Live mode
             Live with 0.15 SOL (~$20)
             Monitoring, alerts

LATER:
  Week 5: LetsBonk (launchpads/letsbonk.py — own heuristics, graduation)
  Week 6: Believe (launchpads/believe.py — dynamic curve, anti-sniper)
  Week 7: Smart Wallets module (separate Source or additional listener)
  Week 8: Telegram alerts (trade notifications)
```


---

## Success metrics

The bot is profitable if over a week:

- **Win rate > 35%** (sufficient given the asymmetric bet)
- **Average win > 2× average loss** (asymmetry)
- **Profit factor > 1.5** (gross profit / gross loss)
- **Max drawdown < 50% of deposit**
- **Tokens filtered out > 95%** (we buy < 5% of what we see)
- **Real vs model slippage < 2%** (SimulatedExecution validation)

If the metrics do not hold after 2 weeks of paper trading — change thresholds or strategy. Do not pour live money into a losing system.
