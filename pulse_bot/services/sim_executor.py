# pulse_bot/services/sim_executor.py
"""Real on-chain simulation wrapper for paper trading.

When ``PULSE_PAPER_USE_REAL_SIM=1`` is set, the bot routes paper-
trade entries/exits through this service. Each paper trade then
reflects what would have happened on real pump.fun infrastructure:
slippage, account-init cost, protocol fees, and the occasional 6000
NotAuthorized that aborts entry. No SOL is spent — every call is
``simulateTransaction`` (free), but the inputs are signed with a
real keypair so the program executes normally.

Public surface:
  * ``SimExecutor.bootstrap()`` — lazy build a singleton from env.
  * ``await sim.simulate_entry(mint, sol_amount_lamports)`` →
    :class:`SimEntryResult` with the realised entry parameters.
  * ``await sim.simulate_exit(mint, token_amount_raw)`` →
    :class:`SimExitResult` with realised proceeds.

Errors propagate via ``success=False`` + ``err`` — the caller
SKIPs the paper trade rather than recording a phantom fill.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class SimEntryResult:
    success: bool
    expected_tokens_raw: int = 0
    sol_in_lamports: int = 0
    slippage_bps_cap: int = 0
    units_consumed: int | None = None
    err: Any | None = None
    raw_logs: list[str] | None = None

    def effective_entry_price_sol(self, decimals: int = 6) -> float:
        """SOL paid per token-in-human-units. Used as paper trade
        entry_price (matches the existing math-based units)."""
        if self.expected_tokens_raw <= 0:
            return 0.0
        sol = self.sol_in_lamports / 1e9
        tokens = self.expected_tokens_raw / (10**decimals)
        if tokens <= 0:
            return 0.0
        return sol / tokens


@dataclass
class SimExitResult:
    success: bool
    expected_sol_out_lamports: int = 0
    tokens_in_raw: int = 0
    slippage_bps_cap: int = 0
    units_consumed: int | None = None
    err: Any | None = None
    raw_logs: list[str] | None = None

    def effective_exit_price_sol(self, decimals: int = 6) -> float:
        if self.tokens_in_raw <= 0:
            return 0.0
        sol = self.expected_sol_out_lamports / 1e9
        tokens = self.tokens_in_raw / (10**decimals)
        if tokens <= 0:
            return 0.0
        return sol / tokens


@dataclass
class SimExecutor:
    """Async on-chain simulator. Wraps :class:`PumpFunExecution` —
    one instance per bot process; safe to share across coroutines.
    """

    enabled: bool = False
    slippage_bps: int = 100
    _execution: Any = field(default=None, repr=False)

    @classmethod
    def bootstrap(cls) -> "SimExecutor":
        """Build from env. Returns a disabled stub if the env flag
        is off — callers can always invoke methods; they short-circuit
        when ``enabled=False``.
        """
        flag = os.environ.get("PULSE_PAPER_USE_REAL_SIM", "").strip().lower()
        enabled = flag in {"1", "true", "yes", "on"}
        if not enabled:
            return cls(enabled=False)
        try:
            from pulse_bot.execution_pumpfun import PumpFunExecution

            ex = PumpFunExecution.from_env(allow_live_submit=False)
        except Exception as exc:
            # Don't crash the bot if SOL_WALLET / HELIUS env is
            # missing — just refuse to enable. Logged once.
            logger.warning("SimExecutor disabled: bootstrap failed (%s)", exc)
            return cls(enabled=False)
        slip_env = os.environ.get("PULSE_PAPER_SIM_SLIPPAGE_BPS", "100")
        try:
            slip = max(1, int(slip_env))
        except ValueError:
            slip = 100
        return cls(enabled=True, slippage_bps=slip, _execution=ex)

    async def simulate_entry(
        self,
        mint: str,
        sol_amount_lamports: int,
    ) -> SimEntryResult:
        if not self.enabled or self._execution is None:
            return SimEntryResult(success=False, err="sim_disabled")
        try:
            res = await self._execution.simulate_buy(
                mint=mint,
                sol_amount_lamports=sol_amount_lamports,
                slippage_bps=self.slippage_bps,
            )
        except Exception as exc:
            return SimEntryResult(
                success=False,
                err=f"sim_exception:{type(exc).__name__}:{exc!r}",
            )
        return SimEntryResult(
            success=res.success,
            expected_tokens_raw=res.expected_tokens or 0,
            sol_in_lamports=res.sol_amount_lamports,
            slippage_bps_cap=res.slippage_bps_cap,
            units_consumed=res.units_consumed,
            err=res.err,
            raw_logs=res.logs,
        )

    async def simulate_exit(
        self,
        mint: str,
        token_amount_raw: int,
    ) -> SimExitResult:
        if not self.enabled or self._execution is None:
            return SimExitResult(success=False, err="sim_disabled")
        try:
            res = await self._execution.simulate_sell(
                mint=mint,
                token_amount_raw=token_amount_raw,
                slippage_bps=self.slippage_bps,
            )
        except Exception as exc:
            return SimExitResult(
                success=False,
                err=f"sim_exception:{type(exc).__name__}:{exc!r}",
            )
        return SimExitResult(
            success=res.success,
            expected_sol_out_lamports=res.expected_sol_out_lamports,
            tokens_in_raw=token_amount_raw,
            slippage_bps_cap=res.slippage_bps_cap,
            units_consumed=res.units_consumed,
            err=res.err,
            raw_logs=res.logs,
        )

    async def estimate_exit_curve_math(
        self,
        mint: str,
        token_amount_raw: int,
    ) -> SimExitResult:
        """Estimate sell proceeds via the bonding-curve formula against
        the *live* curve state — no real position required.

        ``simulate_exit`` via ``simulateTransaction`` cannot run for a
        paper position: the wallet holds 0 tokens, so the program
        reverts with ``3012 AccountNotInitialized``. Instead this uses
        ``estimate_sell_output_lamports`` — the exact inverse-curve math
        the on-chain program runs — applied to the curve state fetched
        right now. Accounts for our own size's slippage on the curve,
        which the observed trade-stream price does not.

        Returns ``success=False`` when the curve account is missing or
        the token has graduated (``state.complete``) — recorded
        honestly so dashboards can tell a real estimate from a
        degenerate one.
        """
        if not self.enabled or self._execution is None:
            return SimExitResult(success=False, err="sim_disabled")
        if token_amount_raw <= 0:
            return SimExitResult(
                success=False,
                tokens_in_raw=token_amount_raw,
                err="non_positive_token_amount",
            )
        try:
            from pulse_bot.execution_pumpfun import (
                Pubkey,
                estimate_sell_output_lamports,
            )

            mint_pk = Pubkey.from_string(mint) if isinstance(mint, str) else mint
            state = await self._execution._rpc.fetch_bonding_curve_state(mint_pk)
            if state is None:
                return SimExitResult(
                    success=False,
                    tokens_in_raw=token_amount_raw,
                    err="bonding_curve_missing",
                )
            if state.complete:
                return SimExitResult(
                    success=False,
                    tokens_in_raw=token_amount_raw,
                    err="curve_complete_post_graduation",
                )
            sol_out = estimate_sell_output_lamports(token_amount_raw, state)
        except Exception as exc:  # noqa: BLE001 — boundary
            return SimExitResult(
                success=False,
                tokens_in_raw=token_amount_raw,
                err=f"sim_exception:{type(exc).__name__}:{exc!r}",
            )
        return SimExitResult(
            success=True,
            expected_sol_out_lamports=int(sol_out),
            tokens_in_raw=token_amount_raw,
            slippage_bps_cap=self.slippage_bps,
        )

    async def close(self) -> None:
        """Close the underlying RPC session. Idempotent."""
        if self._execution is not None:
            # Best-effort RPC close on shutdown — failure is benign.
            try:
                await self._execution._rpc.close()
            except Exception:  # nosec B110
                pass
            self._execution = None
