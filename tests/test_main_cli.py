"""CLI entry tests."""

from __future__ import annotations

from unittest.mock import patch

from ticker_tracker.main import main


@patch("ticker_tracker.engine.run_once")
def test_no_analysis_flag_passed_to_run_once(mock_run: object) -> None:
    main(["--run", "--no-analysis"])
    mock_run.assert_called_once()
    _, kwargs = mock_run.call_args
    assert kwargs.get("analysis_enabled") is False
