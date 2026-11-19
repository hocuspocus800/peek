"""Settings (JSON in Application Support) and secrets (macOS Keychain via keyring).

Rules:
- Secrets never go into settings.json or the log.
- The macOS keyring backend is chosen explicitly: keyring discovers backends
  through package entry points, which a frozen py2app bundle may not carry.
- Never touch the Keychain on the main thread. After a rebuild the Keychain may
  show a password prompt, which would block the UI.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
from dataclasses import asdict, dataclass, field
from pathlib import Path

from peek import BUNDLE_ID
from peek.paths import ensure_private_dir, support_dir

log = logging.getLogger(__name__)

SERVICE = BUNDLE_ID
ANTHROPIC_KEY = "anthropic_api_key"


# --- settings -----------------------------------------------------------------


@dataclass
class Settings:
    provider: str = "claude"
    # Last model chosen per provider (spec: remembered separately).
    models: dict[str, str] = field(default_factory=lambda: {"claude": "claude-sonnet-5-5"})
    double_tap_ms: int = 300
    display_mode: str = "under_pointer"  # under_pointer | all
    keep_answers_visible: bool = False
    # Settings → Overlay → Text size: 0 = match the question (default), else points.
    answer_text_size: int = 0
    # First-run window finished (permissions + provider); until then it opens at launch.
    onboarding_done: bool = False
    # The account's model ids, last loaded (Gemini, ChatGPT): Hard mode picks from them.
    model_lists: dict[str, list[str]] = field(default_factory=dict)
    # Menu toggle: use the provider's most capable (and priciest) model. Manual only.
    hard_mode: bool = False

    def model_for(self, provider: str) -> str | None:
        return self.models.get(provider)


DOUBLE_TAP_RANGE = (200, 500)  # ms, the Settings slider (spec)
TEXT_SIZES = (0, 12, 13, 14, 15, 16, 18, 20, 24)  # 0 = match the question


def settings_path() -> Path:
    return support_dir() / "settings.json"


_settings_lock = threading.Lock()


def load_settings() -> Settings:
    path = settings_path()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return Settings()
    except (OSError, ValueError):
        log.warning("settings.json is unreadable; using defaults")
        return Settings()
    defaults = Settings()
    if not isinstance(raw, dict):
        log.warning("settings.json is not an object; using defaults")
        return defaults
    settings = Settings()
    for name, default in asdict(defaults).items():
        if name in raw and type(raw[name]) is type(default):
            setattr(settings, name, raw[name])
    settings.models = {
        k: v for k, v in settings.models.items() if isinstance(k, str) and isinstance(v, str)
    } or defaults.models
    settings.model_lists = {
        k: [m for m in v if isinstance(m, str)]
        for k, v in settings.model_lists.items()
        if isinstance(k, str) and isinstance(v, list)
    }
    if settings.display_mode not in ("under_pointer", "all"):
        settings.display_mode = defaults.display_mode
    if not DOUBLE_TAP_RANGE[0] <= settings.double_tap_ms <= DOUBLE_TAP_RANGE[1]:
        settings.double_tap_ms = defaults.double_tap_ms
    if settings.answer_text_size not in TEXT_SIZES:
        settings.answer_text_size = defaults.answer_text_size
    return settings


def save_settings(settings: Settings) -> None:
    """Atomic write: a crash never leaves a half-written file."""
    with _settings_lock:
        folder = ensure_private_dir(support_dir())
        fd, tmp = tempfile.mkstemp(prefix=".settings-", dir=folder)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(asdict(settings), f, indent=1)
            os.chmod(tmp, 0o600)
            os.replace(tmp, settings_path())
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise


# --- secrets ------------------------------------------------------------------


def _backend():
    from keyring.backends import macOS

    return macOS.Keyring()


def get_secret(account: str) -> str | None:
    return _backend().get_password(SERVICE, account)


def set_secret(account: str, value: str) -> None:
    _backend().set_password(SERVICE, account, value)


def delete_secret(account: str) -> None:
    """Delete the item; a missing item is fine. Any other failure (the user denied
    Keychain access, the keychain is locked) raises, so sign-out and "remove key"
    can never report success while the secret is still stored."""
    from keyring.backends.macOS import api
    from keyring.errors import PasswordDeleteError

    try:
        _backend().delete_password(SERVICE, account)
    except PasswordDeleteError as exc:
        if isinstance(exc.__cause__, api.NotFound):
            return
        raise


def mask_key(key: str) -> str:
    """As drawn in Figma: Anthropic 'sk-ant-' + bullets + the last four characters
    (3:101); Google 'AIza' + bullets (3:205)."""
    if not key:
        return ""
    if key.startswith("AIza"):
        return "AIza" + "•" * 24
    prefix = "sk-ant-" if key.startswith("sk-ant-") else ""
    return f"{prefix}{'•' * 20}{key[-4:]}"


class SecretCache:
    """In-memory copy of one secret so a capture doesn't hit the Keychain each time.
    load() runs off the main thread; set() updates both."""

    def __init__(self, account: str) -> None:
        self.account = account
        self._lock = threading.Lock()
        self._value: str | None = None
        self._loaded = False
        self._version = 0  # bumped by set(), so a slow read can't overwrite it

    def get(self) -> str | None:
        with self._lock:
            if self._loaded:
                return self._value
            version = self._version
        value = get_secret(self.account)  # may prompt after a rebuild; no lock held
        with self._lock:
            if self._version != version:
                return self._value  # set() ran during the read: its value wins
            self._value, self._loaded = value, True
        return value

    def set(self, value: str | None) -> None:
        if value:
            set_secret(self.account, value)
        else:
            delete_secret(self.account)
        with self._lock:
            self._version += 1
            self._value, self._loaded = (value or None), True
