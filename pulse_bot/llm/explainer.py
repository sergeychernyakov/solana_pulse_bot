# pulse_bot/llm/explainer.py
"""LangChain LCEL chain that turns a raw paper_trade row into a
structured human-readable explanation.

Architecture (LCEL pipeline):

    prompt | llm | parser

* ``prompt`` — :class:`ChatPromptTemplate` with system + user
  messages. The system message defines the analyst persona; the
  user message embeds trade-specific data via ``{placeholders}``.
* ``llm`` — Claude Haiku 4.5 (cheap, ~150ms median latency, $1/M
  input tokens). Picked over GPT-4 / Claude Opus because the task
  is templated and doesn't need frontier reasoning.
* ``parser`` — :class:`PydanticOutputParser` keyed off
  :class:`TradeExplanation`. The schema enforces a deterministic
  shape so downstream consumers (dashboard, CHANGELOG bot, alerts)
  can rely on field names instead of free-text.

The chain is designed to be called either:

* Directly as a synchronous function for one-off explanations
  (:func:`explain_trade`)
* Composed inside a larger :mod:`langgraph` workflow as one node
  among several (see :mod:`pulse_bot.llm.analysis_graph`).
"""

from __future__ import annotations

import logging
import os
from typing import Any, Mapping

from langchain_anthropic import ChatAnthropic
from langchain_core.output_parsers import PydanticOutputParser
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.runnables import Runnable
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


# ── Output schema ────────────────────────────────────────────────
class TradeExplanation(BaseModel):
    """Structured narrative of a single paper_trade decision.

    Fields are intentionally short — this gets rendered into a
    dashboard tooltip and a Telegram alert, not a research report.
    """

    entry_thesis: str = Field(
        description="One-sentence reason the bot entered this trade. "
        "Reference the entry_type (rules vs ml_override) and the "
        "score / proba range.",
    )
    exit_assessment: str = Field(
        description="One-sentence reason the bot exited. If "
        "exit_reason is 'survival_predict' or 'dead_token', be "
        "explicit that the model fired a kill switch.",
    )
    quality_grade: str = Field(
        description="One of: 'good', 'neutral', 'bad'. "
        "'good' = winning trade with sound exit timing. "
        "'bad' = losing trade where exit looks suspicious "
        "(e.g. fee-only loss, kill before signal).",
    )
    follow_up: str = Field(
        description="One actionable suggestion: model retrain, "
        "config tweak, or 'monitor only'.",
    )


# ── Prompt template ──────────────────────────────────────────────
_SYSTEM_PROMPT = """\
You are a senior ML engineer reviewing paper-trades from a Solana \
memecoin trading bot (pump.fun launchpad). The bot uses an XGBoost \
ensemble (entry classifier + T+30 early-decision + regression PnL \
prediction + survival hazard model) to decide entry/exit.

Critical context for grading:
- Average position size is ~0.03-0.1 SOL.
- A loss of EXACTLY -0.0070 SOL with hold ~95-105s is the survival \
model's signature kill (transaction-cost-only loss, token didn't \
move). Flag these as 'bad' quality — the kill switch fires before \
the trade has a chance to develop.
- 'dead_token' exit at hold 100-200s with mild positive/negative \
PnL is healthy — the bot held until token died and exited cleanly.
- 'hard_stop' at -15% pnl_pct is normal SL behaviour, neutral grade.
- 'take_profit' at +30% is the configured TP, good grade.
- entry_type='ml_override' means the rule-based scorer said SKIP \
but the ML ensemble overrode to BUY — high-confidence ML signal.

Output ONLY the JSON described in the format instructions. No prose.
"""

_USER_PROMPT = """\
Trade row:
  mint:               {mint}
  symbol:             {symbol}
  entry_type:         {entry_type}
  entry_score:        {entry_score}
  entry_buyer_number: {entry_buyer_number}
  entry_mcap_sol:     {entry_mcap_sol}
  exit_reason:        {exit_reason}
  exit_mcap_sol:      {exit_mcap_sol}
  hold_seconds:       {hold_seconds}
  pnl_sol:            {pnl_sol}
  pnl_pct:            {pnl_pct}
  total_buys:         {total_buys}
  total_sells:        {total_sells}

{format_instructions}
"""


# ── Chain assembly ───────────────────────────────────────────────
def build_trade_explainer_chain(
    *,
    model_name: str = "claude-haiku-4-5",
    temperature: float = 0.0,
    api_key: str | None = None,
) -> Runnable:
    """Build the LCEL chain ``prompt | llm | parser``.

    Args:
        model_name: Anthropic model id. Default ``claude-haiku-4-5``
            for cheap/fast templated tasks.
        temperature: Sampling temperature. ``0.0`` for reproducible
            outputs (this matters when the explanation is written
            back to the trades table).
        api_key: Override ``ANTHROPIC_API_KEY`` env var. Mainly for
            tests.

    Returns:
        A :class:`Runnable` that takes a ``dict`` of trade fields
        and returns a :class:`TradeExplanation` instance.
    """
    parser = PydanticOutputParser(pydantic_object=TradeExplanation)

    prompt = ChatPromptTemplate.from_messages(
        [
            ("system", _SYSTEM_PROMPT),
            ("user", _USER_PROMPT),
        ]
    ).partial(format_instructions=parser.get_format_instructions())

    llm = ChatAnthropic(
        model=model_name,
        temperature=temperature,
        api_key=api_key or os.environ.get("ANTHROPIC_API_KEY"),
        max_tokens=512,
    )

    return prompt | llm | parser


def explain_trade(
    trade: Mapping[str, Any],
    *,
    chain: Runnable | None = None,
) -> TradeExplanation:
    """One-shot helper: build chain (if not given) and invoke it.

    Args:
        trade: Mapping with the paper_trade columns referenced in
            the prompt template (mint, symbol, entry_type, etc.).
            Missing keys default to empty string.
        chain: Optional pre-built chain. Reuse it across many calls
            to avoid re-instantiating the LLM client.

    Returns:
        :class:`TradeExplanation` with structured narrative fields.
    """
    if chain is None:
        chain = build_trade_explainer_chain()

    # Defensive: PromptTemplate raises KeyError on missing key, but
    # paper_trades may have NULL columns (open trades, partial fills).
    # Fall back to empty string per field — the LLM is robust to
    # "n/a" tokens.
    payload = {
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

    return chain.invoke(payload)
