"""Local-only Flask UI for first-time setup (optional dependency)."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

from flask import Flask, render_template, request

from ticker_tracker.config import (
    EncryptedConfig,
    default_config_path,
    get_finance_api_key,
    get_fmp_api_key,
    get_fx_api_key,
    get_gemini_api_key,
)
from ticker_tracker.setup_core import (
    DEFAULT_COLUMN_LETTERS,
    HOLDINGS_SOURCES,
    KNOWN_FINANCE_SOURCES,
    OUTPUT_FORMATS,
    RECOMMENDED_COLUMNS,
    apply_setup,
    build_column_map_from_recommended_form,
    parse_emails_blob,
    parse_market_overrides_blob,
)
from ticker_tracker.setup_help import HELPS, TITLES


def _finance_labels() -> list[tuple[str, str]]:
    return [
        ("yahoo", "Yahoo Finance (no API key)"),
        ("alpha_vantage", "Alpha Vantage"),
        ("twelve_data", "Twelve Data"),
        ("finnhub", "Finnhub"),
        ("polygon", "Polygon.io"),
    ]


def _fx_choices() -> list[tuple[str, str]]:
    return [
        ("frankfurter", "Frankfurter (free, recommended)"),
        ("open_exchange_rates", "Open Exchange Rates (API key)"),
        ("fixer", "Fixer (API key)"),
        ("currencylayer", "Currencylayer (API key)"),
    ]


def _default_form() -> dict[str, Any]:
    cols = {f"col_{k}": v for k, v in DEFAULT_COLUMN_LETTERS.items()}
    return {
        "google_sheets_id": "",
        "holdings_sheet_name": "Holdings",
        "holdings_source": "google_sheets",
        "local_holdings_path": "",
        "local_holdings_sheet_name": "Holdings",
        "emails": "",
        "base_currency": "SGD",
        "fx_source": "frankfurter",
        "fx_api_key": "",
        "market_overrides": "",
        "run_on_startup": False,
        "upload_to_drive": False,
        "output_formats": ["xlsx"],
        "local_report_dir": "",
        "finance_selected": [],
        "finance_key_action": {s: "keep" for s in KNOWN_FINANCE_SOURCES if s != "yahoo"},
        "fx_key_action": "keep",
        "analysis_enabled": True,
        "llm_provider": "ollama",
        "ollama_url": "http://localhost:11434",
        "analysis_model": "qwen2.5:7b",
        "gemini_model": "gemini-2.0-flash",
        "llm_include_holding_context": False,
        "fmp_key_action": "keep",
        "key_fmp": "",
        "gemini_key_action": "keep",
        "key_gemini": "",
        **{f"key_{s}": "" for s in KNOWN_FINANCE_SOURCES if s != "yahoo"},
        **cols,
    }


def _form_from_request(form: Any) -> dict[str, Any]:
    out = _default_form()
    out["google_sheets_id"] = (form.get("google_sheets_id") or "").strip()
    out["holdings_sheet_name"] = (form.get("holdings_sheet_name") or "").strip() or "Holdings"
    out["holdings_source"] = (form.get("holdings_source") or "google_sheets").strip().lower()
    if out["holdings_source"] not in HOLDINGS_SOURCES:
        out["holdings_source"] = "google_sheets"
    out["local_holdings_path"] = (form.get("local_holdings_path") or "").strip()
    out["local_holdings_sheet_name"] = (
        form.get("local_holdings_sheet_name") or ""
    ).strip() or "Holdings"
    out["emails"] = form.get("emails") or ""
    out["base_currency"] = (form.get("base_currency") or "").strip()
    out["fx_source"] = (form.get("fx_source") or "frankfurter").strip().lower()
    out["fx_api_key"] = (form.get("fx_api_key") or "").strip() or ""
    out["market_overrides"] = form.get("market_overrides") or ""
    out["run_on_startup"] = form.get("run_on_startup") == "1"
    out["upload_to_drive"] = form.get("upload_to_drive") == "1"
    out["output_formats"] = [fmt for fmt in OUTPUT_FORMATS if form.get(f"output_{fmt}") == "1"] or [
        "xlsx"
    ]
    out["local_report_dir"] = (form.get("local_report_dir") or "").strip()
    selected: list[str] = []
    for sid in KNOWN_FINANCE_SOURCES:
        if form.get(f"finance_{sid}") == "1":
            selected.append(sid)
        if sid != "yahoo":
            out[f"key_{sid}"] = (form.get(f"key_{sid}") or "").strip()
            action = (form.get(f"key_action_{sid}") or "keep").strip().lower()
            if action not in {"keep", "replace", "clear"}:
                action = "keep"
            out["finance_key_action"][sid] = action
    out["finance_selected"] = selected
    fx_action = (form.get("fx_key_action") or "keep").strip().lower()
    if fx_action not in {"keep", "replace", "clear"}:
        fx_action = "keep"
    out["fx_key_action"] = fx_action
    for field, _ in RECOMMENDED_COLUMNS:
        out[f"col_{field}"] = (form.get(f"col_{field}") or "").strip()
    return out


def _form_from_config(enc: EncryptedConfig) -> dict[str, Any]:
    form = _default_form()
    try:
        cfg = enc.load()
    except Exception:  # noqa: BLE001
        return form
    form["holdings_source"] = cfg.holdings_source
    form["google_sheets_id"] = cfg.google_sheets_id
    form["holdings_sheet_name"] = cfg.holdings_sheet_name
    form["local_holdings_path"] = cfg.local_holdings_path
    form["local_holdings_sheet_name"] = cfg.local_holdings_sheet_name
    form["emails"] = "\n".join(cfg.email_ids)
    form["base_currency"] = cfg.base_currency
    form["fx_source"] = cfg.fx_source
    form["market_overrides"] = "\n".join(
        f"{suffix}={currency}" for suffix, currency in cfg.market_currency_overrides.items()
    )
    form["run_on_startup"] = cfg.run_on_startup
    form["upload_to_drive"] = cfg.upload_to_drive
    form["finance_selected"] = list(cfg.finance_sources)
    form["output_formats"] = list(cfg.output_formats or ["xlsx"])
    form["local_report_dir"] = cfg.local_report_dir
    form["analysis_enabled"] = cfg.analysis_enabled
    form["llm_provider"] = cfg.llm_provider
    form["ollama_url"] = cfg.ollama_url
    form["analysis_model"] = cfg.analysis_model
    form["gemini_model"] = cfg.gemini_model
    form["llm_include_holding_context"] = cfg.llm_include_holding_context
    for field, _ in RECOMMENDED_COLUMNS:
        if field in cfg.column_map:
            form[f"col_{field}"] = cfg.column_map[field]
    return form


def _key_statuses(*, gemini_probe: bool = False) -> dict[str, str]:
    gemini_status = "not set"
    if get_gemini_api_key():
        gemini_status = "exists (hidden)"
        if gemini_probe:
            from ticker_tracker.analysis.llm_gemini import probe_gemini_api_key

            probe_err = probe_gemini_api_key()
            if probe_err:
                gemini_status = f"rejected — {probe_err[:120]}"
    status: dict[str, str] = {
        "fx": "exists (hidden)" if get_fx_api_key() else "not set",
        "fmp": "exists (hidden)" if get_fmp_api_key() else "not set",
        "gemini": gemini_status,
    }
    for sid in KNOWN_FINANCE_SOURCES:
        if sid == "yahoo":
            continue
        status[sid] = "exists (hidden)" if get_finance_api_key(sid) else "not set"
    return status


def create_app(encrypted_config: EncryptedConfig) -> Flask:
    template_dir = Path(__file__).resolve().parent / "templates"
    app = Flask(__name__, template_folder=str(template_dir))
    app.config["TICKER_ENCRYPTED_CONFIG"] = encrypted_config

    default_cols = {
        field: DEFAULT_COLUMN_LETTERS.get(field, "") for field, _ in RECOMMENDED_COLUMNS
    }

    @app.get("/")
    def get_form() -> str:
        enc: EncryptedConfig = app.config["TICKER_ENCRYPTED_CONFIG"]
        return render_template(
            "setup.html",
            errors=[],
            saved=False,
            form=_form_from_config(enc),
            key_statuses=_key_statuses(),
            helps=HELPS,
            titles=TITLES,
            recommended_columns=RECOMMENDED_COLUMNS,
            finance_labels=_finance_labels(),
            fx_choices=_fx_choices(),
            default_cols=default_cols,
            output_formats=OUTPUT_FORMATS,
        )

    @app.post("/")
    def post_form() -> str:
        enc: EncryptedConfig = app.config["TICKER_ENCRYPTED_CONFIG"]
        form_data = _form_from_request(request.form)
        emails = parse_emails_blob(form_data["emails"])
        overrides = parse_market_overrides_blob(form_data["market_overrides"])
        column_map = build_column_map_from_recommended_form(form_data)

        finance_sources = list(form_data["finance_selected"])
        finance_api_keys: dict[str, str] = {}
        for sid in finance_sources:
            if sid != "yahoo":
                action = form_data["finance_key_action"].get(sid, "keep")
                if action == "clear":
                    finance_api_keys[sid] = ""
                elif action == "replace":
                    finance_api_keys[sid] = str(form_data.get(f"key_{sid}") or "")

        fx_key: str | None
        if form_data["fx_key_action"] == "clear":
            fx_key = ""
        elif form_data["fx_key_action"] == "replace":
            fx_key = form_data["fx_api_key"] or None
        else:
            fx_key = None

        cfg, issues = apply_setup(
            holdings_source=form_data["holdings_source"],
            google_sheets_id=form_data["google_sheets_id"],
            holdings_sheet_name=form_data["holdings_sheet_name"],
            local_holdings_path=form_data["local_holdings_path"],
            local_holdings_sheet_name=form_data["local_holdings_sheet_name"],
            column_map=column_map,
            email_ids=emails,
            finance_sources=finance_sources,
            finance_api_keys=finance_api_keys,
            base_currency=form_data["base_currency"],
            fx_source=form_data["fx_source"],
            fx_api_key=fx_key,
            market_currency_overrides=overrides,
            run_on_startup=bool(form_data["run_on_startup"]),
            upload_to_drive=bool(form_data["upload_to_drive"]),
            output_formats=list(form_data["output_formats"]),
            local_report_dir=form_data["local_report_dir"],
            encrypted_config=enc,
        )
        saved = cfg is not None
        if saved and cfg is not None:
            try:
                from ticker_tracker.ui.startup_registration import (
                    deregister_startup,
                    register_startup,
                )

                if cfg.run_on_startup:
                    register_startup()
                else:
                    deregister_startup()
            except Exception as exc:  # noqa: BLE001
                print(f"Warning: startup registration failed: {exc}", file=sys.stderr)

        return render_template(
            "setup.html",
            errors=issues,
            saved=saved,
            form=form_data,
            key_statuses=_key_statuses(),
            helps=HELPS,
            titles=TITLES,
            recommended_columns=RECOMMENDED_COLUMNS,
            finance_labels=_finance_labels(),
            fx_choices=_fx_choices(),
            default_cols=default_cols,
            output_formats=OUTPUT_FORMATS,
        )

    return app


def run_setup_server(
    *,
    host: str = "127.0.0.1",
    port: int = 8765,
    config_path: Path | None = None,
    debug: bool = False,
) -> None:
    """Start a blocking local web server for setup (binds to *host* only by default)."""
    path = config_path or default_config_path()
    enc = EncryptedConfig(path)
    app = create_app(enc)
    print(f"\nTicker Tracker setup — open in your browser:\n  http://{host}:{port}/\n")
    print("Press Ctrl+C in this terminal when you are finished.\n")
    try:
        app.run(host=host, port=port, debug=debug, use_reloader=False)
    except OSError as e:
        print(f"Could not bind to {host}:{port} — {e}", file=sys.stderr)
        print("Try a different port: ticker-tracker-setup --web --port 9876", file=sys.stderr)
        raise SystemExit(1) from e
