"""Tests for prompt builder and Ollama LLM analyst."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

import httpx
import pytest
from ticker_tracker.analysis.base import (
    FundamentalsResult,
    LLMAnalysis,
    SignalLabel,
    TechnicalResult,
)
from ticker_tracker.analysis.llm_analyst import LLMAnalyst, is_ollama_available
from ticker_tracker.analysis.prompt_builder import (
    build_analysis_prompt,
    build_portfolio_summary_prompt,
)
from ticker_tracker.config import AppConfig


def _fundamentals(**kwargs: object) -> FundamentalsResult:
    defaults = {
        "ticker": "AAPL",
        "source": "yahoo",
        "fetched_at": datetime.now(UTC),
        "fundamentals_available": True,
    }
    defaults.update(kwargs)
    return FundamentalsResult(**defaults)  # type: ignore[arg-type]


def _technicals(**kwargs: object) -> TechnicalResult:
    defaults = {
        "ticker": "AAPL",
        "analysis_date": datetime.now(UTC),
        "period_days": 365,
        "signals": [SignalLabel("RSI Oversold", bullish=True)],
    }
    defaults.update(kwargs)
    return TechnicalResult(**defaults)  # type: ignore[arg-type]


def _holding() -> dict:
    return {
        "shares": 10,
        "cost_basis_base": 1000.0,
        "current_value_base": 1200.0,
    }


def _config() -> AppConfig:
    return AppConfig(
        ollama_url="http://localhost:11434",
        analysis_model="qwen2.5:7b",
    )


def test_build_analysis_prompt_sparse_quality_note() -> None:
    prompt = build_analysis_prompt(
        "D05.SI",
        _fundamentals(fundamentals_available=False, pe_ratio=12.0),
        _technicals(),
        "SGD",
        _holding(),
        include_holding_context=True,
    )
    assert "SPARSE" in prompt
    assert "Fundamentals data quality: SPARSE" in prompt


def test_build_analysis_prompt_excludes_holdings_by_default() -> None:
    prompt = build_analysis_prompt(
        "AAPL",
        _fundamentals(),
        _technicals(),
        "USD",
        {"shares": 99, "cost_basis_base": 1.0, "current_value_base": 2.0},
    )
    assert "Shares held" not in prompt
    assert "Cost basis" not in prompt
    assert "Not included" in prompt


def test_build_analysis_prompt_target_price_uses_native_currency() -> None:
    prompt = build_analysis_prompt(
        "D05.SI",
        _fundamentals(target_price=1.42),
        _technicals(),
        "SGD",
        {"native_ccy": "SGD"},
    )
    assert "Analyst target price: 1.42 SGD" in prompt


def test_build_analysis_prompt_omits_none_fundamentals() -> None:
    prompt = build_analysis_prompt(
        "XYZ",
        _fundamentals(pe_ratio=None, pb_ratio=None, roe=0.15),
        _technicals(),
        "USD",
        _holding(),
        include_holding_context=True,
    )
    assert "P/E ratio" not in prompt
    assert "P/B ratio" not in prompt
    assert "ROE: 15.0%" in prompt
    assert "None" not in prompt


def test_build_portfolio_summary_prompt_lists_analyses() -> None:
    analyses = [
        LLMAnalysis(
            ticker="AAPL",
            model="qwen2.5:7b",
            signal="BUY",
            confidence="High",
            strengths=["a", "b", "c"],
            risks=["x", "y", "z"],
            summary="Strong momentum.",
            generated_at=datetime.now(UTC),
            prompt_tokens_approx=100,
        )
    ]
    prompt = build_portfolio_summary_prompt(analyses)
    assert "AAPL" in prompt
    assert "commentary" in prompt


@patch("ticker_tracker.analysis.llm_analyst.httpx.Client")
def test_analyse_ticker_populates_fields(mock_client_cls: MagicMock) -> None:
    payload = {
        "signal": "BUY",
        "confidence": "High",
        "strengths": ["s1", "s2", "s3"],
        "risks": ["r1", "r2", "r3"],
        "summary": "Looks good. Hold for growth.",
    }
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {"response": json.dumps(payload)}
    mock_resp.raise_for_status = MagicMock()

    mock_client = MagicMock()
    mock_client.__enter__.return_value = mock_client
    mock_client.post.return_value = mock_resp
    mock_client_cls.return_value = mock_client

    analyst = LLMAnalyst(_config())
    result = analyst.analyse_ticker(
        "AAPL",
        _fundamentals(),
        _technicals(),
        "USD",
        _holding(),
    )

    assert result.signal == "BUY"
    assert result.confidence == "High"
    assert len(result.strengths) == 3
    assert len(result.risks) == 3
    assert result.llm_available is True
    assert result.prompt_tokens_approx > 0


@patch("ticker_tracker.analysis.llm_analyst.httpx.Client")
def test_malformed_json_retries_and_fallback(mock_client_cls: MagicMock) -> None:
    bad = MagicMock()
    bad.status_code = 200
    bad.json.return_value = {"response": "not json at all"}
    bad.raise_for_status = MagicMock()

    good = MagicMock()
    good.status_code = 200
    good.json.return_value = {"response": "still bad {{{"}
    good.raise_for_status = MagicMock()

    mock_client = MagicMock()
    mock_client.__enter__.return_value = mock_client
    mock_client.post.side_effect = [bad, good]
    mock_client_cls.return_value = mock_client

    analyst = LLMAnalyst(_config())
    result = analyst.analyse_ticker(
        "BAD",
        _fundamentals(),
        _technicals(),
        "USD",
        _holding(),
    )

    assert result.signal == "INSUFFICIENT DATA"
    assert result.llm_available is True
    assert mock_client.post.call_count == 2
    assert "parse" in result.risks[2].lower() or "invalid" in result.risks[2].lower()


@patch("ticker_tracker.analysis.llm_analyst.httpx.Client")
def test_connect_error_returns_llm_unavailable(mock_client_cls: MagicMock) -> None:
    mock_client = MagicMock()
    mock_client.__enter__.return_value = mock_client
    mock_client.post.side_effect = httpx.ConnectError("connection refused")
    mock_client_cls.return_value = mock_client

    analyst = LLMAnalyst(_config())
    result = analyst.analyse_ticker(
        "OFF",
        _fundamentals(),
        _technicals(),
        "USD",
        _holding(),
    )

    assert result.llm_available is False
    assert result.signal == "INSUFFICIENT DATA"


@patch("ticker_tracker.analysis.llm_analyst.httpx.Client")
def test_is_ollama_available_true_on_200(mock_client_cls: MagicMock) -> None:
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_client = MagicMock()
    mock_client.__enter__.return_value = mock_client
    mock_client.get.return_value = mock_resp
    mock_client_cls.return_value = mock_client

    assert is_ollama_available("http://localhost:11434") is True


@patch("ticker_tracker.analysis.llm_analyst.httpx.Client")
def test_analyse_portfolio_parses_json(mock_client_cls: MagicMock) -> None:
    payload = {
        "commentary": "Diversified. USD heavy. Mixed signals.",
        "top_action": "Trim weakest holding.",
    }
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {"response": json.dumps(payload)}
    mock_resp.raise_for_status = MagicMock()

    mock_client = MagicMock()
    mock_client.__enter__.return_value = mock_client
    mock_client.post.return_value = mock_resp
    mock_client_cls.return_value = mock_client

    analyses = [
        LLMAnalysis(
            ticker="AAPL",
            model="qwen2.5:7b",
            signal="BUY",
            confidence="High",
            strengths=["a", "b", "c"],
            risks=["x", "y", "z"],
            summary="Up.",
            generated_at=datetime.now(UTC),
            prompt_tokens_approx=50,
        )
    ]
    out = LLMAnalyst(_config()).analyse_portfolio(analyses)
    assert "Diversified" in out["commentary"]
    assert out["top_action"] == "Trim weakest holding."
