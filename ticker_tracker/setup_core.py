"""Shared setup validation, persistence, and apply logic (CLI + web)."""

from __future__ import annotations

import os
import re
import tempfile
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from ticker_tracker.config import (
    LLM_PROVIDERS,
    AppConfig,
    EncryptedConfig,
    get_finance_api_key,
    get_fx_api_key,
    get_gemini_api_key,
    set_finance_api_key,
    set_fmp_api_key,
    set_fx_api_key,
    set_gemini_api_key,
)
from ticker_tracker.currency import is_valid_iso4217, normalize_iso4217
from ticker_tracker.finance.twelvedata_adapter import (
    clear_twelvedata_api_key,
    set_twelvedata_api_key,
)
from ticker_tracker.fx.open_exchange_rates import clear_oxr_api_key, set_oxr_api_key

KNOWN_FINANCE_SOURCES = (
    "yahoo",
    "alpha_vantage",
    "twelve_data",
    "finnhub",
    "polygon",
)

HOLDINGS_SOURCES = ("google_sheets", "local_file")
OUTPUT_FORMATS = ("xlsx", "html")

FX_SOURCES = (
    "frankfurter",
    "open_exchange_rates",
    "fixer",
    "currencylayer",
)

FREE_FX_SOURCES = frozenset({"frankfurter"})

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

SUGGESTED_ANALYSIS_MODELS = (
    ("qwen2.5:7b", "recommended balance of quality and speed"),
    ("gemma3:4b", "faster, lighter"),
    ("llama3.2:3b", "fastest"),
)

RECOMMENDED_COLUMNS = (
    ("ticker", "Ticker symbol (local code if you use exchange)"),
    ("exchange", "Listing exchange (e.g. NYSE, SGX, LSE, or Yahoo suffix like .SI)"),
    ("shares", "Share quantity"),
    ("cost_basis", "Cost per share (in purchase currency if set, else base currency)"),
    ("purchase_currency", "Optional ISO 4217 currency you paid in (e.g. SGD, USD)"),
    ("currency_override", "Optional: force quote / native currency for that row"),
)

DEFAULT_COLUMN_LETTERS = {
    "ticker": "A",
    "exchange": "B",
    "shares": "C",
    "cost_basis": "D",
    "purchase_currency": "E",
    "currency_override": "F",
}

# Columns that may be omitted in setup forms (blank = not mapped).
OPTIONAL_COLUMN_FIELDS = frozenset({"exchange", "purchase_currency", "currency_override"})


def _validate_google_sheet_id(sheet_id: str) -> list[str]:
    errors: list[str] = []
    if len(sheet_id) < 20:
        errors.append("Google Sheets ID looks too short.")
    if " " in sheet_id or "/" in sheet_id:
        errors.append("Google Sheets ID should not contain spaces or slashes.")
    if not re.fullmatch(r"[A-Za-z0-9_-]+", sheet_id):
        errors.append("Google Sheets ID should be alphanumeric (with _ or -).")
    return errors


def _validate_holdings_source(source: str) -> list[str]:
    if source not in HOLDINGS_SOURCES:
        choices = ", ".join(HOLDINGS_SOURCES)
        return [f"Unknown holdings source {source!r}. Choose one of: {choices}."]
    return []


def _validate_local_holdings_path(path: str) -> list[str]:
    if not path.strip():
        return ["Local holdings path is required when source is local_file."]
    return []


def _validate_output_formats(formats: list[str]) -> list[str]:
    if not formats:
        return ["Choose at least one output format (xlsx and/or html)."]
    bad = sorted({f for f in formats if f not in OUTPUT_FORMATS})
    if bad:
        return [f"Unsupported output format(s): {', '.join(bad)}."]
    return []


def resolve_local_report_dir(raw: str | None) -> Path:
    """Directory for XLSX/HTML reports. Blank uses the OS temp directory."""
    s = (raw or "").strip()
    if not s:
        return Path(tempfile.gettempdir())
    p = Path(s).expanduser()
    if not p.is_absolute():
        p = (Path.cwd() / p).resolve()
    else:
        p = p.resolve()
    return p


def _validate_local_report_dir(raw: str) -> list[str]:
    if not (raw or "").strip():
        return []
    p = resolve_local_report_dir(raw)
    if p.exists():
        if not p.is_dir():
            return [f"local_report_dir must be a directory: {p}"]
        if not os.access(p, os.W_OK):
            return [f"local_report_dir is not writable: {p}"]
        return []
    parent = p.parent
    if not parent.exists() or not parent.is_dir():
        return [f"local_report_dir parent does not exist: {parent}"]
    if not os.access(parent, os.W_OK):
        return [f"Cannot create local_report_dir (parent not writable): {parent}"]
    return []


def _validate_emails(emails: list[str]) -> list[str]:
    bad = [e for e in emails if not EMAIL_RE.fullmatch(e)]
    if bad:
        return [f"Invalid email format: {', '.join(bad)}"]
    return []


def _validate_finance(sources: list[str], keys: dict[str, str]) -> list[str]:
    errors: list[str] = []
    for s in sources:
        if s == "yahoo":
            continue
        key = keys.get(s, "")
        if len(key) < 8:
            errors.append(f"API key for '{s}' is missing or too short (min 8 characters).")
    return errors


def _validate_column_map(column_map: dict[str, str]) -> list[str]:
    if not column_map:
        return ["Column map is empty; add at least one mapping."]
    return []


def _validate_base_currency(code: str) -> list[str]:
    if not is_valid_iso4217(code):
        return [f"Unknown or invalid ISO 4217 currency code: {code!r}."]
    return []


def _validate_fx_source(fx_source: str) -> list[str]:
    if fx_source not in FX_SOURCES:
        return [f"Unknown FX source {fx_source!r}. Choose one of: {', '.join(FX_SOURCES)}."]
    return []


def validate_ollama_url(url: str) -> list[str]:
    """Return validation issues for an Ollama base URL (empty list if OK)."""
    issues: list[str] = []
    raw = (url or "").strip()
    if not raw:
        issues.append("Ollama URL is required when analysis is enabled.")
        return issues
    parsed = urlparse(raw)
    if parsed.scheme not in ("http", "https"):
        issues.append("Ollama URL must start with http:// or https://")
    if not parsed.netloc:
        issues.append("Ollama URL must include a host (e.g. http://localhost:11434).")
    return issues


def _validate_analysis_ollama(ollama_url: str, analysis_model: str) -> list[str]:
    issues: list[str] = []
    issues.extend(validate_ollama_url(ollama_url))
    if not (analysis_model or "").strip():
        issues.append("Ollama model name is required when analysis is enabled.")
    return issues


def _validate_analysis_gemini(gemini_model: str, *, gemini_api_key: str | None) -> list[str]:
    issues: list[str] = []
    if not (gemini_model or "").strip():
        issues.append("Gemini model name is required when using Gemini for analysis.")
    key = gemini_api_key or get_gemini_api_key()
    if not key or len(key) < 8:
        issues.append(
            "Gemini API key is required (Settings keychain or GEMINI_API_KEY / GOOGLE_API_KEY env)."
        )
    return issues


def _validate_fx_api_key(fx_source: str, fx_api_key: str | None) -> list[str]:
    if fx_source in FREE_FX_SOURCES:
        return []
    if not fx_api_key or len(fx_api_key) < 8:
        return [f"FX API key is required for source {fx_source!r} (min 8 characters)."]
    return []


def _validate_market_overrides(overrides: dict[str, str]) -> list[str]:
    errors: list[str] = []
    for suffix, cur in overrides.items():
        if not suffix.startswith("."):
            errors.append(f"Suffix {suffix!r} should start with '.'")
        if not is_valid_iso4217(cur):
            errors.append(f"Override {suffix!r} → {cur!r} has invalid currency code.")
    return errors


def verify_setup(
    config: AppConfig,
    *,
    finance_api_keys: dict[str, str],
    fx_api_key: str | None,
    require_local_holdings_path: bool = True,
) -> list[str]:
    """Return human-readable issues; empty means OK."""
    issues: list[str] = []
    issues.extend(_validate_holdings_source(config.holdings_source))
    if config.holdings_source == "google_sheets":
        issues.extend(_validate_google_sheet_id(config.google_sheets_id))
    elif require_local_holdings_path:
        issues.extend(_validate_local_holdings_path(config.local_holdings_path))
    issues.extend(_validate_column_map(config.column_map))
    issues.extend(_validate_emails(config.email_ids))
    issues.extend(_validate_finance(config.finance_sources, finance_api_keys))
    issues.extend(_validate_base_currency(config.base_currency))
    issues.extend(_validate_fx_source(config.fx_source))
    issues.extend(_validate_fx_api_key(config.fx_source, fx_api_key))
    issues.extend(_validate_market_overrides(config.market_currency_overrides))
    issues.extend(_validate_output_formats(config.output_formats))
    if config.holdings_source == "local_file" and config.upload_to_drive:
        issues.append("Google Drive upload is unavailable when holdings_source is local_file.")
    issues.extend(_validate_local_report_dir(config.local_report_dir))
    if config.analysis_enabled:
        provider = (config.llm_provider or "ollama").strip().lower()
        if provider not in LLM_PROVIDERS:
            issues.append(f"Unknown LLM provider {provider!r}. Choose: ollama, gemini.")
        elif provider == "gemini":
            issues.extend(_validate_analysis_gemini(config.gemini_model, gemini_api_key=None))
        else:
            issues.extend(_validate_analysis_ollama(config.ollama_url, config.analysis_model))
    issues.extend(_remote_verify_finance_keys(config))
    return issues


def _remote_verify_finance_keys(config: AppConfig) -> list[str]:
    """Placeholder for live API checks; extend when finance adapters exist."""
    return []


def persist_api_keys(
    finance_sources: list[str],
    finance_api_keys: dict[str, str],
    fx_source: str,
    fx_api_key: str | None,
) -> None:
    for src in KNOWN_FINANCE_SOURCES:
        if src not in finance_sources and src != "yahoo":
            set_finance_api_key(src, None)
            if src == "twelve_data":
                clear_twelvedata_api_key()
    for src in finance_sources:
        if src == "yahoo":
            continue
        if src in finance_api_keys:
            key = finance_api_keys.get(src)
            set_finance_api_key(src, key)
        else:
            key = get_finance_api_key(src)
        if src == "twelve_data" and key:
            set_twelvedata_api_key(key)

    if fx_source in FREE_FX_SOURCES:
        set_fx_api_key(None)
    else:
        if fx_api_key is not None:
            set_fx_api_key(fx_api_key)

    if fx_source == "open_exchange_rates":
        if fx_api_key is None:
            existing = get_fx_api_key()
            if existing:
                set_oxr_api_key(existing)
            else:
                clear_oxr_api_key()
        elif fx_api_key:
            set_oxr_api_key(fx_api_key)
        else:
            clear_oxr_api_key()
    else:
        clear_oxr_api_key()


def apply_setup(
    *,
    holdings_source: str,
    google_sheets_id: str,
    holdings_sheet_name: str,
    local_holdings_path: str,
    local_holdings_sheet_name: str,
    column_map: dict[str, str],
    email_ids: list[str],
    finance_sources: list[str],
    finance_api_keys: dict[str, str],
    base_currency: str,
    fx_source: str,
    fx_api_key: str | None,
    market_currency_overrides: dict[str, str],
    run_on_startup: bool,
    upload_to_drive: bool,
    output_formats: list[str],
    local_report_dir: str,
    encrypted_config: EncryptedConfig,
    require_local_holdings_path: bool = True,
    analysis_enabled: bool = True,
    ollama_url: str = "http://localhost:11434",
    analysis_model: str = "qwen2.5:7b",
    llm_provider: str = "ollama",
    gemini_model: str = "gemini-2.0-flash",
    llm_include_holding_context: bool = False,
    fmp_api_key: str | None = None,
    gemini_api_key: str | None = None,
) -> tuple[AppConfig | None, list[str]]:
    """
    Validate, then write ``config.enc`` and keychain entries.

    When *require_local_holdings_path* is False, ``holdings_source=local_file`` may be saved
    with an empty path (e.g. dashboard Settings where the path is chosen on the run panel).

    Returns ``(config, [])`` on success, or ``(None, issues)`` on validation failure.
    """
    cfg = AppConfig(
        holdings_source=holdings_source.strip().lower(),
        google_sheets_id=google_sheets_id.strip(),
        holdings_sheet_name=(holdings_sheet_name.strip() or "Holdings"),
        local_holdings_path=local_holdings_path.strip(),
        local_holdings_sheet_name=(local_holdings_sheet_name.strip() or "Holdings"),
        column_map=column_map,
        email_ids=email_ids,
        finance_sources=finance_sources,
        fx_source=fx_source.strip().lower(),
        base_currency=normalize_iso4217(base_currency),
        market_currency_overrides=market_currency_overrides,
        run_on_startup=run_on_startup,
        upload_to_drive=upload_to_drive,
        output_formats=list(dict.fromkeys(output_formats)),
        local_report_dir=local_report_dir.strip(),
        analysis_enabled=analysis_enabled,
        ollama_url=(ollama_url or "http://localhost:11434").strip().rstrip("/"),
        analysis_model=(analysis_model or "qwen2.5:7b").strip(),
        llm_provider=(llm_provider or "ollama").strip().lower(),
        gemini_model=(gemini_model or "gemini-2.0-flash").strip(),
        llm_include_holding_context=bool(llm_include_holding_context),
    )
    effective_finance_keys = dict(finance_api_keys)
    for src in finance_sources:
        if src == "yahoo":
            continue
        if src not in effective_finance_keys:
            existing = get_finance_api_key(src)
            if existing:
                effective_finance_keys[src] = existing
    effective_fx_key = fx_api_key
    if fx_api_key is None and cfg.fx_source not in FREE_FX_SOURCES:
        effective_fx_key = get_fx_api_key()

    issues = verify_setup(
        cfg,
        finance_api_keys=effective_finance_keys,
        fx_api_key=effective_fx_key,
        require_local_holdings_path=require_local_holdings_path,
    )
    if issues:
        return None, issues

    resolve_local_report_dir(cfg.local_report_dir).mkdir(parents=True, exist_ok=True)

    encrypted_config.save(cfg)
    persist_api_keys(finance_sources, finance_api_keys, cfg.fx_source, fx_api_key)
    if fmp_api_key is not None:
        set_fmp_api_key(fmp_api_key or None)
    if gemini_api_key is not None:
        set_gemini_api_key(gemini_api_key or None)
    return cfg, []


def parse_emails_blob(text: str) -> list[str]:
    return [line.strip() for line in text.splitlines() if line.strip()]


def parse_market_overrides_blob(text: str) -> dict[str, str]:
    """Parse lines like '.KL=MYR' or '.KL = MYR'."""
    out: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        left, right = line.split("=", 1)
        suf = left.strip()
        cur = normalize_iso4217(right.strip())
        if suf:
            out[suf] = cur
    return out


def build_column_map_from_recommended_form(form: dict[str, Any]) -> dict[str, str]:
    """Build column_map from web-style keys col_ticker, col_shares, …"""
    mapping: dict[str, str] = {}
    for field, _desc in RECOMMENDED_COLUMNS:
        key = f"col_{field}"
        raw = (form.get(key) or "").strip().upper()
        if field in OPTIONAL_COLUMN_FIELDS and not raw:
            continue
        if raw:
            if not raw.isalpha():
                continue
            mapping[field] = raw
    return mapping
