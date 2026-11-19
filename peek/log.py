"""Logging: a rotating file in ~/Library/Logs/Peek/ with secrets redacted.

Every handler formats through RedactingFormatter, so API keys, OAuth tokens and
JWTs are scrubbed from messages *and* tracebacks before anything is written.
"""

from __future__ import annotations

import functools
import logging
import logging.handlers
import os
import re
import sys
import threading
from pathlib import Path

from peek.paths import ensure_private_dir, log_dir

REDACTED = "[REDACTED]"

# Order matters: the specific key formats run before the generic key=value rule.
_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    # Anthropic keys (sk-ant-api03-...), OpenAI-style keys (sk-..., sk-proj-...).
    (re.compile(r"\bsk-ant-[A-Za-z0-9_\-]{8,}"), "sk-ant-" + REDACTED),
    (re.compile(r"\bsk-[A-Za-z0-9_\-]{16,}"), "sk-" + REDACTED),
    # Google API keys.
    (re.compile(r"\bAIza[0-9A-Za-z_\-]{20,}"), "AIza" + REDACTED),
    # JWTs (ChatGPT ID/access tokens): header.payload[.signature]
    (re.compile(r"\beyJ[A-Za-z0-9_\-]{5,}\.[A-Za-z0-9_\-]{5,}(?:\.[A-Za-z0-9_\-]*)?"), REDACTED),
    # Authorization: Bearer <anything>
    (re.compile(r"(?i)\b(bearer\s+)[A-Za-z0-9._~+/\-]+=*"), r"\1" + REDACTED),
    # OAuth authorization codes in callback URLs (?code=...). A bare "code" field
    # is left alone: provider error codes such as "rate_limit_exceeded" must stay readable.
    (re.compile(r"([?&]code=)[^&\s\"']+"), r"\1" + REDACTED),
]

# key=value, key: value and "key": "value" for secret-looking field names, in JSON
# (also JSON nested inside JSON strings, where quotes are backslash-escaped), query
# strings, headers and kwargs. A quoted value is redacted up to its closing quote;
# an unquoted one may start with an auth scheme ("Basic xyz"). Flags such as
# has_refresh_token=False and api_key=None stay readable.
_FIELD = re.compile(
    r"""(?ix)
    (?<![A-Za-z0-9_])
    ((?:\\*["'])?
      (?:x-api-key|x-goog-api-key|api[_-]?key|access[_-]?token|refresh[_-]?token|
         id[_-]?token|client[_-]?secret|code[_-]?verifier|password|authorization)
    (?:\\*["'])?\s*[:=]\s*)
    (?!(?:none|null|true|false)\b)
    (?:
        (?P<q>\\*["'])(?:(?!(?P=q)).)*?(?P=q)
      | (?:(?:basic|bearer|token|dpop)\s+)?[^\s"'&,}\\]+
    )
    """
)


def _field_sub(match: re.Match[str]) -> str:
    quote = match.group("q")
    if quote:
        return f"{match.group(1)}{quote}{REDACTED}{quote}"
    return match.group(1) + REDACTED


def redact(text: str) -> str:
    """Return *text* with anything that looks like a credential replaced."""
    for pattern, replacement in _PATTERNS:
        text = pattern.sub(replacement, text)
    return _FIELD.sub(_field_sub, text)


def entry_point(fn):
    """Decorator for every method AppKit calls (menu actions, delegate callbacks)
    and every AppHelper.callAfter target.

    PyObjC turns an exception raised there into an NSException that AppKit only
    NSLogs: unredacted, into the system log, and never into peek.log. This logs
    it through the redacting handlers instead and swallows it. The wrapper has no
    `return value`, so PyObjC keeps the selector's void signature (v@:@); do not
    use it on methods that must return something.
    """

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            fn(*args, **kwargs)
        except Exception:
            logging.getLogger("peek.crash").exception("Unhandled exception in %s", fn.__qualname__)

    return wrapper


class RedactingFormatter(logging.Formatter):
    """Formats normally (including exception text), then redacts the whole line."""

    def format(self, record: logging.LogRecord) -> str:
        return redact(super().format(record))


_FORMAT = "%(asctime)s %(levelname)-7s %(threadName)s %(name)s: %(message)s"


def setup_logging(debug: bool = False, *, directory: Path | None = None) -> Path:
    """Configure the root logger. Returns the log file path."""
    directory = ensure_private_dir(directory or log_dir())
    log_file = directory / "peek.log"

    formatter = RedactingFormatter(_FORMAT)
    file_handler = logging.handlers.RotatingFileHandler(
        log_file, maxBytes=1_000_000, backupCount=5, encoding="utf-8"
    )
    file_handler.setFormatter(formatter)
    os.chmod(log_file, 0o600)
    stderr_handler = logging.StreamHandler(sys.stderr)
    stderr_handler.setFormatter(formatter)

    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)
        handler.close()
    root.addHandler(file_handler)
    root.addHandler(stderr_handler)
    root.setLevel(logging.DEBUG if debug else logging.INFO)
    # HTTP clients and provider SDKs log request details (headers, base64 screenshots,
    # OCR text) at DEBUG; keep them quiet even with --debug.
    for noisy in (
        "httpx",
        "httpx2",
        "httpcore",
        "httpcore2",
        "urllib3",
        "anthropic",
        "openai",
        "google_genai",
        "google.genai",
        "PIL",
    ):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    _install_excepthooks()
    return log_file


def _install_excepthooks() -> None:
    log = logging.getLogger("peek.crash")

    def excepthook(exc_type, exc, tb):
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc, tb)
            return
        log.critical("Uncaught exception", exc_info=(exc_type, exc, tb))

    def thread_excepthook(args: threading.ExceptHookArgs) -> None:
        if args.exc_type is SystemExit:
            return
        log.critical(
            "Uncaught exception in thread %s",
            args.thread.name if args.thread else "?",
            exc_info=(args.exc_type, args.exc_value, args.exc_traceback),
        )

    sys.excepthook = excepthook
    threading.excepthook = thread_excepthook
