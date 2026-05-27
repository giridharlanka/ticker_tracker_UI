"""Prompt templates for the local Ollama analyst."""

from __future__ import annotations

from ticker_tracker.analysis.base import (
    FundamentalsResult,
    LLMAnalysis,
    TechnicalResult,
)

_SYSTEM_CONTEXT = """You are a quantitative portfolio analyst. You will be given \
structured data for a stock holding. Respond ONLY with valid JSON \
— no preamble, no markdown, no explanation outside the JSON."""

_JSON_INSTRUCTION = """
Based on the data above, respond with ONLY this JSON structure:
{
  "signal": "BUY" | "HOLD" | "SELL" | "INSUFFICIENT DATA",
  "confidence": "High" | "Medium" | "Low",
  "strengths": ["...", "...", "..."],
  "risks": ["...", "...", "..."],
  "summary": "Two sentence summary here."
}

Rules:
- Use "INSUFFICIENT DATA" if fundamentals_available is false AND
  fewer than 3 technical signals are present.
- strengths and risks must always have exactly 3 items each.
- summary must be 2 sentences or fewer, plain English, no jargon.
- Do not invent data not present above.
- If news headlines are present, factor in sentiment but don't
  over-weight a single headline.
"""


def _fmt_pct(value: float | None, *, decimals: int = 1) -> str | None:
    if value is None:
        return None
    return f"{value * 100:.{decimals}f}%"


def _fmt_float(value: float | None, *, decimals: int = 2, signed: bool = False) -> str | None:
    if value is None:
        return None
    if signed:
        return f"{value:+.{decimals}f}"
    return f"{value:.{decimals}f}"


def _append_line(lines: list[str], label: str, value: str | None) -> None:
    if value is not None:
        lines.append(f"{label}: {value}")


def build_analysis_prompt(
    ticker: str,
    fundamentals: FundamentalsResult,
    technicals: TechnicalResult,
    base_currency: str,
    holding: dict | None = None,
    *,
    include_holding_context: bool = False,
) -> str:
    """Build the per-ticker analysis prompt (fundamentals + technicals; optional position block)."""
    sections: list[str] = [
        _SYSTEM_CONTEXT,
        "",
        f"## Ticker: {ticker} (Base currency: {base_currency})",
    ]

    if include_holding_context and holding:
        shares = holding.get("shares")
        cost_basis = holding.get("cost_basis_base")
        current_value = holding.get("current_value_base")

        gain_loss: float | None = None
        gain_loss_pct: float | None = None
        if (
            isinstance(cost_basis, int | float)
            and isinstance(current_value, int | float)
            and cost_basis != 0
        ):
            gain_loss = float(current_value) - float(cost_basis)
            gain_loss_pct = gain_loss / float(cost_basis) * 100

        position_lines = ["", "### Portfolio position"]
        if shares is not None:
            position_lines.append(f"Shares held: {shares}")
        if current_value is not None:
            position_lines.append(f"Current value ({base_currency}): {float(current_value):.2f}")
        if cost_basis is not None:
            position_lines.append(f"Cost basis ({base_currency}): {float(cost_basis):.2f}")
        if gain_loss is not None and gain_loss_pct is not None:
            position_lines.append(f"Gain/loss: {gain_loss:.2f} ({gain_loss_pct:.1f}%)")
        sections.extend(position_lines)
    else:
        sections.extend(
            [
                "",
                "### Portfolio position",
                "Not included — analyse using public fundamentals and technicals only.",
            ]
        )

    fund_lines = ["", "### Fundamentals"]
    _append_line(fund_lines, "P/E ratio", _fmt_float(fundamentals.pe_ratio))
    _append_line(fund_lines, "P/B ratio", _fmt_float(fundamentals.pb_ratio))
    _append_line(fund_lines, "EV/EBITDA", _fmt_float(fundamentals.ev_ebitda))
    _append_line(fund_lines, "Net margin", _fmt_pct(fundamentals.net_margin))
    _append_line(fund_lines, "ROE", _fmt_pct(fundamentals.roe))
    _append_line(
        fund_lines,
        "Revenue growth (YoY)",
        _fmt_pct(fundamentals.revenue_growth_yoy),
    )
    _append_line(
        fund_lines,
        "Earnings growth (YoY)",
        _fmt_pct(fundamentals.earnings_growth_yoy),
    )
    _append_line(fund_lines, "Debt/Equity", _fmt_float(fundamentals.debt_to_equity))
    _append_line(fund_lines, "Dividend yield", _fmt_pct(fundamentals.dividend_yield))
    if fundamentals.analyst_recommendation is not None:
        count = fundamentals.analyst_count
        if count is not None:
            fund_lines.append(
                f"Analyst recommendation: {fundamentals.analyst_recommendation} ({count} analysts)"
            )
        else:
            fund_lines.append(f"Analyst recommendation: {fundamentals.analyst_recommendation}")
    if fundamentals.target_price is not None:
        quote_ccy = base_currency
        if holding:
            quote_ccy = (
                str(
                    holding.get("native_currency") or holding.get("native_ccy") or base_currency
                ).strip()
                or base_currency
            )
        fund_lines.append(f"Analyst target price: {fundamentals.target_price} {quote_ccy}")
    surprise = _fmt_float(fundamentals.last_earnings_surprise_pct, decimals=1, signed=True)
    if surprise is not None:
        fund_lines.append(f"Last earnings surprise: {surprise}%")
    quality = (
        "FULL" if fundamentals.fundamentals_available else "SPARSE — limited data for this market"
    )
    fund_lines.append(f"Fundamentals data quality: {quality}")
    sections.extend(fund_lines)

    tech_lines = [
        "",
        f"### Technical signals ({technicals.period_days} days of history)",
    ]
    _append_line(tech_lines, "RSI(14)", _fmt_float(technicals.rsi_14, decimals=1))
    _append_line(tech_lines, "MACD histogram", _fmt_float(technicals.macd_histogram, decimals=4))
    _append_line(
        tech_lines,
        "Price vs EMA200",
        (
            f"{technicals.price_vs_ema200_pct:+.1f}%"
            if technicals.price_vs_ema200_pct is not None
            else None
        ),
    )
    _append_line(
        tech_lines,
        "Bollinger band width",
        (f"{technicals.bb_width_pct:.1f}%" if technicals.bb_width_pct is not None else None),
    )
    _append_line(
        tech_lines,
        "Volume vs 20d avg",
        (
            f"{technicals.volume_vs_20d_avg_pct:+.1f}%"
            if technicals.volume_vs_20d_avg_pct is not None
            else None
        ),
    )
    if technicals.signals:
        active = ", ".join(f"{s.name}{' ▲' if s.bullish else ' ▼'}" for s in technicals.signals)
        tech_lines.append(f"Active signals: {active}")
    sections.extend(tech_lines)

    news_lines = ["", "### Recent news (last 7 days)"]
    if fundamentals.recent_headlines:
        news_lines.extend(fundamentals.recent_headlines[:5])
    else:
        news_lines.append("No recent news available.")
    sections.extend(news_lines)

    sections.append(_JSON_INSTRUCTION)
    return "\n".join(sections)


def build_portfolio_summary_prompt(analyses: list[LLMAnalysis]) -> str:
    """Build prompt for portfolio-level commentary from per-ticker LLM results."""
    lines = [
        _SYSTEM_CONTEXT,
        "",
        "## Portfolio summary request",
        "You have completed per-holding analyses. Summarize the portfolio in JSON only.",
        "",
        "### Holdings analysed",
    ]
    for a in analyses:
        lines.append(
            f"- {a.ticker}: signal={a.signal}, confidence={a.confidence}, summary={a.summary}"
        )
    lines.extend(
        [
            "",
            "Respond with ONLY this JSON structure:",
            '{ "commentary": "Three sentences on concentration, currency exposure, '
            'and overall posture.", "top_action": "Single highest-priority action." }',
            "",
            "Rules:",
            "- commentary must be exactly 3 sentences.",
            "- top_action must be one concise sentence.",
            "- Do not invent holdings not listed above.",
        ]
    )
    return "\n".join(lines)
