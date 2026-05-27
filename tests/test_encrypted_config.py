"""Tests for EncryptedConfig encryption round-trip."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from ticker_tracker.config import (
    KEYRING_CONFIG_KEY_USER,
    KEYRING_SERVICE,
    AppConfig,
    EncryptedConfig,
)


@pytest.fixture()
def isolated_keyring(
    monkeypatch: pytest.MonkeyPatch,
) -> dict[tuple[str, str], str]:
    """In-memory keyring replacement for tests."""
    store: dict[tuple[str, str], str] = {}

    def get_password(service_name: str, username: str) -> str | None:
        return store.get((service_name, username))

    def set_password(service_name: str, username: str, password: str) -> None:
        store[(service_name, username)] = password

    monkeypatch.setattr("ticker_tracker.config.keyring.get_password", get_password)
    monkeypatch.setattr("ticker_tracker.config.keyring.set_password", set_password)
    return store


def test_encrypted_config_round_trip(
    tmp_path: Path, isolated_keyring: dict[tuple[str, str], str]
) -> None:
    path = tmp_path / "config.enc"
    enc = EncryptedConfig(path)

    cfg = AppConfig(
        email_ids=["user@example.com"],
        finance_sources=["yahoo", "alpha_vantage"],
        fx_source="frankfurter",
        base_currency="SGD",
        run_on_startup=True,
        google_sheets_id="1BxiMVs0XRA5nFMdKvBdBZjgmUUqptlbs74OgvE2upms",
        holdings_sheet_name="Holdings",
        column_map={
            "ticker": "A",
            "shares": "B",
            "cost_basis": "C",
            "currency_override": "D",
        },
        market_currency_overrides={".KL": "MYR"},
        upload_to_drive=True,
    )

    enc.save(cfg)
    assert path.is_file()
    loaded = enc.load()

    assert loaded == cfg
    assert cfg.to_dict()["fx_api_key"] is None

    # Ensure on-disk payload is not plain JSON
    raw = path.read_bytes()
    with pytest.raises(json.JSONDecodeError):
        json.loads(raw.decode("utf-8"))

    # Salt should exist in our fake keyring store
    assert isolated_keyring.get((KEYRING_SERVICE, KEYRING_CONFIG_KEY_USER)) is not None


def test_load_undecryptable_file_warns_and_returns_empty(
    tmp_path: Path, isolated_keyring: dict[tuple[str, str], str]
) -> None:
    path = tmp_path / "config.enc"
    enc = EncryptedConfig(path)
    enc.save(AppConfig(google_sheets_id="abc"))
    path.write_bytes(b"x" * 80)
    with pytest.warns(UserWarning, match="Could not decrypt config.enc"):
        loaded = enc.load()
    assert loaded == AppConfig()
    assert enc.can_decrypt() is False
