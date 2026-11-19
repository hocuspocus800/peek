"""Screen Recording and Input Monitoring: checks, requests, System Settings links,
and relaunching Peek after a grant that only takes effect in a new process.
"""

from __future__ import annotations

import ctypes
import logging
import os
import subprocess

import Quartz
from AppKit import NSURL, NSApplication, NSWorkspace
from Foundation import NSBundle

log = logging.getLogger(__name__)

SCREEN_RECORDING_URL = (
    "x-apple.systempreferences:com.apple.preference.security?Privacy_ScreenCapture"
)
INPUT_MONITORING_URL = "x-apple.systempreferences:com.apple.preference.security?Privacy_ListenEvent"

# IOKit/hid/IOHIDLib.h. Not bound by PyObjC, so called through ctypes.
_K_IOHID_REQUEST_TYPE_LISTEN_EVENT = 1
_IOHID_ACCESS = {0: "granted", 1: "denied", 2: "unknown"}
_iokit = ctypes.CDLL("/System/Library/Frameworks/IOKit.framework/IOKit")
_iokit.IOHIDCheckAccess.restype = ctypes.c_uint32
_iokit.IOHIDCheckAccess.argtypes = [ctypes.c_uint32]
_iokit.IOHIDRequestAccess.restype = ctypes.c_bool
_iokit.IOHIDRequestAccess.argtypes = [ctypes.c_uint32]


def screen_recording_granted() -> bool:
    return bool(Quartz.CGPreflightScreenCaptureAccess())


def request_screen_recording() -> bool:
    """Shows the system prompt the first time; afterwards it only returns the state.
    A grant usually takes effect only after Peek is relaunched."""
    return bool(Quartz.CGRequestScreenCaptureAccess())


def input_monitoring_granted() -> bool:
    return bool(Quartz.CGPreflightListenEventAccess())


def input_monitoring_state() -> str:
    """IOHIDCheckAccess for listen events: 'granted', 'denied' or 'unknown'
    (never asked). More detailed than the boolean preflight."""
    return _IOHID_ACCESS.get(_iokit.IOHIDCheckAccess(_K_IOHID_REQUEST_TYPE_LISTEN_EVENT), "unknown")


def request_input_monitoring() -> bool:
    """Ask for Input Monitoring. IOHIDRequestAccess is what adds Peek to the list
    in System Settings (CGRequestListenEventAccess alone did not always, per Apple
    DTS); both are called. Returns whether access is granted now."""
    granted = bool(_iokit.IOHIDRequestAccess(_K_IOHID_REQUEST_TYPE_LISTEN_EVENT))
    granted = bool(Quartz.CGRequestListenEventAccess()) or granted
    log.info("Input Monitoring requested: %s (%s)", granted, input_monitoring_state())
    return granted


def open_system_settings(url: str) -> None:
    NSWorkspace.sharedWorkspace().openURL_(NSURL.URLWithString_(url))


def bundle_path() -> str | None:
    """Path of Peek.app, or None when running unbundled (python -m peek)."""
    path = NSBundle.mainBundle().bundlePath()
    return path if path and path.endswith(".app") else None


def relaunch(*, debug: bool) -> bool:
    """Start a new instance and quit this one. The new instance waits for this
    process to exit before taking the single-instance lock (--relaunch-of)."""
    app = bundle_path()
    if app is None:
        log.warning("Relaunch is only possible from Peek.app")
        return False
    args = ["/usr/bin/open", "-n", app, "--args", "--relaunch-of", str(os.getpid())]
    if debug:
        args.append("--debug")
    log.info("Relaunching Peek")
    subprocess.Popen(args, stdin=subprocess.DEVNULL, start_new_session=True)
    NSApplication.sharedApplication().terminate_(None)
    return True
