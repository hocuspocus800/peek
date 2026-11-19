"""Rectangles and coordinate conversions. Pure Python, unit-tested.

Conventions:
- `Rect` is (x, y, w, h). Image rects are in *pixels* with a top-left origin
  (x right, y down), the frame every OCR box and model coordinate uses.
- Vision returns normalized rects (0-1) with a *bottom-left* origin.
- AppKit screen frames are in points with a bottom-left origin; conversion to
  overlay coordinates happens in layout (milestone 5).
- Side-by-side text (options on one row, table cells) comes back from Vision as
  separate observations when the gap is wide; Peek does not split lines further.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass


@dataclass(frozen=True)
class Rect:
    x: float
    y: float
    w: float
    h: float

    @property
    def max_x(self) -> float:
        return self.x + self.w

    @property
    def max_y(self) -> float:
        return self.y + self.h

    @property
    def mid_y(self) -> float:
        return self.y + self.h / 2

    def union(self, other: Rect) -> Rect:
        x, y = min(self.x, other.x), min(self.y, other.y)
        return Rect(x, y, max(self.max_x, other.max_x) - x, max(self.max_y, other.max_y) - y)

    def as_list(self, ndigits: int = 1) -> list[float]:
        return [round(v, ndigits) for v in (self.x, self.y, self.w, self.h)]


def union_all(rects: Sequence[Rect]) -> Rect:
    it = iter(rects)
    out = next(it)
    for r in it:
        out = out.union(r)
    return out


def vision_to_pixels(nx: float, ny: float, nw: float, nh: float, width: int, height: int) -> Rect:
    """Vision's normalized bottom-left rect -> top-left pixel rect, clamped to the image."""
    x0 = min(max(nx, 0.0), 1.0) * width
    x1 = min(max(nx + nw, 0.0), 1.0) * width
    top = min(max(1.0 - (ny + nh), 0.0), 1.0) * height
    bottom = min(max(1.0 - ny, 0.0), 1.0) * height
    return Rect(x0, top, x1 - x0, bottom - top)


def reading_order(rects: Sequence[Rect], line_height: float) -> list[int]:
    """Indices of *rects* in reading order: rows top to bottom, each row left to
    right. A box joins the current row when its vertical centre is within half a
    line height of the row's first box (not of the previous box, so a row cannot
    drift down into the next one). No fixed grid: boxes on one baseline whose
    centres differ by a pixel or two always share a row."""
    order = sorted(range(len(rects)), key=lambda i: rects[i].mid_y)
    rows: list[list[int]] = []
    anchor = None
    for i in order:
        if anchor is None or rects[i].mid_y - anchor > 0.5 * line_height:
            rows.append([i])
            anchor = rects[i].mid_y
        else:
            rows[-1].append(i)
    return [i for row in rows for i in sorted(row, key=lambda i: rects[i].x)]


def contains_point(frame: Rect, px: float, py: float) -> bool:
    """AppKit's rule for a point on screen (bottom-left origin, NSMouseInRect
    unflipped): x in [minX, maxX), y in (minY, maxY]. Two displays that share an
    edge therefore never both contain the pointer."""
    return frame.x <= px < frame.max_x and frame.y < py <= frame.max_y


def pick_frame(frames: Sequence[Rect], px: float, py: float) -> int:
    """Index of the frame containing the point, else of the nearest frame."""
    for i, f in enumerate(frames):
        if contains_point(f, px, py):
            return i

    def distance(f: Rect) -> float:
        dx = max(f.x - px, 0.0, px - f.max_x)
        dy = max(f.y - py, 0.0, py - f.max_y)
        return dx * dx + dy * dy

    return min(range(len(frames)), key=lambda i: distance(frames[i]))
