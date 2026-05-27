"""Encrypted configuration backed by Fernet and OS keychain material."""

from __future__ import annotations

import base64
import json
import os
import platform
import sys
import uuid
import warnings
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import keyring
import keyring.errors
from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

KEYRING_SERVICE = "ticker-tracker"
KEYRING_FMP_SERVICE = "ticker-tracker-fmp"
KEYRING_GEMINI_SERVICE = "ticker-tracker-gemini"
KEYRING_CONFIG_KEY_USER = "config-key"
KEYRING_FX_API_USER = "fx-api-key"
KEYRING_FMP_API_USER = "api-key"
HKDF_INFO = b"ticker-tracker-fernet-v1"
LOCAL_SALT_FILE = "config.salt"


def finance_keyring_username(source: str) -> str:
    """Keychain username for a finance provider API key."""
    return f"finance-api-{source}"


def get_fx_api_key() -> str | None:
    try:
        return keyring.get_password(KEYRING_SERVICE, KEYRING_FX_API_USER)
    except keyring.errors.KeyringError:
        return None


def set_fx_api_key(key: str | None) -> None:
    try:
        if key:
            keyring.set_password(KEYRING_SERVICE, KEYRING_FX_API_USER, key)
            return
        keyring.delete_password(KEYRING_SERVICE, KEYRING_FX_API_USER)
    except keyring.errors.KeyringError:
        pass


def get_finance_api_key(source: str) -> str | None:
    try:
        return keyring.get_password(KEYRING_SERVICE, finance_keyring_username(source))
    except keyring.errors.KeyringError:
        return None


def set_finance_api_key(source: str, key: str | None) -> None:
    user = finance_keyring_username(source)
    try:
        if key:
            keyring.set_password(KEYRING_SERVICE, user, key)
            return
        keyring.delete_password(KEYRING_SERVICE, user)
    except keyring.errors.KeyringError:
        pass


def get_fmp_api_key() -> str | None:
    try:
        return keyring.get_password(KEYRING_FMP_SERVICE, KEYRING_FMP_API_USER)
    except keyring.errors.KeyringError:
        return None


def set_fmp_api_key(key: str | None) -> None:
    try:
        if key:
            keyring.set_password(KEYRING_FMP_SERVICE, KEYRING_FMP_API_USER, key)
            return
        keyring.delete_password(KEYRING_FMP_SERVICE, KEYRING_FMP_API_USER)
    except keyring.errors.KeyringError:
        pass


_ENV_FILES_LOADED = False


def _parse_env_line(line: str) -> tuple[str, str] | None:
    stripped = line.strip()
    if not stripped or stripped.startswith("#"):
        return None
    if stripped.startswith("export "):
        stripped = stripped[7:].strip()
    if "=" not in stripped:
        return None
    key, _, value = stripped.partition("=")
    key = key.strip()
    if not key:
        return None
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        value = value[1:-1]
    return key, value


def _env_file_candidates(extra: Path | None = None) -> list[Path]:
    seen: set[Path] = set()
    out: list[Path] = []

    def add(path: Path) -> None:
        resolved = path.expanduser().resolve()
        if resolved in seen:
            return
        seen.add(resolved)
        out.append(resolved)

    if extra is not None:
        add(extra)
    add(Path.cwd() / ".env")
    add(Path(__file__).resolve().parents[1] / ".env")
    add(application_config_dir() / ".env")
    return out


def load_env_files(*, path: Path | None = None) -> None:
    """Load ``.env`` into ``os.environ`` without overriding variables already set."""
    global _ENV_FILES_LOADED
    if _ENV_FILES_LOADED and path is None:
        return
    for env_path in _env_file_candidates(path):
        if not env_path.is_file():
            continue
        try:
            text = env_path.read_text(encoding="utf-8")
        except OSError:
            continue
        for line in text.splitlines():
            parsed = _parse_env_line(line)
            if parsed is None:
                continue
            key, value = parsed
            if key not in os.environ:
                os.environ[key] = value
    if path is None:
        _ENV_FILES_LOADED = True


def get_gemini_api_key() -> str | None:
    """Gemini API key from keychain, else GEMINI_API_KEY or GOOGLE_API_KEY env."""
    load_env_files()
    try:
        stored = keyring.get_password(KEYRING_GEMINI_SERVICE, KEYRING_FMP_API_USER)
        if stored:
            return stored
    except keyring.errors.KeyringError:
        pass
    for env_name in ("GEMINI_API_KEY", "GOOGLE_API_KEY"):
        val = os.environ.get(env_name, "").strip()
        if val:
            return val
    return None


def set_gemini_api_key(key: str | None) -> None:
    try:
        if key:
            keyring.set_password(KEYRING_GEMINI_SERVICE, KEYRING_FMP_API_USER, key)
            return
        keyring.delete_password(KEYRING_GEMINI_SERVICE, KEYRING_FMP_API_USER)
    except keyring.errors.KeyringError:
        pass


LLM_PROVIDERS = frozenset({"ollama", "gemini"})


def _machine_fingerprint() -> bytes:
    node = platform.node() or "unknown-host"
    return f"{node}\n{uuid.getnode()}".encode()


def _default_config_dir() -> Path:
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA")
        if base:
            return Path(base) / "ticker-tracker"
        return Path.home() / "AppData" / "Local" / "ticker-tracker"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "ticker-tracker"
    xdg = os.environ.get("XDG_CONFIG_HOME")
    if xdg:
        return Path(xdg) / "ticker-tracker"
    return Path.home() / ".config" / "ticker-tracker"


def default_config_path() -> Path:
    """Filesystem path for the encrypted config blob."""
    return _default_config_dir() / "config.enc"


def application_config_dir() -> Path:
    """Directory for ``config.enc``, ``credentials.json``, and other local app files."""
    return _default_config_dir()


def _get_or_create_keyring_salt() -> bytes:
    """32-byte salt stored in the OS keychain; combined with machine fingerprint for Fernet key."""
    try:
        existing = keyring.get_password(KEYRING_SERVICE, KEYRING_CONFIG_KEY_USER)
        if existing:
            return base64.urlsafe_b64decode(existing.encode("ascii"))
        salt = os.urandom(32)
        keyring.set_password(
            KEYRING_SERVICE,
            KEYRING_CONFIG_KEY_USER,
            base64.urlsafe_b64encode(salt).decode("ascii"),
        )
        return salt
    except keyring.errors.KeyringError:
        # CI/headless environments may not have an OS keychain backend.
        # Fall back to a local random salt file in the app config directory.
        path = application_config_dir() / LOCAL_SALT_FILE
        if path.is_file():
            raw = path.read_bytes()
            if len(raw) == 32:
                return raw
        salt = os.urandom(32)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(salt)
        try:
            path.chmod(0o600)
        except OSError:
            pass
        return salt


def _fernet_from_keychain() -> Fernet:
    salt = _get_or_create_keyring_salt()
    hkdf = HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=salt,
        info=HKDF_INFO,
        backend=default_backend(),
    )
    raw = hkdf.derive(_machine_fingerprint())
    fernet_key = base64.urlsafe_b64encode(raw)
    return Fernet(fernet_key)


@dataclass
class AppConfig:
    """Plain configuration schema (secrets use keyring, not this blob)."""

    email_ids: list[str] = field(default_factory=list)
    finance_sources: list[str] = field(default_factory=list)
    fx_source: str = "frankfurter"
    base_currency: str = "USD"
    run_on_startup: bool = False
    holdings_source: str = "google_sheets"
    google_sheets_id: str = ""
    holdings_sheet_name: str = "Holdings"
    local_holdings_path: str = ""
    local_holdings_sheet_name: str = "Holdings"
    column_map: dict[str, str] = field(default_factory=dict)
    market_currency_overrides: dict[str, str] = field(default_factory=dict)
    upload_to_drive: bool = False
    output_formats: list[str] = field(default_factory=lambda: ["xlsx"])
    local_report_dir: str = ""
    analysis_enabled: bool = True
    llm_provider: str = "ollama"
    analysis_model: str = "qwen2.5:7b"
    ollama_url: str = "http://localhost:11434"
    gemini_model: str = "gemini-2.0-flash"
    llm_include_holding_context: bool = False

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        # Stored encrypted schema: FX/FMP API keys live in OS keychain when needed.
        payload["fx_api_key"] = None
        payload["fmp_api_key"] = None
        return payload

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> AppConfig:
        return cls(
            email_ids=list(data.get("email_ids") or []),
            finance_sources=list(data.get("finance_sources") or []),
            fx_source=str(data.get("fx_source") or "frankfurter"),
            base_currency=str(data.get("base_currency") or "USD"),
            run_on_startup=bool(data.get("run_on_startup", False)),
            holdings_source=str(data.get("holdings_source") or "google_sheets"),
            google_sheets_id=str(data.get("google_sheets_id") or ""),
            holdings_sheet_name=str(data.get("holdings_sheet_name") or "Holdings"),
            local_holdings_path=str(data.get("local_holdings_path") or ""),
            local_holdings_sheet_name=str(data.get("local_holdings_sheet_name") or "Holdings"),
            column_map=dict(data.get("column_map") or {}),
            market_currency_overrides=dict(data.get("market_currency_overrides") or {}),
            upload_to_drive=bool(data.get("upload_to_drive", False)),
            output_formats=list(data.get("output_formats") or ["xlsx"]),
            local_report_dir=str(data.get("local_report_dir") or ""),
            analysis_enabled=bool(data.get("analysis_enabled", True)),
            llm_provider=(
                p
                if (p := str(data.get("llm_provider") or "ollama").strip().lower()) in LLM_PROVIDERS
                else "ollama"
            ),
            analysis_model=str(data.get("analysis_model") or "qwen2.5:7b"),
            ollama_url=str(data.get("ollama_url") or "http://localhost:11434"),
            gemini_model=str(data.get("gemini_model") or "gemini-2.0-flash"),
            llm_include_holding_context=bool(data.get("llm_include_holding_context", False)),
        )


class EncryptedConfig:
    """Load and save JSON config encrypted with Fernet (key material from keychain + machine)."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or default_config_path()

    def can_decrypt(self) -> bool:
        """Return whether ``config.enc`` exists and decrypts with this machine's key material."""
        if not self.path.is_file():
            return True
        fernet = _fernet_from_keychain()
        try:
            fernet.decrypt(self.path.read_bytes())
            return True
        except InvalidToken:
            return False

    def load(self) -> AppConfig:
        if not self.path.is_file():
            return AppConfig()
        fernet = _fernet_from_keychain()
        raw = self.path.read_bytes()
        try:
            decrypted = fernet.decrypt(raw)
        except InvalidToken:
            warnings.warn(
                "Could not decrypt config.enc (wrong machine, missing keychain entry, "
                "or corrupt file). Using default empty settings until you save setup again.",
                UserWarning,
                stacklevel=2,
            )
            return AppConfig()
        payload = json.loads(decrypted.decode("utf-8"))
        return AppConfig.from_dict(payload)

    def save(self, config: AppConfig) -> None:
        fernet = _fernet_from_keychain()
        blob = json.dumps(config.to_dict(), indent=2, sort_keys=True).encode("utf-8")
        token = fernet.encrypt(blob)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_bytes(token)
