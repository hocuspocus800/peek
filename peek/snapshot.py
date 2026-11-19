"""Render Peek's custom-drawn UI to PNGs offscreen, for checking against Figma.

    uv run python -m peek.snapshot OUTPUT_DIR

Needs no permissions and no running app. Writes, at 2x:
- icon-states-light.png / icon-states-dark.png: the four menu-bar states on
  menu-bar-like swatches (Figma frame 3:35 layout);
- menu-header-<state>-<appearance>.png: the dropdown's custom header view.
"""

from __future__ import annotations

import sys
from pathlib import Path

from AppKit import (
    NSAppearance,
    NSAppearanceNameAqua,
    NSAppearanceNameDarkAqua,
    NSApplication,
    NSBezierPath,
    NSBitmapImageFileTypePNG,
    NSBitmapImageRep,
    NSCalibratedRGBColorSpace,
    NSColor,
    NSCompositingOperationSourceAtop,
    NSCompositingOperationSourceOver,
    NSGraphicsContext,
    NSImage,
    NSMakeRect,
    NSMakeSize,
    NSRectFillUsingOperation,
)

from peek.icons import SIZE, dot_rgb, eye_image
from peek.menu import HEADER_HEIGHT, MENU_WIDTH, MenuHeaderView
from peek.status import DotColor, HeaderStatus, IconState, ready_status

SCALE = 2


def _bitmap(width: float, height: float) -> NSBitmapImageRep:
    init = NSBitmapImageRep.alloc().initWithBitmapDataPlanes_pixelsWide_pixelsHigh_bitsPerSample_samplesPerPixel_hasAlpha_isPlanar_colorSpaceName_bytesPerRow_bitsPerPixel_  # noqa: E501
    rep = init(
        None,
        int(width * SCALE),
        int(height * SCALE),
        8,
        4,
        True,
        False,
        NSCalibratedRGBColorSpace,
        0,
        0,
    )
    rep.setSize_(NSMakeSize(width, height))
    return rep


def _save(rep: NSBitmapImageRep, path: Path) -> None:
    data = rep.representationUsingType_properties_(NSBitmapImageFileTypePNG, {})
    data.writeToFile_atomically_(str(path), True)


def _tinted(image: NSImage, color: NSColor) -> NSImage:
    """What the menu bar does to a template image: fill its alpha with *color*.
    Drawn lazily at the destination's resolution (lockFocus would rasterise at the
    main screen's scale, i.e. 1x on a non-Retina main display)."""

    def draw(rect) -> bool:
        image.drawInRect_fromRect_operation_fraction_(
            rect, NSMakeRect(0, 0, 0, 0), NSCompositingOperationSourceOver, 1.0
        )
        color.set()
        NSRectFillUsingOperation(rect, NSCompositingOperationSourceAtop)
        return True

    return NSImage.imageWithSize_flipped_drawingHandler_(image.size(), False, draw)


def render_icon_states(path: Path, *, dark: bool) -> None:
    states = list(IconState)
    cell_w, cell_h, gap = 72.0, 26.0, 20.0
    width = len(states) * cell_w + (len(states) - 1) * gap
    rep = _bitmap(width, cell_h)
    NSGraphicsContext.saveGraphicsState()
    NSGraphicsContext.setCurrentContext_(NSGraphicsContext.graphicsContextWithBitmapImageRep_(rep))
    bg = (0.17, 0.17, 0.18) if dark else (0xE9 / 255, 0xEA / 255, 0xF0 / 255)
    ink = (
        NSColor.whiteColor()
        if dark
        else NSColor.colorWithSRGBRed_green_blue_alpha_(0x1D / 255, 0x1D / 255, 0x1F / 255, 1)
    )
    for i, state in enumerate(states):
        x = i * (cell_w + gap)
        NSColor.colorWithSRGBRed_green_blue_alpha_(*bg, 1).setFill()
        NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
            NSMakeRect(x, 0, cell_w, cell_h), 6, 6
        ).fill()
        ix, iy = x + (cell_w - SIZE) / 2, (cell_h - SIZE) / 2
        glyph = _tinted(eye_image(dashed=state is IconState.WORKING), ink)
        glyph.drawInRect_fromRect_operation_fraction_(
            NSMakeRect(ix, iy, SIZE, SIZE),
            NSMakeRect(0, 0, 0, 0),
            NSCompositingOperationSourceOver,
            1.0,
        )
        rgb = dot_rgb(state)
        if rgb:
            NSColor.colorWithSRGBRed_green_blue_alpha_(*rgb, 1).setFill()
            # Dot at (12, 0.5, 6, 6) in the glyph's top-left coordinates.
            NSBezierPath.bezierPathWithOvalInRect_(
                NSMakeRect(ix + 12, iy + SIZE - 0.5 - 6, 6, 6)
            ).fill()
    NSGraphicsContext.restoreGraphicsState()
    _save(rep, path)


def render_header(path: Path, status: HeaderStatus, *, dark: bool) -> None:
    view = MenuHeaderView.alloc().initWithFrame_(NSMakeRect(0, 0, MENU_WIDTH, HEADER_HEIGHT))
    view.setAppearance_(
        NSAppearance.appearanceNamed_(NSAppearanceNameDarkAqua if dark else NSAppearanceNameAqua)
    )
    view.set_status(status)
    rep = _bitmap(MENU_WIDTH, HEADER_HEIGHT)
    NSGraphicsContext.saveGraphicsState()
    NSGraphicsContext.setCurrentContext_(NSGraphicsContext.graphicsContextWithBitmapImageRep_(rep))
    # Approximate menu material so the transparent labels are visible.
    bg = (0.16, 0.16, 0.17) if dark else (246 / 255, 246 / 255, 248 / 255)
    NSColor.colorWithSRGBRed_green_blue_alpha_(*bg, 1).setFill()
    NSBezierPath.fillRect_(NSMakeRect(0, 0, MENU_WIDTH, HEADER_HEIGHT))
    NSGraphicsContext.restoreGraphicsState()
    view.cacheDisplayInRect_toBitmapImageRep_(view.bounds(), rep)
    _save(rep, path)


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        print(__doc__)
        return 2
    out = Path(argv[0])
    out.mkdir(parents=True, exist_ok=True)
    NSApplication.sharedApplication()  # text rendering needs an NSApplication
    for dark in (False, True):
        tag = "dark" if dark else "light"
        render_icon_states(out / f"icon-states-{tag}.png", dark=dark)
        render_header(
            out / f"menu-header-ready-{tag}.png", ready_status("Claude Sonnet 5.5"), dark=dark
        )
        render_header(
            out / f"menu-header-notset-{tag}.png",
            HeaderStatus("No provider connected", DotColor.NEUTRAL),
            dark=dark,
        )
    print(f"wrote {len(list(out.glob('*.png')))} PNGs to {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
