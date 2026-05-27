"""Holdings display aggregation tests."""

from __future__ import annotations

from ticker_tracker.engine import aggregate_holdings_for_display


def test_aggregate_holdings_merges_lots_weighted_cost() -> None:
    rows = [
        {
            "ticker": "AAPL",
            "shares": 10.0,
            "report_ccy": "USD",
            "cost_basis_purchase": 1000.0,
            "current_value_purchase": 1200.0,
            "cost_basis_base": 1000.0,
            "current_value_base": 1200.0,
            "price_fetch_failed": False,
            "fx_unavailable": False,
        },
        {
            "ticker": "AAPL",
            "shares": 5.0,
            "report_ccy": "USD",
            "cost_basis_purchase": 600.0,
            "current_value_purchase": 750.0,
            "cost_basis_base": 600.0,
            "current_value_base": 750.0,
            "price_fetch_failed": False,
            "fx_unavailable": False,
        },
    ]
    out = aggregate_holdings_for_display(rows)
    assert len(out) == 1
    merged = out[0]
    assert merged["ticker"] == "AAPL"
    assert merged["shares"] == 15.0
    assert merged["cost_basis_purchase"] == 1600.0
    assert merged["current_value_purchase"] == 1950.0
    assert merged["cost_per_share_purchase"] == 1600.0 / 15.0
