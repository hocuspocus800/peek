"""The look of a question's text on screen: its ink colour and its size.

Answers are drawn as plain text in the question's own colour and size (owner's
design). Size comes from the OCR box height (measured: within about
10% of the font size for 10-28 pt system text). Colour comes from the pixels
inside the question's OCR boxes: the background is the most common colour, the
ink is what differs most from it.
"""

from __future__ import annotations

import statistics
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from peek.geometry import Rect

RGB = tuple[int, int, int]

# Colour distance (RGB, 0-255 per channel) above which a pixel counts as ink.
INK_DISTANCE = 60
MAX_SAMPLE = 160  # long edge of the downsampled region, in pixels


@dataclass(frozen=True)
class TextStyle:
    rgb: RGB  # ink colour, sRGB
    background: RGB
    line_height_px: float  # median OCR box height of the question's lines


def _dist2(a: RGB, b: RGB) -> int:
    return (a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2 + (a[2] - b[2]) ** 2


def background_color(pixels: Sequence[RGB]) -> RGB:
    """The most common colour, quantized to 16 levels per channel, then averaged
    over the pixels in that bucket (so anti-aliasing doesn't shift it)."""
    buckets = Counter((r >> 4, g >> 4, b >> 4) for r, g, b in pixels)
    key = buckets.most_common(1)[0][0]
    members = [p for p in pixels if (p[0] >> 4, p[1] >> 4, p[2] >> 4) == key]
    return tuple(round(statistics.fmean(c)) for c in zip(*members, strict=False))  # type: ignore[return-value]


def ink_color(pixels: Sequence[RGB]) -> tuple[RGB, RGB] | None:
    """(ink, background), or None when the region has no clear text."""
    if len(pixels) < 16:
        return None
    bg = background_color(pixels)
    far = [p for p in pixels if _dist2(p, bg) > INK_DISTANCE**2]
    if len(far) < 8:
        return None
    # Anti-aliased edges sit between ink and background: keep the most distinct
    # 40%, the cores of the strokes.
    far.sort(key=lambda p: _dist2(p, bg), reverse=True)
    core = far[: max(8, len(far) * 2 // 5)]
    ink = tuple(round(statistics.median(c)) for c in zip(*core, strict=False))
    return ink, bg  # type: ignore[return-value]


def region_pixels(cgimage, rect: Rect) -> list[RGB]:
    """sRGB pixels of *rect* (capture pixels, top-left origin), downsampled so the
    long edge is at most MAX_SAMPLE."""
    import Quartz

    width = int(Quartz.CGImageGetWidth(cgimage))
    height = int(Quartz.CGImageGetHeight(cgimage))
    x0, y0 = max(0, int(rect.x)), max(0, int(rect.y))
    x1, y1 = min(width, int(rect.max_x + 0.999)), min(height, int(rect.max_y + 0.999))
    if x1 - x0 < 2 or y1 - y0 < 2:
        return []
    crop = Quartz.CGImageCreateWithImageInRect(cgimage, Quartz.CGRectMake(x0, y0, x1 - x0, y1 - y0))
    if crop is None:
        return []
    scale = min(1.0, MAX_SAMPLE / max(x1 - x0, y1 - y0))
    w, h = max(2, int((x1 - x0) * scale)), max(2, int((y1 - y0) * scale))
    space = Quartz.CGColorSpaceCreateWithName(Quartz.kCGColorSpaceSRGB)
    ctx = Quartz.CGBitmapContextCreate(
        None, w, h, 8, w * 4, space, Quartz.kCGImageAlphaNoneSkipLast
    )
    if ctx is None:
        return []
    # Nearest-neighbour keeps stroke cores at their true colour.
    Quartz.CGContextSetInterpolationQuality(ctx, Quartz.kCGInterpolationNone)
    Quartz.CGContextDrawImage(ctx, Quartz.CGRectMake(0, 0, w, h), crop)
    image = Quartz.CGBitmapContextCreateImage(ctx)
    data = bytes(Quartz.CGDataProviderCopyData(Quartz.CGImageGetDataProvider(image)))
    stride = int(Quartz.CGImageGetBytesPerRow(image))
    return [
        (data[i], data[i + 1], data[i + 2])
        for row in range(h)
        for i in range(row * stride, row * stride + w * 4, 4)
    ]


def estimate_style(cgimage, boxes: Iterable[Rect]) -> TextStyle | None:
    """Style of a question from its OCR boxes in the capture (worker thread)."""
    boxes = list(boxes)
    if not boxes:
        return None
    pixels: list[RGB] = []
    for box in boxes:
        pixels.extend(region_pixels(cgimage, box))
    found = ink_color(pixels)
    if found is None:
        return None
    ink, bg = found
    return TextStyle(ink, bg, statistics.median(b.h for b in boxes))
