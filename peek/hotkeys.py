"""Double-tap detection for Control and Command: a pure state machine.

No AppKit or Quartz here; `peek.event_tap` feeds it real events. Every event
carries the full modifier flag word, so the set of held keys is recomputed from
each event instead of being tracked incrementally (which drifts after a missed
key-up).

Definitions (spec, "Hotkeys"):
- A *tap* is one Control (or Command) key going down while no other modifier is
  held, and coming back up within `tap_max_ms` (250 ms) with nothing else in
  between: no other key, no other modifier, no mouse button, no scroll while held.
- A *double-tap* is two taps of the same family (left or right key, mixed freely)
  where the gap from the first key-up to the second key-down is at most `gap_ms`
  (default 300 ms, Settings 200-500 ms). It fires on the second key-up, so
  "tap, then Ctrl+C" never fires.
- After firing, key-downs of the same family within `gap_ms` are ignored, so a
  triple tap fires once.
- Fail closed: if the system's own input counters show a key press or click that
  this process did not see (for example while another app holds secure input, or
  inside Cmd+Tab), the gesture is vetoed.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

# --- modifier flag bits (IOKit IOLLEvent.h / CGEventFlags) --------------------

SHIFT = 0x00020000
CONTROL = 0x00040000
OPTION = 0x00080000
COMMAND = 0x00100000
GENERIC_MASK = SHIFT | CONTROL | OPTION | COMMAND
# Caps Lock (0x10000), NumericPad, Help, SecondaryFn and NonCoalesced are ignored.

# Device-dependent bits: which physical key is down.
DEVICE_BITS = {
    "lctrl": 0x00000001,
    "lshift": 0x00000002,
    "rshift": 0x00000004,
    "lcmd": 0x00000008,
    "rcmd": 0x00000010,
    "lopt": 0x00000020,
    "ropt": 0x00000040,
    "rctrl": 0x00002000,
}
GENERIC_FOR_KEY = {
    "lctrl": CONTROL,
    "rctrl": CONTROL,
    "lcmd": COMMAND,
    "rcmd": COMMAND,
    "lshift": SHIFT,
    "rshift": SHIFT,
    "lopt": OPTION,
    "ropt": OPTION,
}
# Synthetic events (remote desktop, automation) may set only the generic bit.
GENERIC_ONLY_KEY = {CONTROL: "ctrl", COMMAND: "cmd", SHIFT: "shift", OPTION: "opt"}

KEYCODE_CAPS_LOCK = 57
KEYCODE_FN = 63
# Shift, Control, Option and Command keycodes, left and right.
MODIFIER_KEYCODES = frozenset({54, 55, 56, 58, 59, 60, 61, 62})


class Family(Enum):
    CONTROL = "control"
    COMMAND = "command"


_FAMILY_OF_KEY = {
    "lctrl": Family.CONTROL,
    "rctrl": Family.CONTROL,
    "ctrl": Family.CONTROL,
    "lcmd": Family.COMMAND,
    "rcmd": Family.COMMAND,
    "cmd": Family.COMMAND,
}


class EventKind(Enum):
    FLAGS_CHANGED = "flags"
    KEY_DOWN = "key"  # any non-modifier key; its keycode is never read
    MOUSE_DOWN = "mouse"  # left, right or other button
    SCROLL = "scroll"  # finger or wheel scrolling (momentum is filtered out upstream)
    SYSTEM = "system"  # media, brightness and other system-defined keys


@dataclass(frozen=True)
class Event:
    kind: EventKind
    t_ns: int
    keycode: int = 0  # only meaningful for FLAGS_CHANGED
    flags: int = 0
    # System-wide count of key-downs + mouse-button-downs at this moment (HID level),
    # or None when unavailable. Used as an independent witness of "other input".
    input_count: int | None = None


def held_keys(flags: int) -> frozenset[str]:
    """Physical modifier keys held according to one event's flag word."""
    keys = {name for name, bit in DEVICE_BITS.items() if flags & bit}
    for generic, name in GENERIC_ONLY_KEY.items():
        if flags & generic and not any(GENERIC_FOR_KEY[k] == generic for k in keys):
            keys.add(name)
    return frozenset(keys)


class _State(Enum):
    IDLE = 0
    DOWN1 = 1  # first tap's key is down
    WAIT2 = 2  # first tap done, waiting for the second key-down
    DOWN2 = 3  # second tap's key is down


class DoubleTapDetector:
    def __init__(self, gap_ms: int = 300, tap_max_ms: int = 250) -> None:
        self.gap_ms = gap_ms
        self.tap_max_ms = tap_max_ms
        self._held: frozenset[str] = frozenset()
        self._last_t: int | None = None
        self._last_fire: int | None = None
        self._last_fire_family: Family | None = None
        self._to_idle()

    # --- public -------------------------------------------------------------

    def reset(self, flags: int = 0) -> None:
        """Forget any gesture in progress, e.g. after the tap was disabled or the
        Mac woke up (key-ups may have been lost). *flags* is the current modifier
        state, so a key that is still held is known to be held."""
        self._held = held_keys(flags)
        self._last_t = None
        self._last_fire = None
        self._last_fire_family = None
        self._to_idle()

    def feed(self, event: Event) -> Family | None:
        """Process one event; returns the family when a double-tap completes."""
        if self._last_t is not None and event.t_ns < self._last_t:
            # Clock went backwards (should not happen); start over safely.
            self.reset(event.flags)
            return None
        self._last_t = event.t_ns

        if event.kind is EventKind.SCROLL:
            # Ctrl+scroll (screen zoom) and Cmd+scroll are shortcuts, so scrolling
            # while the key is held cancels. A finger scroll in the gap between taps
            # does not. (Momentum events never get here: event_tap drops them.)
            if self._state in (_State.DOWN1, _State.DOWN2):
                self._to_idle()
            return None
        if event.kind is not EventKind.FLAGS_CHANGED:
            self._to_idle()
            return None
        if event.keycode in (KEYCODE_CAPS_LOCK, KEYCODE_FN):
            return None  # toggling Caps Lock or tapping Fn is not "other input"

        previous, current = self._held, held_keys(event.flags)
        self._held = current
        pressed, released = current - previous, previous - current
        if not pressed and not released:
            if event.keycode in MODIFIER_KEYCODES and self._state is not _State.IDLE:
                # A modifier key moved without changing the held set: with
                # generic-only flags, the other key of an already-held family.
                self._to_idle()
            return None

        if pressed and not released:
            (key,) = pressed if len(pressed) == 1 else (None,)
            family = _FAMILY_OF_KEY.get(key) if key else None
            if family is not None and current == {key}:
                self._key_down(family, key, event)
            else:
                self._to_idle()  # another modifier, or two keys at once
            return None

        if released and not pressed and released == {self._key} and not current:
            return self._key_up(event)

        self._to_idle()
        return None

    # --- transitions --------------------------------------------------------

    def _to_idle(self) -> None:
        self._state = _State.IDLE
        self._family: Family | None = None
        self._key: str | None = None
        self._t_down = 0
        self._t_up = 0
        self._count_at_start: int | None = None

    def _key_down(self, family: Family, key: str, event: Event) -> None:
        t = event.t_ns
        gap_ns = self.gap_ms * 1_000_000
        if self._state is _State.WAIT2 and family is self._family and t - self._t_up <= gap_ns:
            self._state, self._key, self._t_down = _State.DOWN2, key, t
            return
        if (
            self._last_fire is not None
            and family is self._last_fire_family
            and 0 <= t - self._last_fire < gap_ns
        ):
            self._to_idle()  # the tail of a triple tap
            return
        self._to_idle()
        self._state, self._family, self._key, self._t_down = _State.DOWN1, family, key, t
        self._count_at_start = event.input_count

    def _key_up(self, event: Event) -> Family | None:
        t = event.t_ns
        held_ok = t - self._t_down <= self.tap_max_ms * 1_000_000
        if self._state is _State.DOWN1 and held_ok:
            self._state, self._t_up, self._key = _State.WAIT2, t, None
            return None
        if self._state is _State.DOWN2 and held_ok:
            family = self._family
            witnessed_other_input = (
                self._count_at_start is not None
                and event.input_count is not None
                and event.input_count != self._count_at_start
            )
            self._to_idle()
            if witnessed_other_input:
                return None
            self._last_fire, self._last_fire_family = t, family
            return family
        self._to_idle()
        return None
