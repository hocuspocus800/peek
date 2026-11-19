"""Small custom views shared by the menu, Settings and onboarding."""

from __future__ import annotations

import objc
from AppKit import NSBezierPath, NSColor, NSInsetRect, NSView

from peek.status import DotColor

# Figma "Peek colors": status/success, status/danger, and the "Not set" grey.
_DOT_RGB = {
    DotColor.SUCCESS: (0x28 / 255, 0xB0 / 255, 0x4A / 255),
    DotColor.DANGER: (0xE5 / 255, 0x39 / 255, 0x2F / 255),
    DotColor.NEUTRAL: (0xAE / 255, 0xAE / 255, 0xB2 / 255),
}


def dot_color(dot: DotColor) -> NSColor:
    return NSColor.colorWithSRGBRed_green_blue_alpha_(*_DOT_RGB[dot], 1.0)


class StatusDotView(NSView):
    """The 8 pt filled circle of the Figma Status component."""

    def initWithFrame_(self, frame):
        self = objc.super(StatusDotView, self).initWithFrame_(frame)
        if self is None:
            return None
        self._color = dot_color(DotColor.NEUTRAL)
        return self

    @objc.python_method
    def set_dot(self, dot: DotColor) -> None:
        self._color = dot_color(dot)
        self.setNeedsDisplay_(True)

    def drawRect_(self, _rect):
        self._color.setFill()
        NSBezierPath.bezierPathWithOvalInRect_(self.bounds()).fill()

    def isOpaque(self):
        return False


class CardView(NSView):
    """The Figma group card: surface fill, 1 pt border, radius 10, sized by its
    content (pinned with Auto Layout). Colors are semantic and resolved at draw
    time, so the card follows light and dark mode."""

    @objc.python_method
    def set_content(self, view: NSView, padding: float = 16) -> None:
        self.setTranslatesAutoresizingMaskIntoConstraints_(False)
        view.setTranslatesAutoresizingMaskIntoConstraints_(False)
        self.addSubview_(view)
        for constraint in (
            view.topAnchor().constraintEqualToAnchor_constant_(self.topAnchor(), padding),
            view.leadingAnchor().constraintEqualToAnchor_constant_(self.leadingAnchor(), padding),
            self.bottomAnchor().constraintEqualToAnchor_constant_(view.bottomAnchor(), padding),
            self.trailingAnchor().constraintGreaterThanOrEqualToAnchor_constant_(
                view.trailingAnchor(), padding
            ),
        ):
            constraint.setActive_(True)

    def drawRect_(self, _rect):
        path = NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
            NSInsetRect(self.bounds(), 0.5, 0.5), 10, 10
        )
        NSColor.controlBackgroundColor().setFill()
        path.fill()
        NSColor.separatorColor().setStroke()
        path.setLineWidth_(1)
        path.stroke()

    def isOpaque(self):
        return False


class KeycapView(NSView):
    """A Figma keycap: a rounded key with a modifier glyph (Settings → Hotkeys)."""

    def initWithFrame_(self, frame):
        self = objc.super(KeycapView, self).initWithFrame_(frame)
        if self is None:
            return None
        self._glyph = ""
        return self

    @objc.python_method
    def set_glyph(self, glyph: str) -> None:
        self._glyph = glyph
        self.setAccessibilityLabel_({"⌃": "Control", "⌘": "Command"}.get(glyph, glyph))
        self.setNeedsDisplay_(True)

    def drawRect_(self, _rect):
        from AppKit import (
            NSAttributedString,
            NSFont,
            NSFontAttributeName,
            NSForegroundColorAttributeName,
            NSMakePoint,
        )

        bounds = self.bounds()
        path = NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
            NSInsetRect(bounds, 0.5, 0.5), 5, 5
        )
        NSColor.controlColor().setFill()
        path.fill()
        NSColor.separatorColor().setStroke()
        path.setLineWidth_(1)
        path.stroke()
        text = NSAttributedString.alloc().initWithString_attributes_(
            self._glyph,
            {
                NSFontAttributeName: NSFont.systemFontOfSize_(13),
                NSForegroundColorAttributeName: NSColor.labelColor(),
            },
        )
        size = text.size()
        text.drawAtPoint_(
            NSMakePoint(
                (bounds.size.width - size.width) / 2, (bounds.size.height - size.height) / 2
            )
        )

    def isOpaque(self):
        return False


class AnswerPreviewView(NSView):
    """Settings → Overlay → Preview: a page with two questions and their answers
    drawn the way the overlay draws them (plain text in the question's colour; the
    multiple-choice letter in yellow at the end of the question)."""

    PAGE_RGB = (0xF2 / 255, 0xF0 / 255, 0xEA / 255)  # Figma preview surface
    INK_RGB = (0x22 / 255, 0x22 / 255, 0x22 / 255)

    def initWithFrame_(self, frame):
        self = objc.super(AnswerPreviewView, self).initWithFrame_(frame)
        if self is None:
            return None
        self._size = 15.0
        return self

    @objc.python_method
    def set_text_size(self, size: float) -> None:
        self._size = size
        self.setNeedsDisplay_(True)

    def isFlipped(self):
        return True

    def drawRect_(self, _rect):
        from AppKit import (
            NSAttributedString,
            NSFont,
            NSFontAttributeName,
            NSForegroundColorAttributeName,
            NSMakePoint,
        )

        from peek.layout import YELLOW

        bounds = self.bounds()
        path = NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
            NSInsetRect(bounds, 0.5, 0.5), 10, 10
        )
        NSColor.colorWithSRGBRed_green_blue_alpha_(*self.PAGE_RGB, 1.0).setFill()
        path.fill()
        NSColor.separatorColor().setStroke()
        path.stroke()

        def text(s, rgb):
            return NSAttributedString.alloc().initWithString_attributes_(
                s,
                {
                    NSFontAttributeName: NSFont.systemFontOfSize_(self._size),
                    NSForegroundColorAttributeName: NSColor.colorWithSRGBRed_green_blue_alpha_(
                        *rgb, 1.0
                    ),
                },
            )

        ink = self.INK_RGB
        yellow = tuple(v / 255 for v in YELLOW)
        parts = [("3x + 4 = 25", ink, 8), ("x = 7", ink, 28), ("Prime?", ink, 6), ("B", yellow, 0)]
        x = 21.0
        for s, rgb, gap in parts:
            t = text(s, rgb)
            size = t.size()
            t.drawAtPoint_(NSMakePoint(x, (bounds.size.height - size.height) / 2))
            x += size.width + gap

    def isOpaque(self):
        return False
