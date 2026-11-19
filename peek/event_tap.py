"""The listen-only event tap that feeds `peek.hotkeys.DoubleTapDetector`.

Runs on its own thread with its own CFRunLoop, so a busy main thread (layout,
Settings, a long AppKit call) can never make macOS disable the tap for being slow.
The callback does almost nothing: read type, keycode (modifier keys only), flags
and timestamp, feed the detector, and hand a completed double-tap to the main
thread. It returns None: legal for listen-only taps, and PyObjC 12.1-13.0 leaks
one CGEvent per call when the event is returned.

Create the tap only after Input Monitoring is granted. A tap created earlier gets
its keyboard event bits stripped and would see modifiers but not other keys, which
would turn Ctrl+C, Ctrl+C into a "double-tap".
"""

from __future__ import annotations

import ctypes
import logging
import os
import threading
import time
from collections.abc import Callable

import objc
import Quartz
from PyObjCTools import AppHelper

from peek.hotkeys import DoubleTapDetector, Event, EventKind, Family

log = logging.getLogger(__name__)

_MOUSE_DOWN = {
    Quartz.kCGEventLeftMouseDown,
    Quartz.kCGEventRightMouseDown,
    Quartz.kCGEventOtherMouseDown,
}
# NX_SYSDEFINED: media, brightness and other system keys (Ctrl+Volume is input
# too). Not part of the public CGEventType enum; same value as NSEventTypeSystemDefined.
_SYSTEM_DEFINED = 14
_WATCHED = [
    Quartz.kCGEventFlagsChanged,
    Quartz.kCGEventKeyDown,
    *_MOUSE_DOWN,
    Quartz.kCGEventScrollWheel,
    _SYSTEM_DEFINED,
]
EVENT_MASK = 0
for _t in _WATCHED:
    EVENT_MASK |= 1 << _t
_WITNESS_TYPES = [Quartz.kCGEventKeyDown, *_MOUSE_DOWN]
_DISABLED = {Quartz.kCGEventTapDisabledByTimeout, Quartz.kCGEventTapDisabledByUserInput}

_carbon = ctypes.CDLL("/System/Library/Frameworks/Carbon.framework/Carbon")
_carbon.IsSecureEventInputEnabled.restype = ctypes.c_ubyte
_libsystem = ctypes.CDLL("/usr/lib/libSystem.B.dylib")
_libsystem.mach_absolute_time.restype = ctypes.c_uint64


class _Timebase(ctypes.Structure):
    _fields_ = [("numer", ctypes.c_uint32), ("denom", ctypes.c_uint32)]


_TIMEBASE = _Timebase()
_libsystem.mach_timebase_info(ctypes.byref(_TIMEBASE))


def secure_input_enabled() -> bool:
    """True while some app (a password field, Terminal's Secure Keyboard Entry)
    hides keystrokes from event taps. Hotkeys must not fire then."""
    return bool(_carbon.IsSecureEventInputEnabled())


def hid_input_count() -> int:
    """System-wide key-down + mouse-button-down count since login (HID level)."""
    state = Quartz.kCGEventSourceStateHIDSystemState
    return sum(Quartz.CGEventSourceCounterForEventType(state, t) for t in _WITNESS_TYPES)


class EventClock:
    """CGEventGetTimestamp is documented as nanoseconds since boot but is reported
    to be mach_absolute_time ticks on some systems (Apple Silicon: 1 tick = 41.67 ns).
    Decide once, from the first real event, by comparing with both clocks."""

    def __init__(self) -> None:
        self.ticks: bool | None = None

    def to_ns(self, ts: int) -> int:
        if ts == 0:  # synthetic events may carry no timestamp
            return time.clock_gettime_ns(time.CLOCK_UPTIME_RAW)
        if self.ticks is None:
            now_ticks = _libsystem.mach_absolute_time()
            now_ns = time.clock_gettime_ns(time.CLOCK_UPTIME_RAW)
            self.ticks = abs(ts - now_ticks) < abs(ts - now_ns)
            log.info("Event timestamps are %s", "mach ticks" if self.ticks else "nanoseconds")
        if self.ticks:
            return ts * _TIMEBASE.numer // _TIMEBASE.denom
        return ts


def translate(event_type: int, t_ns: int, *, momentum_phase: int = 0) -> Event | None:
    """Map a non-flagsChanged CGEvent type to a detector event (None = ignore)."""
    if event_type == Quartz.kCGEventKeyDown:
        return Event(EventKind.KEY_DOWN, t_ns)  # which key is never read
    if event_type == Quartz.kCGEventScrollWheel:
        if momentum_phase != 0:
            # The inertial tail of an earlier trackpad flick keeps arriving every
            # ~16 ms for a second or more; it is not the user scrolling now.
            return None
        return Event(EventKind.SCROLL, t_ns)
    if event_type == _SYSTEM_DEFINED:
        return Event(EventKind.SYSTEM, t_ns)
    if event_type in _MOUSE_DOWN:
        return Event(EventKind.MOUSE_DOWN, t_ns)
    return None


class HotkeyTap:
    """Owns the tap thread. Public methods are called from the main thread.

    *on_started(created, keyboard_events_granted)* is called on the main thread once
    the tap thread knows whether the tap exists (creation is a round trip to the
    window server and can be slow at login)."""

    def __init__(
        self,
        on_double_tap: Callable[[Family], None],
        on_started: Callable[[bool, bool | None], None],
        gap_ms: int = 300,
    ) -> None:
        self._on_double_tap = on_double_tap
        self._on_started = on_started
        self.detector = DoubleTapDetector(gap_ms=gap_ms)
        self._clock = EventClock()
        self._thread: threading.Thread | None = None
        self._runloop = None
        self._tap = None
        # Status for the Diagnostics menu (read on the main thread; plain values only).
        self.created = False
        self.keyboard_events_granted: bool | None = None
        self.disabled_count = 0
        self.events_seen = 0
        self.vetoed_secure_input = 0

    # --- main thread --------------------------------------------------------

    def start(self) -> None:
        """Create the tap on its own thread; the result arrives via on_started."""
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, name="hotkey-tap", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        if self._runloop is not None:
            Quartz.CFRunLoopStop(self._runloop)

    def set_gap_ms(self, gap_ms: int) -> None:
        self.detector.gap_ms = gap_ms

    def request_reset(self) -> None:
        """Forget any gesture in progress (after wake). Runs on the tap thread right
        away, so the detector is only ever touched by one thread and the reset
        cannot land on the first key press after wake."""
        runloop = self._runloop
        if runloop is None:
            return

        def reset() -> None:
            self.detector.reset(self._current_flags())

        Quartz.CFRunLoopPerformBlock(runloop, Quartz.kCFRunLoopCommonModes, reset)
        Quartz.CFRunLoopWakeUp(runloop)

    @property
    def enabled(self) -> bool:
        return bool(self._tap is not None and Quartz.CGEventTapIsEnabled(self._tap))

    # --- tap thread ---------------------------------------------------------

    def _run(self) -> None:
        with objc.autorelease_pool():
            tap = Quartz.CGEventTapCreate(
                Quartz.kCGSessionEventTap,
                Quartz.kCGHeadInsertEventTap,
                Quartz.kCGEventTapOptionListenOnly,
                EVENT_MASK,
                self._callback,
                None,
            )
            if tap is None:
                log.warning("CGEventTapCreate returned NULL (Input Monitoring not granted?)")
                AppHelper.callAfter(self._on_started, False, None)
                return
            self._tap = tap
            source = Quartz.CFMachPortCreateRunLoopSource(None, tap, 0)
            self._runloop = Quartz.CFRunLoopGetCurrent()
            Quartz.CFRunLoopAddSource(self._runloop, source, Quartz.kCFRunLoopCommonModes)
            Quartz.CGEventTapEnable(tap, True)
            self.detector.reset(self._current_flags())
            self.keyboard_events_granted = _tap_receives_key_downs()
            self.created = True
            log.info(
                "Hotkey tap running (keyboard events %s)",
                describe_grant(self.keyboard_events_granted),
            )
            AppHelper.callAfter(self._on_started, True, self.keyboard_events_granted)
        Quartz.CFRunLoopRun()
        log.info("Hotkey tap stopped")

    @staticmethod
    def _current_flags() -> int:
        return int(Quartz.CGEventSourceFlagsState(Quartz.kCGEventSourceStateCombinedSessionState))

    def _callback(self, _proxy, event_type, event, _refcon):
        with objc.autorelease_pool():
            try:
                self._handle(event_type, event)
            except Exception:
                log.exception("Hotkey tap callback failed")
        return None

    def _handle(self, event_type, event) -> None:
        if event_type in _DISABLED:
            self.disabled_count += 1
            log.warning(
                "Hotkey tap disabled by %s; re-enabling",
                "timeout" if event_type == Quartz.kCGEventTapDisabledByTimeout else "user input",
            )
            Quartz.CGEventTapEnable(self._tap, True)
            self.detector.reset(self._current_flags())  # key-ups may have been lost
            return

        self.events_seen += 1
        t_ns = self._clock.to_ns(Quartz.CGEventGetTimestamp(event))
        if event_type == Quartz.kCGEventFlagsChanged:
            ev = Event(
                EventKind.FLAGS_CHANGED,
                t_ns,
                keycode=int(
                    Quartz.CGEventGetIntegerValueField(event, Quartz.kCGKeyboardEventKeycode)
                ),
                flags=int(Quartz.CGEventGetFlags(event)),
                input_count=hid_input_count(),
            )
        else:
            momentum = 0
            if event_type == Quartz.kCGEventScrollWheel:
                momentum = int(
                    Quartz.CGEventGetIntegerValueField(
                        event, Quartz.kCGScrollWheelEventMomentumPhase
                    )
                )
            ev = translate(int(event_type), t_ns, momentum_phase=momentum)
            if ev is None:
                return

        family = self.detector.feed(ev)
        if family is None:
            return
        if secure_input_enabled():
            # Secure input hides key presses from us, so "Ctrl+C, Ctrl+C" in a
            # password field or secure Terminal would look like a double-tap.
            self.vetoed_secure_input += 1
            log.info("Double-tap of %s ignored: secure input is on", family.value)
            return
        AppHelper.callAfter(self._on_double_tap, family)


def describe_grant(granted: bool | None) -> str:
    return {True: "granted", False: "NOT granted", None: "unknown"}[granted]


def _tap_receives_key_downs() -> bool | None:
    """Was keyDown left in this process's tap mask? macOS strips keyboard events
    from taps created without Input Monitoring. None if it cannot be determined."""
    try:
        err, _taps, count = Quartz.CGGetEventTapList(0, None, None)
        if err != 0:
            return None
        err, taps, _count = Quartz.CGGetEventTapList(max(count, 1) + 8, None, None)
    except Exception:
        log.exception("CGGetEventTapList failed")
        return None
    if err != 0:
        return None
    pid = os.getpid()
    mine = [t for t in taps if t.tappingProcess == pid]
    if not mine:
        return None
    return any(t.eventsOfInterest & (1 << Quartz.kCGEventKeyDown) for t in mine)
