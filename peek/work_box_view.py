"""Settings → Show work: a picture of the display with the box the working goes in.

The display is drawn to scale (the main display's shape, its desktop picture if
macOS gives one, a menu-bar strip). The box can be moved by dragging inside it and
resized by dragging an edge or a corner; it stays inside the display and at least
store.MIN_WORK_BOX big. The box is kept as fractions of the display (x, y, w, h,
top-left origin), the same on every display.

hit_test() and drag() are plain geometry, unit-tested; the view only draws and
routes mouse events. Main thread only.
"""

from __future__ import annotations

from collections.abc import Callable

import objc
from AppKit import (
    NSBezierPath,
    NSColor,
    NSCompositingOperationSourceOver,
    NSCursor,
    NSFocusRingTypeNone,
    NSFont,
    NSFontAttributeName,
    NSForegroundColorAttributeName,
    NSGraphicsContext,
    NSImage,
    NSMakeRect,
    NSMutableParagraphStyle,
    NSParagraphStyleAttributeName,
    NSScreen,
    NSTextAlignmentCenter,
    NSView,
    NSWorkspace,
)

from peek.store import MIN_WORK_BOX

Box = tuple[float, float, float, float]  # x, y, w, h as fractions, top-left origin

HANDLE = 7.0  # points around an edge or corner that grab it
HANDLE_SQUARE = 6.0  # the drawn corner handles
MENU_BAR = 0.035  # the menu-bar strip, as a share of the display's height


def _inner(size: float) -> float:
    """How far inside the box an edge still grabs: HANDLE, or a sixth of a small
    box, so a small box keeps a middle (about 44% of it) to move it by."""
    return min(HANDLE, size / 6)


def hit_test(box_pt: tuple[float, float, float, float], x: float, y: float) -> str | None:
    """What a click at (x, y) grabs: 'move', an edge ('n', 's', 'e', 'w'), a corner
    ('ne', 'nw', 'se', 'sw'), or None. Flipped coordinates (y down). An edge grabs
    HANDLE outside the box and _inner() inside it."""
    bx, by, bw, bh = box_pt
    if not (bx - HANDLE <= x <= bx + bw + HANDLE and by - HANDLE <= y <= by + bh + HANDLE):
        return None
    ix, iy = _inner(bw), _inner(bh)
    vertical = (
        "n" if by - HANDLE <= y <= by + iy else "s" if by + bh - iy <= y <= by + bh + HANDLE else ""
    )
    horizontal = (
        "w" if bx - HANDLE <= x <= bx + ix else "e" if bx + bw - ix <= x <= bx + bw + HANDLE else ""
    )
    if vertical or horizontal:
        return vertical + horizontal
    if bx < x < bx + bw and by < y < by + bh:
        return "move"
    return None


def drag(start: Box, mode: str, dx: float, dy: float, minimum: float = MIN_WORK_BOX) -> Box:
    """The box after dragging by (dx, dy) display fractions from where it was
    grabbed: moved (kept inside the display) or resized from the grabbed edges
    (the opposite edges stay put; never smaller than *minimum*)."""
    x, y, w, h = start
    if mode == "move":
        return (min(max(x + dx, 0.0), 1.0 - w), min(max(y + dy, 0.0), 1.0 - h), w, h)
    left, top, right, bottom = x, y, x + w, y + h
    if "w" in mode:
        left = min(max(left + dx, 0.0), right - minimum)
    if "e" in mode:
        right = max(min(right + dx, 1.0), left + minimum)
    if "n" in mode:
        top = min(max(top + dy, 0.0), bottom - minimum)
    if "s" in mode:
        bottom = max(min(bottom + dy, 1.0), top + minimum)
    return (left, top, right - left, bottom - top)


def display_aspect() -> float:
    """Width / height of the main display (where the menu bar is)."""
    screen = NSScreen.mainScreen() or (NSScreen.screens() or [None])[0]
    if screen is None:
        return 16 / 10
    size = screen.frame().size
    return float(size.width / size.height) if size.height else 16 / 10


def fill_crop(image_w: float, image_h: float, aspect: float) -> tuple[float, float, float, float]:
    """The centred part of an image with the display's shape (aspect fill, like the
    desktop itself): x, y, w, h in the image's own coordinates."""
    if image_w <= 0 or image_h <= 0 or aspect <= 0:
        return (0.0, 0.0, max(image_w, 0.0), max(image_h, 0.0))
    w = min(image_w, image_h * aspect)
    h = w / aspect
    return ((image_w - w) / 2, (image_h - h) / 2, w, h)


def _desktop_picture():
    try:
        screen = NSScreen.mainScreen()
        url = NSWorkspace.sharedWorkspace().desktopImageURLForScreen_(screen)
        image = NSImage.alloc().initWithContentsOfURL_(url) if url is not None else None
        return image if image is not None and image.isValid() else None
    except Exception:
        return None


NUDGE, BIG_NUDGE = 0.01, 0.05  # arrow keys move (or with Shift, resize) the box


_CURSORS = {
    "move": "openHandCursor",
    "n": "resizeUpDownCursor",
    "s": "resizeUpDownCursor",
    "e": "resizeLeftRightCursor",
    "w": "resizeLeftRightCursor",
}


class WorkBoxView(NSView):
    """Draws the display and the work box; drag to move, edges and corners resize."""

    def initWithFrame_(self, frame):
        self = objc.super(WorkBoxView, self).initWithFrame_(frame)
        if self is None:
            return None
        self.box: Box = (0.62, 0.12, 0.34, 0.5)
        self.rgb = (255, 204, 0)
        self.enabled = True
        self.aspect = display_aspect()
        self.picture = _desktop_picture()
        self.on_change: Callable[[Box], None] | None = None
        self._grab = None  # (mode, start box, start point)
        # No focus ring: AppKit redraws it after the box, so it trailed behind drags.
        self.setFocusRingType_(NSFocusRingTypeNone)
        return self

    def isFlipped(self):
        return True

    def acceptsFirstMouse_(self, _event):
        return True

    # Keyboard: Tab reaches the box while Show work is on; arrows move it (Option:
    # in bigger steps), Shift-arrows resize it from its right and bottom edges.

    def acceptsFirstResponder(self):
        return self.enabled

    def canBecomeKeyView(self):
        return self.enabled

    def keyDown_(self, event):
        if not self.enabled:
            objc.super(WorkBoxView, self).keyDown_(event)
            return
        self.interpretKeyEvents_([event])

    @objc.python_method
    def _nudge(self, dx: int, dy: int) -> None:
        from AppKit import NSApp, NSEventModifierFlagOption, NSEventModifierFlagShift

        event = NSApp.currentEvent()
        flags = int(event.modifierFlags()) if event is not None else 0
        step = BIG_NUDGE if flags & NSEventModifierFlagOption else NUDGE
        start = self.box
        if flags & NSEventModifierFlagShift:
            mode = ("e" if dx else "") + ("s" if dy else "")
            new = drag(start, mode, dx * step, dy * step)
        else:
            new = drag(start, "move", dx * step, dy * step)
        if new != start:
            self.set_box(new)
            if self.on_change is not None:
                self.on_change(self.box)

    def moveLeft_(self, _sender):
        self._nudge(-1, 0)

    def moveRight_(self, _sender):
        self._nudge(1, 0)

    def moveUp_(self, _sender):
        self._nudge(0, -1)

    def moveDown_(self, _sender):
        self._nudge(0, 1)

    def moveLeftAndModifySelection_(self, _sender):  # Shift-arrows
        self._nudge(-1, 0)

    def moveRightAndModifySelection_(self, _sender):
        self._nudge(1, 0)

    def moveUpAndModifySelection_(self, _sender):
        self._nudge(0, -1)

    def moveDownAndModifySelection_(self, _sender):
        self._nudge(0, 1)

    # VoiceOver: one element that says where the box is.

    def isAccessibilityElement(self):
        return True

    def accessibilityRole(self):
        from AppKit import NSAccessibilityGroupRole

        return NSAccessibilityGroupRole

    def accessibilityLabel(self):
        return "Work box"

    def accessibilityValue(self):
        x, y, w, h = (round(v * 100) for v in self.box)
        state = "" if self.enabled else ", Show work is off"
        return f"left {x}%, top {y}%, width {w}%, height {h}%{state}"

    def accessibilityHelp(self):
        return "Arrow keys move the box; Shift with arrow keys resizes it."

    # --- state --------------------------------------------------------------

    @objc.python_method
    def _invalidate_cursors(self) -> None:
        if self.window() is not None:
            self.window().invalidateCursorRectsForView_(self)

    @objc.python_method
    def set_box(self, box) -> None:
        self.box = tuple(float(v) for v in box)
        self.setNeedsDisplay_(True)
        self._invalidate_cursors()
        from AppKit import NSAccessibilityPostNotification, NSAccessibilityValueChangedNotification

        NSAccessibilityPostNotification(self, NSAccessibilityValueChangedNotification)

    @objc.python_method
    def set_rgb(self, rgb) -> None:
        self.rgb = tuple(int(c) for c in rgb)
        self.setNeedsDisplay_(True)

    @objc.python_method
    def set_enabled(self, enabled: bool) -> None:
        self.enabled = bool(enabled)
        if not self.enabled:
            self._grab = None  # a drag that began before the switch flipped stops here
            if self.window() is not None and self.window().firstResponder() is self:
                self.window().makeFirstResponder_(None)
        self.setNeedsDisplay_(True)
        self._invalidate_cursors()

    # --- geometry -----------------------------------------------------------

    @objc.python_method
    def screen_rect(self) -> tuple[float, float, float, float]:
        """The display's picture inside the view: as large as fits, centred."""
        b = self.bounds()
        w, h = float(b.size.width) - 2, float(b.size.height) - 2
        if w / h > self.aspect:
            sw, sh = h * self.aspect, h
        else:
            sw, sh = w, w / self.aspect
        return (1 + (w - sw) / 2, 1 + (h - sh) / 2, sw, sh)

    @objc.python_method
    def box_rect(self) -> tuple[float, float, float, float]:
        sx, sy, sw, sh = self.screen_rect()
        x, y, w, h = self.box
        return (sx + x * sw, sy + y * sh, w * sw, h * sh)

    # --- drawing ------------------------------------------------------------

    def drawRect_(self, _rect):
        sx, sy, sw, sh = self.screen_rect()
        screen = NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
            NSMakeRect(sx, sy, sw, sh), 6, 6
        )
        NSGraphicsContext.saveGraphicsState()
        screen.addClip()
        if self.picture is not None:
            size = self.picture.size()
            crop = fill_crop(float(size.width), float(size.height), self.aspect)
            self.picture.drawInRect_fromRect_operation_fraction_respectFlipped_hints_(
                NSMakeRect(sx, sy, sw, sh),
                NSMakeRect(*crop),
                NSCompositingOperationSourceOver,
                1.0,
                True,
                None,
            )
            NSColor.colorWithWhite_alpha_(0.0, 0.35).setFill()  # dim, so the box stands out
            NSBezierPath.fillRect_(NSMakeRect(sx, sy, sw, sh))
        else:
            NSColor.colorWithSRGBRed_green_blue_alpha_(0.22, 0.25, 0.32, 1).setFill()
            NSBezierPath.fillRect_(NSMakeRect(sx, sy, sw, sh))
        NSColor.colorWithWhite_alpha_(1.0, 0.18).setFill()  # the menu bar
        NSBezierPath.fillRect_(NSMakeRect(sx, sy, sw, max(4.0, sh * MENU_BAR)))
        NSGraphicsContext.restoreGraphicsState()
        NSColor.separatorColor().setStroke()
        screen.setLineWidth_(1)
        screen.stroke()

        bx, by, bw, bh = self.box_rect()
        r, g, b = (c / 255 for c in self.rgb)
        alpha = 1.0 if self.enabled else 0.45
        color = NSColor.colorWithSRGBRed_green_blue_alpha_(r, g, b, alpha)
        color.colorWithAlphaComponent_(0.18 * alpha).setFill()
        frame = NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
            NSMakeRect(bx, by, bw, bh), 3, 3
        )
        frame.fill()
        color.setStroke()
        frame.setLineWidth_(1.5)
        frame.stroke()
        self._draw_label(bx, by, bw, bh, color)
        if self.enabled:
            for hx, hy in ((bx, by), (bx + bw, by), (bx, by + bh), (bx + bw, by + bh)):
                s = HANDLE_SQUARE
                handle = NSBezierPath.bezierPathWithRect_(NSMakeRect(hx - s / 2, hy - s / 2, s, s))
                NSColor.whiteColor().setFill()
                handle.fill()
                color.setStroke()
                handle.setLineWidth_(1)
                handle.stroke()

    @objc.python_method
    def _draw_label(self, bx, by, bw, bh, color) -> None:
        """'Work shows here', as large as fits the box (like the real working)."""
        from AppKit import NSAttributedString

        style = NSMutableParagraphStyle.alloc().init()
        style.setAlignment_(NSTextAlignmentCenter)
        size = 13.0
        while size > 7:
            attrs = {
                NSFontAttributeName: NSFont.systemFontOfSize_(size),
                NSForegroundColorAttributeName: color,
                NSParagraphStyleAttributeName: style,
            }
            text = NSAttributedString.alloc().initWithString_attributes_("Work shows here", attrs)
            ts = text.size()
            if ts.width <= bw - 8 and ts.height <= bh - 4:
                text.drawInRect_(NSMakeRect(bx, by + (bh - ts.height) / 2, bw, ts.height))
                return
            size -= 1

    # --- mouse --------------------------------------------------------------

    def resetCursorRects(self):
        if not self.enabled:
            return
        bx, by, bw, bh = self.box_rect()
        hd, ix, iy = HANDLE, _inner(bw), _inner(bh)
        self.addCursorRect_cursor_(NSMakeRect(bx, by, bw, bh), NSCursor.openHandCursor())
        edges = {  # the same areas hit_test grabs
            "n": (bx, by - hd, bw, hd + iy),
            "s": (bx, by + bh - iy, bw, hd + iy),
            "w": (bx - hd, by, hd + ix, bh),
            "e": (bx + bw - ix, by, hd + ix, bh),
        }
        for mode, rect in edges.items():
            self.addCursorRect_cursor_(NSMakeRect(*rect), getattr(NSCursor, _CURSORS[mode])())
        corners = (
            (bx - hd, by - hd),
            (bx + bw - ix, by - hd),
            (bx - hd, by + bh - iy),
            (bx + bw - ix, by + bh - iy),
        )
        for cx, cy in corners:
            self.addCursorRect_cursor_(
                NSMakeRect(cx, cy, hd + ix, hd + iy), NSCursor.crosshairCursor()
            )

    @objc.python_method
    def _point(self, event):
        p = self.convertPoint_fromView_(event.locationInWindow(), None)
        return float(p.x), float(p.y)

    def mouseDown_(self, event):
        if not self.enabled:
            return
        if self.window() is not None:
            self.window().makeFirstResponder_(self)  # then the arrow keys work too
        x, y = self._point(event)
        mode = hit_test(self.box_rect(), x, y)
        self._grab = (mode, self.box, (x, y)) if mode else None
        if mode == "move":
            NSCursor.closedHandCursor().push()

    def mouseDragged_(self, event):
        if self._grab is None:
            return
        mode, start, (x0, y0) = self._grab
        x, y = self._point(event)
        _sx, _sy, sw, sh = self.screen_rect()
        self.box = drag(start, mode, (x - x0) / sw, (y - y0) / sh)
        self.setNeedsDisplay_(True)

    def mouseUp_(self, _event):
        if self._grab is None:
            return
        mode, start, _ = self._grab
        self._grab = None
        if mode == "move":
            NSCursor.pop()
        self._invalidate_cursors()
        if self.box != start and self.on_change is not None:
            self.on_change(self.box)
