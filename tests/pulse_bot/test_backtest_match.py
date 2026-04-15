# tests/pulse_bot/test_backtest_match.py
"""Tests proving backtest produces identical results to live bot.

Verifies that:
- FastFilter returns correct decisions for zero and non-zero trades.
- Scorer is deterministic (same input -> same output).
- Scorer uses passed creator snapshot instead of DB queries.
- Pipeline correctly tags source as 'live' or 'backtest'.
- ReplayLaunchpad yields exact trades matching live trade IDs.
- ReplayLaunchpad yields zero trades in fast phase when live had zero.
"""

from __future__ import annotations

import asyncio
import sqlite3
import tempfile
import time
from typing import AsyncIterator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from pulse_bot.config import PulseBotConfig
from pulse_bot.filters.fast import FastFilter, FastResult
from pulse_bot.filters.scorer import Scorer
from pulse_bot.models import CreatorStats, ScoringResult, Token, Trade
from pulse_bot.pipeline import Pipeline
from pulse_bot.sources.replay import ReplayLaunchpad


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def config() -> PulseBotConfig:
    """Return a default PulseBotConfig for tests."""
    return PulseBotConfig()


@pytest.fixture()
def sample_token() -> Token:
    """Return a sample Token object for testing."""
    return Token(
        mint="MINTabcdef1234567890abcdef1234567890abcde",
        name="TestToken",
        symbol="TEST",
        creator="CREATORwallet0000000000000000000000000000",
        created_at=1_700_000_000.0,
        uri="https://example.com/meta.json",
        launchpad="pumpfun",
    )


@pytest.fixture()
def sample_trades(sample_token: Token) -> list[Trade]:
    """Return a list of sample Trade objects simulating organic activity.

    Contains 5 buys from 4 unique wallets and 1 sell, spread over ~4 seconds.
    """
    mint = sample_token.mint
    base_ts = sample_token.created_at + 1.0
    return [
        Trade(
            mint=mint, wallet="walletA000000000000000000000000000000000",
            tx_type="buy", sol_amount=0.1, token_amount=1_000_000.0,
            new_token_balance=1_000_000.0, bonding_curve_key="curve1",
            v_sol_in_bonding_curve=30.1, v_tokens_in_bonding_curve=500_000_000.0,
            market_cap_sol=30.0, timestamp=base_ts + 0.0,
        ),
        Trade(
            mint=mint, wallet="walletB000000000000000000000000000000000",
            tx_type="buy", sol_amount=0.2, token_amount=1_800_000.0,
            new_token_balance=1_800_000.0, bonding_curve_key="curve1",
            v_sol_in_bonding_curve=30.3, v_tokens_in_bonding_curve=498_000_000.0,
            market_cap_sol=30.3, timestamp=base_ts + 1.0,
        ),
        Trade(
            mint=mint, wallet="walletC000000000000000000000000000000000",
            tx_type="buy", sol_amount=0.15, token_amount=1_300_000.0,
            new_token_balance=1_300_000.0, bonding_curve_key="curve1",
            v_sol_in_bonding_curve=30.45, v_tokens_in_bonding_curve=497_000_000.0,
            market_cap_sol=30.5, timestamp=base_ts + 2.0,
        ),
        Trade(
            mint=mint, wallet="walletD000000000000000000000000000000000",
            tx_type="buy", sol_amount=0.3, token_amount=2_500_000.0,
            new_token_balance=2_500_000.0, bonding_curve_key="curve1",
            v_sol_in_bonding_curve=30.75, v_tokens_in_bonding_curve=495_000_000.0,
            market_cap_sol=30.8, timestamp=base_ts + 3.0,
        ),
        Trade(
            mint=mint, wallet="walletA000000000000000000000000000000000",
            tx_type="sell", sol_amount=0.05, token_amount=500_000.0,
            new_token_balance=500_000.0, bonding_curve_key="curve1",
            v_sol_in_bonding_curve=30.70, v_tokens_in_bonding_curve=495_500_000.0,
            market_cap_sol=30.7, timestamp=base_ts + 3.5,
        ),
        Trade(
            mint=mint, wallet="walletB000000000000000000000000000000000",
            tx_type="buy", sol_amount=0.25, token_amount=2_000_000.0,
            new_token_balance=3_800_000.0, bonding_curve_key="curve1",
            v_sol_in_bonding_curve=30.95, v_tokens_in_bonding_curve=493_500_000.0,
            market_cap_sol=31.0, timestamp=base_ts + 4.0,
        ),
    ]


@pytest.fixture()
def creator_snapshot() -> CreatorStats:
    """Return a sample CreatorStats snapshot."""
    return CreatorStats(
        wallet="CREATORwallet0000000000000000000000000000",
        total_tokens_created=2,
        times_seen=2,
        tokens_where_creator_sold_early=0,
        first_seen_at=1_699_999_000.0,
        last_seen_at=1_700_000_000.0,
        blacklisted=False,
    )


@pytest.fixture()
def mock_db() -> MagicMock:
    """Return a mocked Database object that does not touch real SQLite."""
    db = MagicMock()
    db.get_creator_stats_sync.return_value = None
    db.get_creator_tokens_today_sync.return_value = 0
    db.get_tokens_last_5min_sync.return_value = 0
    db.insert_token = AsyncMock()
    db.insert_trades_batch = AsyncMock(return_value=[1, 2, 3, 4, 5, 6])
    db.upsert_creator = AsyncMock()
    db.upsert_scoring_result = AsyncMock()
    db.log_event = AsyncMock()
    db.save_live_decision = AsyncMock()
    return db


def _make_replay_db(
    token: Token,
    trades: list[Trade],
    fast_trade_ids: list[int] | None = None,
    full_trade_ids: list[int] | None = None,
) -> str:
    """Create a temporary SQLite DB populated with token and trade data.

    Returns the path to the temp DB file.
    """
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    path = tmp.name
    tmp.close()

    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE tokens (mint TEXT PRIMARY KEY, name TEXT, symbol TEXT, "
        "creator TEXT, created_at REAL, uri TEXT, launchpad TEXT)"
    )
    conn.execute(
        "INSERT INTO tokens VALUES (?,?,?,?,?,?,?)",
        (token.mint, token.name, token.symbol, token.creator,
         token.created_at, token.uri, token.launchpad),
    )

    conn.execute(
        "CREATE TABLE trades (id INTEGER PRIMARY KEY AUTOINCREMENT, mint TEXT, "
        "wallet TEXT, tx_type TEXT, sol_amount REAL, token_amount REAL, "
        "market_cap_sol REAL, v_sol_in_bonding_curve REAL, timestamp REAL, "
        "is_creator INTEGER DEFAULT 0)"
    )
    for trade in trades:
        conn.execute(
            "INSERT INTO trades (mint, wallet, tx_type, sol_amount, token_amount, "
            "market_cap_sol, v_sol_in_bonding_curve, timestamp, is_creator) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (trade.mint, trade.wallet, trade.tx_type, trade.sol_amount,
             trade.token_amount, trade.market_cap_sol,
             trade.v_sol_in_bonding_curve, trade.timestamp, int(trade.is_creator)),
        )

    conn.execute(
        "CREATE TABLE IF NOT EXISTS token_scores ("
        "mint TEXT, source TEXT DEFAULT 'live', fast_trade_count INTEGER DEFAULT 0, "
        "full_trade_count INTEGER DEFAULT 0, fast_trade_ids TEXT DEFAULT '', "
        "full_trade_ids TEXT DEFAULT '', PRIMARY KEY (mint, source))"
    )
    if fast_trade_ids is not None or full_trade_ids is not None:
        fast_ids_str = ",".join(str(i) for i in (fast_trade_ids or []))
        full_ids_str = ",".join(str(i) for i in (full_trade_ids or []))
        conn.execute(
            "INSERT INTO token_scores (mint, source, fast_trade_count, full_trade_count, "
            "fast_trade_ids, full_trade_ids) VALUES (?,?,?,?,?,?)",
            (token.mint, "live",
             len(fast_trade_ids or []), len(full_trade_ids or []),
             fast_ids_str, full_ids_str),
        )

    conn.commit()
    conn.close()
    return path


# ---------------------------------------------------------------------------
# Test 1: FastFilter returns WAIT with score=0 when no trades
# ---------------------------------------------------------------------------


class TestFastFilterNoTrades:
    """FastFilter must return WAIT with score=0 when given zero trades."""

    def test_fast_filter_no_trades_returns_wait(
        self, config: PulseBotConfig, sample_token: Token,
    ) -> None:
        """Passing an empty trade list produces WAIT decision and zero score."""
        # Arrange
        fast_filter = FastFilter(config)

        # Act
        result = fast_filter.evaluate(sample_token, [])

        # Assert
        assert result.decision == "WAIT"
        assert result.score == 0
        assert result.buy_count == 0
        assert result.unique_buyers == 0
        assert result.volume_sol == 0.0
        assert "no_trades" in result.reasons


# ---------------------------------------------------------------------------
# Test 2: FastFilter scores correctly with sample trades
# ---------------------------------------------------------------------------


class TestFastFilterWithTrades:
    """FastFilter scores correctly when given actual trade data."""

    def test_fast_filter_with_trades(
        self,
        config: PulseBotConfig,
        sample_token: Token,
        sample_trades: list[Trade],
    ) -> None:
        """Non-empty trades produce a non-zero score and correct buy count."""
        # Arrange
        fast_filter = FastFilter(config)

        # Act
        result = fast_filter.evaluate(sample_token, sample_trades)

        # Assert
        assert isinstance(result, FastResult)
        assert result.buy_count == 5
        assert result.sell_count == 1
        assert result.unique_buyers == 4
        assert result.volume_sol == pytest.approx(1.0, abs=0.01)
        assert result.decision in ("FAST_BUY", "WAIT")
        assert result.score >= 0
        assert result.elapsed > 0.0


# ---------------------------------------------------------------------------
# Test 3: Scorer is deterministic (same input -> same score, twice)
# ---------------------------------------------------------------------------


class TestScorerDeterministic:
    """Scorer must produce identical results for identical inputs."""

    def test_scorer_deterministic(
        self,
        config: PulseBotConfig,
        mock_db: MagicMock,
        sample_token: Token,
        sample_trades: list[Trade],
        creator_snapshot: CreatorStats,
    ) -> None:
        """Running score() twice with the same data yields equal total_score."""
        # Arrange
        scorer = Scorer(config, mock_db)

        # Act
        result_a = scorer.score(
            sample_token, sample_trades,
            tokens_last_5min=0, concurrent_observations=0,
            creator_snapshot=creator_snapshot,
        )
        result_b = scorer.score(
            sample_token, sample_trades,
            tokens_last_5min=0, concurrent_observations=0,
            creator_snapshot=creator_snapshot,
        )

        # Assert
        assert result_a.total_score == result_b.total_score
        assert result_a.decision == result_b.decision
        assert result_a.buy_count == result_b.buy_count
        assert result_a.unique_buyers == result_b.unique_buyers
        assert result_a.buy_volume_sol == pytest.approx(result_b.buy_volume_sol)
        assert result_a.curve_progress_pct == pytest.approx(result_b.curve_progress_pct)
        assert result_a.reasons_summary == result_b.reasons_summary


# ---------------------------------------------------------------------------
# Test 4: Scorer uses passed snapshot, not DB query
# ---------------------------------------------------------------------------


class TestScorerCreatorSnapshot:
    """Scorer must use the creator_snapshot argument, not query the DB."""

    def test_scorer_creator_snapshot(
        self,
        config: PulseBotConfig,
        mock_db: MagicMock,
        sample_token: Token,
        sample_trades: list[Trade],
    ) -> None:
        """When creator_snapshot is provided, the DB is NOT queried for creator stats."""
        # Arrange
        snapshot = CreatorStats(
            wallet=sample_token.creator,
            total_tokens_created=3,
            times_seen=3,
            tokens_where_creator_sold_early=0,
            first_seen_at=1_699_999_000.0,
            last_seen_at=1_700_000_000.0,
            blacklisted=False,
        )
        scorer = Scorer(config, mock_db)

        # Act
        result = scorer.score(
            sample_token, sample_trades,
            creator_snapshot=snapshot,
        )

        # Assert -- DB creator lookup was NOT called
        mock_db.get_creator_stats_sync.assert_not_called()

        # The result should contain the creator-based scoring from the snapshot
        assert isinstance(result, ScoringResult)
        assert result.decision in ("BUY", "SKIP", "BORDERLINE")

    def test_scorer_falls_back_to_db_without_snapshot(
        self,
        config: PulseBotConfig,
        mock_db: MagicMock,
        sample_token: Token,
        sample_trades: list[Trade],
    ) -> None:
        """Without creator_snapshot, the scorer queries the DB for creator stats."""
        # Arrange
        mock_db.get_creator_stats_sync.return_value = CreatorStats(
            wallet=sample_token.creator,
            total_tokens_created=1,
            times_seen=1,
        )
        scorer = Scorer(config, mock_db)

        # Act
        scorer.score(sample_token, sample_trades, creator_snapshot=None)

        # Assert -- DB was queried
        mock_db.get_creator_stats_sync.assert_called_once_with(sample_token.creator)


# ---------------------------------------------------------------------------
# Test 5: Pipeline with PumpFunLaunchpad sets source='live'
# ---------------------------------------------------------------------------


class TestPipelineSourceLive:
    """Pipeline must tag ScoringResult.source='live' when using PumpFunLaunchpad."""

    async def test_pipeline_sets_source_live(
        self,
        config: PulseBotConfig,
        mock_db: MagicMock,
        sample_token: Token,
        sample_trades: list[Trade],
        creator_snapshot: CreatorStats,
    ) -> None:
        """_handle_token with a pumpfun launchpad writes source='live'."""
        # Arrange
        launchpad = MagicMock()
        launchpad.name = "pumpfun"
        launchpad.subscribe_trades = AsyncMock()
        launchpad.unsubscribe_trades = AsyncMock()

        async def _stream_trades_gen(mint: str, duration: float) -> AsyncIterator[Trade]:
            for trade in sample_trades:
                yield trade

        launchpad.stream_trades = _stream_trades_gen

        scorer = Scorer(config, mock_db)
        fast_filter = FastFilter(config)
        pipeline = Pipeline(config, mock_db, launchpad, scorer, fast_filter)

        # Act
        await pipeline._handle_token(sample_token, creator_snapshot)

        # Assert -- the ScoringResult saved to DB has source='live'
        mock_db.upsert_scoring_result.assert_called_once()
        saved_result: ScoringResult = mock_db.upsert_scoring_result.call_args[0][0]
        assert saved_result.source == "live"


# ---------------------------------------------------------------------------
# Test 6: Pipeline with ReplayLaunchpad sets source='backtest'
# ---------------------------------------------------------------------------


class TestPipelineSourceBacktest:
    """Pipeline must tag ScoringResult.source='backtest' when using ReplayLaunchpad."""

    async def test_pipeline_sets_source_backtest(
        self,
        config: PulseBotConfig,
        mock_db: MagicMock,
        sample_token: Token,
        sample_trades: list[Trade],
        creator_snapshot: CreatorStats,
    ) -> None:
        """_handle_token with a replay launchpad writes source='backtest'."""
        # Arrange
        launchpad = MagicMock()
        launchpad.name = "replay"
        launchpad.subscribe_trades = AsyncMock()
        launchpad.unsubscribe_trades = AsyncMock()

        async def _stream_trades_gen(mint: str, duration: float) -> AsyncIterator[Trade]:
            for trade in sample_trades:
                # Tag with _db_id as ReplayLaunchpad does
                trade._db_id = sample_trades.index(trade) + 1
                yield trade

        launchpad.stream_trades = _stream_trades_gen

        scorer = Scorer(config, mock_db)
        fast_filter = FastFilter(config)
        pipeline = Pipeline(config, mock_db, launchpad, scorer, fast_filter)

        # Act
        await pipeline._handle_token(sample_token, creator_snapshot)

        # Assert -- the ScoringResult saved to DB has source='backtest'
        mock_db.upsert_scoring_result.assert_called_once()
        saved_result: ScoringResult = mock_db.upsert_scoring_result.call_args[0][0]
        assert saved_result.source == "backtest"


# ---------------------------------------------------------------------------
# Test 7: ReplayLaunchpad yields exact trades matching live IDs
# ---------------------------------------------------------------------------


class TestReplayStreamTradesUsesIds:
    """ReplayLaunchpad must yield only trades whose DB IDs match live scoring."""

    async def test_replay_stream_trades_uses_ids(
        self, sample_token: Token, sample_trades: list[Trade],
    ) -> None:
        """When live scores specify trade IDs, replay yields exactly those trades."""
        # Arrange -- create a temp DB with 6 trades (IDs 1..6)
        # Live scoring recorded fast_ids={1,2,3}, full_ids={1,2,3,4,5,6}
        fast_ids = [1, 2, 3]
        full_ids = [1, 2, 3, 4, 5, 6]
        db_path = _make_replay_db(
            sample_token, sample_trades,
            fast_trade_ids=fast_ids,
            full_trade_ids=full_ids,
        )

        replay = ReplayLaunchpad(db_path, speed=0.0)
        replay._running = True

        # Act -- subscribe loads trades + live counts
        await replay.subscribe_trades(sample_token.mint)

        # First call = fast phase
        fast_collected: list[Trade] = []
        async for trade in replay.stream_trades(sample_token.mint, duration_seconds=5.0):
            fast_collected.append(trade)

        # Second call = full phase (remaining trades)
        full_extra_collected: list[Trade] = []
        async for trade in replay.stream_trades(sample_token.mint, duration_seconds=40.0):
            full_extra_collected.append(trade)

        # Assert
        fast_db_ids = {getattr(t, "_db_id", None) for t in fast_collected}
        full_extra_db_ids = {getattr(t, "_db_id", None) for t in full_extra_collected}

        assert fast_db_ids == set(fast_ids)
        assert full_extra_db_ids == set(full_ids) - set(fast_ids)
        assert len(fast_collected) == 3
        assert len(full_extra_collected) == 3

        await replay.disconnect()


# ---------------------------------------------------------------------------
# Test 8: ReplayLaunchpad yields 0 trades in fast phase when live had 0
# ---------------------------------------------------------------------------


class TestReplayFastZeroTrades:
    """ReplayLaunchpad must yield zero trades in fast phase when live recorded zero."""

    async def test_replay_fast_zero_trades(
        self, sample_token: Token, sample_trades: list[Trade],
    ) -> None:
        """When live fast_trade_ids is empty, fast phase yields nothing."""
        # Arrange -- live had 0 fast trades, all trades in full phase
        fast_ids: list[int] = []
        full_ids = [1, 2, 3, 4, 5, 6]
        db_path = _make_replay_db(
            sample_token, sample_trades,
            fast_trade_ids=fast_ids,
            full_trade_ids=full_ids,
        )

        replay = ReplayLaunchpad(db_path, speed=0.0)
        replay._running = True

        # Act
        await replay.subscribe_trades(sample_token.mint)

        fast_collected: list[Trade] = []
        async for trade in replay.stream_trades(sample_token.mint, duration_seconds=5.0):
            fast_collected.append(trade)

        full_collected: list[Trade] = []
        async for trade in replay.stream_trades(sample_token.mint, duration_seconds=40.0):
            full_collected.append(trade)

        # Assert
        assert len(fast_collected) == 0
        assert len(full_collected) == 6

        await replay.disconnect()
