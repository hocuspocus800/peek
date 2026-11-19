"""Pure helpers for the strings shown in the menu (no AppKit, unit-tested)."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class IconState(Enum):
    """The four menu-bar icon states drawn in Figma frame 3:35."""

    IDLE = "idle"
    WORKING = "working"
    READY = "ready"  # answers exist (hidden or shown): blue dot
    ERROR = "error"  # red dot


class DotColor(Enum):
    """Status dot in the menu header (Figma Status component)."""

    SUCCESS = "success"  # #28B04A
    DANGER = "danger"  # #E5392F
    NEUTRAL = "neutral"  # #AEAEB2 ("Not set")


@dataclass(frozen=True)
class HeaderStatus:
    text: str
    dot: DotColor


def plural(n: int, singular: str, plural_form: str | None = None) -> str:
    return f"{n} {singular if n == 1 else (plural_form or singular + 's')}"


def time_ago(seconds: float) -> str:
    """Short relative time, matching the Figma copy "2 min ago"."""
    s = max(0, int(seconds))
    if s < 60:
        return "just now"
    minutes = s // 60
    if minutes < 60:
        return f"{minutes} min ago"
    hours = minutes // 60
    if hours < 24:
        return f"{hours} hr ago"
    return plural(hours // 24, "day") + " ago"


def last_capture_line(question_count: int, seconds_ago: float) -> str:
    """'Last capture: 4 questions · 2 min ago' (Figma frame 3:3)."""
    return f"Last capture: {plural(question_count, 'question')} · {time_ago(seconds_ago)}"


def ready_status(model_display_name: str) -> HeaderStatus:
    """'Ready · Claude Sonnet 5.5' with a green dot."""
    return HeaderStatus(f"Ready · {model_display_name}", DotColor.SUCCESS)
