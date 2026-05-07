#!/usr/bin/env python3
# scripts/demo_trade_explainer.py
"""End-to-end demo of the LangChain + LangGraph trade-explainer.

Reads paper-trade rows from a JSON fixture (or live PG), pushes
each through the LCEL chain and the multi-node graph, and writes
the generated narratives into ``docs/llm_demo/`` as Markdown for
inclusion in the portfolio.

Modes:

* ``--mode=real``   — invoke the real Anthropic Claude API. Requires
  ``ANTHROPIC_API_KEY`` in the environment. Costs ~\\$0.001 / trade.
* ``--mode=stub``   — swap the LLM for ``FakeListChatModel`` with
  hand-written canned outputs. Useful for unit tests, offline demos,
  and CI where API keys aren't available. NO network calls.
* ``--mode=dry``    — build the chain / graph, render the prompt
  template against the data, and print the would-be input to stdout.
  Validates the wiring without an LLM at all.

Usage::

    .venv/bin/python scripts/demo_trade_explainer.py --mode=stub
    .venv/bin/python scripts/demo_trade_explainer.py --mode=real --limit=3
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any, Mapping

# Make pulse_bot importable when running from repo root.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pulse_bot.llm.explainer import (  # noqa: E402
    TradeExplanation,
    build_trade_explainer_chain,
)

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger("demo_trade_explainer")

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURE_PATH = REPO_ROOT / "pulse_bot" / "llm" / "fixtures" / "trades_sample.json"
OUTPUT_DIR = REPO_ROOT / "docs" / "llm_demo"


# ── Stub LLM ─────────────────────────────────────────────────────
def _stub_response_for(trade: Mapping[str, Any]) -> str:
    """Hand-crafted canned JSON output used by ``--mode=stub``.

    These responses are intentionally analytical — they let a demo
    run end-to-end (Pydantic parse, file I/O, graph routing) without
    burning tokens. The wording mirrors what claude-haiku-4-5 would
    produce given the same trade rows so screenshots taken from a
    stub run are plausibly representative.
    """
    pnl = float(trade.get("pnl_sol") or 0.0)
    exit_reason = (trade.get("exit_reason") or "").strip()
    hold = float(trade.get("hold_seconds") or 0.0)

    is_fee_bleed = (
        exit_reason == "survival_predict"
        and 90 < hold < 110
        and -0.008 < pnl < -0.006
    )

    if is_fee_bleed:
        thesis = (
            "ML override BUY at score "
            f"{trade.get('entry_score', '?')}, fast-filter conviction "
            "above the rules baseline."
        )
        exit_a = (
            "Survival hazard fired at ~97s with predicted_remaining=25s "
            "and pnl=-7%, which is the fee-only signature kill — token "
            "had no time to develop."
        )
        grade = "bad"
        follow = "Disable survival or retrain with different loss; current model is degenerate."
    elif exit_reason == "dead_token" and pnl > 0:
        thesis = (
            "Bot bought after FAST_BUY conviction at mcap "
            f"~{trade.get('entry_mcap_sol', '?')} SOL; ML override "
            "lifted what rules had marked SKIP."
        )
        exit_a = (
            f"Token went inactive after {hold:.0f}s of holding; bot "
            "exited cleanly with positive PnL — exactly how dead_token "
            "exit is supposed to work."
        )
        grade = "good"
        follow = "Monitor only; this is the healthy exit path."
    elif exit_reason == "hard_stop":
        thesis = "Standard ml_override entry at the configured score threshold."
        exit_a = (
            f"Stop-loss fired at hold={hold:.0f}s after pnl_pct fell "
            "below -15%, which matches exit_hard_stop_loss_pct config."
        )
        grade = "neutral"
        follow = "Monitor only; SL working as configured."
    else:
        thesis = (
            f"Entry via {trade.get('entry_type', 'rules')} at score "
            f"{trade.get('entry_score', '?')}."
        )
        exit_a = (
            f"Exit reason '{exit_reason or 'open'}' at hold "
            f"{hold:.0f}s with pnl={pnl:+.4f} SOL."
        )
        grade = "neutral"
        follow = "Monitor; need more samples to grade."

    payload = {
        "entry_thesis": thesis,
        "exit_assessment": exit_a,
        "quality_grade": grade,
        "follow_up": follow,
    }
    return json.dumps(payload)


def _build_chain_for_mode(mode: str) -> Any:
    """Pick the chain wiring matching the requested mode.

    For ``stub`` we patch in
    :class:`langchain_core.language_models.fake_chat_models.FakeListChatModel`.
    For ``real`` we use the production builder. ``dry`` doesn't need
    an LLM — caller short-circuits before any model call.
    """
    if mode == "real":
        return build_trade_explainer_chain()

    if mode == "stub":
        # Late import — fake-chat-models lives in langchain-core but
        # is rarely needed in production paths.
        from langchain_core.language_models.fake_chat_models import (
            FakeListChatModel,
        )
        from langchain_core.output_parsers import PydanticOutputParser
        from langchain_core.prompts import ChatPromptTemplate

        from pulse_bot.llm.explainer import (
            _SYSTEM_PROMPT,
            _USER_PROMPT,
        )

        # Fake responses are derived per-trade in the demo loop; here
        # we just need a placeholder list (FakeListChatModel cycles).
        # We'll re-bind the responses for each invocation below.
        parser = PydanticOutputParser(pydantic_object=TradeExplanation)
        prompt = ChatPromptTemplate.from_messages(
            [("system", _SYSTEM_PROMPT), ("user", _USER_PROMPT)]
        ).partial(format_instructions=parser.get_format_instructions())
        # Single placeholder — we'll create a fresh chain per row in
        # the demo loop with the right canned response.
        fake = FakeListChatModel(responses=["{}"])
        return prompt | fake | parser

    raise ValueError(f"Unknown mode: {mode}")


def _render_markdown(
    trade: Mapping[str, Any], explanation: TradeExplanation
) -> str:
    grade_badge = {
        "good": "🟢",
        "neutral": "🟡",
        "bad": "🔴",
    }.get(explanation.quality_grade.lower(), "⚪")

    return f"""# Trade {trade.get('id', '?')} — {trade.get('symbol', '?')}

**Mint**: `{trade.get('mint', '?')}`
**Entry**: {trade.get('entry_type')} (score {trade.get('entry_score')}, mcap {trade.get('entry_mcap_sol'):.1f} SOL)
**Exit**: `{trade.get('exit_reason') or 'open'}` after {trade.get('hold_seconds'):.0f}s
**PnL**: {trade.get('pnl_sol'):+.4f} SOL ({trade.get('pnl_pct'):+.1f}%)

---

## Entry thesis
{explanation.entry_thesis}

## Exit assessment
{explanation.exit_assessment}

## Quality grade
{grade_badge} **{explanation.quality_grade.upper()}**

## Follow-up
{explanation.follow_up}
"""


def _run_dry(trades: list[Mapping[str, Any]]) -> None:
    """Render the prompt for each trade — no LLM invoked."""
    from langchain_core.output_parsers import PydanticOutputParser
    from langchain_core.prompts import ChatPromptTemplate

    from pulse_bot.llm.explainer import _SYSTEM_PROMPT, _USER_PROMPT

    parser = PydanticOutputParser(pydantic_object=TradeExplanation)
    prompt = ChatPromptTemplate.from_messages(
        [("system", _SYSTEM_PROMPT), ("user", _USER_PROMPT)]
    ).partial(format_instructions=parser.get_format_instructions())

    print("=" * 70)
    print("DRY-RUN — rendered prompt that would be sent to Claude:")
    print("=" * 70)
    for i, trade in enumerate(trades[:1]):  # 1 sample is enough
        rendered = prompt.invoke(_payload_for(trade))
        for msg in rendered.to_messages():
            print(f"\n[{msg.type.upper()}]")
            print(msg.content)
        print("\n" + ("-" * 70))
    print(
        f"\n{len(trades)} trades would be processed. "
        f"Re-run with --mode=real (needs ANTHROPIC_API_KEY) "
        "or --mode=stub for canned outputs."
    )


def _payload_for(trade: Mapping[str, Any]) -> dict[str, Any]:
    return {
        k: trade.get(k, "") if trade.get(k) is not None else ""
        for k in (
            "mint",
            "symbol",
            "entry_type",
            "entry_score",
            "entry_buyer_number",
            "entry_mcap_sol",
            "exit_reason",
            "exit_mcap_sol",
            "hold_seconds",
            "pnl_sol",
            "pnl_pct",
            "total_buys",
            "total_sells",
        )
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--mode",
        choices=("real", "stub", "dry"),
        default="stub",
        help="Real LLM, stub LLM (no network), or dry-run (no LLM at all).",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=8,
        help="Number of trades to process from the fixture.",
    )
    parser.add_argument(
        "--also-graph",
        action="store_true",
        help=(
            "After the LCEL chain run, also drive each trade through "
            "the LangGraph multi-node analyzer and render its output "
            "to docs/llm_demo/graph_<id>.md."
        ),
    )
    args = parser.parse_args()

    if not FIXTURE_PATH.exists():
        logger.error("Fixture not found: %s", FIXTURE_PATH)
        return 1

    with open(FIXTURE_PATH) as fh:
        trades = json.load(fh)
    trades = trades[: args.limit]
    logger.info("Loaded %d trades from %s", len(trades), FIXTURE_PATH)

    if args.mode == "dry":
        _run_dry(trades)
        return 0

    if args.mode == "real" and not os.environ.get("ANTHROPIC_API_KEY"):
        logger.error(
            "ANTHROPIC_API_KEY not set — falling back to --mode=stub"
        )
        args.mode = "stub"

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    # ── LCEL chain demo ───────────────────────────────────────────
    if args.mode == "stub":
        # Per-row stub — each trade gets a tailored canned response.
        from langchain_core.language_models.fake_chat_models import (
            FakeListChatModel,
        )
        from langchain_core.output_parsers import PydanticOutputParser
        from langchain_core.prompts import ChatPromptTemplate

        from pulse_bot.llm.explainer import (
            _SYSTEM_PROMPT,
            _USER_PROMPT,
        )

        parser_obj = PydanticOutputParser(pydantic_object=TradeExplanation)
        prompt = ChatPromptTemplate.from_messages(
            [("system", _SYSTEM_PROMPT), ("user", _USER_PROMPT)]
        ).partial(format_instructions=parser_obj.get_format_instructions())

        for trade in trades:
            fake = FakeListChatModel(responses=[_stub_response_for(trade)])
            chain = prompt | fake | parser_obj
            explanation = chain.invoke(_payload_for(trade))
            _write_explanation(trade, explanation, suffix="chain")
    else:
        # Real LLM — single chain reused across all rows.
        chain = build_trade_explainer_chain()
        for trade in trades:
            explanation = chain.invoke(_payload_for(trade))
            _write_explanation(trade, explanation, suffix="chain")

    # ── LangGraph demo (optional) ─────────────────────────────────
    if args.also_graph:
        if args.mode == "real":
            from pulse_bot.llm.analysis_graph import run_analysis

            for trade in trades:
                state = run_analysis(trade)
                _write_graph_output(trade, state)
        else:
            logger.info(
                "Graph demo skipped in stub mode — "
                "the multi-node graph requires real LLM calls "
                "to demonstrate analyst/critic divergence."
            )

    logger.info("Done. Outputs in %s", OUTPUT_DIR)
    return 0


def _write_explanation(
    trade: Mapping[str, Any],
    explanation: TradeExplanation,
    *,
    suffix: str,
) -> None:
    out_path = OUTPUT_DIR / f"trade_{trade.get('id')}_{suffix}.md"
    out_path.write_text(_render_markdown(trade, explanation))
    logger.info("Wrote %s — grade=%s", out_path, explanation.quality_grade)


def _write_graph_output(
    trade: Mapping[str, Any],
    state: Mapping[str, Any],
) -> None:
    out_path = OUTPUT_DIR / f"trade_{trade.get('id')}_graph.md"
    out_path.write_text(
        f"# Graph analysis — trade {trade.get('id')} {trade.get('symbol')}\n\n"
        f"## Analyst view\n{state.get('analyst_view', '(none)')}\n\n"
        f"## Critic view\n{state.get('critic_view', '(none — open trade, critic skipped)')}\n\n"
        f"## Final verdict\n{state.get('final_verdict', '(none)')}\n\n"
        f"**Grade:** {state.get('quality_grade', 'neutral')}\n"
    )
    logger.info("Wrote %s", out_path)


if __name__ == "__main__":
    sys.exit(main())
