"""Technical analysis via yfinance price history and pandas-ta indicators."""

from __future__ import annotations

import contextlib
import logging
import warnings
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

import pandas as pd

# pandas-ta is unavailable on PyPI for py3.11; pandas-ta-classic provides the same df.ta API.
import pandas_ta_classic  # noqa: F401
import yfinance as yf

from ticker_tracker.analysis.base import SignalLabel, TechnicalResult

logger = logging.getLogger(__name__)

_OHLCV = ("Open", "High", "Low", "Close", "Volume")

_COL_EMA = {20: "EMA_20", 50: "EMA_50", 200: "EMA_200"}
_COL_RSI = "RSI_14"
_COL_MACD = "MACD_12_26_9"
_COL_MACD_SIGNAL = "MACDs_12_26_9"
_COL_MACD_HIST = "MACDh_12_26_9"
_COL_BB_UPPER = "BBU_20_2.0"
_COL_BB_MIDDLE = "BBM_20_2.0"
_COL_BB_LOWER = "BBL_20_2.0"
_COL_ATR = "ATRr_14"
_COL_OBV = "OBV"

_GOLDEN_CROSS_LOOKBACK = 10
_MACD_CROSS_LOOKBACK = 5


@contextlib.contextmanager
def _suppress_yfinance_pandas_utcnoise() -> Iterator[None]:
    with warnings.catch_warnings():
        for _cat in (FutureWarning, DeprecationWarning):
            warnings.filterwarnings("ignore", message=".*utcnow.*", category=_cat)
        p4 = getattr(pd.errors, "Pandas4Warning", None)
        if p4 is not None:
            warnings.filterwarnings("ignore", message=".*utcnow.*", category=p4)
        yield


def _safe_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        f = float(value)
        if f != f:
            return None
        return f
    except (TypeError, ValueError):
        return None


def yfinance_period_for_days(period_days: int) -> str:
    """Map requested history length to a yfinance ``period`` string."""
    if period_days <= 5:
        return "5d"
    if period_days <= 30:
        return "1mo"
    if period_days <= 90:
        return "3mo"
    if period_days <= 180:
        return "6mo"
    if period_days <= 365:
        return "1y"
    if period_days <= 730:
        return "2y"
    if period_days <= 1825:
        return "5y"
    return "max"


def extract_ticker_ohlcv(downloaded: pd.DataFrame, ticker: str) -> pd.DataFrame | None:
    """Extract OHLCV for one symbol from a single- or multi-ticker download."""
    if downloaded is None or downloaded.empty:
        return None

    if isinstance(downloaded.columns, pd.MultiIndex):
        levels = downloaded.columns
        tickers_l1 = set(levels.get_level_values(1).astype(str))
        tickers_l0 = set(levels.get_level_values(0).astype(str))
        if ticker in tickers_l1:
            sub = downloaded.xs(ticker, axis=1, level=1)
        elif ticker in tickers_l0:
            sub = downloaded.xs(ticker, axis=1, level=0)
        else:
            return None
    else:
        sub = downloaded

    cols = [c for c in _OHLCV if c in sub.columns]
    if "Close" not in cols:
        return None
    out = sub[cols].copy()
    out = out.dropna(how="all")
    return out if not out.empty else None


def _last_value(series: pd.Series | None) -> float | None:
    if series is None or series.empty:
        return None
    val = series.dropna()
    if val.empty:
        return None
    return _safe_float(val.iloc[-1])


def _crossed_above(
    fast: pd.Series,
    slow: pd.Series,
    lookback: int,
) -> bool | None:
    """True if fast crossed above slow between ``lookback`` days ago and today."""
    diff = (fast - slow).dropna()
    if len(diff) < lookback + 1:
        return None
    today = diff.iloc[-1]
    past = diff.iloc[-(lookback + 1)]
    if today > 0 and past <= 0:
        return True
    return False


def _crossed_below(
    fast: pd.Series,
    slow: pd.Series,
    lookback: int,
) -> bool | None:
    diff = (fast - slow).dropna()
    if len(diff) < lookback + 1:
        return None
    today = diff.iloc[-1]
    past = diff.iloc[-(lookback + 1)]
    if today < 0 and past >= 0:
        return True
    return False


def compute_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """Apply pandas-ta indicators; skip lengths when history is too short."""
    out = df.copy()
    n = len(out)
    if n >= 20:
        out.ta.ema(length=20, append=True)
    if n >= 50:
        out.ta.ema(length=50, append=True)
    if n >= 200:
        out.ta.ema(length=200, append=True)
    if n >= 15:
        out.ta.rsi(length=14, append=True)
    if n >= 35:
        out.ta.macd(fast=12, slow=26, signal=9, append=True)
    if n >= 20:
        out.ta.bbands(length=20, std=2, append=True)
    if n >= 15:
        out.ta.atr(length=14, append=True)
    if "Volume" in out.columns:
        out.ta.obv(append=True)
    return out


def generate_signals(result: TechnicalResult) -> list[SignalLabel]:
    """Build plain-English signal labels from computed technical fields."""
    signals: list[SignalLabel] = []

    if result.rsi_14 is not None:
        if result.rsi_14 < 30:
            signals.append(SignalLabel("RSI Oversold", bullish=True))
        elif result.rsi_14 > 70:
            signals.append(SignalLabel("RSI Overbought", bullish=False))

    if result.price_vs_ema200_pct is not None:
        if result.price_vs_ema200_pct > 0:
            signals.append(SignalLabel("Above 200-day EMA", bullish=True))
        elif result.price_vs_ema200_pct < 0:
            signals.append(SignalLabel("Below 200-day EMA", bullish=False))

    if result.golden_cross is True:
        signals.append(SignalLabel("Golden Cross (EMA50/200)", bullish=True))
    if result.death_cross is True:
        signals.append(SignalLabel("Death Cross (EMA50/200)", bullish=False))

    if result.macd_bullish_cross is True:
        signals.append(SignalLabel("MACD Bullish Cross", bullish=True))

    if result.macd_histogram is not None:
        if result.macd_histogram > 0:
            signals.append(SignalLabel("MACD Positive", bullish=True))
        elif result.macd_histogram < 0:
            signals.append(SignalLabel("MACD Negative", bullish=False))

    if result.bb_width_pct is not None and result.bb_width_pct < 5:
        signals.append(SignalLabel("Bollinger Squeeze (low volatility)", bullish=False))

    if result.volume_vs_20d_avg_pct is not None and result.volume_vs_20d_avg_pct > 50:
        signals.append(SignalLabel("High Volume Spike", bullish=False))

    return signals


def analyse_ohlcv(
    df: pd.DataFrame,
    ticker: str,
    *,
    period_days: int = 365,
) -> TechnicalResult:
    """Run technical analysis on a prepared OHLCV DataFrame (used by tests and production)."""
    n = len(df)
    analysis_date = datetime.now(UTC)
    if not df.empty and isinstance(df.index, pd.DatetimeIndex):
        ts = df.index[-1]
        if hasattr(ts, "to_pydatetime"):
            analysis_date = ts.to_pydatetime().replace(tzinfo=UTC)

    result = TechnicalResult(
        ticker=ticker,
        analysis_date=analysis_date,
        period_days=period_days,
    )

    if n < 2:
        result.warnings.append("Insufficient price history for technical analysis.")
        result.signals = generate_signals(result)
        return result

    if n < 200:
        result.warnings.append(f"Only {n} days of history available — EMA200 not computed.")

    try:
        enriched = compute_indicators(df)
    except Exception as exc:
        result.warnings.append(f"Indicator computation failed: {exc}")
        result.signals = generate_signals(result)
        return result

    close = enriched["Close"]
    price = _last_value(close)

    if _COL_EMA[20] in enriched.columns:
        result.ema_20 = _last_value(enriched[_COL_EMA[20]])
        if price is not None and result.ema_20:
            result.price_vs_ema20_pct = (price - result.ema_20) / result.ema_20 * 100

    if _COL_EMA[50] in enriched.columns:
        result.ema_50 = _last_value(enriched[_COL_EMA[50]])

    if _COL_EMA[200] in enriched.columns:
        result.ema_200 = _last_value(enriched[_COL_EMA[200]])
        if price is not None and result.ema_200:
            result.price_vs_ema200_pct = (price - result.ema_200) / result.ema_200 * 100

    if _COL_EMA[50] in enriched.columns and _COL_EMA[200] in enriched.columns:
        ema50 = enriched[_COL_EMA[50]]
        ema200 = enriched[_COL_EMA[200]]
        result.golden_cross = _crossed_above(ema50, ema200, _GOLDEN_CROSS_LOOKBACK)
        result.death_cross = _crossed_below(ema50, ema200, _GOLDEN_CROSS_LOOKBACK)

    if _COL_RSI in enriched.columns:
        result.rsi_14 = _last_value(enriched[_COL_RSI])

    if _COL_MACD in enriched.columns:
        result.macd_line = _last_value(enriched[_COL_MACD])
    if _COL_MACD_SIGNAL in enriched.columns:
        result.macd_signal = _last_value(enriched[_COL_MACD_SIGNAL])
    if _COL_MACD_HIST in enriched.columns:
        result.macd_histogram = _last_value(enriched[_COL_MACD_HIST])

    if _COL_MACD in enriched.columns and _COL_MACD_SIGNAL in enriched.columns:
        macd_line = enriched[_COL_MACD]
        macd_sig = enriched[_COL_MACD_SIGNAL]
        result.macd_bullish_cross = _crossed_above(macd_line, macd_sig, _MACD_CROSS_LOOKBACK)

    if _COL_BB_UPPER in enriched.columns:
        result.bb_upper = _last_value(enriched[_COL_BB_UPPER])
    if _COL_BB_MIDDLE in enriched.columns:
        result.bb_middle = _last_value(enriched[_COL_BB_MIDDLE])
    if _COL_BB_LOWER in enriched.columns:
        result.bb_lower = _last_value(enriched[_COL_BB_LOWER])
    if result.bb_upper is not None and result.bb_lower is not None and result.bb_middle:
        result.bb_width_pct = (result.bb_upper - result.bb_lower) / result.bb_middle * 100

    if _COL_ATR in enriched.columns:
        result.atr_14 = _last_value(enriched[_COL_ATR])

    if _COL_OBV in enriched.columns:
        result.obv = _last_value(enriched[_COL_OBV])

    if "Volume" in enriched.columns and n >= 21:
        vol = enriched["Volume"].dropna()
        if len(vol) >= 21:
            last_vol = _safe_float(vol.iloc[-1])
            avg20 = _safe_float(vol.iloc[-21:-1].mean())
            if last_vol is not None and avg20 and avg20 > 0:
                result.volume_vs_20d_avg_pct = (last_vol - avg20) / avg20 * 100

    result.signals = generate_signals(result)
    return result


class TechnicalAnalyser:
    """Fetch OHLCV via yfinance and compute technical indicators."""

    def analyse(
        self,
        tickers: list[str],
        period_days: int = 365,
    ) -> dict[str, TechnicalResult]:
        if not tickers:
            return {}

        ordered: list[str] = []
        for raw in tickers:
            t = raw.strip()
            if t and t not in ordered:
                ordered.append(t)

        period = yfinance_period_for_days(period_days)
        out: dict[str, TechnicalResult] = {}

        with _suppress_yfinance_pandas_utcnoise():
            try:
                downloaded = yf.download(
                    tickers=ordered if len(ordered) > 1 else ordered[0],
                    period=period,
                    auto_adjust=True,
                    progress=False,
                    threads=False,
                )
            except Exception as exc:
                logger.exception("yfinance.download failed for technical analysis")
                for ticker in ordered:
                    r = TechnicalResult(
                        ticker=ticker,
                        analysis_date=datetime.now(UTC),
                        period_days=period_days,
                    )
                    r.warnings.append(f"yfinance download error: {exc}")
                    r.signals = generate_signals(r)
                    out[ticker] = r
                return out

        for ticker in ordered:
            try:
                ohlcv = extract_ticker_ohlcv(downloaded, ticker)
                if ohlcv is None or ohlcv.empty:
                    r = TechnicalResult(
                        ticker=ticker,
                        analysis_date=datetime.now(UTC),
                        period_days=period_days,
                    )
                    r.warnings.append("No price history returned from yfinance.")
                    r.signals = generate_signals(r)
                    out[ticker] = r
                    continue
                out[ticker] = analyse_ohlcv(ohlcv, ticker, period_days=period_days)
            except Exception as exc:
                logger.warning("Technical analysis failed for %s: %s", ticker, exc)
                r = TechnicalResult(
                    ticker=ticker,
                    analysis_date=datetime.now(UTC),
                    period_days=period_days,
                )
                r.warnings.append(f"technical analysis error: {exc}")
                r.signals = generate_signals(r)
                out[ticker] = r

        return out
