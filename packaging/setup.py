"""py2app configuration for Peek.app. Do not run directly; use scripts/build.sh.

This file lives in packaging/ on purpose: py2app refuses a distribution that
declares install_requires, and setuptools would derive those from the
pyproject.toml if this file sat next to it.
"""

import os
import platform
import sys

from setuptools import setup

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from peek import APP_NAME, BUNDLE_ID, __version__  # noqa: E402


def _patch_builtin_zlib() -> None:
    """py2app #555: a full build copies zlib.__file__ into the bundle, but uv's
    python-build-standalone interpreters compile zlib in, so the attribute does
    not exist. Nothing needs copying in that case: give zlib a sentinel path and
    make py2app's copy_file skip it."""
    import zlib

    if hasattr(zlib, "__file__"):
        return
    from py2app.build_app import py2app as py2app_command

    sentinel = "<builtin zlib>"
    zlib.__file__ = sentinel
    original = py2app_command.copy_file

    def copy_file(self, src, *args, **kwargs):
        if src == sentinel:
            return (src, 0)
        return original(self, src, *args, **kwargs)

    py2app_command.copy_file = copy_file


_patch_builtin_zlib()

PLIST = {
    "CFBundleName": APP_NAME,
    "CFBundleDisplayName": APP_NAME,
    "CFBundleExecutable": APP_NAME,
    "CFBundleIdentifier": BUNDLE_ID,
    "CFBundleShortVersionString": __version__,
    "CFBundleVersion": __version__,
    "LSUIElement": True,  # menu-bar app: no Dock icon, no app menu bar
    "LSMinimumSystemVersion": "14.0",
    "LSApplicationCategoryType": "public.app-category.utilities",
    "NSHighResolutionCapable": True,
}

OPTIONS = {
    "argv_emulation": False,  # pass `open --args --debug` through untouched
    "plist": PLIST,
    # The menu-bar eye on a transparent background (scripts/make-icon.py).
    "iconfile": os.path.join(HERE, "Peek.icns"),
    # The provider SDKs import parts of themselves lazily, which py2app's import
    # scan can miss: bundle them whole.
    "packages": [
        "peek",
        "anthropic",
        "openai",
        "jwt",
        "cryptography",
        "httpx2",
        "pydantic",
        "certifi",
    ],
    # keyring finds backends through entry points, which the bundle may lack;
    # store.py imports the macOS backend directly, listed here so it is bundled.
    # google.genai lives in the "google" namespace package, which "packages" cannot
    # take; included by module instead.
    # _cffi_backend: cryptography (ID-token checks) needs it; py2app misses it.
    "includes": ["keyring.backends.macOS", "google.genai", "google.genai.types", "_cffi_backend"],
    "excludes": ["tkinter", "_tkinter", "Tkinter", "test", "unittest", "pytest", "ruff"],
    "dylib_excludes": ["Tcl.framework", "Tk.framework"],
    # One architecture per build (py2app cannot merge them). Override with PEEK_ARCH.
    "arch": os.environ.get("PEEK_ARCH", platform.machine()),
}

setup(
    name=APP_NAME,
    version=__version__,
    app=[os.path.join(HERE, "peek_main.py")],
    options={"py2app": OPTIONS},
)
