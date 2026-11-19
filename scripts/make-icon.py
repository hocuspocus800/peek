"""Render Peek's app icon (packaging/Peek.icns) from the menu-bar eye.

The eye is the same drawing as the menu-bar glyph (peek/icons.py), scaled to each
icon size, on a transparent background and without the state dot.

    uv run python scripts/make-icon.py [--style STYLE] [--preview FILE]

--preview writes a comparison sheet of every style on light and dark backgrounds
instead of the .icns.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import AppKit  # noqa: E402
from AppKit import (  # noqa: E402
    NSAffineTransform,
    NSBezierPath,
    NSBitmapImageRep,
    NSColor,
    NSDeviceRGBColorSpace,
    NSGraphicsContext,
    NSMakeRect,
)

from peek.icons import SIZE, _eye_outline  # noqa: E402

ICNS = ROOT / "packaging" / "Peek.icns"
# iconset names and their pixel sizes (Apple's required set).
ICONSET = [
    (f"icon_{s}x{s}{'@2x' if k == 2 else ''}.png", s * k)
    for s in (16, 32, 128, 256, 512)
    for k in (1, 2)
]


def _rgb(hex6: str):
    r, g, b = (int(hex6[i : i + 2], 16) / 255 for i in (0, 2, 4))
    return NSColor.colorWithSRGBRed_green_blue_alpha_(r, g, b, 1.0)


# style -> (eye colour, outline halo colour or None)
STYLES = {
    "black": ("1D1D1F", None),  # the menu-bar eye in light mode
    "white": ("FFFFFF", None),  # the menu-bar eye in dark mode
    "grey": ("8E8E93", None),  # systemGray: readable on both
    "blue": ("0A84FF", None),  # the "answers ready" dot's blue (Figma accent/default)
}
# The owner's choice: blue, which reads on both the light and the dark tile macOS 26
# puts behind an icon with a transparent background (an .icns can't follow
# light/dark mode; that needs an asset catalog built with Xcode's actool).
DEFAULT_STYLE = "blue"


def render(pixels: int, style: str) -> NSBitmapImageRep:
    colour, halo = STYLES[style]
    init = NSBitmapImageRep.alloc().initWithBitmapDataPlanes_pixelsWide_pixelsHigh_bitsPerSample_samplesPerPixel_hasAlpha_isPlanar_colorSpaceName_bytesPerRow_bitsPerPixel_  # noqa: E501
    rep = init(None, pixels, pixels, 8, 4, True, False, NSDeviceRGBColorSpace, 0, 0)
    ctx = NSGraphicsContext.graphicsContextWithBitmapImageRep_(rep)
    NSGraphicsContext.saveGraphicsState()
    NSGraphicsContext.setCurrentContext_(ctx)
    NSColor.clearColor().set()
    AppKit.NSRectFillUsingOperation(
        NSMakeRect(0, 0, pixels, pixels), AppKit.NSCompositingOperationCopy
    )
    # The glyph's coordinates are top-left origin in an 18 x 18 box: flip and scale.
    t = NSAffineTransform.transform()
    t.translateXBy_yBy_(0, pixels)
    t.scaleXBy_yBy_(pixels / SIZE, -pixels / SIZE)
    t.concat()
    outline = _eye_outline()
    pupil = NSBezierPath.bezierPathWithOvalInRect_(NSMakeRect(6.75, 6.75, 4.5, 4.5))
    if halo is not None:
        _rgb(halo).set()
        wide = _eye_outline()
        wide.setLineWidth_(outline.lineWidth() + 1.2)
        wide.stroke()
        ring = NSBezierPath.bezierPathWithOvalInRect_(NSMakeRect(6.15, 6.15, 5.7, 5.7))
        ring.fill()
    _rgb(colour).set()
    outline.stroke()
    pupil.fill()
    NSGraphicsContext.restoreGraphicsState()
    return rep


def _png(rep: NSBitmapImageRep) -> bytes:
    return bytes(rep.representationUsingType_properties_(AppKit.NSBitmapImageFileTypePNG, None))


def write_icns(style: str) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        iconset = Path(tmp) / "Peek.iconset"
        iconset.mkdir()
        for name, pixels in ICONSET:
            (iconset / name).write_bytes(_png(render(pixels, style)))
        subprocess.run(["iconutil", "-c", "icns", str(iconset), "-o", str(ICNS)], check=True)
    print(f"Wrote {ICNS.relative_to(ROOT)} ({style})")


def write_preview(path: Path) -> None:
    """Each style at 128 px (and 32 px) on a light and a dark Finder background."""
    from PIL import Image

    cell, small, pad = 128, 32, 24
    styles = list(STYLES)
    sheet = Image.new(
        "RGBA", (len(styles) * (cell + pad) + pad, 2 * (cell + pad) + pad), (0, 0, 0, 0)
    )
    for row, bg in enumerate(((246, 246, 246), (30, 30, 30))):
        y = pad + row * (cell + pad)
        band = Image.new("RGBA", (sheet.width, cell + pad), (*bg, 255))
        sheet.paste(band, (0, y - pad // 2))
        for col, style in enumerate(styles):
            x = pad + col * (cell + pad)
            for size, dx in ((cell, 0), (small, cell - small)):
                with tempfile.NamedTemporaryFile(suffix=".png") as f:
                    f.write(_png(render(size * 2, style)))
                    f.flush()
                    im = Image.open(f.name).convert("RGBA").resize((size, size), Image.LANCZOS)
                    if size == cell:
                        sheet.alpha_composite(im, (x, y))
                    else:
                        sheet.alpha_composite(im, (x + dx, y + cell - small))
    sheet.save(path)
    print(f"Wrote {path} ({', '.join(styles)}; light row, dark row)")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--style", choices=sorted(STYLES), default=DEFAULT_STYLE)
    parser.add_argument("--preview", type=Path)
    args = parser.parse_args()
    AppKit.NSApplication.sharedApplication()
    if args.preview:
        write_preview(args.preview)
    else:
        if shutil.which("iconutil") is None:
            sys.exit("iconutil not found (it comes with macOS)")
        write_icns(args.style)


if __name__ == "__main__":
    main()
