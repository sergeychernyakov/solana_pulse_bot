# tests/pulse_bot/test_execution_live.py
"""Tests for LiveExecution — keypair load, dry-run behavior, API contracts.

No live API calls. Uses a fake aiohttp session injected into LiveExecution
to exercise the quote / swap / sign paths.
"""

from __future__ import annotations

import asyncio
import base64
import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from pulse_bot.execution_live import (
    DEFAULT_PRIORITY_FEE_LAMPORTS,
    DEFAULT_SLIPPAGE_BPS,
    JUPITER_QUOTE_URL,
    JUPITER_SWAP_URL,
    SOL_MINT,
    ExecutionResult,
    LiveExecution,
)

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")


# ── Dry-run (no keypair needed) ─────────────────────────────────────


def test_dry_run_is_default() -> None:
    exe = LiveExecution(helius_api_key="x", keypair_base58=None)
    assert exe.dry_run is True


def test_is_ready_in_dry_run_without_keypair() -> None:
    exe = LiveExecution(helius_api_key="", keypair_base58=None, dry_run=True)
    # dry_run mode short-circuits before actual submission, so "ready".
    assert exe.is_ready is True


def test_is_ready_in_live_requires_keypair_and_key() -> None:
    exe = LiveExecution(
        helius_api_key="h",
        keypair_base58=None,
        dry_run=False,
    )
    assert exe.is_ready is False


def test_bad_keypair_returns_none_no_crash() -> None:
    exe = LiveExecution(helius_api_key="x", keypair_base58="!!!not-base58")
    assert exe._keypair is None
    assert exe.wallet_address is None


def test_valid_keypair_loads(tmp_path_factory) -> None:
    # Generate a fresh random keypair for the test (no secret exposure).
    from solders.keypair import Keypair

    kp = Keypair()
    b58 = str(kp)  # solders encodes Keypair → base58 via __str__
    exe = LiveExecution(helius_api_key="x", keypair_base58=b58)
    assert exe._keypair is not None
    assert exe.wallet_address == str(kp.pubkey())


def test_execution_result_dataclass_defaults() -> None:
    r = ExecutionResult(side="buy", mint="mintX", dry_run=True, success=False)
    assert r.signature is None
    assert r.in_amount_raw == 0
    assert r.extra == {}


# ── Async flow with mocked aiohttp ─────────────────────────────────


class FakeResponse:
    def __init__(self, status: int = 200, payload: dict | None = None, text: str = ""):
        self.status = status
        self._payload = payload or {}
        self._text = text or json.dumps(payload)

    async def __aenter__(self) -> "FakeResponse":
        return self

    async def __aexit__(self, *a) -> None:
        return None

    async def json(self) -> dict:
        return self._payload

    async def text(self) -> str:
        return self._text


class FakeSession:
    """Minimal stand-in for aiohttp.ClientSession — records calls, returns
    queued responses."""

    def __init__(self) -> None:
        self.get_calls: list[tuple[str, dict]] = []
        self.post_calls: list[tuple[str, dict]] = []
        self.get_response: FakeResponse | None = None
        self.post_responses: dict[str, FakeResponse] = {}

    def get(self, url, params=None, timeout=None) -> FakeResponse:
        self.get_calls.append((url, params or {}))
        return self.get_response or FakeResponse(200, {})

    def post(self, url, json=None, timeout=None) -> FakeResponse:
        self.post_calls.append((url, json or {}))
        # Match response by URL (Jupiter swap vs Helius RPC).
        for prefix, resp in self.post_responses.items():
            if prefix in url:
                return resp
        return FakeResponse(200, {})

    async def close(self) -> None:
        pass


@pytest.mark.asyncio
async def test_dry_run_buy_returns_success_no_send() -> None:
    exe = LiveExecution(helius_api_key="x", keypair_base58=None, dry_run=True)
    fake = FakeSession()
    fake.get_response = FakeResponse(
        200,
        {
            "outAmount": "123456",
            "routePlan": [{"swapInfo": {}}, {"swapInfo": {}}],
        },
    )
    exe._session = fake
    result = await exe.buy("MINTxxx", amount_sol=0.1, slippage_bps=500)
    assert result.dry_run is True
    assert result.success is True
    assert result.out_amount_raw == 123456
    assert result.signature is None
    # Ensure we called Jupiter quote but NOT swap (dry-run short-circuit).
    assert any(JUPITER_QUOTE_URL in c[0] for c in fake.get_calls)
    assert all(JUPITER_SWAP_URL not in c[0] for c in fake.post_calls)


@pytest.mark.asyncio
async def test_dry_run_sell_lamports_conversion() -> None:
    exe = LiveExecution(helius_api_key="x", keypair_base58=None, dry_run=True)
    fake = FakeSession()
    fake.get_response = FakeResponse(200, {"outAmount": "999", "routePlan": []})
    exe._session = fake
    await exe.sell("MINTxxx", token_amount_raw=5_000_000, slippage_bps=300)
    # Check quote params: inputMint should be the token, outputMint SOL
    assert fake.get_calls[0][1]["inputMint"] == "MINTxxx"
    assert fake.get_calls[0][1]["outputMint"] == SOL_MINT
    assert fake.get_calls[0][1]["amount"] == "5000000"
    assert fake.get_calls[0][1]["slippageBps"] == "300"


@pytest.mark.asyncio
async def test_quote_timeout_returns_error() -> None:
    exe = LiveExecution(helius_api_key="x", keypair_base58=None, dry_run=True)
    fake = FakeSession()
    fake.get_response = FakeResponse(500, {}, text="server error")
    exe._session = fake
    result = await exe.buy("MINTxxx", amount_sol=0.1)
    assert result.success is False
    assert result.error == "quote_failed"


@pytest.mark.asyncio
async def test_live_mode_without_keypair_fails_gracefully() -> None:
    exe = LiveExecution(helius_api_key="x", keypair_base58=None, dry_run=False)
    fake = FakeSession()
    fake.get_response = FakeResponse(200, {"outAmount": "100", "routePlan": []})
    exe._session = fake
    result = await exe.buy("MINTxxx", amount_sol=0.1)
    assert result.success is False
    assert result.error == "not_ready"


@pytest.mark.asyncio
async def test_buy_params_lamports_conversion() -> None:
    exe = LiveExecution(helius_api_key="x", keypair_base58=None, dry_run=True)
    fake = FakeSession()
    fake.get_response = FakeResponse(200, {"outAmount": "1", "routePlan": []})
    exe._session = fake
    await exe.buy("MINTxxx", amount_sol=0.5)
    # 0.5 SOL = 500_000_000 lamports
    assert fake.get_calls[0][1]["amount"] == "500000000"
    assert fake.get_calls[0][1]["slippageBps"] == str(DEFAULT_SLIPPAGE_BPS)


def test_from_env_reads_vars(monkeypatch) -> None:
    monkeypatch.setenv("HELIUS_API_KEY", "key123")
    monkeypatch.delenv("SOL_WALLET_KEYPAIR", raising=False)
    exe = LiveExecution.from_env()
    assert exe._helius_api_key == "key123"
    assert exe._keypair is None
    assert exe.dry_run is True
