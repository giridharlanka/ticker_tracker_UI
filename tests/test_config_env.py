"""Tests for .env loading (Gemini / dashboard startup)."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from ticker_tracker.config import get_gemini_api_key, load_env_files


@pytest.fixture(autouse=True)
def _clear_gemini_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    monkeypatch.setattr(
        "ticker_tracker.config.keyring.get_password",
        lambda *_a, **_k: None,
    )
    monkeypatch.setattr("ticker_tracker.config._ENV_FILES_LOADED", False)


def test_load_env_files_reads_google_api_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text('GOOGLE_API_KEY="test-google-key-12345678"\n', encoding="utf-8")
    load_env_files(path=env_file)
    assert get_gemini_api_key() == "test-google-key-12345678"


def test_load_env_files_does_not_override_existing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GOOGLE_API_KEY", "from-shell")
    env_file = tmp_path / ".env"
    env_file.write_text("GOOGLE_API_KEY=from-dotenv\n", encoding="utf-8")
    load_env_files(path=env_file)
    assert os.environ["GOOGLE_API_KEY"] == "from-shell"
    assert get_gemini_api_key() == "from-shell"
