# tests/pulse_bot/test_collector.py
"""Tests for HeliusCollector — parsing, trade detection, DB writing."""

from __future__ import annotations

import asyncio
import os
import sqlite3
import tempfile

import pytest

from pulse_bot.collector import HeliusCollector

# ---------------------------------------------------------------------------
# Test Helius transaction parsing
# ---------------------------------------------------------------------------

SAMPLE_TX_BUY = {
    "type": "SWAP",
    "source": "PUMP_FUN",
    "fee": 5000,
    "feePayer": "BuyerWallet111111111111111111111111111111111",
    "signature": "sig123",
    "timestamp": 1700000000,
    "tokenTransfers": [
        {
            "fromUserAccount": "CurveAccount1111111111111111111111111111111",
            "toUserAccount": "BuyerWallet111111111111111111111111111111111",
            "tokenAmount": 5000000.0,
            "mint": "TokenMint111111111111111111111111111111pump",
        }
    ],
    "nativeTransfers": [],
    "accountData": [
        {
            "account": "BuyerWallet111111111111111111111111111111111",
            "nativeBalanceChange": -500000000,  # spent 0.5 SOL
            "tokenBalanceChanges": [],
        }
    ],
}

SAMPLE_TX_SELL = {
    "type": "SWAP",
    "source": "PUMP_FUN",
    "fee": 5000,
    "feePayer": "SellerWallet11111111111111111111111111111111",
    "signature": "sig456",
    "timestamp": 1700000010,
    "tokenTransfers": [
        {
            "fromUserAccount": "SellerWallet11111111111111111111111111111111",
            "toUserAccount": "CurveAccount1111111111111111111111111111111",
            "tokenAmount": 3000000.0,
            "mint": "TokenMint111111111111111111111111111111pump",
        }
    ],
    "nativeTransfers": [],
    "accountData": [
        {
            "account": "SellerWallet11111111111111111111111111111111",
            "nativeBalanceChange": 300000000,  # received 0.3 SOL
            "tokenBalanceChanges": [],
        }
    ],
}

SAMPLE_TX_NOT_PUMP = {
    "type": "SWAP",
    "source": "RAYDIUM",
    "fee": 5000,
    "feePayer": "SomeWallet",
    "timestamp": 1700000020,
    "tokenTransfers": [],
    "accountData": [{"account": "SomeWallet", "nativeBalanceChange": -100000000}],
}

MINT = "TokenMint111111111111111111111111111111pump"


class TestHeliusParser:
    """Test transaction parsing logic."""

    def test_parse_buy(self) -> None:
        """Negative SOL balance change = BUY."""
        collector = HeliusCollector.__new__(HeliusCollector)
        collector._api_key = "fake"
        trade = collector._parse_transaction(SAMPLE_TX_BUY, MINT)

        assert trade is not None
        assert trade["tx_type"] == "buy"
        assert trade["wallet"] == "BuyerWallet111111111111111111111111111111111"
        assert trade["sol_amount"] == pytest.approx(0.5)
        assert trade["token_amount"] == pytest.approx(5000000.0)
        assert trade["timestamp"] == 1700000000

    def test_parse_sell(self) -> None:
        """Positive SOL balance change = SELL."""
        collector = HeliusCollector.__new__(HeliusCollector)
        collector._api_key = "fake"
        trade = collector._parse_transaction(SAMPLE_TX_SELL, MINT)

        assert trade is not None
        assert trade["tx_type"] == "sell"
        assert trade["wallet"] == "SellerWallet11111111111111111111111111111111"
        assert trade["sol_amount"] == pytest.approx(0.3)
        assert trade["token_amount"] == pytest.approx(3000000.0)

    def test_parse_non_pump_returns_none(self) -> None:
        """Non PUMP_FUN source → None."""
        collector = HeliusCollector.__new__(HeliusCollector)
        collector._api_key = "fake"
        trade = collector._parse_transaction(SAMPLE_TX_NOT_PUMP, MINT)

        assert trade is None

    def test_parse_zero_sol_change_returns_none(self) -> None:
        """Zero SOL balance change → None."""
        tx = {
            "type": "SWAP",
            "source": "PUMP_FUN",
            "feePayer": "W",
            "timestamp": 100,
            "tokenTransfers": [{"mint": MINT, "tokenAmount": 1000}],
            "accountData": [{"account": "W", "nativeBalanceChange": 0}],
        }
        collector = HeliusCollector.__new__(HeliusCollector)
        collector._api_key = "fake"
        assert collector._parse_transaction(tx, MINT) is None

    def test_parse_zero_tokens_returns_none(self) -> None:
        """Zero token amount → None."""
        tx = {
            "type": "SWAP",
            "source": "PUMP_FUN",
            "feePayer": "W",
            "timestamp": 100,
            "tokenTransfers": [{"mint": MINT, "tokenAmount": 0}],
            "accountData": [{"account": "W", "nativeBalanceChange": -100000}],
        }
        collector = HeliusCollector.__new__(HeliusCollector)
        collector._api_key = "fake"
        assert collector._parse_transaction(tx, MINT) is None


class TestHeliusApiKeyRequired:
    """API key validation."""

    def test_no_key_raises(self) -> None:
        """HeliusCollector raises if no API key."""
        old = os.environ.pop("HELIUS_API_KEY", None)
        try:
            with pytest.raises(ValueError, match="HELIUS_API_KEY"):
                HeliusCollector(api_key=None)
        finally:
            if old:
                os.environ["HELIUS_API_KEY"] = old


class TestHeliusLiveApi:
    """Test real Helius API (requires HELIUS_API_KEY)."""

    @pytest.mark.skipif(
        not os.environ.get("HELIUS_API_KEY"),
        reason="HELIUS_API_KEY not set",
    )
    def test_download_real_trades(self) -> None:
        """Download trades for a known Pump.fun token."""
        collector = HeliusCollector()

        async def _run() -> list[dict]:
            return await collector.get_trades_for_mint(
                "149fj6cqUCLnQGRCVdtRFPTdesWsiHZdwQ4mJ5TLpump",
                limit=5,
            )

        trades = asyncio.run(_run())
        assert len(trades) > 0
        assert trades[0]["tx_type"] in ("buy", "sell")
        assert trades[0]["sol_amount"] > 0
        assert trades[0]["token_amount"] > 0
        assert trades[0]["timestamp"] > 0
