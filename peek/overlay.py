"""The answers overlay: one transparent, click-through panel per display.

Window setup: a non-activating NSPanel at screen-saver
level, on every Space and over full-screen apps, ignoring the mouse, never key,
never in the window cycle. sharingType None is set, but nothing relies on it:
the panels are ordered out before every capture and the capture filter excludes
Peek's windows.

Drawing is plain text only (owner's style): no background, no outline, no shadow.
Main thread only.
"""

from __future__ import annotations

import logging

import objc
from AppKit import (
    NSAttributedString,
    NSBackingStoreBuffered,
    NSColor,
    NSFont,
    NSFontAttributeName,
    NSForegroundColorAttributeName,
    NSMakePoint,
    NSMakeRect,
    NSMakeSize,
    NSPanel,
    NSScreen,
    NSScreenSaverWindowLevel,
    NSStringDrawingUsesDeviceMetrics,
    NSStringDrawingUsesLineFragmentOrigin,
    NSView,
    NSWindowAnimationBehaviorNone,
    NSWindowCollectionBehaviorCanJoinAllApplications,
    NSWindowCollectionBehaviorCanJoinAllSpaces,
    NSWindowCollectionBehaviorFullScreenAuxiliary,
    NSWindowCollectionBehaviorIgnoresCycle,
    NSWindowCollectionBehaviorStationary,
    NSWindowSharingNone,
    NSWindowStyleMaskBorderless,
    NSWindowStyleMaskNonactivatingPanel,
)

from peek.layout import DrawItem

log = logging.getLogger(__name__)


def _font(size: float) -> NSFont:
    return NSFont.systemFontOfSize_(size)


def _attributed(item_text: str, size: float, rgb) -> NSAttributedString:
    color = NSColor.colorWithSRGBRed_green_blue_alpha_(
        rgb[0] / 255, rgb[1] / 255, rgb[2] / 255, 1.0
    )
    return NSAttributedString.alloc().initWithString_attributes_(
        item_text, {NSFontAttributeName: _font(size), NSForegroundColorAttributeName: color}
    )


def ink_height(text: str) -> float:
    """Tight glyph height of *text* in the system font, per point of font size."""
    size = 100.0
    bounds = (
        NSAttributedString.alloc()
        .initWithString_attributes_(text, {NSFontAttributeName: _font(size)})
        .boundingRectWithSize_options_(
            NSMakeSize(1e6, 1e6),
            NSStringDrawingUsesDeviceMetrics | NSStringDrawingUsesLineFragmentOrigin,
        )
    )
    return float(bounds.size.height) / size


def measure(text: str, font_size: float) -> tuple[float, float]:
    """The layout's text measurer: the same font the overlay draws with."""
    size = (
        NSAttributedString.alloc()
        .initWithString_attributes_(text, {NSFontAttributeName: _font(font_size)})
        .size()
    )
    return float(size.width), float(size.height)


class OverlayView(NSView):
    def initWithFrame_(self, frame):
        self = objc.super(OverlayView, self).initWithFrame_(frame)
        if self is None:
            return None
        self.items: list[DrawItem] = []
        return self

    def isFlipped(self):
        return True  # layout works top-left, like the capture

    def isOpaque(self):
        return False

    def drawRect_(self, _rect):
        for item in self.items:
            # A wrapped answer: one line per row, spaced as the layout measured them.
            lines = item.text.split("\n")
            step = item.rect.h / len(lines)
            for i, line in enumerate(lines):
                _attributed(line, item.font_size, item.rgb).drawAtPoint_(
                    NSMakePoint(item.rect.x, item.rect.y + i * step)
                )


class OverlayPanel(NSPanel):
    def canBecomeKeyWindow(self):
        return False

    def canBecomeMainWindow(self):
        return False


def _screen_id(screen) -> int:
    return int(screen.deviceDescription()["NSScreenNumber"])


def _make_panel(screen) -> OverlayPanel:
    frame = screen.frame()
    panel = OverlayPanel.alloc().initWithContentRect_styleMask_backing_defer_(
        frame,
        NSWindowStyleMaskBorderless | NSWindowStyleMaskNonactivatingPanel,
        NSBackingStoreBuffered,
        False,
    )
    # The rect passed above is screen-relative for some initialisers; set the
    # global frame explicitly so displays with negative origins land right.
    panel.setFrame_display_(frame, False)
    panel.setOpaque_(False)
    panel.setBackgroundColor_(NSColor.clearColor())
    panel.setHasShadow_(False)
    panel.setIgnoresMouseEvents_(True)
    panel.setLevel_(NSScreenSaverWindowLevel)
    panel.setCollectionBehavior_(
        NSWindowCollectionBehaviorCanJoinAllSpaces
        | NSWindowCollectionBehaviorCanJoinAllApplications
        | NSWindowCollectionBehaviorFullScreenAuxiliary
        | NSWindowCollectionBehaviorStationary
        | NSWindowCollectionBehaviorIgnoresCycle
    )
    panel.setSharingType_(NSWindowSharingNone)
    panel.setHidesOnDeactivate_(False)
    panel.setReleasedWhenClosed_(False)
    panel.setAnimationBehavior_(NSWindowAnimationBehaviorNone)
    view = OverlayView.alloc().initWithFrame_(NSMakeRect(0, 0, frame.size.width, frame.size.height))
    panel.setContentView_(view)
    return panel


class Overlay:
    """Owns the panels. Answers and the work box are kept per display id, as two
    layers: show()/hide() switch the answers, show_work()/hide_work() the working."""

    def __init__(self) -> None:
        self._panels: dict[int, OverlayPanel] = {}
        self._items: dict[int, list[DrawItem]] = {}
        self._work: dict[int, list[DrawItem]] = {}
        self.visible = False
        self.work_visible = False
        self.rebuild()

    @property
    def has_answers(self) -> bool:
        return any(self._items.values())

    @property
    def has_work(self) -> bool:
        return any(self._work.values())

    def screen_size(self, display_id: int) -> tuple[float, float] | None:
        panel = self._panels.get(display_id)
        if panel is None:
            return None
        size = panel.frame().size
        return float(size.width), float(size.height)

    def rebuild(self) -> None:
        """(Re)create one panel per screen; call after screen parameters change.
        Answers are dropped: their positions belong to the old arrangement."""
        for panel in self._panels.values():
            panel.orderOut_(None)
        self._panels = {_screen_id(s): _make_panel(s) for s in NSScreen.screens()}
        self._items = {}
        self._work = {}
        self.visible = self.work_visible = False
        log.info("Overlay panels for displays %s", sorted(self._panels))

    def set_items(self, display_id: int, items: list[DrawItem], work: list[DrawItem] = ()) -> bool:
        """Store and draw a display's answers and working; False if that display has
        no panel. Each layer shows only while it is switched on."""
        if display_id not in self._panels:
            log.warning("No overlay panel for display %d; answers not shown", display_id)
            return False
        self._items[display_id] = list(items)
        self._work[display_id] = list(work)
        self._redraw(display_id)
        return True

    def clear(self) -> None:
        self._items = {}
        self._work = {}
        self.visible = self.work_visible = False
        self._redraw_all()

    def show(self) -> None:
        self.visible = True
        self._redraw_all()

    def hide(self) -> None:
        """Answers hidden. With the working hidden too, nothing is on screen at all
        (Figma 3:400)."""
        self.visible = False
        self._redraw_all()

    def show_work(self) -> None:
        self.work_visible = True
        self._redraw_all()

    def hide_work(self) -> None:
        self.work_visible = False
        self._redraw_all()

    def _redraw_all(self) -> None:
        for display_id in self._panels:
            self._redraw(display_id)

    def _redraw(self, display_id: int) -> None:
        """Draw the layers that are on; a panel with nothing to draw is ordered out."""
        panel = self._panels[display_id]
        items = list(self._items.get(display_id, ())) if self.visible else []
        if self.work_visible:
            items += self._work.get(display_id, ())
        view = panel.contentView()
        view.items = items
        view.setNeedsDisplay_(True)
        if items:
            panel.orderFrontRegardless()
        else:
            panel.orderOut_(None)

    def window_numbers(self) -> list[int]:
        return [int(p.windowNumber()) for p in self._panels.values()]
