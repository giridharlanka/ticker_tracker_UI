"""Tabbed HTML report tests."""

from __future__ import annotations

from ticker_tracker.html_report import TabbedReportState, build_portfolio_tabbed_html


def test_tabbed_html_includes_sheet_tabs_and_drilldown() -> None:
    html = build_portfolio_tabbed_html(
        base="SGD",
        summary={
            "total_cost_basis_base": 1000,
            "total_current_value_base": 1100,
            "holding_count": 1,
        },
        holdings=[
            {
                "ticker": "AAPL",
                "shares": 10,
                "report_ccy": "USD",
                "cost_per_share_purchase": 100,
                "cost_basis_purchase": 1000,
                "price_per_share_purchase": 110,
                "current_value_purchase": 1100,
                "gain_loss_purchase": 100,
                "gain_loss_pct_purchase": 10.0,
                "cost_basis_base": 1000,
                "current_value_base": 1100,
                "gain_loss_base": 100,
                "gain_loss_pct": 10.0,
                "price_fetch_failed": False,
                "fx_unavailable": False,
            },
            {
                "ticker": "MSFT",
                "shares": 5,
                "report_ccy": "USD",
                "cost_per_share_purchase": 200,
                "cost_basis_purchase": 1000,
                "price_per_share_purchase": 180,
                "current_value_purchase": 900,
                "gain_loss_purchase": -100,
                "gain_loss_pct_purchase": -10.0,
                "cost_basis_base": 1000,
                "current_value_base": 900,
                "gain_loss_base": -100,
                "gain_loss_pct": -10.0,
                "price_fetch_failed": False,
                "fx_unavailable": False,
            },
        ],
        metadata={"run_timestamp_utc": "2026-01-01T00:00:00+00:00", "fx_source": "frankfurter"},
        analysis_enabled=True,
        fundamentals={},
        technicals={},
    )
    assert "tab-holdings" in html
    assert "tab-summary" in html
    assert "tab-metadata" in html
    assert "tab-analysis" in html
    assert "tab-signals" in html
    assert "tt-holding-row" in html
    assert "tt-ticker-data" in html
    assert "data-ticker" in html
    assert "Top 5 gainers" in html


def test_holdings_lots_toggle_when_multiple_lots() -> None:
    lot = {
        "ticker": "AAPL",
        "shares": 10,
        "report_ccy": "USD",
        "cost_per_share_purchase": 100,
        "cost_basis_purchase": 1000,
        "price_per_share_purchase": 110,
        "current_value_purchase": 1100,
        "gain_loss_purchase": 100,
        "gain_loss_pct_purchase": 10.0,
    }
    consolidated = [
        {
            **lot,
            "shares": 15,
            "cost_basis_purchase": 1600,
            "current_value_purchase": 1950,
        }
    ]
    lots = [lot, {**lot, "shares": 5, "cost_basis_purchase": 600, "current_value_purchase": 850}]
    html = build_portfolio_tabbed_html(
        base="USD",
        summary={},
        holdings=consolidated,
        holdings_lots=lots,
        metadata={},
    )
    assert "holdings-toolbar" in html
    assert "holdings-panel-lots" in html
    assert 'data-view="lots"' in html


def test_tabbed_report_state_publish() -> None:
    published: list[str] = []
    state = TabbedReportState(publish=published.append)
    state.set_core(
        base="USD",
        summary={},
        holdings=[{"ticker": "MSFT", "shares": 1}],
        metadata={},
        analysis_enabled=False,
    )
    assert len(published) == 1
    assert "tab-holdings" in published[0]
    assert "tab-analysis" not in published[0]
