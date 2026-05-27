"""Tests for the fundamentals adapter layer."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest
from ticker_tracker.analysis.fundamentals import (
    FundamentalsAdapter,
    normalize_analyst_recommendation,
)
from ticker_tracker.analysis.base import FundamentalsResult

REALISTIC_YF_INFO = {
    "trailingPE": 28.5,
    "priceToBook": 12.3,
    "enterpriseToEbitda": 18.2,
    "priceToSalesTrailing12Months": 7.1,
    "dividendYield": 0.012,
    "grossMargins": 0.45,
    "operatingMargins": 0.28,
    "profitMargins": 0.22,
    "returnOnEquity": 0.35,
    "returnOnAssets": 0.18,
    "debtToEquity": 85.0,
    "currentRatio": 1.4,
    "freeCashflow": 9_500_000_000.0,
    "targetMeanPrice": 210.0,
    "numberOfAnalystOpinions": 42,
    "recommendationKey": "buy",
}


def _mock_ticker(
    info: dict | None = None,
    *,
    financials: pd.DataFrame | None = None,
    news: list | None = None,
    earnings_dates: pd.DataFrame | None = None,
    raise_info: bool = False,
) -> MagicMock:
    mock = MagicMock()
    if raise_info:
        type(mock).info = property(lambda self: (_ for _ in ()).throw(RuntimeError("boom")))
    else:
        mock.info = info or {}
    mock.financials = financials if financials is not None else pd.DataFrame()
    mock.news = news if news is not None else []
    mock.earnings_dates = earnings_dates if earnings_dates is not None else pd.DataFrame()
    return mock


@patch("ticker_tracker.analysis.fundamentals.yf.Ticker")
def test_yfinance_field_mappings(mock_ticker_cls: MagicMock) -> None:
    mock_ticker_cls.return_value = _mock_ticker(REALISTIC_YF_INFO)

    out = FundamentalsAdapter().get_fundamentals(["AAPL"])
    r = out["AAPL"]

    assert r.source == "yahoo"
    assert r.fundamentals_available is True
    assert r.pe_ratio == 28.5
    assert r.pb_ratio == 12.3
    assert r.ev_ebitda == 18.2
    assert r.price_to_sales == 7.1
    assert r.dividend_yield == 0.012
    assert r.gross_margin == 0.45
    assert r.operating_margin == 0.28
    assert r.net_margin == 0.22
    assert r.roe == 0.35
    assert r.roa == 0.18
    assert r.debt_to_equity == 85.0
    assert r.current_ratio == 1.4
    assert r.free_cash_flow == 9_500_000_000.0
    assert r.target_price == 210.0
    assert r.analyst_count == 42
    assert r.analyst_recommendation == "buy"


@patch("ticker_tracker.analysis.fundamentals.yf.Ticker")
def test_pe_ratio_falls_back_to_forward_pe(mock_ticker_cls: MagicMock) -> None:
    info = {"forwardPE": 19.0}
    mock_ticker_cls.return_value = _mock_ticker(info)

    r = FundamentalsAdapter().get_fundamentals(["XYZ"])["XYZ"]
    assert r.pe_ratio == 19.0


@patch("ticker_tracker.analysis.fundamentals.yf.Ticker")
def test_fundamentals_available_false_when_sparse(mock_ticker_cls: MagicMock) -> None:
    sparse = {"trailingPE": 10.0, "priceToBook": 2.0}
    mock_ticker_cls.return_value = _mock_ticker(sparse)

    r = FundamentalsAdapter().get_fundamentals(["SPARSE"])["SPARSE"]
    assert r.fundamentals_available is False
    assert any("Sparse fundamentals" in w for w in r.warnings)


@patch("ticker_tracker.analysis.fundamentals.yf.Ticker")
def test_one_ticker_failure_does_not_abort_batch(mock_ticker_cls: MagicMock) -> None:
    good = _mock_ticker(REALISTIC_YF_INFO)
    bad = _mock_ticker(raise_info=True)

    def side_effect(symbol: str) -> MagicMock:
        if symbol == "BAD":
            return bad
        return good

    mock_ticker_cls.side_effect = side_effect

    out = FundamentalsAdapter().get_fundamentals(["GOOD", "BAD"])
    assert out["GOOD"].fundamentals_available is True
    assert out["BAD"].fundamentals_available is False
    assert out["BAD"].warnings


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("strong_buy", "strong_buy"),
        ("strongBuy", "strong_buy"),
        ("buy", "buy"),
        ("hold", "hold"),
        ("sell", "sell"),
        ("strong_sell", "strong_sell"),
        ("strongSell", "strong_sell"),
        ("underperform", "sell"),
        ("outperform", "buy"),
        ("none", "hold"),
        ("neutral", "hold"),
        (None, None),
        ("", None),
    ],
)
def test_analyst_recommendation_normalisation(raw: str | None, expected: str | None) -> None:
    assert normalize_analyst_recommendation(raw) == expected


@patch("ticker_tracker.analysis.fundamentals.yf.Ticker")
def test_revenue_and_earnings_growth_yoy(mock_ticker_cls: MagicMock) -> None:
    financials = pd.DataFrame(
        {
            pd.Timestamp("2024-12-31"): [110.0, 11.0],
            pd.Timestamp("2023-12-31"): [100.0, 10.0],
        },
        index=["Total Revenue", "Net Income"],
    )
    mock_ticker_cls.return_value = _mock_ticker(REALISTIC_YF_INFO, financials=financials)

    r = FundamentalsAdapter().get_fundamentals(["GROW"])["GROW"]
    assert r.revenue_growth_yoy == pytest.approx(0.1)
    assert r.earnings_growth_yoy == pytest.approx(0.1)


@patch("ticker_tracker.analysis.fundamentals.yf.Ticker")
def test_recent_headlines_truncated(mock_ticker_cls: MagicMock) -> None:
    long_title = "x" * 150
    news = [{"title": long_title}, {"title": "Short headline"}]
    mock_ticker_cls.return_value = _mock_ticker(REALISTIC_YF_INFO, news=news)

    r = FundamentalsAdapter().get_fundamentals(["NEWS"])["NEWS"]
    assert len(r.recent_headlines) == 2
    assert len(r.recent_headlines[0]) == 120
    assert r.recent_headlines[1] == "Short headline"


@patch("ticker_tracker.analysis.fundamentals.yf.Ticker")
def test_earnings_surprise_pct(mock_ticker_cls: MagicMock) -> None:
    earnings_dates = pd.DataFrame(
        {"Surprise(%)": [None, 5.2, 1.0]},
        index=[
            pd.Timestamp("2025-01-01"),
            pd.Timestamp("2024-10-01"),
            pd.Timestamp("2024-07-01"),
        ],
    )
    mock_ticker_cls.return_value = _mock_ticker(
        REALISTIC_YF_INFO, earnings_dates=earnings_dates
    )

    r = FundamentalsAdapter().get_fundamentals(["ERN"])["ERN"]
    assert r.last_earnings_surprise_pct == 5.2


@patch("ticker_tracker.analysis.fundamentals.get_fmp_api_key", return_value="test-fmp-key")
@patch("ticker_tracker.analysis.fundamentals._fmp_request")
@patch("ticker_tracker.analysis.fundamentals.yf.Ticker")
def test_fmp_fallback_when_yfinance_sparse(
    mock_ticker_cls: MagicMock,
    mock_fmp_request: MagicMock,
    _mock_fmp_key: MagicMock,
) -> None:
    mock_ticker_cls.return_value = _mock_ticker({"trailingPE": 5.0})
    mock_fmp_request.side_effect = [
        [{"pe": 8.0, "pb": 1.5, "dividendYield": 0.02}],
        [{"priceEarningsRatio": 8.0, "grossProfitMargin": 0.4, "returnOnEquity": 0.2}],
    ]

    r = FundamentalsAdapter().get_fundamentals(["FALLBACK"])["FALLBACK"]
    assert mock_fmp_request.call_count == 2
    assert r.pe_ratio == 5.0
    assert r.pb_ratio == 1.5
    assert r.gross_margin == 0.4


@patch("ticker_tracker.analysis.fundamentals.get_fmp_api_key", return_value=None)
@patch("ticker_tracker.analysis.fundamentals._fmp_request")
@patch("ticker_tracker.analysis.fundamentals.yf.Ticker")
def test_fmp_skipped_without_api_key(
    mock_ticker_cls: MagicMock,
    mock_fmp_request: MagicMock,
    _mock_fmp_key: MagicMock,
) -> None:
    mock_ticker_cls.return_value = _mock_ticker({})
    FundamentalsAdapter().get_fundamentals(["NOKEY"])
    mock_fmp_request.assert_not_called()


def test_core_ratio_none_count_helper() -> None:
    r = FundamentalsResult(
        ticker="T",
        source="test",
        fetched_at=datetime.now(UTC),
        fundamentals_available=True,
        pe_ratio=1.0,
        pb_ratio=2.0,
    )
    from ticker_tracker.analysis.fundamentals import _core_ratio_none_count

    assert _core_ratio_none_count(r) == 8
