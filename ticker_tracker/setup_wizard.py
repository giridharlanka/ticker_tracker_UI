"""Interactive setup: terminal (CLI) or local web UI."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable
from pathlib import Path

from ticker_tracker.analysis.llm_analyst import is_ollama_available
from ticker_tracker.config import (
    AppConfig,
    EncryptedConfig,
    default_config_path,
    get_finance_api_key,
    get_fmp_api_key,
    get_fx_api_key,
)
from ticker_tracker.currency import normalize_iso4217
from ticker_tracker.setup_core import (
    DEFAULT_COLUMN_LETTERS,
    FREE_FX_SOURCES,
    FX_SOURCES,
    HOLDINGS_SOURCES,
    KNOWN_FINANCE_SOURCES,
    OPTIONAL_COLUMN_FIELDS,
    OUTPUT_FORMATS,
    RECOMMENDED_COLUMNS,
    SUGGESTED_ANALYSIS_MODELS,
    apply_setup,
    validate_ollama_url,
)
from ticker_tracker.setup_help import print_section


def _prompt(label: str, default: str | None = None) -> str:
    hint = f" [{default}]" if default is not None else ""
    raw = input(f"{label}{hint}: ").strip()
    if not raw and default is not None:
        return default
    return raw


def _prompt_yes_no(label: str, default: bool = False) -> bool:
    default_hint = "Y/n" if default else "y/N"
    raw = input(f"{label} ({default_hint}): ").strip().lower()
    if not raw:
        return default
    return raw in ("y", "yes", "1", "true", "t")


def _collect_column_map() -> dict[str, str]:
    print_section("column_mapping")
    if _prompt_yes_no(
        "Use recommended fields (ticker, exchange, shares, cost_basis, "
        "purchase_currency, optional currency_override)?",
        True,
    ):
        mapping: dict[str, str] = {}
        for field, desc in RECOMMENDED_COLUMNS:
            hint_default = (
                None
                if field in OPTIONAL_COLUMN_FIELDS
                else (DEFAULT_COLUMN_LETTERS.get(field) or None)
            )
            col = _prompt(f"  Column for '{field}' ({desc})", hint_default).strip().upper()
            if field in OPTIONAL_COLUMN_FIELDS and not col:
                continue
            if col:
                mapping[field] = col
        return mapping

    print("Enter an empty field name when done.")
    manual_map: dict[str, str] = {}
    while True:
        field_name = input("  Field name (e.g. ticker, shares): ").strip()
        if not field_name:
            break
        col = input(f"  Column letter for '{field_name}': ").strip().upper()
        if not col or not col.isalpha():
            print("  Invalid column; use letters only (e.g. A or AB).")
            continue
        manual_map[field_name] = col
    return manual_map


def _collect_emails() -> list[str]:
    print_section("emails")
    emails: list[str] = []
    while True:
        line = input("  Email (empty line when done): ").strip()
        if not line:
            break
        emails.append(line)
    return emails


def _collect_finance() -> tuple[list[str], dict[str, str]]:
    print_section("finance_sources")
    print(f"Known sources: {', '.join(KNOWN_FINANCE_SOURCES)}")
    raw = input("Enter sources, comma-separated: ").strip().lower()
    names = [p.strip() for p in raw.split(",") if p.strip()]
    unknown = [n for n in names if n not in KNOWN_FINANCE_SOURCES]
    if unknown:
        print(f"  Unknown source(s) ignored: {', '.join(unknown)}")
        names = [n for n in names if n in KNOWN_FINANCE_SOURCES]

    keys: dict[str, str] = {}
    for name in names:
        if name == "yahoo":
            continue
        existing = bool(get_finance_api_key(name))
        print(f"  API key status for '{name}': {'exists (hidden)' if existing else 'not set'}")
        action = (
            _prompt(
                "  Key action: [k]eep existing, [r]eplace with new key, [c]lear existing",
                "k",
            )
            .strip()
            .lower()
        )
        if action == "c":
            keys[name] = ""
        elif action == "r":
            keys[name] = input(f"  New API key for '{name}': ").strip()
    return names, keys


def _collect_base_currency() -> str:
    print_section("base_currency")
    return _prompt("Base currency (ISO 4217)", "SGD")


def _collect_fx_source() -> tuple[str, str | None]:
    print_section("fx_source")
    print("Options: frankfurter (free), open_exchange_rates, fixer, currencylayer")
    choice = _prompt("FX source", "frankfurter").strip().lower()
    if choice not in FX_SOURCES:
        return choice, None
    if choice in FREE_FX_SOURCES:
        return choice, None
    existing = bool(get_fx_api_key())
    print(f"  FX key status: {'exists (hidden)' if existing else 'not set'}")
    action = (
        _prompt(
            "  Key action: [k]eep existing, [r]eplace with new key, [c]lear existing",
            "k",
        )
        .strip()
        .lower()
    )
    if action == "c":
        return choice, ""
    if action == "r":
        key = input("  New FX API key: ").strip()
        return choice, (key or None)
    return choice, None


def _collect_holdings_source() -> str:
    print_section("holdings_source")
    print(f"Options: {', '.join(HOLDINGS_SOURCES)}")
    return _prompt("Holdings source", "google_sheets").strip().lower()


def _collect_local_holdings() -> tuple[str, str]:
    print_section("local_holdings_path")
    path = _prompt("Local holdings file path (.csv or .xlsx)")
    print_section("local_holdings_sheet_name")
    sheet_name = _prompt("Sheet tab name for .xlsx files", "Holdings")
    return path, sheet_name


def _collect_output_formats() -> list[str]:
    print_section("output_formats")
    print(f"Options: {', '.join(OUTPUT_FORMATS)}")
    raw = _prompt("Output formats (comma-separated)", "xlsx")
    chosen = [p.strip().lower() for p in raw.split(",") if p.strip()]
    return chosen or ["xlsx"]


def _collect_local_report_dir() -> str:
    print_section("local_report_dir")
    return _prompt(
        "Local folder for XLSX/HTML reports (empty = system temp folder)",
        "",
    ).strip()


def _collect_fmp_api_key_optional() -> str | None:
    """Return FMP key action: None = leave keychain unchanged, '' = clear, else new key."""
    if not _prompt_yes_no(
        "  Do you have a Financial Modeling Prep API key? "
        "(optional — improves data for SGX/HKEx tickers)",
        default=False,
    ):
        return None
    existing = bool(get_fmp_api_key())
    print(f"    FMP key status: {'exists (hidden)' if existing else 'not set'}")
    action = (
        _prompt(
            "    Key action: [k]eep existing, [r]eplace with new key, [c]lear existing",
            "k",
        )
        .strip()
        .lower()
    )
    if action == "c":
        return ""
    if action == "r":
        return input("    New FMP API key: ").strip() or None
    return None


def _collect_analysis_config() -> tuple[bool, str, str, str | None]:
    """
    Analysis wizard step. Returns
    (analysis_enabled, ollama_url, analysis_model, fmp_api_key).
    """
    print_section("analysis_configuration")
    enabled = _prompt_yes_no(
        "Enable fundamental and technical analysis? (recommended: yes)",
        default=True,
    )
    if not enabled:
        return False, "http://localhost:11434", "qwen2.5:7b", None

    ollama_url = _prompt("Ollama URL", "http://localhost:11434").strip().rstrip("/")
    url_issues = validate_ollama_url(ollama_url)
    if url_issues:
        print("  URL validation:")
        for msg in url_issues:
            print(f"    - {msg}")
    elif is_ollama_available(ollama_url):
        print("  Ollama connectivity: reachable (GET /api/tags returned 200).")
    else:
        print(
            "  Warning: could not reach Ollama at that URL. "
            "Setup will continue — start Ollama before your first analysed run."
        )

    print("  Suggested models:")
    for name, hint in SUGGESTED_ANALYSIS_MODELS:
        print(f"    - {name} ({hint})")
    analysis_model = _prompt("Ollama model to use for analysis", "qwen2.5:7b").strip()

    fmp_key = _collect_fmp_api_key_optional()
    return enabled, ollama_url, analysis_model, fmp_key


def _collect_market_overrides() -> dict[str, str]:
    print_section("market_overrides")
    if not _prompt_yes_no("Add any custom suffix → currency mappings?", False):
        return {}
    out: dict[str, str] = {}
    while True:
        suf = input("  Ticker suffix (e.g. .KL, empty to finish): ").strip()
        if not suf:
            break
        if not suf.startswith("."):
            print("    Suffix should start with '.' (e.g. .KL).")
            continue
        cur = normalize_iso4217(input(f"  Currency for suffix '{suf}' (ISO code): ").strip())
        out[suf] = cur
    return out


def run_wizard(save_path: Callable[[], EncryptedConfig] | None = None) -> AppConfig:
    print("Ticker Tracker — setup wizard (terminal)\n")

    holdings_source = _collect_holdings_source()
    sheet_id = ""
    sheet_name = "Holdings"
    local_path = ""
    local_sheet_name = "Holdings"
    if holdings_source == "google_sheets":
        print_section("google_sheets_id")
        sheet_id = _prompt("Google Sheets ID")
        print_section("holdings_sheet_name")
        sheet_name = _prompt("Holdings sheet (tab) name", "Holdings")
    else:
        local_path, local_sheet_name = _collect_local_holdings()

    column_map = _collect_column_map()
    emails = _collect_emails()
    sources, finance_keys = _collect_finance()

    base_raw = _collect_base_currency()
    base_currency = normalize_iso4217(base_raw)

    fx_source, fx_key = _collect_fx_source()

    analysis_enabled, ollama_url, analysis_model, fmp_key = _collect_analysis_config()

    overrides = _collect_market_overrides()

    print_section("upload_to_drive")
    upload_drive = _prompt_yes_no(
        "Upload Excel report to Google Drive (still emailed if enabled below)?",
        default=False,
    )

    print_section("run_on_startup")
    run_startup = _prompt_yes_no("Run on OS startup", default=False)
    output_formats = _collect_output_formats()
    local_report_dir = _collect_local_report_dir()

    enc = save_path() if save_path else EncryptedConfig()
    cfg, issues = apply_setup(
        holdings_source=holdings_source,
        google_sheets_id=sheet_id.strip(),
        holdings_sheet_name=sheet_name.strip() or "Holdings",
        local_holdings_path=local_path.strip(),
        local_holdings_sheet_name=local_sheet_name.strip() or "Holdings",
        column_map=column_map,
        email_ids=emails,
        finance_sources=sources,
        finance_api_keys=finance_keys,
        base_currency=base_currency,
        fx_source=fx_source,
        fx_api_key=fx_key,
        market_currency_overrides=overrides,
        run_on_startup=run_startup,
        upload_to_drive=upload_drive,
        output_formats=output_formats,
        local_report_dir=local_report_dir,
        encrypted_config=enc,
        analysis_enabled=analysis_enabled,
        ollama_url=ollama_url,
        analysis_model=analysis_model,
        fmp_api_key=fmp_key,
    )
    if issues:
        print("\nVerification found problems (nothing was saved):")
        for msg in issues:
            print(f"  - {msg}")
        print("\nFix the items above and run the setup wizard again.")
        sys.exit(1)

    assert cfg is not None
    print(f"\nSaved encrypted config to {enc.path.resolve()}")
    print("Finance and FX API keys were stored in the OS keychain where applicable.")
    print("\nAll checks passed for the information you provided.")

    try:
        from ticker_tracker.ui.startup_registration import deregister_startup, register_startup

        if cfg.run_on_startup:
            register_startup()
            print("Registered this app to run at OS login (headless: main.py --run).")
        else:
            deregister_startup()
            print("Removed OS login startup entry for this app (if one was present).")
    except Exception as exc:  # noqa: BLE001
        print(f"\nWarning: could not update OS startup registration: {exc}")

    return cfg


def _run_web(host: str, port: int, config_path: Path | None) -> None:
    try:
        from ticker_tracker.web.setup_server import run_setup_server
    except ImportError as e:
        print(
            "The web interface needs Flask. Install with:\n  pip install 'ticker-tracker[web]'\n",
            file=sys.stderr,
        )
        raise SystemExit(1) from e
    run_setup_server(host=host, port=port, config_path=config_path)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Configure Ticker Tracker (encrypted config + OS keychain)."
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--web",
        action="store_true",
        help="Open a browser-based setup form (requires: pip install 'ticker-tracker[web]').",
    )
    mode.add_argument(
        "--cli",
        action="store_true",
        help="Use the terminal questionnaire (default if no flag is passed).",
    )
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="With --web: bind address (default 127.0.0.1 for local-only).",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8765,
        help="With --web: TCP port (default 8765).",
    )
    args = parser.parse_args()

    path = default_config_path()
    print(f"Config will be stored at: {path.resolve()}\n")

    use_web = args.web
    if not args.web and not args.cli:
        print("How would you like to run setup?")
        print("  [1] Terminal — step-by-step questions in this window")
        print("  [2] Web browser — a form at http://127.0.0.1 (easier if you avoid the CLI)")
        choice = input("Enter 1 or 2 [1]: ").strip() or "1"
        use_web = choice == "2"

    if use_web:
        _run_web(args.host, args.port, None)
    else:
        run_wizard(lambda: EncryptedConfig(path))


if __name__ == "__main__":
    main()
