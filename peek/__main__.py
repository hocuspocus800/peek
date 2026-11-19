"""Entry point: `python -m peek [--debug]`, or the py2app bundle's main script.

Launch the bundle with `open dist/Peek.app --args --debug` for debug mode;
macOS attributes permissions to the bundle only when it is started this way.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys

from peek import __version__
from peek.log import setup_logging
from peek.paths import support_dir
from peek.single_instance import InstanceLock

log = logging.getLogger("peek")


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="peek", description="Peek screen Q&A overlay")
    parser.add_argument(
        "--debug",
        action="store_true",
        default=os.environ.get("PEEK_DEBUG") == "1",
        help="verbose logging, Diagnostics menu, and (later) annotated debug captures",
    )
    # Used by "Quit & Reopen": wait for the old process to exit before taking the lock.
    parser.add_argument("--relaunch-of", type=int, default=None, help=argparse.SUPPRESS)
    # Developer self-test: N captures of the display under the pointer, logging
    # latency and memory, then quit. Writes no images, even with --debug.
    parser.add_argument("--self-test-captures", type=int, default=0, help=argparse.SUPPRESS)
    # Developer self-test: the test worksheet rendered offscreen -> OCR -> Claude,
    # answers logged, then quit. Needs a stored API key; costs one request.
    parser.add_argument("--self-test-solve", action="store_true", help=argparse.SUPPRESS)
    # Release check: import every provider module and SDK, print the result, quit.
    # No network, no window. Writes to the file given (stdout is not visible from
    # `open`).
    parser.add_argument("--check-imports", metavar="FILE", default=None, help=argparse.SUPPRESS)
    parser.add_argument("--version", action="version", version=f"Peek {__version__}")
    # LaunchServices and AppKit may append their own flags (-psn_…, -NS…); ignore them.
    args, _unknown = parser.parse_known_args(argv)
    return args


CHECKED_MODULES = (
    "peek.app",
    "peek.providers.claude",
    "peek.providers.gemini",
    "peek.providers.chatgpt",
    "peek.providers.oauth_chatgpt",
    "anthropic",
    "openai",
    "google.genai",
    "google.genai.types",
    "jwt",
    "cryptography.hazmat.primitives.asymmetric.rsa",
    "httpx2",
    "keyring.backends.macOS",
    "ServiceManagement",
    "PIL.ImageFilter",
)


def check_imports(path: str) -> int:
    import importlib
    import traceback

    lines, failed = [], 0
    for name in CHECKED_MODULES:
        try:
            importlib.import_module(name)
            lines.append(f"ok   {name}")
        except Exception:
            failed += 1
            lines.append(f"FAIL {name}\n{traceback.format_exc()}")
    lines.append(f"{len(CHECKED_MODULES) - failed}/{len(CHECKED_MODULES)} imported")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    return 1 if failed else 0


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    if args.check_imports:
        return check_imports(args.check_imports)

    # Lock before opening the log: a second instance must not write to (or rotate)
    # the running instance's log file.
    lock = InstanceLock(support_dir() / "peek.lock")
    if not lock.acquire(wait_for_pid=args.relaunch_of):
        print("Another Peek is already running; exiting.", file=sys.stderr)
        return 1

    log_file = setup_logging(args.debug)
    log.info("Logging to %s (debug=%s)", log_file, args.debug)

    from peek.app import run  # AppKit import deferred so --help works headless

    try:
        return run(
            debug=args.debug,
            self_test_captures=max(0, args.self_test_captures),
            self_test_solve=args.self_test_solve,
        )
    finally:
        lock.release()


if __name__ == "__main__":
    sys.exit(main())
