"""Tests for Gemini Flash LLM provider."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

from ticker_tracker.analysis.llm_analyst import LLMAnalyst, is_llm_available
from ticker_tracker.analysis.llm_gemini import (
    GeminiProvider,
    format_gemini_http_error,
    reset_gemini_rate_limit_for_tests,
)
from ticker_tracker.config import AppConfig


def test_gemini_provider_parses_json_response() -> None:
    reset_gemini_rate_limit_for_tests()
    payload = {
        "candidates": [
            {
                "content": {
                    "parts": [
                        {
                            "text": json.dumps(
                                {
                                    "signal": "HOLD",
                                    "confidence": "Medium",
                                    "strengths": ["a", "b", "c"],
                                    "risks": ["x", "y", "z"],
                                    "summary": "Neutral stance.",
                                }
                            )
                        }
                    ]
                }
            }
        ]
    }
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.is_error = False
    mock_resp.json.return_value = payload
    mock_resp.raise_for_status = MagicMock()

    mock_client = MagicMock()
    mock_client.__enter__.return_value = mock_client
    mock_client.post.return_value = mock_resp

    with patch(
        "ticker_tracker.analysis.llm_gemini.get_gemini_api_key",
        return_value="test-key-12345678",
    ):
        with patch("ticker_tracker.analysis.llm_gemini.httpx.Client", return_value=mock_client):
            provider = GeminiProvider(model="gemini-2.0-flash")
            parsed = provider.generate_json('{"prompt": "x"}')

    assert parsed is not None
    assert parsed["signal"] == "HOLD"


def test_format_gemini_http_error_403_omits_key() -> None:
    resp = MagicMock()
    resp.status_code = 403
    resp.reason_phrase = "Forbidden"
    resp.json.return_value = {
        "error": {"code": 403, "message": "API key not valid.", "status": "PERMISSION_DENIED"}
    }
    msg = format_gemini_http_error(resp)
    assert "403" in msg
    assert "aistudio.google.com" in msg
    assert "AIza" not in msg


def test_llm_analyst_gemini_config_uses_provider() -> None:
    cfg = AppConfig(
        llm_provider="gemini",
        gemini_model="gemini-2.0-flash",
        llm_include_holding_context=False,
    )
    with patch(
        "ticker_tracker.analysis.llm_analyst.is_gemini_configured",
        return_value=True,
    ):
        assert is_llm_available(cfg) is True
    analyst = LLMAnalyst(cfg)
    assert analyst.model.startswith("gemini:")
