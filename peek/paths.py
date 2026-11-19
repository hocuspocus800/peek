"""Filesystem locations. Nothing here touches AppKit, so tests can import it."""

from __future__ import annotations

import os
from pathlib import Path

from peek import APP_NAME


def _home() -> Path:
    return Path(os.environ.get("PEEK_HOME_OVERRIDE") or Path.home())


def support_dir() -> Path:
    """~/Library/Application Support/Peek (settings JSON, lock file, debug output)."""
    return _home() / "Library" / "Application Support" / APP_NAME


def log_dir() -> Path:
    """~/Library/Logs/Peek (rotating log files)."""
    return _home() / "Library" / "Logs" / APP_NAME


def debug_dir() -> Path:
    """Where --debug writes captures and annotated PNGs. Never used without --debug."""
    return support_dir() / "debug"


def ensure_private_dir(path: Path) -> Path:
    """Create *path* (and parents) readable only by the current user."""
    path.mkdir(parents=True, exist_ok=True)
    os.chmod(path, 0o700)
    return path
