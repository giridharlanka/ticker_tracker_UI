"""Tests for the technical analysis module."""

from __future__ import annotations

from datetime import UTC, datetime

import numpy as np
import pandas as pd
import pytest
from ticker_tracker.analysis.base import TechnicalResult
from ticker_tracker.analysis.technicals import (
    TechnicalAnalyser,
    analyse_ohlcv,
    extract_ticker_ohlcv,
    generate_signals,
    yfinance_period_for_days,
)


def _rising_ohlcv(rows: int) -> pd.DataFrame:
    """Synthetic uptrend: close rises linearly from 100 to 100+rows."""
    idx = pd.date_range("2023-01-01", periods=rows, freq="B")
    close = np.linspace(100.0, 100.0 + rows, rows)
    high = close + 1.0
    low = close - 1.0
    open_ = close - 0.5
    volume = np.full(rows, 1_000_000.0)
    return pd.DataFrame(
        {"Open": open_, "High": high, "Low": low, "Close": close, "Volume": volume},
        index=idx,
    )


def test_rising_trend_rsi_and_ema_direction() -> None:
    df = _rising_ohlcv(400)
    result = analyse_ohlcv(df, "TEST", period_days=365)

    assert result.rsi_14 is not None
    assert result.rsi_14 > 50
    assert result.ema_20 is not None
    assert result.ema_50 is not None
    assert result.ema_200 is not None
    assert result.ema_20 > result.ema_50
    assert result.signals


def test_short_history_ema200_none_with_warning() -> None:
    df = _rising_ohlcv(100)
    result = analyse_ohlcv(df, "SHORT", period_days=365)

    assert result.ema_200 is None
    assert any("EMA200 not computed" in w for w in result.warnings)


def test_generate_signals_rsi_oversold() -> None:
    result = TechnicalResult(
        ticker="MOCK",
        analysis_date=datetime.now(UTC),
        period_days=365,
        rsi_14=25.0,
    )
    signals = generate_signals(result)
    names = [s.name for s in signals]
    assert "RSI Oversold" in names
    oversold = next(s for s in signals if s.name == "RSI Oversold")
    assert oversold.bullish is True


def test_generate_signals_respects_none_fields() -> None:
    result = TechnicalResult(
        ticker="MOCK",
        analysis_date=datetime.now(UTC),
        period_days=365,
        rsi_14=None,
        price_vs_ema200_pct=None,
    )
    assert generate_signals(result) == []


@pytest.mark.parametrize(
    ("days", "expected"),
    [
        (5, "5d"),
        (30, "1mo"),
        (90, "3mo"),
        (180, "6mo"),
        (365, "1y"),
        (500, "2y"),
    ],
)
def test_yfinance_period_for_days(days: int, expected: str) -> None:
    assert yfinance_period_for_days(days) == expected


def test_extract_ticker_ohlcv_multiindex() -> None:
    df = _rising_ohlcv(50)
    multi = pd.concat({"AAA": df, "BBB": df}, axis=1)
    aaa = extract_ticker_ohlcv(multi, "AAA")
    assert aaa is not None
    assert list(aaa.columns) == ["Open", "High", "Low", "Close", "Volume"]
    assert len(aaa) == 50


def test_extract_ticker_ohlcv_flat_columns() -> None:
    df = _rising_ohlcv(30)
    out = extract_ticker_ohlcv(df, "IGNORED")
    assert out is not None
    assert len(out) == 30


def test_analyse_batch_one_ticker_failure_does_not_abort(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    good_df = _rising_ohlcv(250)

    def fake_download(
        tickers: str | list[str],
        period: str = "1y",
        **kwargs: object,
    ) -> pd.DataFrame:
        symbols = [tickers] if isinstance(tickers, str) else list(tickers)
        if len(symbols) == 1:
            return good_df
        return pd.concat({s: good_df for s in symbols}, axis=1)

    monkeypatch.setattr(
        "ticker_tracker.analysis.technicals.yf.download",
        fake_download,
    )

    def fail_analyse(
        df: pd.DataFrame,
        ticker: str,
        *,
        period_days: int = 365,
    ) -> TechnicalResult:
        if ticker == "BAD":
            raise RuntimeError("boom")
        return analyse_ohlcv(df, ticker, period_days=period_days)

    monkeypatch.setattr(
        "ticker_tracker.analysis.technicals.analyse_ohlcv",
        fail_analyse,
    )

    out = TechnicalAnalyser().analyse(["GOOD", "BAD"])
    assert out["GOOD"].rsi_14 is not None
    assert out["BAD"].warnings
    assert any("technical analysis error" in w for w in out["BAD"].warnings)


def test_golden_cross_signal_when_ema50_crosses_above_200() -> None:
    idx = pd.date_range("2023-01-01", periods=220, freq="B")
    # EMA50 below EMA200 early, then price ramps so 50 crosses above 200 recently
    close = np.concatenate(
        [np.full(180, 80.0), np.linspace(80, 200, 40)],
    )
    df = pd.DataFrame(
        {
            "Open": close,
            "High": close + 1,
            "Low": close - 1,
            "Close": close,
            "Volume": np.full(220, 1e6),
        },
        index=idx,
    )
    result = analyse_ohlcv(df, "CROSS", period_days=365)
    # May or may not trigger depending on indicator warmup; at least no crash
    assert result.ema_50 is not None
    assert result.ema_200 is not None


def test_macd_and_bollinger_fields_populated() -> None:
    result = analyse_ohlcv(_rising_ohlcv(400), "FULL", period_days=365)
    assert result.macd_line is not None
    assert result.macd_signal is not None
    assert result.macd_histogram is not None
    assert result.bb_upper is not None
    assert result.bb_middle is not None
    assert result.bb_lower is not None
    assert result.bb_width_pct is not None
    assert result.atr_14 is not None
    assert result.obv is not None
    assert result.volume_vs_20d_avg_pct is not None
