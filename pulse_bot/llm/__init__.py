# pulse_bot/llm/__init__.py
"""LLM-based post-hoc explainability for paper_trades.

This package is OFF the hot path — LangChain / LangGraph calls are
~500-1500ms per LLM invocation, far too slow for live entry/exit
decisions (T+30 budget is <100ms). It runs asynchronously, after a
trade has closed, to generate human-readable narratives of *why*
each ML decision happened.

Two layers:

* :mod:`pulse_bot.llm.explainer` — bare LangChain chain (LCEL):
  prompt → LLM → Pydantic-parsed structured output. Useful as a
  one-shot building block.

* :mod:`pulse_bot.llm.analysis_graph` — LangGraph state machine that
  composes multiple LLM calls (analyst + critic + summary) with
  conditional branching for open vs closed trades. Demonstrates
  the multi-agent orchestration pattern.
"""

from pulse_bot.llm.explainer import (
    TradeExplanation,
    build_trade_explainer_chain,
    explain_trade,
)
from pulse_bot.llm.analysis_graph import (
    AnalysisState,
    build_analysis_graph,
    run_analysis,
)

__all__ = [
    "TradeExplanation",
    "build_trade_explainer_chain",
    "explain_trade",
    "AnalysisState",
    "build_analysis_graph",
    "run_analysis",
]
