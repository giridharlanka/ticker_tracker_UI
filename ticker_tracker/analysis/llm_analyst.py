"""LLM analyst — Ollama (local) or Gemini Flash (cloud)."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any, Protocol

import httpx

from ticker_tracker.analysis.base import (
    FundamentalsResult,
    LLMAnalysis,
    TechnicalResult,
)
from ticker_tracker.analysis.llm_common import (
    SIMPLIFIED_JSON_INSTRUCTION,
    coerce_confidence,
    coerce_signal,
    normalize_triple,
    parse_llm_json,
)
from ticker_tracker.analysis.llm_gemini import (
    GeminiProvider,
    is_gemini_configured,
    probe_gemini_api_key,
)
from ticker_tracker.analysis.prompt_builder import (
    build_analysis_prompt,
    build_portfolio_summary_prompt,
)
from ticker_tracker.config import LLM_PROVIDERS, AppConfig

logger = logging.getLogger(__name__)


class _LLMBackend(Protocol):
    def is_available(self) -> bool: ...
    def generate_json(self, prompt: str) -> dict[str, Any] | None: ...
    @property
    def model_label(self) -> str: ...


def is_ollama_available(ollama_url: str, *, timeout: float = 5.0) -> bool:
    """Return True if Ollama responds to GET /api/tags with HTTP 200."""
    base = ollama_url.rstrip("/")
    try:
        with httpx.Client(timeout=timeout) as client:
            resp = client.get(f"{base}/api/tags")
            return resp.status_code == 200
    except httpx.HTTPError as exc:
        logger.debug("Ollama availability check failed: %s", exc)
        return False


def llm_offline_hint(config: AppConfig) -> str:
    """User-facing hint when the configured LLM provider is unavailable."""
    if (config.llm_provider or "ollama").strip().lower() == "gemini":
        probe_err = probe_gemini_api_key()
        if probe_err:
            return probe_err
        return (
            "Gemini API unavailable — set a Gemini API key in Settings "
            "(or GEMINI_API_KEY / GOOGLE_API_KEY in .env). "
            "Key must be from https://aistudio.google.com/app/apikey"
        )
    url = (config.ollama_url or "http://localhost:11434").rstrip("/")
    return f"Ensure Ollama is running at {url}."


def is_llm_available(config: AppConfig) -> bool:
    """True if the configured analysis LLM provider is ready."""
    provider = (config.llm_provider or "ollama").strip().lower()
    if provider == "gemini":
        return is_gemini_configured()
    return is_ollama_available(config.ollama_url or "http://localhost:11434")


class _OllamaBackend:
    def __init__(self, *, ollama_url: str, model: str, timeout: float = 60.0) -> None:
        self.ollama_url = ollama_url.rstrip("/")
        self.model = model
        self.timeout = timeout

    @property
    def model_label(self) -> str:
        return self.model

    def is_available(self) -> bool:
        return is_ollama_available(self.ollama_url)

    def _generate(self, prompt: str, *, json_format: bool) -> str | None:
        url = f"{self.ollama_url}/api/generate"
        body: dict[str, Any] = {
            "model": self.model,
            "prompt": prompt,
            "stream": False,
        }
        if json_format:
            body["format"] = "json"
        with httpx.Client(timeout=self.timeout) as client:
            resp = client.post(url, json=body)
            resp.raise_for_status()
            payload = resp.json()
        if not isinstance(payload, dict):
            return None
        response = payload.get("response")
        return str(response) if response is not None else None

    def generate_json(self, prompt: str) -> dict[str, Any] | None:
        raw = self._generate(prompt, json_format=True)
        parsed = parse_llm_json(raw or "")
        if parsed is not None:
            return parsed
        simplified = prompt.split("Based on the data above")[0] + SIMPLIFIED_JSON_INSTRUCTION
        raw_retry = self._generate(simplified, json_format=True)
        return parse_llm_json(raw_retry or "")


def _make_backend(config: AppConfig) -> _LLMBackend:
    provider = (config.llm_provider or "ollama").strip().lower()
    if provider not in LLM_PROVIDERS:
        provider = "ollama"
    if provider == "gemini":
        return GeminiProvider(model=config.gemini_model or "gemini-2.0-flash")
    return _OllamaBackend(
        ollama_url=config.ollama_url or "http://localhost:11434",
        model=config.analysis_model or "qwen2.5:7b",
    )


class LLMAnalyst:
    """Per-ticker and portfolio LLM synthesis via Ollama or Gemini."""

    def __init__(self, config: AppConfig) -> None:
        self._config = config
        self._backend = _make_backend(config)
        self.include_holding_context = bool(config.llm_include_holding_context)
        self.model = self._backend.model_label
        self.ollama_url = (config.ollama_url or "http://localhost:11434").rstrip("/")

    def is_available(self) -> bool:
        return self._backend.is_available()

    def is_ollama_available(self) -> bool:
        """Backward-compatible alias when provider is Ollama."""
        return self._backend.is_available()

    def _fallback_analysis(
        self,
        ticker: str,
        *,
        prompt: str,
        llm_available: bool,
        parse_warning: str | None = None,
    ) -> LLMAnalysis:
        risks = ["Data parse issue", "Review manually", parse_warning or "LLM response invalid"]
        return LLMAnalysis(
            ticker=ticker,
            model=self.model,
            signal="INSUFFICIENT DATA",
            confidence="Low",
            strengths=["Unavailable", "Unavailable", "Unavailable"],
            risks=normalize_triple(risks, filler="Unavailable"),
            summary="Automated analysis could not be completed.",
            generated_at=datetime.now(UTC),
            prompt_tokens_approx=len(prompt) // 4,
            llm_available=llm_available,
        )

    def _analysis_from_parsed(
        self,
        ticker: str,
        prompt: str,
        parsed: dict[str, Any],
        *,
        llm_available: bool = True,
    ) -> LLMAnalysis:
        return LLMAnalysis(
            ticker=ticker,
            model=self.model,
            signal=coerce_signal(parsed.get("signal")),
            confidence=coerce_confidence(parsed.get("confidence")),
            strengths=normalize_triple(parsed.get("strengths"), filler="Not specified"),
            risks=normalize_triple(parsed.get("risks"), filler="Not specified"),
            summary=str(parsed.get("summary") or "").strip() or "No summary provided.",
            generated_at=datetime.now(UTC),
            prompt_tokens_approx=len(prompt) // 4,
            llm_available=llm_available,
        )

    def _call_llm(self, prompt: str) -> dict[str, Any] | None:
        return self._backend.generate_json(prompt)

    def analyse_ticker(
        self,
        ticker: str,
        fundamentals: FundamentalsResult,
        technicals: TechnicalResult,
        base_currency: str,
        holding: dict | None = None,
    ) -> LLMAnalysis:
        prompt = build_analysis_prompt(
            ticker,
            fundamentals,
            technicals,
            base_currency,
            holding,
            include_holding_context=self.include_holding_context,
        )
        try:
            parsed = self._call_llm(prompt)
        except httpx.ConnectError as exc:
            logger.warning("LLM unreachable for %s: %s", ticker, exc)
            return self._fallback_analysis(ticker, prompt=prompt, llm_available=False)
        except httpx.HTTPError as exc:
            logger.warning("LLM HTTP error for %s: %s", ticker, exc)
            return self._fallback_analysis(
                ticker,
                prompt=prompt,
                llm_available=True,
                parse_warning=f"LLM error: {exc}",
            )

        if parsed is None:
            backend_hint = getattr(self._backend, "last_error", None)
            return self._fallback_analysis(
                ticker,
                prompt=prompt,
                llm_available=True,
                parse_warning=backend_hint or "Could not parse LLM JSON after retry",
            )
        return self._analysis_from_parsed(ticker, prompt, parsed)

    def analyse_portfolio(self, analyses: list[LLMAnalysis]) -> dict[str, str]:
        """Return portfolio commentary and top action."""
        empty = {
            "commentary": "No ticker analyses available for portfolio summary.",
            "top_action": "Run per-ticker analysis first.",
        }
        if not analyses:
            return empty

        prompt = build_portfolio_summary_prompt(analyses)
        try:
            parsed = self._call_llm(prompt)
        except httpx.HTTPError as exc:
            logger.warning("Portfolio summary LLM call failed: %s", exc)
            return {
                "commentary": "LLM offline — portfolio commentary unavailable.",
                "top_action": llm_offline_hint(self._config),
            }

        if not parsed:
            return {
                "commentary": "Could not parse portfolio summary from LLM.",
                "top_action": "Review individual ticker signals manually.",
            }

        commentary = str(parsed.get("commentary") or "").strip()
        top_action = str(parsed.get("top_action") or "").strip()
        return {
            "commentary": commentary or empty["commentary"],
            "top_action": top_action or empty["top_action"],
        }
