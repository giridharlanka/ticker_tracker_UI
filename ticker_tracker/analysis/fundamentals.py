"""Fundamentals adapter: yfinance primary, FMP Cloud fallback."""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import pandas as pd
import yfinance as yf

from ticker_tracker.analysis.base import FundamentalsResult
from ticker_tracker.analysis.technicals import _suppress_yfinance_pandas_utcnoise
from ticker_tracker.config import get_fmp_api_key

logger = logging.getLogger(__name__)

FMP_BASE = "https://financialmodelingprep.com/api/v3"
HEADLINE_MAX_LEN = 120

CORE_RATIO_FIELDS = (
    "pe_ratio",
    "pb_ratio",
    "ev_ebitda",
    "price_to_sales",
    "dividend_yield",
    "gross_margin",
    "operating_margin",
    "net_margin",
    "roe",
    "roa",
)

# yfinance recommendationKey -> canonical values
_RECOMMENDATION_MAP: dict[str, str] = {
    "strong_buy": "strong_buy",
    "strongbuy": "strong_buy",
    "buy": "buy",
    "hold": "hold",
    "sell": "sell",
    "strong_sell": "strong_sell",
    "strongsell": "strong_sell",
    "underperform": "sell",
    "outperform": "buy",
    "none": "hold",
    "neutral": "hold",
}

_REVENUE_ROW_NAMES = ("Total Revenue", "Total Revenues", "Revenue")
_EARNINGS_ROW_NAMES = (
    "Net Income",
    "Net Income Common Stockholders",
    "Net Income Applicable To Common Shares",
)


def _safe_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        f = float(value)
        if f != f:  # NaN
            return None
        return f
    except (TypeError, ValueError):
        return None


def _safe_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def normalize_analyst_recommendation(raw: str | None) -> str | None:
    """Map yfinance ``recommendationKey`` to a canonical recommendation string."""
    if not raw:
        return None
    key = str(raw).strip().lower().replace(" ", "_").replace("-", "_")
    return _RECOMMENDATION_MAP.get(key)


def _core_ratio_none_count(result: FundamentalsResult) -> int:
    return sum(1 for name in CORE_RATIO_FIELDS if getattr(result, name) is None)


def _set_fundamentals_available(result: FundamentalsResult) -> None:
    missing = _core_ratio_none_count(result)
    if missing > 5:
        result.fundamentals_available = False
        result.warnings.append(
            f"Sparse fundamentals for {result.ticker}: {missing} of "
            f"{len(CORE_RATIO_FIELDS)} core ratio fields missing."
        )
    else:
        result.fundamentals_available = True


def _find_row(financials: pd.DataFrame, names: tuple[str, ...]) -> pd.Series | None:
    for name in names:
        if name in financials.index:
            return financials.loc[name]
    for name in names:
        for idx in financials.index:
            if str(idx).strip().lower() == name.lower():
                return financials.loc[idx]
    return None


def _yoy_from_financials(financials: pd.DataFrame | None, row_names: tuple[str, ...]) -> float | None:
    if financials is None or financials.empty or len(financials.columns) < 2:
        return None
    row = _find_row(financials, row_names)
    if row is None:
        return None
    values = []
    for col in financials.columns[:2]:
        v = _safe_float(row.get(col))
        if v is not None:
            values.append(v)
    if len(values) < 2:
        return None
    newer, older = values[0], values[1]
    if older == 0:
        return None
    return (newer - older) / abs(older)


def _headlines_from_news(news: list[dict[str, Any]] | None) -> list[str]:
    if not news:
        return []
    titles: list[str] = []
    for item in news[:5]:
        if not isinstance(item, dict):
            continue
        title = item.get("title")
        if not title:
            continue
        text = str(title).strip()
        if len(text) > HEADLINE_MAX_LEN:
            text = text[:HEADLINE_MAX_LEN].rstrip()
        titles.append(text)
    return titles


def _earnings_surprise_pct(earnings_dates: pd.DataFrame | None) -> float | None:
    if earnings_dates is None or earnings_dates.empty:
        return None
    col = None
    for candidate in ("Surprise(%)", "Surprise (%)"):
        if candidate in earnings_dates.columns:
            col = candidate
            break
    if col is None:
        return None
    for idx in earnings_dates.index:
        val = _safe_float(earnings_dates.loc[idx, col])
        if val is not None:
            return val
    return None


def _map_yfinance_info(info: dict[str, Any], result: FundamentalsResult) -> None:
    result.pe_ratio = _safe_float(info.get("trailingPE")) or _safe_float(info.get("forwardPE"))
    result.pb_ratio = _safe_float(info.get("priceToBook"))
    result.ev_ebitda = _safe_float(info.get("enterpriseToEbitda"))
    result.price_to_sales = _safe_float(info.get("priceToSalesTrailing12Months"))
    result.dividend_yield = _safe_float(info.get("dividendYield"))
    result.gross_margin = _safe_float(info.get("grossMargins"))
    result.operating_margin = _safe_float(info.get("operatingMargins"))
    result.net_margin = _safe_float(info.get("profitMargins"))
    result.roe = _safe_float(info.get("returnOnEquity"))
    result.roa = _safe_float(info.get("returnOnAssets"))
    result.debt_to_equity = _safe_float(info.get("debtToEquity"))
    result.current_ratio = _safe_float(info.get("currentRatio"))
    result.free_cash_flow = _safe_float(info.get("freeCashflow"))
    result.target_price = _safe_float(info.get("targetMeanPrice"))
    result.analyst_count = _safe_int(info.get("numberOfAnalystOpinions"))
    result.analyst_recommendation = normalize_analyst_recommendation(info.get("recommendationKey"))


def _fmp_request(path: str, api_key: str) -> Any:
    sep = "&" if "?" in path else "?"
    url = f"{FMP_BASE}{path}{sep}apikey={urllib.parse.quote(api_key)}"
    req = urllib.request.Request(url, headers={"User-Agent": "ticker-tracker/0.1"})
    try:
        with urllib.request.urlopen(req, timeout=45) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        raise OSError(f"FMP HTTP error: {exc}") from exc
    except urllib.error.URLError as exc:
        raise OSError(f"FMP network error: {exc}") from exc


def _apply_fmp_profile(profile: dict[str, Any], result: FundamentalsResult) -> None:
    if result.pe_ratio is None:
        result.pe_ratio = _safe_float(profile.get("pe"))
    if result.pb_ratio is None:
        result.pb_ratio = _safe_float(profile.get("pb"))
    if result.price_to_sales is None:
        result.price_to_sales = _safe_float(profile.get("priceToSalesRatio"))
    if result.dividend_yield is None:
        result.dividend_yield = _safe_float(profile.get("lastDiv")) or _safe_float(
            profile.get("dividendYield")
        )
    if result.analyst_recommendation is None:
        result.analyst_recommendation = normalize_analyst_recommendation(
            profile.get("recommendation")
        )


def _apply_fmp_ratios(ratios: dict[str, Any], result: FundamentalsResult) -> None:
    if result.pe_ratio is None:
        result.pe_ratio = _safe_float(ratios.get("priceEarningsRatio"))
    if result.pb_ratio is None:
        result.pb_ratio = _safe_float(ratios.get("priceToBookRatio"))
    if result.ev_ebitda is None:
        result.ev_ebitda = _safe_float(ratios.get("enterpriseValueMultiple"))
    if result.price_to_sales is None:
        result.price_to_sales = _safe_float(ratios.get("priceToSalesRatio"))
    if result.dividend_yield is None:
        result.dividend_yield = _safe_float(ratios.get("dividendYield"))
    if result.gross_margin is None:
        result.gross_margin = _safe_float(ratios.get("grossProfitMargin"))
    if result.operating_margin is None:
        result.operating_margin = _safe_float(ratios.get("operatingProfitMargin"))
    if result.net_margin is None:
        result.net_margin = _safe_float(ratios.get("netProfitMargin"))
    if result.roe is None:
        result.roe = _safe_float(ratios.get("returnOnEquity"))
    if result.roa is None:
        result.roa = _safe_float(ratios.get("returnOnAssets"))
    if result.debt_to_equity is None:
        result.debt_to_equity = _safe_float(ratios.get("debtEquityRatio"))
    if result.current_ratio is None:
        result.current_ratio = _safe_float(ratios.get("currentRatio"))
    if result.free_cash_flow is None:
        result.free_cash_flow = _safe_float(ratios.get("freeCashFlowPerShare"))


_ETF_QUOTE_TYPES = frozenset({"ETF", "MUTUALFUND", "INDEX"})


def _is_etf_like(info: dict[str, Any]) -> bool:
    qt = str(info.get("quoteType") or info.get("quoteTypeDisp") or "").upper()
    return qt in _ETF_QUOTE_TYPES


class FundamentalsAdapter:
    """Fetch fundamentals via yfinance, with optional FMP enrichment."""

    def get_fundamentals(
        self,
        tickers: list[str],
        *,
        progress_callback: Callable[[int, int, str], None] | None = None,
        ticker_result_callback: Callable[[str, FundamentalsResult], None] | None = None,
    ) -> dict[str, FundamentalsResult]:
        if not tickers:
            return {}

        ordered: list[str] = []
        for raw in tickers:
            t = raw.strip()
            if t and t not in ordered:
                ordered.append(t)

        out: dict[str, FundamentalsResult] = {}
        total = len(ordered)
        with _suppress_yfinance_pandas_utcnoise():
            for idx, ticker in enumerate(ordered, start=1):
                if progress_callback is not None:
                    progress_callback(idx, total, ticker)
                result = self._fetch_yfinance(ticker)
                out[ticker] = result
                if ticker_result_callback is not None:
                    ticker_result_callback(ticker, result)

        fmp_key = get_fmp_api_key()
        if fmp_key:
            for ticker, result in out.items():
                if result.fundamentals_available:
                    continue
                try:
                    self._enrich_fmp(ticker, result, fmp_key)
                    _set_fundamentals_available(result)
                    if result.fundamentals_available:
                        result.source = "fmp"
                except Exception as exc:
                    logger.debug("FMP fallback failed for %s: %s", ticker, exc)
                    result.warnings.append(f"FMP fallback failed: {exc}")

        return out

    def _fetch_yfinance(self, ticker: str) -> FundamentalsResult:
        fetched_at = datetime.now(UTC)
        result = FundamentalsResult(
            ticker=ticker,
            source="yahoo",
            fetched_at=fetched_at,
            fundamentals_available=True,
        )
        try:
            yt = yf.Ticker(ticker)
            info = dict(yt.info or {})
            _map_yfinance_info(info, result)
            etf_like = _is_etf_like(info)

            if not etf_like:
                try:
                    financials = yt.financials
                    if isinstance(financials, pd.DataFrame):
                        result.revenue_growth_yoy = _yoy_from_financials(
                            financials, _REVENUE_ROW_NAMES
                        )
                        result.earnings_growth_yoy = _yoy_from_financials(
                            financials, _EARNINGS_ROW_NAMES
                        )
                except Exception as exc:
                    result.warnings.append(f"financials: {exc}")

                try:
                    ed = yt.earnings_dates
                    if isinstance(ed, pd.DataFrame):
                        result.last_earnings_surprise_pct = _earnings_surprise_pct(ed)
                except Exception as exc:
                    result.warnings.append(f"earnings_dates: {exc}")
            else:
                result.warnings.append(
                    "ETF/index — skipped financials and earnings dates (not applicable)."
                )

            try:
                news = yt.news
                if isinstance(news, list):
                    result.recent_headlines = _headlines_from_news(news)
            except Exception as exc:
                result.warnings.append(f"news: {exc}")

            _set_fundamentals_available(result)
        except Exception as exc:
            logger.warning("yfinance fundamentals failed for %s: %s", ticker, exc)
            result.fundamentals_available = False
            result.warnings.append(f"yfinance error: {exc}")

        return result

    def _enrich_fmp(self, ticker: str, result: FundamentalsResult, api_key: str) -> None:
        profile_data = _fmp_request(f"/profile/{urllib.parse.quote(ticker)}", api_key)
        if isinstance(profile_data, list) and profile_data:
            prof = profile_data[0]
            if isinstance(prof, dict):
                _apply_fmp_profile(prof, result)

        ratios_data = _fmp_request(f"/ratios/{urllib.parse.quote(ticker)}?limit=1", api_key)
        if isinstance(ratios_data, list) and ratios_data:
            row = ratios_data[0]
            if isinstance(row, dict):
                _apply_fmp_ratios(row, result)
