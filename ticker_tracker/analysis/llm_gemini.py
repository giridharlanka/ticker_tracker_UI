"""Google Gemini Flash analyst (Generative Language API)."""

from __future__ import annotations

import logging
import time
import urllib.parse

import httpx

from ticker_tracker.analysis.llm_common import SIMPLIFIED_JSON_INSTRUCTION, parse_llm_json
from ticker_tracker.config import get_gemini_api_key

logger = logging.getLogger(__name__)

_GEMINI_API_BASE = "https://generativelanguage.googleapis.com/v1beta"
_AI_STUDIO_KEY_URL = "https://aistudio.google.com/app/apikey"
# Free-tier friendly pacing (~10 RPM on Flash).
_MIN_INTERVAL_SEC = 6.0
_last_request_at: float = 0.0


def format_gemini_http_error(response: httpx.Response | None) -> str:
    """User-facing error text from a Gemini API response (never includes the API key)."""
    if response is None:
        return "Gemini API request failed."
    status = response.status_code
    message = ""
    try:
        payload = response.json()
        if isinstance(payload, dict):
            err = payload.get("error")
            if isinstance(err, dict):
                message = str(err.get("message") or err.get("status") or "").strip()
    except Exception:  # noqa: BLE001
        pass
    if status == 403:
        hint = (
            "API key rejected (403). Create a Gemini key at "
            f"{_AI_STUDIO_KEY_URL} — not a generic Google Cloud / Maps key. "
            "If this key was exposed in logs, revoke it and create a new one."
        )
        return f"{hint} {message}".strip() if message else hint
    if status == 404:
        return (
            f"Model not found (404). Check the Gemini model name in Settings. {message}"
        ).strip()
    if status == 429:
        return f"Rate limit exceeded (429). Wait and retry, or reduce holdings. {message}".strip()
    if message:
        return f"Gemini API HTTP {status}: {message}"
    return f"Gemini API HTTP {status} {response.reason_phrase}"


def probe_gemini_api_key(*, timeout: float = 10.0) -> str | None:
    """Return an error message if the configured key fails a lightweight API check, else None."""
    api_key = get_gemini_api_key()
    if not api_key:
        return "No Gemini API key (Settings, GEMINI_API_KEY, or GOOGLE_API_KEY in .env)."
    try:
        with httpx.Client(timeout=timeout) as client:
            resp = client.get(
                f"{_GEMINI_API_BASE}/models",
                params={"key": api_key, "pageSize": 1},
            )
        if resp.status_code == 200:
            return None
        return format_gemini_http_error(resp)
    except httpx.HTTPError as exc:
        return f"Could not reach Gemini API: {exc}"


def is_gemini_configured() -> bool:
    """True when an API key is available (keychain or GEMINI_API_KEY / GOOGLE_API_KEY env)."""
    key = get_gemini_api_key()
    return bool(key and len(key) >= 8)


def _rate_limit() -> None:
    global _last_request_at
    now = time.monotonic()
    wait = _MIN_INTERVAL_SEC - (now - _last_request_at)
    if wait > 0:
        time.sleep(wait)
    _last_request_at = time.monotonic()


def _extract_text(payload: dict) -> str | None:
    candidates = payload.get("candidates")
    if not isinstance(candidates, list) or not candidates:
        return None
    first = candidates[0]
    if not isinstance(first, dict):
        return None
    content = first.get("content")
    if not isinstance(content, dict):
        return None
    parts = content.get("parts")
    if not isinstance(parts, list):
        return None
    chunks: list[str] = []
    for part in parts:
        if isinstance(part, dict) and part.get("text"):
            chunks.append(str(part["text"]))
    return "\n".join(chunks).strip() if chunks else None


class GeminiProvider:
    """Call Gemini generateContent with JSON response MIME type."""

    def __init__(self, *, model: str, timeout: float = 90.0) -> None:
        self.model = (model or "gemini-2.0-flash").strip()
        self.timeout = timeout
        self.last_error: str | None = None

    @property
    def model_label(self) -> str:
        return f"gemini:{self.model}"

    def is_available(self) -> bool:
        return is_gemini_configured()

    def _generate_raw(self, prompt: str, *, json_mode: bool) -> str | None:
        api_key = get_gemini_api_key()
        if not api_key:
            self.last_error = "No Gemini API key configured."
            return None
        _rate_limit()
        model_path = urllib.parse.quote(self.model, safe="")
        url = f"{_GEMINI_API_BASE}/models/{model_path}:generateContent"
        body: dict = {
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        }
        if json_mode:
            body["generationConfig"] = {"responseMimeType": "application/json"}
        with httpx.Client(timeout=self.timeout) as client:
            resp = client.post(url, params={"key": api_key}, json=body)
            if resp.is_error:
                self.last_error = format_gemini_http_error(resp)
                logger.warning(
                    "Gemini API error for model %s: %s",
                    self.model,
                    self.last_error,
                )
                return None
            data = resp.json()
        if not isinstance(data, dict):
            self.last_error = "Gemini returned an unexpected response."
            return None
        text = _extract_text(data)
        if not text:
            self.last_error = "Gemini returned no text content."
        return text

    def generate_json(self, prompt: str) -> dict | None:
        """Return parsed JSON dict from Gemini, with one simplified retry."""
        self.last_error = None
        raw = self._generate_raw(prompt, json_mode=True)
        parsed = parse_llm_json(raw or "")
        if parsed is not None:
            return parsed
        simplified = prompt.split("Based on the data above")[0] + SIMPLIFIED_JSON_INSTRUCTION
        raw_retry = self._generate_raw(simplified, json_mode=True)
        parsed_retry = parse_llm_json(raw_retry or "")
        if parsed_retry is None and self.last_error is None:
            self.last_error = "Could not parse Gemini JSON response."
        return parsed_retry


def reset_gemini_rate_limit_for_tests() -> None:
    """Reset pacing clock (tests only)."""
    global _last_request_at
    _last_request_at = 0.0
