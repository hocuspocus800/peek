"""ScreenCaptureKit screenshots of one display, with Peek's own windows excluded.

`target_displays()` runs on the main thread (NSScreen, NSEvent). `capture()` runs
on the pipeline worker: ScreenCaptureKit's completion handlers arrive on its own
queues, and the worker blocks on an Event with a timeout until they do.

Screenshots live in memory only; `peek.debug` writes them to disk with --debug.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass

import objc
import Quartz
import ScreenCaptureKit as SCK
from AppKit import NSEvent, NSScreen

from peek.geometry import Rect, pick_frame

log = logging.getLogger(__name__)

TIMEOUT_S = 10.0
# SCStreamErrorUserDeclined: Screen Recording not granted to this app.
_SC_ERROR_USER_DECLINED = -3801


class CaptureError(Exception):
    def __init__(self, kind: str, message: str) -> None:
        super().__init__(message)
        self.kind = kind  # "permission" | "timeout" | "no_display" | "failed"


@dataclass(frozen=True)
class DisplayTarget:
    """A display chosen at the moment of the double-tap (main thread)."""

    display_id: int
    frame: Rect  # AppKit global points, bottom-left origin (may be negative)
    backing_scale: float  # NSScreen.backingScaleFactor; layout re-derives scale from pixels


@dataclass
class Capture:
    image: object  # CGImage; dropped once OCR and encoding are done
    width: int  # pixels
    height: int
    target: DisplayTarget
    content_rect: Rect  # points, from the filter
    point_pixel_scale: float
    excluded_by: str  # "application" | "windows" | "nothing"
    elapsed_ms: float

    @property
    def scale(self) -> float:
        """Image pixels per screen point, from the actual image (also right for
        scaled 'More Space' modes)."""
        return self.width / self.target.frame.w if self.target.frame.w else 1.0


def _screen_target(screen) -> DisplayTarget:
    f = screen.frame()
    return DisplayTarget(
        display_id=int(screen.deviceDescription()["NSScreenNumber"]),
        frame=Rect(f.origin.x, f.origin.y, f.size.width, f.size.height),
        backing_scale=float(screen.backingScaleFactor()),
    )


def target_displays(mode: str = "under_pointer") -> list[DisplayTarget]:
    """Main thread only. mode: 'under_pointer' (default) or 'all'."""
    screens = list(NSScreen.screens())
    if not screens:
        return []
    if mode == "all":
        return [_screen_target(s) for s in screens]
    p = NSEvent.mouseLocation()
    frames = [_screen_target(s).frame for s in screens]
    return [_screen_target(screens[pick_frame(frames, p.x, p.y)])]


def _wait(start, what: str):
    """Call start(handler) and block until the handler runs; returns its object."""
    done = threading.Event()
    box: dict = {}

    def handler(obj, error) -> None:
        box["obj"], box["error"] = obj, error
        done.set()

    start(handler)
    if not done.wait(TIMEOUT_S):
        # A late handler still fills `box`, but nothing reads it any more.
        raise CaptureError(
            "timeout",
            f"{what} did not answer within {TIMEOUT_S:.0f} s "
            "(a macOS screen-recording prompt may be open)",
        )
    error = box.get("error")
    if error is not None:
        code = int(error.code())
        if code == _SC_ERROR_USER_DECLINED:
            raise CaptureError("permission", "Screen Recording is not granted to Peek")
        raise CaptureError("failed", f"{what} failed: {error.localizedDescription()} ({code})")
    if box.get("obj") is None:
        raise CaptureError("failed", f"{what} returned nothing")
    return box["obj"]


def capture(target: DisplayTarget) -> Capture:
    """Screenshot one display, excluding every window of this process."""
    started = time.monotonic()
    with objc.autorelease_pool():
        content = _wait(
            lambda h: (
                SCK.SCShareableContent.getShareableContentExcludingDesktopWindows_onScreenWindowsOnly_completionHandler_(
                    False, True, h
                )
            ),
            "SCShareableContent",
        )
        display = next(
            (d for d in content.displays() if int(d.displayID()) == target.display_id), None
        )
        if display is None:
            raise CaptureError("no_display", f"display {target.display_id} is not shareable")

        pid = os.getpid()
        me = [a for a in content.applications() if int(a.processID()) == pid]
        if me:
            content_filter = (
                SCK.SCContentFilter.alloc().initWithDisplay_excludingApplications_exceptingWindows_(
                    display, me, []
                )
            )
            excluded_by = "application"
        else:
            # An accessory app may be missing from the application list: exclude
            # its windows one by one instead.
            mine = [
                w
                for w in content.windows()
                if w.owningApplication() is not None
                and int(w.owningApplication().processID()) == pid
            ]
            content_filter = SCK.SCContentFilter.alloc().initWithDisplay_excludingWindows_(
                display, mine
            )
            excluded_by = "windows" if mine else "nothing"

        rect = content_filter.contentRect()
        scale = float(content_filter.pointPixelScale())
        width = round(rect.size.width * scale)
        height = round(rect.size.height * scale)
        config = SCK.SCStreamConfiguration.alloc().init()
        config.setWidth_(width)
        config.setHeight_(height)
        config.setShowsCursor_(False)
        config.setCaptureResolution_(SCK.SCCaptureResolutionBest)

        image = _wait(
            lambda h: (
                SCK.SCScreenshotManager.captureImageWithFilter_configuration_completionHandler_(
                    content_filter, config, h
                )
            ),
            "SCScreenshotManager",
        )
    return Capture(
        image=image,
        width=int(Quartz.CGImageGetWidth(image)),
        height=int(Quartz.CGImageGetHeight(image)),
        target=target,
        content_rect=Rect(rect.origin.x, rect.origin.y, rect.size.width, rect.size.height),
        point_pixel_scale=scale,
        excluded_by=excluded_by,
        elapsed_ms=(time.monotonic() - started) * 1000,
    )
