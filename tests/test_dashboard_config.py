"""Dashboard Settings API tests."""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("flask")

from ticker_tracker.config import EncryptedConfig
from ticker_tracker.web.dashboard_server import create_dashboard_app


def test_dashboard_config_round_trip_analysis_fields(tmp_path: Path) -> None:
    enc = EncryptedConfig(tmp_path / "config.enc")
    app = create_dashboard_app(enc)
    client = app.test_client()

    payload = {
        "holdings_source": "google_sheets",
        "google_sheets_id": "1" * 22,
        "holdings_sheet_name": "Holdings",
        "emails": "",
        "base_currency": "SGD",
        "fx_source": "frankfurter",
        "fx_key_action": "keep",
        "market_overrides": "",
        "run_on_startup": False,
        "upload_to_drive": False,
        "output_formats": ["xlsx"],
        "finance_selected": ["yahoo"],
        "finance_key_action": {},
        "columns": {"ticker": "A", "shares": "B", "cost_basis": "C"},
        "analysis_enabled": False,
        "ollama_url": "http://192.168.1.10:11434",
        "analysis_model": "gemma3:4b",
        "fmp_key_action": "keep",
    }
    resp = client.post("/api/config", json=payload)
    assert resp.status_code == 200
    data = resp.get_json()
    assert data.get("saved") is True
    assert data["form"]["analysis_enabled"] is False
    assert data["form"]["ollama_url"] == "http://192.168.1.10:11434"
    assert data["form"]["analysis_model"] == "gemma3:4b"

    loaded = enc.load()
    assert loaded.analysis_enabled is False
    assert loaded.ollama_url == "http://192.168.1.10:11434"
    assert loaded.analysis_model == "gemma3:4b"


def test_dashboard_config_no_false_decrypt_warning(tmp_path: Path) -> None:
    """Saved config must not trigger decrypt warning (form uses col_*, not column_map)."""
    enc = EncryptedConfig(tmp_path / "config.enc")
    app = create_dashboard_app(enc)
    client = app.test_client()

    save_payload = {
        "holdings_source": "google_sheets",
        "google_sheets_id": "1" * 22,
        "holdings_sheet_name": "Holdings",
        "emails": "",
        "base_currency": "SGD",
        "fx_source": "frankfurter",
        "fx_key_action": "keep",
        "market_overrides": "",
        "run_on_startup": False,
        "upload_to_drive": False,
        "output_formats": ["xlsx"],
        "finance_selected": ["yahoo"],
        "finance_key_action": {},
        "columns": {"ticker": "A", "shares": "B", "cost_basis": "C"},
    }
    assert client.post("/api/config", json=save_payload).status_code == 200

    get_data = client.get("/api/config").get_json()
    assert get_data.get("config_warning") is None
    assert enc.can_decrypt() is True


def test_dashboard_config_decrypt_warning_only_when_corrupt(tmp_path: Path) -> None:
    enc = EncryptedConfig(tmp_path / "config.enc")
    enc.save(enc.load())
    enc.path.write_bytes(b"not-a-valid-fernet-token" + b"x" * 64)
    app = create_dashboard_app(enc)
    data = app.test_client().get("/api/config").get_json()
    assert data.get("config_warning")
    assert enc.can_decrypt() is False
