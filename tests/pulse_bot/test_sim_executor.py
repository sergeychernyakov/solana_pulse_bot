# tests/pulse_bot/test_sim_executor.py
"""Tests for the SimExecutor service — the gate that turns paper
trades into real on-chain simulations when ``PULSE_PAPER_USE_REAL_SIM=1``."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

from pulse_bot.services.sim_executor import (
    SimEntryResult,
    SimExecutor,
    SimExitResult,
)


def test_disabled_executor_short_circuits_entry():
    sim = SimExecutor(enabled=False)
    import asyncio
    res = asyncio.get_event_loop().run_until_complete(
        sim.simulate_entry("MintXYZ", sol_amount_lamports=1_000_000)
    ) if False else None
    # asyncio invocation pattern
    res = _await(sim.simulate_entry("MintXYZ", 1_000_000))
    assert isinstance(res, SimEntryResult)
    assert res.success is False
    assert res.err == "sim_disabled"


def test_disabled_executor_short_circuits_exit():
    sim = SimExecutor(enabled=False)
    res = _await(sim.simulate_exit("MintXYZ", 1_000_000))
    assert isinstance(res, SimExitResult)
    assert res.success is False
    assert res.err == "sim_disabled"


def test_bootstrap_off_when_env_missing(monkeypatch):
    monkeypatch.delenv("PULSE_PAPER_USE_REAL_SIM", raising=False)
    sim = SimExecutor.bootstrap()
    assert sim.enabled is False


def test_bootstrap_off_when_flag_zero(monkeypatch):
    monkeypatch.setenv("PULSE_PAPER_USE_REAL_SIM", "0")
    sim = SimExecutor.bootstrap()
    assert sim.enabled is False


def test_bootstrap_off_when_execution_init_fails(monkeypatch):
    """Even with the flag on, missing wallet/helius envs should leave
    the executor disabled — the bot keeps running with math-based
    paper trades rather than crashing."""
    monkeypatch.setenv("PULSE_PAPER_USE_REAL_SIM", "1")
    monkeypatch.delenv("SOL_WALLET_KEYPAIR", raising=False)
    monkeypatch.delenv("HELIUS_API_KEY", raising=False)
    sim = SimExecutor.bootstrap()
    # PumpFunExecution.from_env raises without those — bootstrap must
    # swallow and return disabled.
    assert sim.enabled is False


def test_entry_result_effective_price_basic():
    """expected_tokens=1e12 raw (= 1e6 human), sol_in=1e9 (=1 SOL).
    Effective price per token = 1 SOL / 1e6 tokens = 1e-6 SOL each."""
    res = SimEntryResult(
        success=True,
        expected_tokens_raw=1_000_000_000_000,
        sol_in_lamports=1_000_000_000,
    )
    assert res.effective_entry_price_sol() == pytest.approx(1e-6, rel=1e-9)


def test_entry_result_effective_price_zero_tokens():
    res = SimEntryResult(success=False, expected_tokens_raw=0, sol_in_lamports=1_000_000)
    assert res.effective_entry_price_sol() == 0.0


def test_exit_result_effective_price_basic():
    res = SimExitResult(
        success=True,
        expected_sol_out_lamports=500_000_000,  # 0.5 SOL
        tokens_in_raw=500_000_000_000,           # 5e5 human
    )
    # 0.5 SOL / 5e5 tokens = 1e-6 SOL per token
    assert res.effective_exit_price_sol() == pytest.approx(1e-6, rel=1e-9)


@dataclass
class _FakeExecutionResult:
    success: bool
    expected_tokens: int = 0
    expected_sol_out_lamports: int = 0
    sol_amount_lamports: int = 0
    slippage_bps_cap: int = 0
    units_consumed: int | None = None
    err: Any | None = None
    logs: list[str] | None = None


class _FakeExecution:
    """Minimal stand-in for :class:`PumpFunExecution`."""

    def __init__(self, *, buy_result, sell_result):
        self._buy = buy_result
        self._sell = sell_result
        self._rpc = self  # for close() — see below

    async def simulate_buy(self, *, mint, sol_amount_lamports, slippage_bps):
        # Mimic the real attr: pop sol amount onto the result.
        self._buy.sol_amount_lamports = sol_amount_lamports
        self._buy.slippage_bps_cap = slippage_bps
        return self._buy

    async def simulate_sell(self, *, mint, token_amount_raw, slippage_bps):
        self._sell.slippage_bps_cap = slippage_bps
        return self._sell

    async def close(self):
        pass


def test_simulate_entry_passes_through_success():
    fake = _FakeExecution(
        buy_result=_FakeExecutionResult(
            success=True, expected_tokens=12345, units_consumed=42_000,
        ),
        sell_result=_FakeExecutionResult(success=False),
    )
    sim = SimExecutor(enabled=True, slippage_bps=200, _execution=fake)
    res = _await(sim.simulate_entry("MintZ", 5_000_000))
    assert res.success is True
    assert res.expected_tokens_raw == 12345
    assert res.sol_in_lamports == 5_000_000
    assert res.slippage_bps_cap == 200
    assert res.units_consumed == 42_000


def test_simulate_entry_propagates_failure():
    fake = _FakeExecution(
        buy_result=_FakeExecutionResult(
            success=False, err={"InstructionError": [3, {"Custom": 6000}]},
        ),
        sell_result=_FakeExecutionResult(success=False),
    )
    sim = SimExecutor(enabled=True, _execution=fake)
    res = _await(sim.simulate_entry("MintZ", 1_000_000))
    assert res.success is False
    assert res.err is not None


def test_simulate_exit_passes_through_success():
    fake = _FakeExecution(
        buy_result=_FakeExecutionResult(success=False),
        sell_result=_FakeExecutionResult(
            success=True, expected_sol_out_lamports=900_000, units_consumed=33_000,
        ),
    )
    sim = SimExecutor(enabled=True, _execution=fake)
    res = _await(sim.simulate_exit("MintZ", 1_000_000_000))
    assert res.success is True
    assert res.expected_sol_out_lamports == 900_000
    assert res.tokens_in_raw == 1_000_000_000
    assert res.units_consumed == 33_000


def test_simulate_entry_handles_exception():
    class _Boom:
        async def simulate_buy(self, **kwargs):
            raise RuntimeError("network down")
        _rpc = property(lambda self: self)

        async def close(self):
            pass

    sim = SimExecutor(enabled=True, _execution=_Boom())
    res = _await(sim.simulate_entry("MintZ", 1_000_000))
    assert res.success is False
    assert res.err is not None
    assert "network down" in str(res.err)


# ── estimate_exit_curve_math (bonding-curve sell estimate) ───────
_VALID_MINT = "So11111111111111111111111111111111111111112"


class _FakeRpcCurve:
    """Stand-in RPC whose fetch_bonding_curve_state returns a preset."""

    def __init__(self, state):
        self._state = state

    async def fetch_bonding_curve_state(self, mint):
        return self._state


class _FakeExecCurve:
    def __init__(self, state):
        self._rpc = _FakeRpcCurve(state)


def _make_curve_state(*, complete: bool = False):
    from pulse_bot.execution_pumpfun import BondingCurveState
    from solders.pubkey import Pubkey

    return BondingCurveState(
        virtual_token_reserves=1_000_000_000_000,
        virtual_sol_reserves=30_000_000_000,
        real_token_reserves=800_000_000_000,
        real_sol_reserves=5_000_000_000,
        token_total_supply=1_000_000_000_000,
        complete=complete,
        creator=Pubkey.default(),
    )


def test_estimate_exit_disabled_short_circuits():
    sim = SimExecutor(enabled=False)
    res = _await(sim.estimate_exit_curve_math("MintXYZ", 1_000_000))
    assert res.success is False
    assert res.err == "sim_disabled"


def test_estimate_exit_rejects_non_positive_amount():
    sim = SimExecutor(enabled=True, _execution=_FakeExecCurve(_make_curve_state()))
    res = _await(sim.estimate_exit_curve_math("MintXYZ", 0))
    assert res.success is False
    assert res.err == "non_positive_token_amount"


def test_estimate_exit_curve_missing():
    sim = SimExecutor(enabled=True, _execution=_FakeExecCurve(None))
    res = _await(sim.estimate_exit_curve_math(_VALID_MINT, 1_000_000))
    assert res.success is False
    assert res.err == "bonding_curve_missing"


def test_estimate_exit_curve_complete_post_graduation():
    sim = SimExecutor(
        enabled=True, _execution=_FakeExecCurve(_make_curve_state(complete=True))
    )
    res = _await(sim.estimate_exit_curve_math(_VALID_MINT, 1_000_000))
    assert res.success is False
    assert res.err == "curve_complete_post_graduation"


def test_estimate_exit_curve_math_success():
    from pulse_bot.execution_pumpfun import estimate_sell_output_lamports

    state = _make_curve_state()
    sim = SimExecutor(enabled=True, slippage_bps=150, _execution=_FakeExecCurve(state))
    tokens = 10_000_000_000
    res = _await(sim.estimate_exit_curve_math(_VALID_MINT, tokens))
    assert res.success is True
    assert res.tokens_in_raw == tokens
    assert res.slippage_bps_cap == 150
    # Matches the pure curve-math helper exactly.
    assert res.expected_sol_out_lamports == estimate_sell_output_lamports(
        tokens, state
    )
    assert res.expected_sol_out_lamports > 0


# ── helpers ──────────────────────────────────────────────────
def _await(coro):
    """Sync test helper around asyncio.run for one-shot awaits."""
    import asyncio
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():  # pragma: no cover — pytest-asyncio mode
            return asyncio.ensure_future(coro)
    except RuntimeError:
        pass
    return asyncio.run(coro)
