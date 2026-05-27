"""Portfolio run: Sheets → FX → prices → XLSX → optional Drive → email."""

from __future__ import annotations

import html
import logging
import re
from collections.abc import Callable, Mapping
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from google.oauth2.credentials import Credentials
from ticker_tracker.analysis.base import FundamentalsResult, LLMAnalysis, TechnicalResult
from ticker_tracker.analysis.fundamentals import FundamentalsAdapter
from ticker_tracker.analysis.llm_analyst import LLMAnalyst, is_llm_available, llm_offline_hint
from ticker_tracker.analysis.technicals import TechnicalAnalyser
from ticker_tracker.calculator import (
    cost_basis_base,
    current_value_base,
    gain_loss_base,
    gain_loss_pct,
    portfolio_summary,
    purchase_amount_lines_by_ccy,
)
from ticker_tracker.config import AppConfig, EncryptedConfig
from ticker_tracker.currency import currency_for_ticker, normalize_iso4217
from ticker_tracker.exchange_map import build_yahoo_price_symbol, listing_currency_for_exchange
from ticker_tracker.finance.alphavantage_adapter import AlphaVantageAdapter
from ticker_tracker.finance.base import FinanceAdapter, FinanceAdapterError, PriceResult
from ticker_tracker.finance.finnhub_adapter import FinnhubAdapter
from ticker_tracker.finance.registry import get_prices_with_fallback
from ticker_tracker.finance.twelvedata_adapter import TwelveDataAdapter
from ticker_tracker.finance.yfinance_adapter import YFinanceAdapter
from ticker_tracker.fx.base import FXAdapter, FXAdapterError
from ticker_tracker.fx.forex_python import ForexPythonAdapter
from ticker_tracker.fx.frankfurter import FrankfurterAdapter
from ticker_tracker.fx.open_exchange_rates import OpenExchangeRatesAdapter
from ticker_tracker.fx.registry import FXRunRegistry
from ticker_tracker.google.drive import upload_file
from ticker_tracker.google.gmail import send_email
from ticker_tracker.google.sheets import read_holdings
from ticker_tracker.html_report import TabbedReportState, build_portfolio_tabbed_html
from ticker_tracker.local_holdings import read_local_holdings
from ticker_tracker.report_builder import (
    build_portfolio_workbook,
    default_workbook_filename,
)
from ticker_tracker.setup_core import resolve_local_report_dir

logger = logging.getLogger(__name__)

_FINANCE_ADAPTER_CLASSES: dict[str, type[FinanceAdapter]] = {
    "yahoo": YFinanceAdapter,
    "finnhub": FinnhubAdapter,
    "alpha_vantage": AlphaVantageAdapter,
    "twelve_data": TwelveDataAdapter,
}


def finance_adapters_from_config(config: AppConfig) -> list[FinanceAdapter]:
    """Build adapters in the same order as ``config.finance_sources`` (try-first → try-last)."""
    out: list[FinanceAdapter] = []
    seen: set[str] = set()
    supported = ", ".join(sorted(_FINANCE_ADAPTER_CLASSES))
    for raw in config.finance_sources:
        sid = str(raw).strip().lower()
        if sid in seen:
            continue
        seen.add(sid)
        cls = _FINANCE_ADAPTER_CLASSES.get(sid)
        if cls is None:
            logger.warning(
                "finance_sources entry %r is not a supported source (ignored). Supported: %s",
                sid,
                supported,
            )
            continue
        out.append(cls())
    return out


def fx_adapters_for_config(config: AppConfig) -> tuple[FXAdapter, ForexPythonAdapter | None]:
    src = config.fx_source.strip().lower()
    if src == "frankfurter":
        return FrankfurterAdapter(), ForexPythonAdapter()
    if src == "open_exchange_rates":
        return OpenExchangeRatesAdapter(usd_base_only=True), ForexPythonAdapter()
    raise ValueError(
        f"Unsupported fx_source {config.fx_source!r}. "
        "The engine currently supports frankfurter and open_exchange_rates."
    )


def _parse_float(value: Any) -> float:
    s = str(value).strip().replace(",", "")
    if not s:
        return 0.0
    neg = False
    if s.startswith("(") and s.endswith(")"):
        neg = True
        s = s[1:-1].strip()
    # Accept common sheet formatting like "$79.08" while rejecting ambiguous text.
    cleaned = re.sub(r"[^0-9.\-+]", "", s)
    if cleaned in {"", "-", "+", ".", "-.", "+."}:
        raise ValueError(f"Could not parse numeric value from {value!r}")
    out = float(cleaned)
    return -out if neg else out


def _market_code(sheet_ticker: str, price_symbol: str) -> str:
    ps = price_symbol.strip().upper()
    if "." in ps:
        return ps.rsplit(".", 1)[-1]
    st = sheet_ticker.strip().upper()
    if "." in st:
        return st.rsplit(".", 1)[-1]
    return "—"


def _row_price_symbol(row: dict[str, Any]) -> str:
    t = str(row.get("ticker") or "").strip()
    ex = str(row.get("exchange") or "").strip()
    return build_yahoo_price_symbol(t, ex)


def _yahoo_symbols_for_rows(rows_raw: list[dict[str, Any]]) -> tuple[list[str], dict[str, str]]:
    """
    Map holdings rows to Yahoo symbols for analysis.

    Returns (ordered yahoo symbols, yahoo_symbol -> sheet ticker).
    """
    yahoo_to_sheet: dict[str, str] = {}
    ordered: list[str] = []
    for row in rows_raw:
        sheet = str(row.get("ticker") or "").strip()
        if not sheet:
            continue
        yahoo = _row_price_symbol(row)
        if yahoo not in ordered:
            ordered.append(yahoo)
        yahoo_to_sheet[yahoo] = sheet
    return ordered, yahoo_to_sheet


def _remap_analysis_results(
    by_yahoo: dict[str, Any],
    yahoo_to_sheet: dict[str, str],
) -> dict[str, Any]:
    """Re-key analysis results from Yahoo symbol to sheet ticker."""
    out: dict[str, Any] = {}
    for yahoo, value in by_yahoo.items():
        sheet = yahoo_to_sheet.get(yahoo, yahoo)
        if hasattr(value, "ticker"):
            value.ticker = sheet
        out[sheet] = value
    return out


def _purchase_currency_iso(row: dict[str, Any]) -> str | None:
    raw = str(row.get("purchase_currency") or "").strip()
    if not raw:
        return None
    return normalize_iso4217(raw)


def _resolve_native_currency(
    row: dict[str, Any],
    price: PriceResult | None,
    market_overrides: dict[str, str],
) -> str:
    override = str(row.get("currency_override") or "").strip()
    if override:
        return normalize_iso4217(override)
    if price is not None:
        return normalize_iso4217(price.currency)
    ex = str(row.get("exchange") or "").strip()
    if ex:
        listed = listing_currency_for_exchange(ex)
        if listed:
            return normalize_iso4217(listed)
    guessed = currency_for_ticker(str(row.get("ticker") or ""), market_overrides)
    if guessed:
        return normalize_iso4217(guessed)
    return "USD"


def _email_fmt_number(value: Any, *, pct: bool = False) -> str:
    if value in ("—", None, ""):
        return "—"
    try:
        x = float(value)
    except (TypeError, ValueError):
        return html.escape(str(value))
    if pct:
        return f"{x:,.2f}%"
    return f"{x:,.2f}"


def _email_fmt_shares(value: Any) -> str:
    """Share counts in email: no fractional decimals."""
    if value in ("—", None, ""):
        return "—"
    try:
        x = float(value)
    except (TypeError, ValueError):
        return html.escape(str(value))
    return f"{x:,.0f}"


def _email_fmt_gl_pct_html(value: Any) -> str:
    """HTML for gain/loss % with green (up) / red (down) styling for email tables."""
    if value in ("—", None, ""):
        return "—"
    try:
        x = float(value)
    except (TypeError, ValueError):
        return html.escape(str(value))
    s = f"{x:,.2f}%"
    esc = html.escape(s)
    if x > 0:
        return f'<span style="color:#1a7f37;font-weight:600">{esc}</span>'
    if x < 0:
        return f'<span style="color:#c5221f;font-weight:600">{esc}</span>'
    return esc


def _html_table(headers: list[str], rows: list[list[str]], *, table_bg: str | None = None) -> str:
    style = "border-collapse:collapse;margin:0.75rem 0;font-size:0.95rem"
    if table_bg:
        style += f";background:{table_bg}"
    out = [f"<table style='{style}'>", "<thead><tr>"]
    for h in headers:
        out.append(
            f"<th style='border:1px solid #ccc;padding:6px 8px;text-align:left;background:#f4f4f4'>"
            f"{html.escape(h)}</th>"
        )
    out.append("</tr></thead><tbody>")
    for row in rows:
        out.append("<tr>")
        for cell in row:
            out.append(f"<td style='border:1px solid #ccc;padding:6px 8px'>{cell}</td>")
        out.append("</tr>")
    out.append("</tbody></table>")
    return "".join(out)


def _merge_holdings_rows_for_ticker(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Combine all sheet rows for one ticker (case-insensitive key) for ranking / summary tables."""
    first = rows[0]
    ticker_display = str(first.get("ticker") or "").strip()
    shares = sum(float(r.get("shares") or 0.0) for r in rows)
    sum_cb_base = sum(float(r.get("cost_basis_base") or 0.0) for r in rows)
    sum_cv_base = sum(float(r.get("current_value_base") or 0.0) for r in rows)
    gl_b = gain_loss_base(sum_cv_base, sum_cb_base)
    gl_pct_base = gain_loss_pct(sum_cv_base, sum_cb_base)

    excluded = any(bool(r.get("price_fetch_failed")) or bool(r.get("fx_unavailable")) for r in rows)

    report_ccys = {
        str(r.get("report_ccy") or "").strip().upper()
        for r in rows
        if str(r.get("report_ccy") or "").strip()
    }
    report_ccys.discard("")

    merged: dict[str, Any] = {
        "ticker": ticker_display,
        "shares": shares,
        "cost_basis_base": sum_cb_base,
        "current_value_base": sum_cv_base,
        "gain_loss_base": gl_b,
        "gain_loss_pct": gl_pct_base,
        "aggregate_rank_excluded": excluded,
        "base_currency": first.get("base_currency"),
        "market_code": first.get("market_code"),
        "native_ccy": first.get("native_ccy"),
    }

    def _dash_purchase_side() -> None:
        merged["report_ccy"] = next(iter(report_ccys)) if len(report_ccys) == 1 else "MIXED"
        merged["cost_basis_purchase"] = "—"
        merged["current_value_purchase"] = "—"
        merged["gain_loss_purchase"] = "—"
        merged["gain_loss_pct_purchase"] = gl_pct_base
        merged["cost_per_share_purchase"] = "—"
        merged["price_per_share_purchase"] = "—"

    if excluded or len(report_ccys) != 1:
        _dash_purchase_side()
        return merged

    ccy = next(iter(report_ccys))
    merged["report_ccy"] = ccy
    sum_cbp = sum(
        float(r["cost_basis_purchase"])
        for r in rows
        if isinstance(r.get("cost_basis_purchase"), int | float)
    )
    sum_cvp = sum(
        float(r["current_value_purchase"])
        for r in rows
        if isinstance(r.get("current_value_purchase"), int | float)
    )
    merged["cost_basis_purchase"] = sum_cbp
    merged["current_value_purchase"] = sum_cvp
    merged["gain_loss_purchase"] = sum_cvp - sum_cbp
    merged["gain_loss_pct_purchase"] = gain_loss_pct(sum_cvp, sum_cbp)
    merged["cost_per_share_purchase"] = (sum_cbp / shares) if shares else 0.0
    merged["price_per_share_purchase"] = (sum_cvp / shares) if shares else 0.0

    return merged


def aggregate_holdings_for_display(holdings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """
    One row per ticker for reports (weighted-average cost across multiple lots).

    Sheet rows with the same ticker at different purchase prices are merged so
    holdings tables match how portfolios are usually viewed.
    """
    return _aggregate_holdings_by_ticker(holdings)


def _aggregate_holdings_by_ticker(holdings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    from collections import defaultdict

    buckets: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for h in holdings:
        k = str(h.get("ticker") or "").strip().upper()
        if not k:
            continue
        buckets[k].append(h)
    return [_merge_holdings_rows_for_ticker(rows) for rows in buckets.values()]


def _aggregate_holdings_by_ticker_currency(holdings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    from collections import defaultdict

    buckets: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for h in holdings:
        ticker = str(h.get("ticker") or "").strip().upper()
        report_ccy = str(h.get("report_ccy") or "").strip().upper()
        if not ticker or not report_ccy:
            continue
        buckets[(ticker, report_ccy)].append(h)
    return [_merge_holdings_rows_for_ticker(rows) for rows in buckets.values()]


def _rank_best_worst(
    holdings: list[dict[str, Any]], *, n: int = 3
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    aggregated = _aggregate_holdings_by_ticker(holdings)
    elig: list[dict[str, Any]] = []
    for h in aggregated:
        if h.get("aggregate_rank_excluded"):
            continue
        if float(h.get("cost_basis_base") or 0) <= 0:
            continue
        elig.append(h)
    if not elig:
        return [], []
    by_pct = sorted(elig, key=lambda x: float(x.get("gain_loss_pct") or 0.0), reverse=True)
    best = by_pct[:n]
    worst = sorted(elig, key=lambda x: float(x.get("gain_loss_pct") or 0.0))[:n]
    return best, worst


def _rank_best_worst_by_currency(
    holdings: list[dict[str, Any]], *, n: int = 5
) -> dict[str, tuple[list[dict[str, Any]], list[dict[str, Any]]]]:
    aggregated = _aggregate_holdings_by_ticker_currency(holdings)
    by_ccy: dict[str, list[dict[str, Any]]] = {}
    for h in aggregated:
        if h.get("aggregate_rank_excluded"):
            continue
        ccy = str(h.get("report_ccy") or "").strip().upper()
        if not ccy or ccy == "MIXED":
            continue
        if float(h.get("cost_basis_purchase") or 0.0) <= 0:
            continue
        if not isinstance(h.get("gain_loss_pct_purchase"), int | float):
            continue
        by_ccy.setdefault(ccy, []).append(h)

    out: dict[str, tuple[list[dict[str, Any]], list[dict[str, Any]]]] = {}
    for ccy in sorted(by_ccy):
        elig = by_ccy[ccy]
        best = sorted(elig, key=lambda x: float(x["gain_loss_pct_purchase"]), reverse=True)[:n]
        worst = sorted(elig, key=lambda x: float(x["gain_loss_pct_purchase"]))[:n]
        out[ccy] = (best, worst)
    return out


def _holding_table_rows(holdings: list[dict[str, Any]]) -> list[list[str]]:
    rows: list[list[str]] = []
    for h in holdings:
        rows.append(
            [
                html.escape(str(h.get("ticker") or "")),
                _email_fmt_shares(h.get("shares")),
                _email_fmt_number(h.get("cost_per_share_purchase")),
                html.escape(str(h.get("report_ccy") or "")),
                _email_fmt_number(h.get("cost_basis_purchase")),
                _email_fmt_number(h.get("price_per_share_purchase")),
                _email_fmt_number(h.get("current_value_purchase")),
                _email_fmt_number(h.get("gain_loss_purchase")),
                _email_fmt_gl_pct_html(h.get("gain_loss_pct_purchase")),
            ]
        )
    return rows


def _perf_table_rows(holdings: list[dict[str, Any]]) -> list[list[str]]:
    rows: list[list[str]] = []
    for h in holdings:
        rows.append(
            [
                html.escape(str(h.get("ticker") or "")),
                _email_fmt_shares(h.get("shares")),
                _email_fmt_number(h.get("cost_basis_purchase")),
                _email_fmt_number(h.get("current_value_purchase")),
                _email_fmt_number(h.get("gain_loss_purchase")),
                _email_fmt_gl_pct_html(h.get("gain_loss_pct_purchase")),
            ]
        )
    return rows


def _portfolio_summary_email_rows(summary: Mapping[str, Any], base_upper: str) -> list[list[str]]:
    """Metric | Base (amount + ISO) | Purchased (one row per purchase currency when needed)."""
    bu = html.escape(base_upper)

    def fmt_base_money(v: Any) -> str:
        if v in (None, "", "—"):
            return "—"
        if isinstance(v, bool) or not isinstance(v, int | float):
            return html.escape(str(v))
        return f"{_email_fmt_number(v)} {bu}"

    rows: list[list[str]] = []

    cost_map: dict[str, float] = dict(summary.get("totals_purchase_cost_by_ccy") or {})
    val_map: dict[str, float] = dict(summary.get("totals_purchase_value_by_ccy") or {})
    gl_map: dict[str, float] = dict(summary.get("totals_purchase_gl_by_ccy") or {})

    if not cost_map and summary.get("totals_purchase_cost_formatted"):
        rows.append(
            [
                "Total invested",
                fmt_base_money(summary.get("total_cost_basis_base")),
                html.escape(str(summary.get("totals_purchase_cost_formatted"))),
            ]
        )
        rows.append(
            [
                "Current value",
                fmt_base_money(summary.get("total_current_value_base")),
                html.escape(str(summary.get("totals_purchase_value_formatted"))),
            ]
        )
        rows.append(
            [
                "Gain / loss",
                fmt_base_money(summary.get("total_gain_loss_base")),
                html.escape(str(summary.get("totals_purchase_gl_formatted"))),
            ]
        )
    else:
        for label, base_key, pmap in (
            ("Total invested", "total_cost_basis_base", cost_map),
            ("Current value", "total_current_value_base", val_map),
            ("Gain / loss", "total_gain_loss_base", gl_map),
        ):
            lines = purchase_amount_lines_by_ccy(pmap)
            rows.append([label, fmt_base_money(summary.get(base_key)), html.escape(lines[0])])
            for ln in lines[1:]:
                rows.append(["", "", html.escape(ln)])

    tr = _email_fmt_number(summary.get("total_return_pct"), pct=True)
    rows.append(["Total return %", tr, tr])

    cnt = int(summary.get("distinct_ticker_count") or summary.get("holding_count") or 0)
    rows.append(
        [
            "Holdings count (distinct tickers)",
            html.escape(str(cnt)),
            "—",
        ]
    )
    return rows


_EMAIL_MOBILE_CSS = """
body { margin: 0; padding: 12px; -webkit-text-size-adjust: 100%; }
.tt-wrap { max-width: 720px; margin: 0 auto; }
.tt-scroll { overflow-x: auto; -webkit-overflow-scrolling: touch; margin: 0.75rem 0; }
table { width: 100%; min-width: 480px; }
@media (max-width: 600px) {
  body { padding: 8px; font-size: 14px; }
  h2 { font-size: 1.1rem; }
  h3 { font-size: 0.95rem; }
  table { min-width: 520px; font-size: 12px; }
  th, td { padding: 4px 6px; }
}
"""


def build_portfolio_email_html(
    *,
    base: str,
    summary: Mapping[str, Any],
    holdings: list[dict[str, Any]],
    drive_url: str | None,
) -> str:
    """HTML body for email (mobile-friendly tables; no JavaScript)."""
    b = base.upper()
    eb = html.escape(b)
    generated_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    parts: list[str] = [
        "<html><head>",
        '<meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width, initial-scale=1">',
        f"<style>{_EMAIL_MOBILE_CSS}</style>",
        "</head>",
        '<body style="font-family:system-ui,-apple-system,sans-serif;line-height:1.45">',
        '<div class="tt-wrap">',
        (
            f'<h2 style="margin-bottom:0.25rem">'
            f"Portfolio summary ({eb}) as of {html.escape(generated_at)}"
            "</h2>"
        ),
    ]

    summary_headers = ["Metric", "Base", "Purchased"]
    summary_rows = _portfolio_summary_email_rows(summary, b)
    parts.append('<div class="tt-scroll">')
    parts.append(_html_table(summary_headers, summary_rows))
    parts.append("</div>")

    hold_headers = [
        "Ticker",
        "Shares",
        "Cost/sh (purch.)",
        "CCY",
        "Cost basis (purch.)",
        "Price/sh (purch.)",
        "Latest value (purch.)",
        "G/L (purch.)",
        "G/L %",
    ]
    parts.append("<h3>Holdings (one row per ticker, purchased currency)</h3>")
    parts.append('<div class="tt-scroll">')
    parts.append(_html_table(hold_headers, _holding_table_rows(holdings)))
    parts.append("</div>")

    perf_cols = [
        "Ticker",
        "Shares",
        "Cost basis (purch.)",
        "Latest value (purch.)",
        "G/L (purch.)",
        "G/L %",
    ]

    ranked_by_ccy = _rank_best_worst_by_currency(holdings, n=5)
    if not ranked_by_ccy:
        parts.append("<h3>Top 5 gainers / losers (by return %, per currency)</h3>")
        parts.append(
            "<p><i>No holdings with valid prices, FX, and positive cost basis for ranking.</i></p>"
        )
    else:
        for ccy, (best, worst) in ranked_by_ccy.items():
            esc_ccy = html.escape(ccy)
            parts.append(f"<h3>Top 5 gainers ({esc_ccy}, by return %, distinct tickers)</h3>")
            if best:
                parts.append('<div class="tt-scroll">')
                parts.append(_html_table(perf_cols, _perf_table_rows(best), table_bg="#d4edda"))
                parts.append("</div>")
            else:
                parts.append("<p><i>No eligible holdings for this currency.</i></p>")

            parts.append(f"<h3>Top 5 losers ({esc_ccy}, by return %, distinct tickers)</h3>")
            if worst:
                parts.append('<div class="tt-scroll">')
                parts.append(_html_table(perf_cols, _perf_table_rows(worst), table_bg="#f8d7da"))
                parts.append("</div>")
            else:
                parts.append("<p><i>No eligible holdings for this currency.</i></p>")

    if drive_url:
        safe = html.escape(drive_url, quote=True)
        parts.append(f'<p><a href="{safe}">Open report in Google Drive</a></p>')
    parts.append(
        '<p style="color:#555;font-size:0.9rem">'
        "Open the attached HTML report for the full interactive tabbed view (Analysis, signals, "
        "ticker drill-down). This email uses a simplified layout for mobile inboxes.</p>"
    )
    parts.append(
        '<p style="color:#555;font-size:0.9rem">'
        "Detailed numbers are also in the attached workbook.</p>"
    )
    parts.append("</div></body></html>")
    return "".join(parts)


def build_portfolio_html_report(
    *,
    base: str,
    summary: Mapping[str, Any],
    holdings: list[dict[str, Any]],
    drive_url: str | None,
    metadata: Mapping[str, Any] | None = None,
    holdings_lots: list[dict[str, Any]] | None = None,
    fundamentals: dict[str, FundamentalsResult] | None = None,
    technicals: dict[str, TechnicalResult] | None = None,
    analyses: dict[str, LLMAnalysis] | None = None,
    portfolio_summary: Mapping[str, str] | None = None,
    llm_offline_hint: str = "Ensure the analysis LLM is configured and reachable.",
    llm_available: bool | None = None,
) -> str:
    """Standalone HTML report for local viewing (tabbed; mirrors workbook sheets)."""
    ollama_ok = llm_available if llm_available is not None else bool(analyses)
    has_analysis = fundamentals is not None and technicals is not None
    return build_portfolio_tabbed_html(
        base=base,
        summary=summary,
        holdings=holdings,
        holdings_lots=holdings_lots,
        metadata=metadata or {},
        drive_url=drive_url,
        fundamentals=fundamentals,
        technicals=technicals,
        analyses=analyses,
        portfolio_summary=portfolio_summary,
        llm_offline_hint=llm_offline_hint,
        llm_available=ollama_ok,
        analysis_enabled=has_analysis,
    )


def run_once(
    *,
    app_config: AppConfig | None = None,
    encrypted_config: EncryptedConfig | None = None,
    credentials: Credentials | None = None,
    upload_to_drive: bool | None = None,
    send_email_notifications: bool = True,
    workbook_path: Path | None = None,
    allow_local_file_email: bool = False,
    status_callback: Callable[[str], None] | None = None,
    progress_callback: Callable[[int, str], None] | None = None,
    report_state: TabbedReportState | None = None,
    on_analysis_ticker: Callable[[str, LLMAnalysis], None] | None = None,
    analysis_enabled: bool | None = None,
) -> dict[str, Any]:
    """
    End-to-end portfolio run for multi-currency holdings.

    Provide *app_config* for tests; otherwise load via *encrypted_config* or default path.

    *status_callback* receives short progress strings for UI (e.g. Tk popup).

    *progress_callback*, when set, receives ``(percent_0_to_100, message)`` at coarse milestones
    (e.g. CLI progress bar). *status_callback* is still invoked with the same *message* text.

    *report_state*, when set, publishes tabbed HTML after each milestone (holdings/summary/metadata,
    then per-ticker fundamentals, technicals, LLM).

    *on_analysis_ticker*, when set, is invoked after each successful LLM ticker analysis.

    When *holdings_source* is ``local_file``, notification email is normally disabled. Pass
    *allow_local_file_email* ``True`` (e.g. dashboard upload + user opt-in) to honor
    *send_email_notifications* instead.
    """
    cfg = app_config if app_config is not None else (encrypted_config or EncryptedConfig()).load()
    if analysis_enabled is not None:
        cfg = replace(cfg, analysis_enabled=analysis_enabled)
    if not cfg.column_map:
        raise ValueError("Config is missing column_map.")

    def _status(msg: str) -> None:
        if status_callback:
            status_callback(msg)

    def _progress(pct: int, msg: str) -> None:
        if progress_callback is not None:
            progress_callback(pct, msg)
        _status(msg)

    creds = credentials
    use_drive = cfg.upload_to_drive if upload_to_drive is None else upload_to_drive
    if cfg.holdings_source == "local_file":
        use_drive = False
        if not allow_local_file_email:
            send_email_notifications = False

    _progress(5, "Starting portfolio run…")
    _progress(10, "Reading holdings…")
    if cfg.holdings_source == "local_file":
        rows_raw = read_local_holdings(
            cfg.local_holdings_path,
            column_map=cfg.column_map,
            sheet_name=cfg.local_holdings_sheet_name,
        )
    else:
        if not cfg.google_sheets_id:
            raise ValueError("Config is missing google_sheets_id for google_sheets source.")
        rows_raw = read_holdings(
            cfg.google_sheets_id,
            cfg.holdings_sheet_name,
            cfg.column_map,
            credentials=creds,
        )
    _progress(18, f"Loaded {len(rows_raw)} sheet row(s).")

    def _sheet_ticker(r: dict[str, Any]) -> str:
        return str(r.get("ticker") or "").strip()

    sheet_tickers = list(dict.fromkeys(_sheet_ticker(r) for r in rows_raw if _sheet_ticker(r)))
    if not sheet_tickers:
        raise ValueError("No holdings rows with tickers were read from the sheet.")

    price_symbols: list[str] = []
    seen_ps: set[str] = set()
    for row in rows_raw:
        t = str(row.get("ticker") or "").strip()
        if not t:
            continue
        ps = _row_price_symbol(row)
        if ps not in seen_ps:
            seen_ps.add(ps)
            price_symbols.append(ps)

    adapters = finance_adapters_from_config(cfg)
    if not adapters:
        raise ValueError("No supported finance_sources in config (e.g. yahoo).")

    _progress(28, "Fetching market prices…")
    price_failed: list[str] = []
    prices: dict[str, PriceResult] = {}
    try:
        prices = get_prices_with_fallback(adapters, price_symbols)
    except FinanceAdapterError as exc:
        logger.error("Price fetch failed for all adapters: %s", exc)
        price_failed = list(dict.fromkeys(sheet_tickers))
    _progress(48, "Prices updated; applying FX…")

    base = normalize_iso4217(cfg.base_currency)
    natives: set[str] = {base}
    for row in rows_raw:
        t = str(row.get("ticker") or "").strip()
        if not t:
            continue
        pc = _purchase_currency_iso(row)
        if pc:
            natives.add(pc)
        ex = str(row.get("exchange") or "").strip()
        if ex:
            lc = listing_currency_for_exchange(ex)
            if lc:
                natives.add(lc)
        ps = _row_price_symbol(row)
        pr = prices.get(ps) if prices else None
        natives.add(_resolve_native_currency(row, pr, cfg.market_currency_overrides))

    fx_primary, fx_fallback = fx_adapters_for_config(cfg)
    fx_reg = FXRunRegistry(fx_primary, base, natives, fallback_adapter=fx_fallback)

    enriched: list[dict[str, Any]] = []
    fx_unavailable: list[str] = []
    cost_fx_unavailable: list[str] = []

    for row in rows_raw:
        ticker = str(row.get("ticker") or "").strip()
        if not ticker:
            continue
        shares = _parse_float(row.get("shares"))
        cost_per_share_sheet = _parse_float(row.get("cost_basis"))
        ps = _row_price_symbol(row)
        pr = prices.get(ps)
        price_missing = ticker in price_failed or pr is None
        native = _resolve_native_currency(
            row,
            pr if not price_missing else None,
            cfg.market_currency_overrides,
        )

        fx_rate: float | None = None
        fx_display: Any = "—"
        price_native = 0.0 if price_missing or pr is None else float(pr.price)
        fx_flag = False
        if not price_missing:
            try:
                fx_rate = float(fx_reg.convert(1.0, native, base))
                fx_display = fx_rate
            except FXAdapterError as exc:
                logger.warning("FX unavailable for %s (%s→%s): %s", ticker, native, base, exc)
                fx_unavailable.append(ticker)
                fx_flag = True

        cv = 0.0
        cp_base = 0.0
        if not price_missing and fx_rate is not None:
            cv = current_value_base(shares, price_native, fx_rate)
            cp_base = price_native * fx_rate

        purchase_ccy = _purchase_currency_iso(row)
        cost_fx_flag = False
        if purchase_ccy:
            row_cost = shares * cost_per_share_sheet
            try:
                cb_row = float(fx_reg.convert(row_cost, purchase_ccy, base))
            except FXAdapterError as exc:
                logger.warning(
                    "Cost basis FX failed for %s (%s→%s): %s", ticker, purchase_ccy, base, exc
                )
                cost_fx_unavailable.append(ticker)
                cb_row = 0.0
                cost_fx_flag = True
        else:
            cb_row = cost_basis_base(shares, cost_per_share_sheet)

        gl = gain_loss_base(cv, cb_row)
        glp = gain_loss_pct(cv, cb_row)

        report_ccy = purchase_ccy if purchase_ccy else base
        cost_basis_purchase = float(shares) * float(cost_per_share_sheet)
        cost_per_share_purchase = float(cost_per_share_sheet)

        price_per_share_purchase: float | str = "—"
        current_value_purchase: float | str = "—"
        gain_loss_purchase: float | str = "—"
        gain_loss_pct_purchase: float | str = "—"

        if not price_missing and not fx_flag and fx_rate is not None:
            try:
                if native == report_ccy:
                    ppp = float(price_native)
                else:
                    ppp = float(fx_reg.convert(float(price_native), native, report_ccy))
                price_per_share_purchase = ppp
                cvp = float(shares) * ppp
                current_value_purchase = cvp
                glpu = float(cvp) - float(cost_basis_purchase)
                gain_loss_purchase = glpu
                gain_loss_pct_purchase = gain_loss_pct(float(cvp), float(cost_basis_purchase))
            except FXAdapterError as exc:
                logger.warning(
                    "FX for quote→report currency failed for %s (%s→%s): %s",
                    ticker,
                    native,
                    report_ccy,
                    exc,
                )

        enriched.append(
            {
                "ticker": ticker,
                "shares": shares,
                "report_ccy": report_ccy,
                "cost_per_share_purchase": cost_per_share_purchase,
                "cost_basis_purchase": cost_basis_purchase,
                "price_per_share_purchase": price_per_share_purchase,
                "current_value_purchase": current_value_purchase,
                "gain_loss_purchase": gain_loss_purchase,
                "gain_loss_pct_purchase": gain_loss_pct_purchase,
                "cost_basis_base": cb_row,
                "native_ccy": native,
                "price_native": price_native if not price_missing else "—",
                "fx_rate_display": fx_display,
                "current_price_base": cp_base if not price_missing and not fx_flag else "—",
                "current_value_base": cv,
                "gain_loss_base": gl,
                "gain_loss_pct": glp,
                "base_currency": base,
                "market_code": _market_code(ticker, ps),
                "price_fetch_failed": price_missing,
                "fx_unavailable": fx_flag,
                "cost_fx_unavailable": cost_fx_flag,
            }
        )

    _progress(62, "Computing portfolio summary…")
    summary = portfolio_summary(enriched)
    display_holdings = aggregate_holdings_for_display(enriched)
    row_count = int(summary.get("holding_row_count") or len(enriched))
    summary = {
        **summary,
        "assumption_notes": [
            *(summary.get("assumption_notes") or []),
            (
                f"Holdings tables show one row per ticker ({len(display_holdings)} positions "
                f"from {row_count} sheet row(s)); cost basis uses weighted averages across lots."
            ),
        ],
    }
    run_ts = datetime.now(UTC).isoformat()

    finance_by_ticker: dict[str, str] = {}
    for row in rows_raw:
        t = str(row.get("ticker") or "").strip()
        if not t:
            continue
        pr = prices.get(_row_price_symbol(row))
        finance_by_ticker[t] = pr.source if pr else "—"

    price_missing_sheet: list[str] = []
    for row in rows_raw:
        t = str(row.get("ticker") or "").strip()
        if not t:
            continue
        if t in price_failed or prices.get(_row_price_symbol(row)) is None:
            price_missing_sheet.append(t)

    metadata: dict[str, Any] = {
        "run_timestamp_utc": run_ts,
        "fx_source": cfg.fx_source,
        "fx_rates": fx_reg.cached_fx_rates(),
        "finance_source_by_ticker": finance_by_ticker,
        "price_fetch_failed": list(dict.fromkeys(price_missing_sheet)),
        "fx_unavailable_tickers": sorted(set(fx_unavailable)),
        "cost_fx_unavailable_tickers": sorted(set(cost_fx_unavailable)),
    }

    llm_hint = llm_offline_hint(cfg)

    if report_state is not None:
        report_state.set_core(
            base=base,
            summary=summary,
            holdings=display_holdings,
            holdings_lots=enriched,
            metadata=metadata,
            drive_url=None,
            analysis_enabled=cfg.analysis_enabled,
            llm_offline_hint=llm_hint,
        )

    fundamentals_by_ticker = None
    technicals_by_ticker = None
    analyses_by_ticker: dict[str, LLMAnalysis] | None = None
    llm_portfolio_summary: dict[str, str] | None = None
    ollama_ok = False

    if cfg.analysis_enabled:
        yahoo_symbols, yahoo_to_sheet = _yahoo_symbols_for_rows(rows_raw)
        n_analysis = len(yahoo_symbols) or 1

        def _fund_progress(i: int, n: int, symbol: str) -> None:
            sheet = yahoo_to_sheet.get(symbol, symbol)
            _progress(
                64 + min(4, int(4 * i / max(n, 1))),
                f"Fundamentals {sheet} ({i}/{n})…",
            )

        _progress(64, f"Fetching fundamentals for {n_analysis} ticker(s)…")

        def _fund_result_cb(yahoo_sym: str, result: FundamentalsResult) -> None:
            if report_state is None:
                return
            sheet_t = yahoo_to_sheet.get(yahoo_sym, yahoo_sym)
            report_state.set_fundamental(sheet_t, result)

        fund_yahoo = FundamentalsAdapter().get_fundamentals(
            yahoo_symbols,
            progress_callback=_fund_progress,
            ticker_result_callback=_fund_result_cb if report_state else None,
        )
        fundamentals_by_ticker = _remap_analysis_results(fund_yahoo, yahoo_to_sheet)

        _progress(68, f"Technical analysis for {n_analysis} ticker(s)…")
        tech_yahoo = TechnicalAnalyser().analyse(yahoo_symbols)
        technicals_by_ticker = _remap_analysis_results(tech_yahoo, yahoo_to_sheet)
        if report_state is not None:
            report_state.set_technicals(technicals_by_ticker)

        ollama_ok = is_llm_available(cfg)
        logger.info(
            "LLM provider %s available: %s",
            cfg.llm_provider,
            ollama_ok,
        )
        if not ollama_ok:
            logger.warning(
                "%s — skipping LLM ticker analysis; "
                "fundamentals and technicals will still appear in the report.",
                llm_hint,
            )
        else:
            analyst = LLMAnalyst(cfg)
            analyses_by_ticker = {}
            llm_results: list[LLMAnalysis] = []
            llm_tickers = list(
                dict.fromkeys(
                    str(h.get("ticker") or "") for h in display_holdings if h.get("ticker")
                )
            )
            n_llm = len(llm_tickers) or 1
            holdings_by_ticker = {
                str(h.get("ticker") or ""): h for h in display_holdings if h.get("ticker")
            }
            for idx, ticker in enumerate(llm_tickers, start=1):
                fund = fundamentals_by_ticker.get(ticker)
                tech = technicals_by_ticker.get(ticker)
                if fund is None or tech is None:
                    continue
                holding = holdings_by_ticker.get(ticker, {})
                msg = f"LLM analysis {ticker} ({idx}/{n_llm})…"
                _progress(69 + min(1, int(idx / max(n_llm, 1))), msg)
                logger.info("LLM analysing %s (%s/%s)…", ticker, idx, n_llm)
                holding_dict = {
                    "shares": holding.get("shares"),
                    "cost_basis_base": holding.get("cost_basis_base"),
                    "current_value_base": holding.get("current_value_base"),
                    "native_currency": holding.get("native_ccy"),
                }
                analysis = analyst.analyse_ticker(ticker, fund, tech, base, holding_dict)
                analyses_by_ticker[ticker] = analysis
                llm_results.append(analysis)
                if report_state is not None:
                    report_state.set_analysis(ticker, analysis)
                if on_analysis_ticker is not None:
                    on_analysis_ticker(ticker, analysis)
            _progress(70, "Generating portfolio commentary…")
            llm_portfolio_summary = analyst.analyse_portfolio(llm_results)
            if report_state is not None:
                report_state.set_portfolio_summary(
                    llm_portfolio_summary,
                    llm_available=ollama_ok,
                )

    output_formats = list(dict.fromkeys(cfg.output_formats or ["xlsx"]))
    workbook_out_path: Path | None = None
    html_out_path: Path | None = None
    fname = default_workbook_filename()
    output_dir = resolve_local_report_dir(cfg.local_report_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    if "xlsx" in output_formats:
        if workbook_path is not None:
            workbook_out_path = Path(workbook_path)
            fname = workbook_out_path.name
        else:
            workbook_out_path = output_dir / fname
        _progress(72, "Building Excel report…")
        build_portfolio_workbook(
            workbook_out_path,
            base_currency=base,
            holdings_rows=display_holdings,
            summary=summary,
            metadata=metadata,
            fundamentals=fundamentals_by_ticker,
            technicals=technicals_by_ticker,
            analyses=analyses_by_ticker,
            portfolio_summary=llm_portfolio_summary,
            llm_offline_hint=llm_hint,
            llm_available=ollama_ok if cfg.analysis_enabled else None,
        )
    if "html" in output_formats:
        base_name = Path(fname).stem
        html_out_path = output_dir / f"{base_name}.html"
        html_out_path.write_text(
            build_portfolio_html_report(
                base=base,
                summary=summary,
                holdings=display_holdings,
                holdings_lots=enriched,
                drive_url=None,
                metadata=metadata,
                fundamentals=fundamentals_by_ticker,
                technicals=technicals_by_ticker,
                analyses=analyses_by_ticker,
                portfolio_summary=llm_portfolio_summary,
                llm_offline_hint=llm_hint,
                llm_available=ollama_ok if cfg.analysis_enabled else None,
            ),
            encoding="utf-8",
        )
    if report_state is not None:
        report_state.flush()
    _progress(80, "Report file(s) ready.")

    drive_url: str | None = None
    if use_drive:
        if workbook_out_path is None:
            raise ValueError("Drive upload requires xlsx output.")
        _progress(84, "Uploading to Google Drive…")
        drive_url = upload_file(workbook_out_path, fname, folder_id=None, credentials=creds)
        _progress(90, "Drive upload complete.")

    emails_sent = 0
    if send_email_notifications and cfg.email_ids:
        subject = f"Ticker summary ({base}) — {fname}"
        body = build_portfolio_email_html(
            base=base,
            summary=summary,
            holdings=display_holdings,
            drive_url=drive_url,
        )
        recipients: list[str] = []
        seen_recips: set[str] = set()
        for raw in cfg.email_ids:
            addr = str(raw).strip()
            if not addr:
                continue
            key = addr.lower()
            if key in seen_recips:
                continue
            seen_recips.add(key)
            recipients.append(addr)

        _progress(92, "Sending notification email(s)…")
        for addr in recipients:
            send_email(
                addr,
                subject,
                body,
                attachment_path=workbook_out_path,
                credentials=creds,
            )
            emails_sent += 1

    if drive_url:
        _progress(100, "Done! File saved to Drive.")
    else:
        _progress(100, "Done! Report saved locally.")

    email_html = build_portfolio_email_html(
        base=base,
        summary=summary,
        holdings=display_holdings,
        drive_url=drive_url,
    )

    return {
        "config": cfg,
        "summary": summary,
        "holdings": enriched,
        "holdings_display": display_holdings,
        "workbook_path": str(workbook_out_path) if workbook_out_path else None,
        "workbook_filename": fname if workbook_out_path else None,
        "html_report_path": str(html_out_path) if html_out_path else None,
        "drive_url": drive_url,
        "emails_sent": emails_sent,
        "metadata": metadata,
        "email_html": email_html,
    }


def run(**kwargs: Any) -> dict[str, Any]:
    """Alias for :func:`run_once` (GUI and docs)."""
    return run_once(**kwargs)


__all__ = [
    "build_portfolio_email_html",
    "finance_adapters_from_config",
    "fx_adapters_for_config",
    "run",
    "run_once",
]
