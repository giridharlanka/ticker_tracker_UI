"""Engine orchestration tests (mocked Google and market data)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from ticker_tracker.config import AppConfig
from ticker_tracker.engine import (
    _email_fmt_shares,
    _parse_float,
    _rank_best_worst,
    _row_price_symbol,
    build_portfolio_email_html,
    run_once,
)
from ticker_tracker.finance.base import PriceResult


def _minimal_config(tmp_path: Path) -> AppConfig:
    return AppConfig(
        email_ids=[],
        finance_sources=["yahoo"],
        fx_source="frankfurter",
        base_currency="SGD",
        google_sheets_id="1" * 22,
        holdings_sheet_name="Holdings",
        column_map={"ticker": "A", "shares": "B", "cost_basis": "C"},
        upload_to_drive=False,
        analysis_enabled=False,
    )


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("79.08", 79.08),
        ("$79.08", 79.08),
        ("1,234.56", 1234.56),
        ("(79.08)", -79.08),
    ],
)
def test_parse_float_accepts_common_sheet_formats(raw: str, expected: float) -> None:
    assert _parse_float(raw) == pytest.approx(expected)


def test_email_fmt_shares_no_fractional_decimals() -> None:
    assert _email_fmt_shares(100.0) == "100"
    assert _email_fmt_shares(100.7) == "101"


def test_row_price_symbol_uses_exchange_map() -> None:
    row = {"ticker": "Z74", "exchange": "SGX"}
    assert _row_price_symbol(row) == "Z74.SI"


def _row_us(
    ticker: str,
    shares: float,
    cb_b: float,
    cv_b: float,
    cb_p: float,
    cv_p: float,
) -> dict[str, object]:
    return {
        "ticker": ticker,
        "shares": shares,
        "cost_basis_base": cb_b,
        "current_value_base": cv_b,
        "price_fetch_failed": False,
        "fx_unavailable": False,
        "report_ccy": "USD",
        "cost_basis_purchase": cb_p,
        "current_value_purchase": cv_p,
    }


def test_rank_best_worst_merges_duplicate_ticker_rows() -> None:
    """Multiple sheet rows for one ticker become one line in top-N ranking."""
    holdings = [
        _row_us("AZN", 1.0, 100.0, 110.0, 100.0, 110.0),
        _row_us("AZN", 1.0, 100.0, 110.0, 100.0, 110.0),
        _row_us("AZN", 1.0, 100.0, 110.0, 100.0, 110.0),
        _row_us("VOO", 1.0, 200.0, 210.0, 200.0, 210.0),
        _row_us("Z", 1.0, 100.0, 109.0, 100.0, 109.0),
    ]
    best, _ = _rank_best_worst(holdings, n=3)
    tickers = [b["ticker"] for b in best]
    assert tickers.count("AZN") == 1
    assert len(best) == 3
    azn = next(b for b in best if b["ticker"] == "AZN")
    assert azn["shares"] == pytest.approx(3.0)
    assert azn["cost_basis_base"] == pytest.approx(300.0)
    assert azn["current_value_base"] == pytest.approx(330.0)


@patch("ticker_tracker.engine.upload_file")
@patch("ticker_tracker.engine.read_holdings")
@patch("ticker_tracker.engine.get_prices_with_fallback")
@patch("ticker_tracker.fx.frankfurter.urllib.request.urlopen")
def test_run_once_default_skips_drive_upload(
    mock_urlopen: MagicMock,
    mock_prices: MagicMock,
    mock_read: MagicMock,
    mock_upload: MagicMock,
    tmp_path: Path,
) -> None:
    import json

    mock_read.return_value = [{"ticker": "VOO", "shares": "1", "cost_basis": "500"}]
    mock_prices.return_value = {"VOO": PriceResult(400.0, "USD", 400.0, "yahoo")}

    class _Resp:
        def read(self) -> bytes:
            return json.dumps(
                {"amount": 1.0, "base": "SGD", "date": "2026-01-10", "rates": {"USD": 1.0}}
            ).encode()

        def __enter__(self) -> _Resp:
            return self

        def __exit__(self, *a: object) -> bool:
            return False

    mock_urlopen.return_value = _Resp()
    out = run_once(
        app_config=_minimal_config(tmp_path),
        send_email_notifications=False,
        workbook_path=tmp_path / "n.xlsx",
    )
    assert out["drive_url"] is None
    mock_upload.assert_not_called()


@patch("ticker_tracker.engine.upload_file")
@patch("ticker_tracker.engine.read_holdings")
@patch("ticker_tracker.engine.get_prices_with_fallback")
@patch("ticker_tracker.fx.frankfurter.urllib.request.urlopen")
def test_run_once_fetches_price_symbol_from_exchange(
    mock_urlopen: MagicMock,
    mock_prices: MagicMock,
    mock_read: MagicMock,
    mock_upload: MagicMock,
    tmp_path: Path,
) -> None:
    import json

    mock_read.return_value = [
        {
            "ticker": "Z74",
            "exchange": "SGX",
            "shares": "100",
            "cost_basis": "1",
            "purchase_currency": "SGD",
        },
    ]
    mock_prices.return_value = {"Z74.SI": PriceResult(2.0, "SGD", 2.0, "yahoo")}

    class _Resp:
        def read(self) -> bytes:
            return json.dumps(
                {"amount": 1.0, "base": "SGD", "date": "2026-01-10", "rates": {"USD": 0.74}}
            ).encode()

        def __enter__(self) -> _Resp:
            return self

        def __exit__(self, *a: object) -> bool:
            return False

    mock_urlopen.return_value = _Resp()
    cfg = _minimal_config(tmp_path)
    cfg.column_map = {
        "ticker": "A",
        "exchange": "B",
        "shares": "C",
        "cost_basis": "D",
        "purchase_currency": "E",
    }
    out = run_once(
        app_config=cfg,
        upload_to_drive=False,
        send_email_notifications=False,
        workbook_path=tmp_path / "sg.xlsx",
    )
    mock_prices.assert_called_once()
    assert mock_prices.call_args[0][1] == ["Z74.SI"]
    h0 = out["holdings"][0]
    assert h0["ticker"] == "Z74"
    assert h0["native_ccy"] == "SGD"
    assert h0["cost_basis_base"] == pytest.approx(100.0)
    assert h0["current_value_base"] == pytest.approx(200.0)


@patch("ticker_tracker.engine.upload_file")
@patch("ticker_tracker.engine.read_holdings")
@patch("ticker_tracker.engine.get_prices_with_fallback")
@patch("ticker_tracker.fx.frankfurter.urllib.request.urlopen")
def test_run_once_batches_fx_and_prices(
    mock_urlopen: MagicMock,
    mock_prices: MagicMock,
    mock_read: MagicMock,
    mock_upload: MagicMock,
    tmp_path: Path,
) -> None:
    import json

    mock_read.return_value = [
        {"ticker": "VOO", "shares": "1", "cost_basis": "500"},
    ]
    mock_prices.return_value = {
        "VOO": PriceResult(400.0, "USD", 400.0, "yahoo"),
    }

    class _Resp:
        def read(self) -> bytes:
            return json.dumps(
                {
                    "amount": 1.0,
                    "base": "SGD",
                    "date": "2026-01-10",
                    "rates": {"USD": 0.74},
                }
            ).encode()

        def __enter__(self) -> _Resp:
            return self

        def __exit__(self, *a: object) -> bool:
            return False

    mock_urlopen.return_value = _Resp()
    mock_upload.return_value = "https://drive.example/x"

    xlsx = tmp_path / "out.xlsx"
    out = run_once(
        app_config=_minimal_config(tmp_path),
        credentials=MagicMock(),
        upload_to_drive=True,
        send_email_notifications=False,
        workbook_path=xlsx,
    )

    assert xlsx.is_file()
    assert out["summary"]["holding_count"] == 1
    assert mock_urlopen.call_count == 1
    mock_prices.assert_called_once()
    h0 = out["holdings"][0]
    assert h0["ticker"] == "VOO"
    assert h0["native_ccy"] == "USD"
    assert h0["current_value_base"] == pytest.approx(400.0 * (1.0 / 0.74))
    mock_upload.assert_called_once()


@patch("ticker_tracker.engine.upload_file")
@patch("ticker_tracker.engine.read_holdings")
@patch("ticker_tracker.engine.get_prices_with_fallback")
@patch("ticker_tracker.fx.frankfurter.urllib.request.urlopen")
def test_run_once_uses_cost_basis_per_share(
    mock_urlopen: MagicMock,
    mock_prices: MagicMock,
    mock_read: MagicMock,
    mock_upload: MagicMock,
    tmp_path: Path,
) -> None:
    import json

    mock_read.return_value = [{"ticker": "VOO", "shares": "10", "cost_basis": "50"}]
    mock_prices.return_value = {"VOO": PriceResult(40.0, "USD", 40.0, "yahoo")}

    class _Resp:
        def read(self) -> bytes:
            return json.dumps(
                {"amount": 1.0, "base": "SGD", "date": "2026-01-10", "rates": {"USD": 1.0}}
            ).encode()

        def __enter__(self) -> _Resp:
            return self

        def __exit__(self, *a: object) -> bool:
            return False

    mock_urlopen.return_value = _Resp()
    out = run_once(
        app_config=_minimal_config(tmp_path),
        upload_to_drive=False,
        send_email_notifications=False,
        workbook_path=tmp_path / "c.xlsx",
    )
    h0 = out["holdings"][0]
    assert h0["cost_basis_base"] == pytest.approx(500.0)
    assert h0["current_value_base"] == pytest.approx(400.0)
    assert h0["gain_loss_base"] == pytest.approx(-100.0)
    mock_upload.assert_not_called()


@patch("ticker_tracker.engine.send_email")
@patch("ticker_tracker.engine.read_holdings")
@patch("ticker_tracker.engine.get_prices_with_fallback")
@patch("ticker_tracker.fx.frankfurter.urllib.request.urlopen")
def test_run_once_deduplicates_recipients(
    mock_urlopen: MagicMock,
    mock_prices: MagicMock,
    mock_read: MagicMock,
    mock_send_email: MagicMock,
    tmp_path: Path,
) -> None:
    import json

    cfg = _minimal_config(tmp_path)
    cfg.email_ids = ["a@example.com", "A@example.com", " a@example.com "]
    mock_read.return_value = [{"ticker": "VOO", "shares": "1", "cost_basis": "50"}]
    mock_prices.return_value = {"VOO": PriceResult(40.0, "USD", 40.0, "yahoo")}

    class _Resp:
        def read(self) -> bytes:
            return json.dumps(
                {"amount": 1.0, "base": "SGD", "date": "2026-01-10", "rates": {"USD": 1.0}}
            ).encode()

        def __enter__(self) -> _Resp:
            return self

        def __exit__(self, *a: object) -> bool:
            return False

    mock_urlopen.return_value = _Resp()
    out = run_once(
        app_config=cfg,
        upload_to_drive=False,
        send_email_notifications=True,
        workbook_path=tmp_path / "e.xlsx",
    )
    assert out["emails_sent"] == 1
    mock_send_email.assert_called_once()
    body = mock_send_email.call_args[0][2]
    assert "Top 5 gainers" in body
    assert "Top 5 losers" in body
    assert "Holdings" in body


@patch("ticker_tracker.engine.send_email")
@patch("ticker_tracker.engine.upload_file")
@patch("ticker_tracker.engine.read_holdings")
@patch("ticker_tracker.engine.read_local_holdings")
@patch("ticker_tracker.engine.get_prices_with_fallback")
@patch("ticker_tracker.fx.frankfurter.urllib.request.urlopen")
def test_run_once_local_source_skips_google_features(
    mock_urlopen: MagicMock,
    mock_prices: MagicMock,
    mock_read_local: MagicMock,
    mock_read_sheets: MagicMock,
    mock_upload: MagicMock,
    mock_send_email: MagicMock,
    tmp_path: Path,
) -> None:
    import json

    cfg = _minimal_config(tmp_path)
    cfg.holdings_source = "local_file"
    cfg.local_holdings_path = str(tmp_path / "h.csv")
    cfg.output_formats = ["html"]
    cfg.upload_to_drive = True
    cfg.email_ids = ["a@example.com"]
    mock_read_local.return_value = [{"ticker": "VOO", "shares": "1", "cost_basis": "500"}]
    mock_prices.return_value = {"VOO": PriceResult(400.0, "USD", 400.0, "yahoo")}

    class _Resp:
        def read(self) -> bytes:
            return json.dumps(
                {"amount": 1.0, "base": "SGD", "date": "2026-01-10", "rates": {"USD": 1.0}}
            ).encode()

        def __enter__(self) -> _Resp:
            return self

        def __exit__(self, *a: object) -> bool:
            return False

    mock_urlopen.return_value = _Resp()
    out = run_once(app_config=cfg, send_email_notifications=True)
    assert out["workbook_path"] is None
    assert out["html_report_path"]
    mock_read_sheets.assert_not_called()
    mock_upload.assert_not_called()
    mock_send_email.assert_not_called()


def test_build_portfolio_email_html_highlights_tables() -> None:
    summary = {
        "total_cost_basis_base": 200.0,
        "total_current_value_base": 250.0,
        "total_gain_loss_base": 50.0,
        "total_return_pct": 25.0,
        "holding_count": 2,
        "distinct_ticker_count": 2,
        "totals_purchase_cost_formatted": "USD 200.00",
        "totals_purchase_value_formatted": "USD 250.00",
        "totals_purchase_gl_formatted": "USD 50.00",
        "totals_purchase_cost_by_ccy": {"USD": 200.0},
        "totals_purchase_value_by_ccy": {"USD": 250.0},
        "totals_purchase_gl_by_ccy": {"USD": 50.0},
    }
    holdings = [
        {
            "ticker": "AAA",
            "shares": 1.0,
            "report_ccy": "USD",
            "cost_per_share_purchase": 100.0,
            "cost_basis_purchase": 100.0,
            "price_per_share_purchase": 150.0,
            "current_value_purchase": 150.0,
            "gain_loss_purchase": 50.0,
            "gain_loss_pct_purchase": 50.0,
            "cost_basis_base": 100.0,
            "native_ccy": "USD",
            "price_native": 100.0,
            "fx_rate_display": 1.0,
            "current_price_base": 100.0,
            "current_value_base": 150.0,
            "gain_loss_base": 50.0,
            "gain_loss_pct": 50.0,
            "price_fetch_failed": False,
            "fx_unavailable": False,
        },
        {
            "ticker": "BBB",
            "shares": 1.0,
            "report_ccy": "USD",
            "cost_per_share_purchase": 100.0,
            "cost_basis_purchase": 100.0,
            "price_per_share_purchase": 100.0,
            "current_value_purchase": 100.0,
            "gain_loss_purchase": 0.0,
            "gain_loss_pct_purchase": 0.0,
            "cost_basis_base": 100.0,
            "native_ccy": "USD",
            "price_native": 100.0,
            "fx_rate_display": 1.0,
            "current_price_base": 100.0,
            "current_value_base": 100.0,
            "gain_loss_base": 0.0,
            "gain_loss_pct": 0.0,
            "price_fetch_failed": False,
            "fx_unavailable": False,
        },
    ]
    body = build_portfolio_email_html(
        base="SGD", summary=summary, holdings=holdings, drive_url="https://drive.example/x"
    )
    assert "#d4edda" in body
    assert "#f8d7da" in body
    assert "AAA" in body and "BBB" in body
    assert "Top 5 gainers (USD" in body
    assert "Top 5 losers (USD" in body
    assert "Base</th>" in body
    assert "Base (SGD)" not in body
    assert "Portfolio summary (SGD) as of " in body


@patch("ticker_tracker.engine.upload_file")
@patch("ticker_tracker.engine.read_holdings")
@patch("ticker_tracker.engine.get_prices_with_fallback")
@patch("ticker_tracker.fx.frankfurter.urllib.request.urlopen")
def test_run_once_uploads_drive_when_config_enabled(
    mock_urlopen: MagicMock,
    mock_prices: MagicMock,
    mock_read: MagicMock,
    mock_upload: MagicMock,
    tmp_path: Path,
) -> None:
    import json

    cfg = _minimal_config(tmp_path)
    cfg.upload_to_drive = True
    mock_read.return_value = [{"ticker": "VOO", "shares": "1", "cost_basis": "50"}]
    mock_prices.return_value = {"VOO": PriceResult(40.0, "USD", 40.0, "yahoo")}

    class _Resp:
        def read(self) -> bytes:
            return json.dumps(
                {"amount": 1.0, "base": "SGD", "date": "2026-01-10", "rates": {"USD": 1.0}}
            ).encode()

        def __enter__(self) -> _Resp:
            return self

        def __exit__(self, *a: object) -> bool:
            return False

    mock_urlopen.return_value = _Resp()
    mock_upload.return_value = "https://drive.example/x"
    run_once(
        app_config=cfg,
        credentials=MagicMock(),
        send_email_notifications=False,
        workbook_path=tmp_path / "d.xlsx",
    )
    mock_upload.assert_called_once()


@patch("ticker_tracker.fx.frankfurter.urllib.request.urlopen")
@patch("ticker_tracker.engine.read_holdings")
@patch("ticker_tracker.engine.get_prices_with_fallback")
def test_run_once_marks_price_failure(
    mock_prices: MagicMock,
    mock_read: MagicMock,
    mock_urlopen: MagicMock,
    tmp_path: Path,
) -> None:
    import json

    from ticker_tracker.finance.base import FinanceAdapterError

    mock_read.return_value = [{"ticker": "ZZZ", "shares": "1", "cost_basis": "10"}]
    mock_prices.side_effect = FinanceAdapterError("down")

    class _Resp:
        def read(self) -> bytes:
            return json.dumps(
                {"amount": 1.0, "base": "SGD", "date": "2026-01-10", "rates": {"USD": 0.74}}
            ).encode()

        def __enter__(self) -> _Resp:
            return self

        def __exit__(self, *a: object) -> bool:
            return False

    mock_urlopen.return_value = _Resp()

    xlsx = tmp_path / "f.xlsx"
    out = run_once(
        app_config=_minimal_config(tmp_path),
        credentials=MagicMock(),
        upload_to_drive=False,
        send_email_notifications=False,
        workbook_path=xlsx,
    )
    assert out["holdings"][0]["price_fetch_failed"] is True
    assert "ZZZ" in out["metadata"]["price_fetch_failed"]


@patch("ticker_tracker.engine.read_holdings")
@patch("ticker_tracker.engine.get_prices_with_fallback")
@patch("ticker_tracker.engine.FXRunRegistry")
def test_run_once_marks_fx_unavailable(
    mock_fxr: MagicMock,
    mock_prices: MagicMock,
    mock_read: MagicMock,
    tmp_path: Path,
) -> None:
    from ticker_tracker.fx.base import FXAdapterError

    mock_read.return_value = [{"ticker": "X", "shares": "1", "cost_basis": "1"}]
    mock_prices.return_value = {"X": PriceResult(1.0, "EUR", 1.0, "yahoo")}
    inst = MagicMock()
    inst.cached_fx_rates.return_value = []
    inst.convert.side_effect = FXAdapterError("no rate")
    mock_fxr.return_value = inst

    out = run_once(
        app_config=_minimal_config(tmp_path),
        credentials=MagicMock(),
        upload_to_drive=False,
        send_email_notifications=False,
        workbook_path=tmp_path / "u.xlsx",
    )
    assert out["holdings"][0]["fx_unavailable"] is True
    assert "X" in out["metadata"]["fx_unavailable_tickers"]


@patch("ticker_tracker.engine.get_prices_with_fallback")
@patch("ticker_tracker.engine.read_holdings")
def test_run_once_rejects_unsupported_fx(
    mock_rh: MagicMock, mock_prices: MagicMock, tmp_path: Path
) -> None:
    mock_rh.return_value = [{"ticker": "X", "shares": "1", "cost_basis": "1"}]
    mock_prices.return_value = {"X": PriceResult(1.0, "USD", 1.0, "yahoo")}
    cfg = _minimal_config(tmp_path)
    cfg.fx_source = "fixer"
    with pytest.raises(ValueError, match="Unsupported fx_source"):
        run_once(
            app_config=cfg,
            upload_to_drive=False,
            send_email_notifications=False,
            workbook_path=tmp_path / "z.xlsx",
        )


@patch("ticker_tracker.engine.get_prices_with_fallback")
@patch("ticker_tracker.engine.read_holdings")
@patch("ticker_tracker.fx.frankfurter.urllib.request.urlopen")
def test_run_once_progress_callback_reaches_one_hundred(
    mock_urlopen: MagicMock,
    mock_read: MagicMock,
    mock_prices: MagicMock,
    tmp_path: Path,
) -> None:
    import json

    mock_read.return_value = [{"ticker": "VOO", "shares": "1", "cost_basis": "500"}]
    mock_prices.return_value = {"VOO": PriceResult(400.0, "USD", 400.0, "yahoo")}

    class _Resp:
        def read(self) -> bytes:
            return json.dumps(
                {"amount": 1.0, "base": "SGD", "date": "2026-01-10", "rates": {"USD": 1.0}}
            ).encode()

        def __enter__(self) -> _Resp:
            return self

        def __exit__(self, *a: object) -> bool:
            return False

    mock_urlopen.return_value = _Resp()
    seen: list[tuple[int, str]] = []

    def progress_cb(pct: int, msg: str) -> None:
        seen.append((pct, msg))

    run_once(
        app_config=_minimal_config(tmp_path),
        send_email_notifications=False,
        workbook_path=tmp_path / "prog.xlsx",
        progress_callback=progress_cb,
    )
    pcts = [p for p, _ in seen]
    assert pcts[0] < pcts[-1]
    assert pcts[-1] == 100
    assert any("Reading holdings" in m for _, m in seen)
    assert any("Done!" in m for _, m in seen)
