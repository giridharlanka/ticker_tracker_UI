"""Shared dataclasses for the analysis module."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class FundamentalsResult:
    ticker: str
    source: str
    fetched_at: datetime
    fundamentals_available: bool  # False for tickers with sparse data (e.g. some SGX)

    # Valuation
    pe_ratio: float | None = None
    pb_ratio: float | None = None
    ev_ebitda: float | None = None
    price_to_sales: float | None = None
    dividend_yield: float | None = None

    # Profitability
    gross_margin: float | None = None
    operating_margin: float | None = None
    net_margin: float | None = None
    roe: float | None = None
    roa: float | None = None

    # Growth (YoY)
    revenue_growth_yoy: float | None = None
    earnings_growth_yoy: float | None = None

    # Health
    debt_to_equity: float | None = None
    current_ratio: float | None = None
    free_cash_flow: float | None = None

    # Analyst signals
    analyst_recommendation: str | None = None  # strong_buy, buy, hold, sell, strong_sell
    analyst_count: int | None = None
    target_price: float | None = None  # in native currency

    # Recent news headlines (last 7 days, for LLM context)
    recent_headlines: list[str] = field(default_factory=list)  # max 5

    # Earnings
    last_earnings_surprise_pct: float | None = None  # positive = beat

    # Raw notes (for Metadata sheet)
    warnings: list[str] = field(default_factory=list)


@dataclass
class SignalLabel:
    name: str  # e.g. "RSI Oversold", "Above EMA200", "MACD Bearish Cross"
    bullish: bool  # True = bullish signal, False = bearish signal


@dataclass
class TechnicalResult:
    ticker: str
    analysis_date: datetime
    period_days: int  # how many days of history were used

    # Trend
    ema_20: float | None = None
    ema_50: float | None = None
    ema_200: float | None = None
    price_vs_ema20_pct: float | None = None  # (price - ema20) / ema20 * 100
    price_vs_ema200_pct: float | None = None
    golden_cross: bool | None = None  # EMA50 recently crossed above EMA200
    death_cross: bool | None = None  # EMA50 recently crossed below EMA200

    # Momentum
    rsi_14: float | None = None
    macd_line: float | None = None
    macd_signal: float | None = None
    macd_histogram: float | None = None
    macd_bullish_cross: bool | None = None  # MACD line crossed above signal

    # Volatility
    bb_upper: float | None = None
    bb_middle: float | None = None
    bb_lower: float | None = None
    bb_width_pct: float | None = None  # (upper - lower) / middle * 100
    atr_14: float | None = None

    # Volume
    obv: float | None = None
    volume_vs_20d_avg_pct: float | None = None  # current vol vs 20d avg

    # Pre-computed plain-English signals for the LLM prompt
    signals: list[SignalLabel] = field(default_factory=list)

    warnings: list[str] = field(default_factory=list)


@dataclass
class LLMAnalysis:
    ticker: str
    model: str
    signal: str  # BUY | HOLD | SELL | INSUFFICIENT DATA
    confidence: str  # High | Medium | Low
    strengths: list[str]  # exactly 3 items
    risks: list[str]  # exactly 3 items
    summary: str  # 2 sentences max
    generated_at: datetime
    prompt_tokens_approx: int  # len(prompt) // 4, for cost awareness
    llm_available: bool = True  # False if Ollama unreachable
