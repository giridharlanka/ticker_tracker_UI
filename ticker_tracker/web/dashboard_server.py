"""Local dashboard Flask app (portfolio UI + config API)."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import threading
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path
from typing import Any

from flask import Flask, jsonify, request, send_from_directory

from ticker_tracker.analysis.base import LLMAnalysis
from ticker_tracker.config import AppConfig, EncryptedConfig, default_config_path, load_env_files
from ticker_tracker.engine import run_once
from ticker_tracker.google.auth import get_credentials
from ticker_tracker.google.gmail import send_email
from ticker_tracker.html_report import TabbedReportState
from ticker_tracker.setup_core import (
    HOLDINGS_SOURCES,
    KNOWN_FINANCE_SOURCES,
    OUTPUT_FORMATS,
    RECOMMENDED_COLUMNS,
    apply_setup,
    build_column_map_from_recommended_form,
    parse_emails_blob,
    parse_market_overrides_blob,
)
from ticker_tracker.web.analyse_job import JOB_STORE
from ticker_tracker.web.setup_server import (
    _default_form,
    _form_from_config,
    _key_statuses,
)

_last_report_html: str | None = None
_last_email_subject: str | None = None


def _effective_analyse_formats(cfg: AppConfig) -> list[str]:
    """Always HTML for the dashboard; XLSX only when enabled in saved config."""
    out = ["html"]
    if "xlsx" in (cfg.output_formats or []):
        out.append("xlsx")
    return list(dict.fromkeys(out))


def _resolve_gemini_key(form_data: Mapping[str, Any]) -> str | None:
    action = str(form_data.get("gemini_key_action") or "keep").strip().lower()
    if action == "clear":
        return ""
    if action == "replace":
        return str(form_data.get("key_gemini") or "")
    return None


def _parse_run_analysis_enabled() -> bool | None:
    """Per-run analysis toggle from dashboard Analyse (``None`` = use saved config)."""
    if request.is_json:
        payload = request.get_json(silent=True) or {}
        if "analysis_enabled" not in payload:
            return None
        return bool(payload.get("analysis_enabled"))
    raw = request.form.get("analysis_enabled")
    if raw is None:
        return None
    return str(raw).strip().lower() in ("1", "true", "yes", "on")


def _with_run_output_dir(cfg: AppConfig, output_dir: str | None) -> AppConfig:
    """Per-run output folder override from the dashboard Run panel."""
    s = (output_dir or "").strip()
    if s:
        return replace(cfg, local_report_dir=s)
    return cfg


def _analyse_response_from_result(
    result: dict[str, Any],
    *,
    cfg_out: AppConfig,
) -> dict[str, Any]:
    """Build JSON payload (html + summary) from :func:`run_once` result."""
    html_path = result.get("html_report_path")
    if not html_path:
        raise ValueError("Run finished but no HTML report path was returned.")
    path_obj = Path(html_path)
    html_content = path_obj.read_text(encoding="utf-8")
    base_ccy = str(cfg_out.base_currency)
    fname = result.get("workbook_filename") or path_obj.name
    return {
        "html": html_content,
        "email_html": result.get("email_html"),
        "summary": {
            "workbook_path": result.get("workbook_path"),
            "html_report_path": html_path,
            "drive_url": result.get("drive_url"),
            "emails_sent": result.get("emails_sent", 0),
            "base_currency": base_ccy,
        },
        "metadata": result.get("metadata"),
        "email_subject": f"Ticker summary ({base_ccy}) — {fname}",
    }


def _execute_analyse_run(
    enc: EncryptedConfig,
    *,
    source: str,
    send_mail: bool,
    output_dir_raw: str | None,
    holdings_path_raw: str = "",
    sheet_name_run: str = "Holdings",
    upload_file: Any = None,
    job_id: str | None = None,
    run_analysis_enabled: bool | None = None,
) -> dict[str, Any]:
    """Run portfolio analyse; optional *job_id* feeds :data:`JOB_STORE` progress."""
    base_cfg = enc.load()
    cfg_with_out = _with_run_output_dir(base_cfg, output_dir_raw)

    progress_cb = JOB_STORE.progress_callback(job_id) if job_id else None

    def _stdout_progress(pct: int, msg: str) -> None:
        print(f"[analyse {pct:3d}%] {msg}", flush=True)
        if progress_cb is not None:
            progress_cb(pct, msg)

    report_state: TabbedReportState | None = None
    on_ticker_cb = None
    if job_id is not None:
        report_state = TabbedReportState(
            publish=lambda html: JOB_STORE.set_preview_html(job_id, html),
        )

        def _on_ticker(ticker: str, analysis: LLMAnalysis) -> None:
            JOB_STORE.add_live_signal(
                job_id,
                ticker=ticker,
                signal=str(analysis.signal),
                confidence=str(analysis.confidence),
            )

        on_ticker_cb = _on_ticker

    tmp_created: Path | None = None
    try:
        if source == "upload":
            holdings_on_disk: Path | None = None
            if holdings_path_raw:
                holdings_on_disk = Path(holdings_path_raw).expanduser().resolve()
                if not holdings_on_disk.is_file():
                    raise ValueError(f"Holdings file not found: {holdings_on_disk}")
            elif upload_file is not None and getattr(upload_file, "filename", None):
                suffix = Path(upload_file.filename).suffix.lower()
                if suffix not in {".csv", ".xlsx", ".xls"}:
                    raise ValueError("Please upload a .csv or .xlsx file.")
                fd, tmp_path_str = tempfile.mkstemp(suffix=suffix)
                os.close(fd)
                tmp_created = Path(tmp_path_str)
                upload_file.save(tmp_created)
                holdings_on_disk = tmp_created
            else:
                raise ValueError(
                    "Choose a holdings file: set path on this machine, browse, "
                    "or upload / drag-and-drop.",
                )

            cfg_run = replace(
                cfg_with_out,
                holdings_source="local_file",
                local_holdings_path=str(holdings_on_disk),
                local_holdings_sheet_name=sheet_name_run,
                output_formats=_effective_analyse_formats(base_cfg),
            )
            creds = None
            if send_mail and cfg_run.email_ids:
                creds = get_credentials()
            result = run_once(
                app_config=cfg_run,
                send_email_notifications=send_mail,
                allow_local_file_email=True,
                credentials=creds,
                progress_callback=_stdout_progress,
                report_state=report_state,
                on_analysis_ticker=on_ticker_cb,
                analysis_enabled=run_analysis_enabled,
            )
            cfg_out = result["config"]
        elif source == "google_sheets":
            creds = get_credentials()
            cfg_run = replace(
                cfg_with_out,
                output_formats=_effective_analyse_formats(base_cfg),
            )
            result = run_once(
                app_config=cfg_run,
                send_email_notifications=send_mail,
                credentials=creds,
                progress_callback=_stdout_progress,
                report_state=report_state,
                on_analysis_ticker=on_ticker_cb,
                analysis_enabled=run_analysis_enabled,
            )
            cfg_out = result["config"]
        else:
            raise ValueError(f"Unknown source {source!r}.")
    finally:
        if tmp_created is not None:
            try:
                tmp_created.unlink(missing_ok=True)
            except OSError:
                pass

    return _analyse_response_from_result(result, cfg_out=cfg_out)


def _analyse_worker(
    job_id: str,
    enc: EncryptedConfig,
    *,
    source: str,
    send_mail: bool,
    output_dir_raw: str | None,
    holdings_path_raw: str,
    sheet_name_run: str,
    upload_bytes: bytes | None,
    upload_filename: str | None,
    run_analysis_enabled: bool | None = None,
) -> None:
    global _last_report_html, _last_email_subject
    JOB_STORE.update(job_id, status="running", message="Starting portfolio run…")
    upload_file = None
    if upload_bytes and upload_filename:
        from io import BytesIO

        from werkzeug.datastructures import FileStorage

        upload_file = FileStorage(
            stream=BytesIO(upload_bytes),
            filename=upload_filename,
        )
    try:
        payload = _execute_analyse_run(
            enc,
            source=source,
            send_mail=send_mail,
            output_dir_raw=output_dir_raw,
            holdings_path_raw=holdings_path_raw,
            sheet_name_run=sheet_name_run,
            upload_file=upload_file,
            job_id=job_id,
            run_analysis_enabled=run_analysis_enabled,
        )
        _last_report_html = payload.get("email_html") or payload["html"]
        _last_email_subject = payload.get("email_subject")
        JOB_STORE.complete(job_id, payload)
    except Exception as exc:  # noqa: BLE001
        JOB_STORE.fail(job_id, str(exc))


def _json_to_form(data: Mapping[str, Any]) -> dict[str, Any]:
    """Same shape as :func:`ticker_tracker.web.setup_server._form_from_request`."""
    out = _default_form()
    if not data:
        return out
    out["google_sheets_id"] = str(data.get("google_sheets_id") or "").strip()
    out["holdings_sheet_name"] = str(data.get("holdings_sheet_name") or "").strip() or "Holdings"
    hs = str(data.get("holdings_source") or "google_sheets").strip().lower()
    out["holdings_source"] = hs if hs in HOLDINGS_SOURCES else "google_sheets"
    out["local_holdings_path"] = str(data.get("local_holdings_path") or "").strip()
    out["local_holdings_sheet_name"] = (
        str(data.get("local_holdings_sheet_name") or "").strip() or "Holdings"
    )
    out["emails"] = str(data.get("emails") or "")
    out["base_currency"] = str(data.get("base_currency") or "").strip()
    out["fx_source"] = str(data.get("fx_source") or "frankfurter").strip().lower()
    out["fx_api_key"] = str(data.get("fx_api_key") or "").strip()
    out["market_overrides"] = str(data.get("market_overrides") or "")
    out["run_on_startup"] = bool(data.get("run_on_startup"))
    out["upload_to_drive"] = bool(data.get("upload_to_drive"))
    fmts = data.get("output_formats")
    if isinstance(fmts, list) and fmts:
        cleaned = [str(f).lower() for f in fmts if str(f).lower() in OUTPUT_FORMATS]
        out["output_formats"] = cleaned or ["xlsx"]
    out["local_report_dir"] = str(data.get("local_report_dir") or "").strip()
    out["analysis_enabled"] = bool(data.get("analysis_enabled", True))
    provider = str(data.get("llm_provider") or "ollama").strip().lower()
    out["llm_provider"] = provider if provider in ("ollama", "gemini") else "ollama"
    out["ollama_url"] = str(data.get("ollama_url") or "http://localhost:11434").strip().rstrip("/")
    out["analysis_model"] = str(data.get("analysis_model") or "qwen2.5:7b").strip()
    out["gemini_model"] = str(data.get("gemini_model") or "gemini-2.0-flash").strip()
    out["llm_include_holding_context"] = bool(data.get("llm_include_holding_context", False))
    gemini_action = str(data.get("gemini_key_action") or "keep").strip().lower()
    if gemini_action in {"keep", "replace", "clear"}:
        out["gemini_key_action"] = gemini_action
    if data.get("key_gemini") is not None:
        out["key_gemini"] = str(data.get("key_gemini") or "").strip()
    fmp_action = str(data.get("fmp_key_action") or "keep").strip().lower()
    if fmp_action in {"keep", "replace", "clear"}:
        out["fmp_key_action"] = fmp_action
    if data.get("key_fmp") is not None:
        out["key_fmp"] = str(data.get("key_fmp") or "").strip()

    selected_raw = data.get("finance_selected")
    selected: list[str] = []
    if isinstance(selected_raw, list):
        for s in selected_raw:
            sid = str(s).strip().lower()
            if sid in KNOWN_FINANCE_SOURCES:
                selected.append(sid)
    out["finance_selected"] = selected

    fka_in = data.get("finance_key_action")
    if isinstance(fka_in, dict):
        for sid in KNOWN_FINANCE_SOURCES:
            if sid == "yahoo":
                continue
            a = str(fka_in.get(sid) or "keep").strip().lower()
            if a in {"keep", "replace", "clear"}:
                out["finance_key_action"][sid] = a

    fkeys = data.get("finance_keys")
    if isinstance(fkeys, dict):
        for sid, val in fkeys.items():
            k = str(sid).strip().lower()
            if k != "yahoo" and f"key_{k}" in out:
                out[f"key_{k}"] = str(val or "").strip()

    for sid in KNOWN_FINANCE_SOURCES:
        if sid == "yahoo":
            continue
        if data.get(f"key_{sid}") is not None:
            out[f"key_{sid}"] = str(data.get(f"key_{sid}") or "").strip()

    fx_action = str(data.get("fx_key_action") or "keep").strip().lower()
    if fx_action in {"keep", "replace", "clear"}:
        out["fx_key_action"] = fx_action

    cols = data.get("columns")
    if isinstance(cols, dict):
        for field, _ in RECOMMENDED_COLUMNS:
            if field in cols:
                out[f"col_{field}"] = str(cols[field] or "").strip().upper()
    for field, _ in RECOMMENDED_COLUMNS:
        if data.get(f"col_{field}") is not None:
            out[f"col_{field}"] = str(data.get(f"col_{field}") or "").strip().upper()

    return out


def _tk_dialog_subprocess(kind: str) -> str | None:
    """
    Run Tk filedialog in a **child Python process**.

    Flask handles HTTP on worker threads; on macOS, ``Tk()`` must run on the process
    main thread (``NSWindow should only be instantiated on the main thread``).
    A subprocess has its own main thread, so the dialog is safe there.
    """
    if kind not in {"folder", "file"}:
        raise ValueError(kind)
    script = f"""
import sys
_kind = {repr(kind)}
try:
    import tkinter as tk
    from tkinter import filedialog
except ImportError as exc:
    sys.stderr.write(str(exc))
    sys.exit(2)
root = tk.Tk()
root.withdraw()
try:
    root.attributes("-topmost", True)
except tk.TclError:
    pass
try:
    if _kind == "folder":
        picked = filedialog.askdirectory(mustexist=True) or ""
    else:
        picked = filedialog.askopenfilename(
            filetypes=[
                ("CSV or Excel", "*.csv *.xlsx *.xls"),
                ("CSV", "*.csv"),
                ("Excel", "*.xlsx *.xls"),
                ("All files", "*.*"),
            ]
        ) or ""
finally:
    root.destroy()
sys.stdout.write(picked)
"""
    try:
        proc = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=3600,
            env=os.environ.copy(),
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError("Native picker timed out.") from None

    if proc.returncode == 2:
        raise RuntimeError("Tkinter is not available for native picker.")
    if proc.returncode != 0:
        err = (proc.stderr or "").strip() or "Native picker failed."
        raise RuntimeError(err)

    path = (proc.stdout or "").strip()
    return path or None


def _pick_folder_dialog() -> str | None:
    """Blocking OS folder picker (localhost dashboard only)."""
    return _tk_dialog_subprocess("folder")


def _pick_file_dialog() -> str | None:
    """Blocking OS file picker for CSV/XLSX (localhost dashboard only)."""
    return _tk_dialog_subprocess("file")


def _dedupe_emails(raw: list[str]) -> list[str]:
    recipients: list[str] = []
    seen: set[str] = set()
    for line in raw:
        addr = str(line).strip()
        if not addr:
            continue
        key = addr.lower()
        if key in seen:
            continue
        seen.add(key)
        recipients.append(addr)
    return recipients


def create_dashboard_app(encrypted_config: EncryptedConfig) -> Flask:
    dash_dir = Path(__file__).resolve().parent / "dashboard"
    app = Flask(__name__, static_folder=str(dash_dir), static_url_path="")
    app.config["TICKER_ENCRYPTED_CONFIG"] = encrypted_config

    @app.get("/")
    def index() -> Any:
        return send_from_directory(dash_dir, "index.html")

    @app.get("/api/config")
    def api_get_config() -> Any:
        enc: EncryptedConfig = app.config["TICKER_ENCRYPTED_CONFIG"]
        form = _form_from_config(enc)
        config_warning: str | None = None
        if enc.path.is_file() and not enc.can_decrypt():
            config_warning = (
                "Could not decrypt config.enc on this machine (wrong device, missing "
                "keychain entry, or corrupt file). Open Settings and save again, or run "
                "ticker-tracker-setup."
            )
        gemini_probe = (form.get("llm_provider") or "ollama").strip().lower() == "gemini"
        return jsonify(
            {
                "form": form,
                "key_statuses": _key_statuses(gemini_probe=gemini_probe),
                "config_warning": config_warning,
                "config_path": str(enc.path.resolve()),
                "output_formats": list(OUTPUT_FORMATS),
                "recommended_columns": [
                    {"field": f, "description": d} for f, d in RECOMMENDED_COLUMNS
                ],
            }
        )

    @app.post("/api/config")
    def api_post_config() -> Any:
        enc: EncryptedConfig = app.config["TICKER_ENCRYPTED_CONFIG"]
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            return jsonify({"error": "Expected JSON body."}), 400
        form_data = _json_to_form(data)
        existing_cfg = enc.load() if enc.can_decrypt() else None
        if existing_cfg is not None:
            # Dashboard Settings omits local paths; keep CLI/Tk values on disk.
            form_data["local_holdings_path"] = existing_cfg.local_holdings_path
            form_data["local_holdings_sheet_name"] = existing_cfg.local_holdings_sheet_name
            form_data["local_report_dir"] = existing_cfg.local_report_dir
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

        fmp_key: str | None = None
        fmp_action = str(form_data.get("fmp_key_action") or "keep").strip().lower()
        if fmp_action == "clear":
            fmp_key = ""
        elif fmp_action == "replace":
            fmp_key = str(form_data.get("key_fmp") or "")

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
            require_local_holdings_path=False,
            analysis_enabled=bool(form_data.get("analysis_enabled", True)),
            llm_provider=str(form_data.get("llm_provider") or "ollama"),
            ollama_url=str(form_data.get("ollama_url") or "http://localhost:11434"),
            analysis_model=str(form_data.get("analysis_model") or "qwen2.5:7b"),
            gemini_model=str(form_data.get("gemini_model") or "gemini-2.0-flash"),
            llm_include_holding_context=bool(form_data.get("llm_include_holding_context", False)),
            fmp_api_key=fmp_key,
            gemini_api_key=_resolve_gemini_key(form_data),
        )
        if issues:
            return jsonify({"saved": False, "errors": issues}), 400

        if cfg is not None:
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

        saved_form = _form_from_config(enc)
        gemini_probe = (saved_form.get("llm_provider") or "ollama").strip().lower() == "gemini"
        return jsonify(
            {
                "saved": True,
                "form": saved_form,
                "key_statuses": _key_statuses(gemini_probe=gemini_probe),
            }
        )

    @app.post("/api/native-pick-folder")
    def api_native_pick_folder() -> Any:
        try:
            picked = _pick_folder_dialog()
        except RuntimeError as exc:
            return jsonify({"error": str(exc)}), 503
        if not picked:
            return jsonify({"cancelled": True})
        return jsonify({"path": picked})

    @app.post("/api/native-pick-file")
    def api_native_pick_file() -> Any:
        try:
            picked = _pick_file_dialog()
        except RuntimeError as exc:
            return jsonify({"error": str(exc)}), 503
        if not picked:
            return jsonify({"cancelled": True})
        return jsonify({"path": picked})

    @app.get("/api/analyse/status/<job_id>")
    def api_analyse_status(job_id: str) -> Any:
        snap = JOB_STORE.snapshot(job_id)
        if snap is None:
            return jsonify({"error": "Unknown or expired job."}), 404
        return jsonify(snap)

    @app.post("/api/analyse")
    def api_analyse() -> Any:
        enc: EncryptedConfig = app.config["TICKER_ENCRYPTED_CONFIG"]
        try:
            enc.load()
        except Exception as exc:  # noqa: BLE001
            return jsonify({"error": str(exc)}), 400

        upload_bytes: bytes | None = None
        upload_filename: str | None = None
        output_dir_raw: str | None = None
        payload: dict[str, Any] = {}
        if request.is_json:
            payload = request.get_json(silent=True) or {}
            source = str(payload.get("source") or "google_sheets").strip().lower()
            send_mail = bool(payload.get("send_email", True))
            output_dir_raw = str(payload.get("output_dir") or "").strip() or None
            holdings_path_raw = str(payload.get("holdings_path") or "").strip()
            sheet_name_run = str(payload.get("holdings_sheet_name") or "").strip() or "Holdings"
        else:
            source = (request.form.get("source") or "google_sheets").strip().lower()
            send_mail = (request.form.get("send_email") or "true").strip().lower() in (
                "1",
                "true",
                "on",
                "yes",
            )
            upload_file = request.files.get("file")
            output_dir_raw = str(request.form.get("output_dir") or "").strip() or None
            holdings_path_raw = str(request.form.get("holdings_path") or "").strip()
            sheet_name_run = (
                str(request.form.get("holdings_sheet_name") or "").strip() or "Holdings"
            )
            if upload_file is not None and upload_file.filename:
                upload_bytes = upload_file.read()
                upload_filename = upload_file.filename

        if source == "upload" and not holdings_path_raw and not upload_bytes:
            return jsonify(
                {
                    "error": "Choose a holdings file: set path on this machine, browse, "
                    "or upload / drag-and-drop.",
                },
            ), 400

        run_analysis_enabled = _parse_run_analysis_enabled()

        job_id = JOB_STORE.create()
        thread = threading.Thread(
            target=_analyse_worker,
            kwargs={
                "job_id": job_id,
                "enc": enc,
                "source": source,
                "send_mail": send_mail,
                "output_dir_raw": output_dir_raw,
                "holdings_path_raw": holdings_path_raw if source == "upload" else "",
                "sheet_name_run": sheet_name_run if source == "upload" else "Holdings",
                "upload_bytes": upload_bytes,
                "upload_filename": upload_filename,
                "run_analysis_enabled": run_analysis_enabled,
            },
            daemon=True,
        )
        thread.start()
        return jsonify({"job_id": job_id})

    @app.post("/api/email-report")
    def api_email_report() -> Any:
        global _last_report_html
        enc: EncryptedConfig = app.config["TICKER_ENCRYPTED_CONFIG"]
        if not _last_report_html:
            return jsonify({"error": "Run Analyse first so there is a report to email."}), 400
        try:
            cfg = enc.load()
        except Exception as exc:  # noqa: BLE001
            return jsonify({"error": str(exc)}), 400
        recipients = _dedupe_emails(list(cfg.email_ids or []))
        if not recipients:
            return jsonify({"error": "Add at least one email in Settings (email_ids)."}), 400
        try:
            creds = get_credentials()
        except Exception as exc:  # noqa: BLE001
            return jsonify({"error": str(exc)}), 400
        subject = _last_email_subject or "Ticker summary"
        body = _last_report_html
        sent = 0
        for addr in recipients:
            send_email(addr, subject, body, attachment_path=None, credentials=creds)
            sent += 1
        return jsonify({"ok": True, "sent": sent})

    return app


def run_dashboard_server(
    *,
    host: str = "127.0.0.1",
    port: int = 5225,
    config_path: Path | None = None,
    debug: bool = False,
) -> None:
    load_env_files()
    path = config_path or default_config_path()
    enc = EncryptedConfig(path)
    app = create_dashboard_app(enc)
    print(f"\nTicker Tracker — dashboard\n  http://{host}:{port}/\n", file=sys.stderr)
    print("Press Ctrl+C to stop.\n", file=sys.stderr)
    try:
        app.run(host=host, port=port, debug=debug, use_reloader=False)
    except OSError as exc:
        print(f"Could not bind to {host}:{port} — {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
