"""Menu-bar eye glyph (Figma frame 3:35), drawn with NSBezierPath.

The eye is an 18x18 *template* image so macOS tints it for light/dark menu bars
and inactive displays. Template images are monochrome, so the blue (answers
ready) and red (error) dots are a small layer-backed view on the status button,
not part of the image.
"""

from __future__ import annotations

import objc
from AppKit import (
    NSBezierPath,
    NSColor,
    NSImage,
    NSLineJoinStyleRound,
    NSMakePoint,
    NSMakeRect,
    NSMakeSize,
    NSView,
    NSViewFrameDidChangeNotification,
)
from Foundation import NSNotificationCenter, NSObject

from peek.status import IconState

SIZE = 18.0
LINE_WIDTH = 1.5
# Working state: 8 dashes around the outline, one centred on each tip (Figma export).
DASH = (2.6855, 2.1486)
DASH_PHASE = DASH[0] / 2

# Fixed Figma colours for the state dots (accent/default, status/danger).
READY_DOT_RGB = (0x0A / 255, 0x84 / 255, 0xFF / 255)
ERROR_DOT_RGB = (0xE5 / 255, 0x39 / 255, 0x2F / 255)
# Dot geometry inside the 18x18 glyph, top-left origin.
DOT_RECT_FLIPPED = (12.0, 0.5, 6.0, 6.0)


def _eye_outline() -> NSBezierPath:
    """Coordinates are the Figma SVG's (top-left origin); draw in a flipped context."""
    p = NSBezierPath.bezierPath()
    p.moveToPoint_(NSMakePoint(1.5, 9))
    p.curveToPoint_controlPoint1_controlPoint2_(
        NSMakePoint(9, 3.75), NSMakePoint(3.2, 5.6), NSMakePoint(5.9, 3.75)
    )
    p.curveToPoint_controlPoint1_controlPoint2_(
        NSMakePoint(16.5, 9), NSMakePoint(12.1, 3.75), NSMakePoint(14.8, 5.6)
    )
    p.curveToPoint_controlPoint1_controlPoint2_(
        NSMakePoint(9, 14.25), NSMakePoint(14.8, 12.4), NSMakePoint(12.1, 14.25)
    )
    p.curveToPoint_controlPoint1_controlPoint2_(
        NSMakePoint(1.5, 9), NSMakePoint(5.9, 14.25), NSMakePoint(3.2, 12.4)
    )
    p.closePath()
    p.setLineWidth_(LINE_WIDTH)
    p.setLineJoinStyle_(NSLineJoinStyleRound)
    return p


def eye_image(*, dashed: bool) -> NSImage:
    def draw(_rect) -> bool:
        NSColor.blackColor().set()
        outline = _eye_outline()
        if dashed:
            outline.setLineDash_count_phase_(list(DASH), 2, DASH_PHASE)
        outline.stroke()
        NSBezierPath.bezierPathWithOvalInRect_(NSMakeRect(6.75, 6.75, 4.5, 4.5)).fill()
        return True

    image = NSImage.imageWithSize_flipped_drawingHandler_(NSMakeSize(SIZE, SIZE), True, draw)
    image.setTemplate_(True)
    image.setAccessibilityDescription_("Peek")
    return image


def dot_rgb(state: IconState) -> tuple[float, float, float] | None:
    if state is IconState.READY:
        return READY_DOT_RGB
    if state is IconState.ERROR:
        return ERROR_DOT_RGB
    return None


def dot_frame(button_width: float, button_height: float, flipped: bool):
    """Frame of the state dot in the button's coordinates (glyph centred in the button)."""
    img_x = (button_width - SIZE) / 2
    img_y = (button_height - SIZE) / 2
    dx, dy, dw, dh = DOT_RECT_FLIPPED
    y = img_y + dy if flipped else img_y + (SIZE - dy - dh)
    return (img_x + dx, y, dw, dh)


class _FrameObserver(NSObject):
    """Calls back when the status button is resized by the menu bar."""

    @objc.python_method
    def watch(self, view, callback):
        self._callback = callback
        view.setPostsFrameChangedNotifications_(True)
        NSNotificationCenter.defaultCenter().addObserver_selector_name_object_(
            self, "frameDidChange:", NSViewFrameDidChangeNotification, view
        )
        return self

    def frameDidChange_(self, _notification):
        self._callback()


class StatusIcon:
    """Owns the status button's image and the coloured dot view.

    The dot is placed with frame math rather than Auto Layout: anchors follow a
    control's alignment rect, which is not the frame the image is centred in.
    """

    def __init__(self, button) -> None:
        self._button = button
        self._idle = eye_image(dashed=False)
        self._working = eye_image(dashed=True)
        self._dot = NSView.alloc().initWithFrame_(NSMakeRect(0, 0, 6, 6))
        self._dot.setWantsLayer_(True)
        self._dot.layer().setCornerRadius_(3.0)
        self._dot.setHidden_(True)
        button.addSubview_(self._dot)
        self._observer = _FrameObserver.alloc().init().watch(button, self._layout_dot)
        self.state = IconState.IDLE
        self.set_state(IconState.IDLE)

    @property
    def dot_view(self):
        return self._dot

    def set_state(self, state: IconState) -> None:
        self.state = state
        self._button.setImage_(self._working if state is IconState.WORKING else self._idle)
        self._layout_dot()
        rgb = dot_rgb(state)
        if rgb is None:
            self._dot.setHidden_(True)
            return
        color = NSColor.colorWithSRGBRed_green_blue_alpha_(*rgb, 1.0)
        self._dot.layer().setBackgroundColor_(color.CGColor())
        self._dot.setHidden_(False)

    def relayout(self) -> None:
        """Call after the button's title or image position changed."""
        self._layout_dot()

    def _layout_dot(self) -> None:
        bounds = self._button.bounds()
        flipped = self._button.isFlipped()
        if self._button.title():
            # With a title the image is not centred: place the dot on the image rect.
            r = self._button.cell().imageRectForBounds_(bounds)
            dx, dy, dw, dh = DOT_RECT_FLIPPED
            y = r.origin.y + dy if flipped else r.origin.y + (SIZE - dy - dh)
            self._dot.setFrame_(NSMakeRect(r.origin.x + dx, y, dw, dh))
            return
        frame = dot_frame(bounds.size.width, bounds.size.height, flipped)
        self._dot.setFrame_(NSMakeRect(*frame))
