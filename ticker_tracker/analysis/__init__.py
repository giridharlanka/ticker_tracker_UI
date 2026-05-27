"""Portfolio analysis: fundamentals, technicals, and LLM insights."""

from ticker_tracker.analysis.base import (
    FundamentalsResult,
    LLMAnalysis,
    SignalLabel,
    TechnicalResult,
)
from ticker_tracker.analysis.fundamentals import FundamentalsAdapter
from ticker_tracker.analysis.llm_analyst import (
    LLMAnalyst,
    is_llm_available,
    is_ollama_available,
    llm_offline_hint,
)
from ticker_tracker.analysis.prompt_builder import (
    build_analysis_prompt,
    build_portfolio_summary_prompt,
)
from ticker_tracker.analysis.technicals import TechnicalAnalyser, analyse_ohlcv, generate_signals

__all__ = [
    "FundamentalsAdapter",
    "FundamentalsResult",
    "LLMAnalysis",
    "LLMAnalyst",
    "SignalLabel",
    "TechnicalAnalyser",
    "TechnicalResult",
    "analyse_ohlcv",
    "build_analysis_prompt",
    "build_portfolio_summary_prompt",
    "generate_signals",
    "is_llm_available",
    "is_ollama_available",
    "llm_offline_hint",
]
