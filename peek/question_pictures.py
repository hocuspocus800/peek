"""Save questions: one picture of each question, cropped from the capture.

Settings → Hotkeys & capture → Save questions (off by default). After a capture's
answers are read, each question is cropped from the screenshot and saved as a PNG
in the folder the user chose. The screenshot never contains answers: the overlay is
ordered out before a capture and excluded from it.

A crop covers the question's own text, its answer area (box, blank line, options)
and the figure nearest to it, if one is close enough, plus a small margin. Pure
geometry here; the writing is in save().
"""

from __future__ import annotations

import logging
import os
import re
import tempfile
import time
from collections.abc import Sequence
from pathlib import Path

from peek.geometry import Rect, union_all
from peek.schema import ParsedQuestion

log = logging.getLogger(__name__)

PAD_PT = 12.0  # margin around a crop, in points
# A figure belongs to a question when the gap between them is at most this share of
# the screen's width (a multi-part page: every part gets the shared figure).
FIGURE_REACH = 0.4
# A detected "figure" mostly inside the question's own area is its answer widget or
# drawing canvas, already in the crop: it never stands in for the real figure.
INSIDE_QUESTION = 0.5


def _gap(a: Rect, b: Rect) -> float:
    """Distance between two rects (0 when they touch or overlap)."""
    dx = max(b.x - a.max_x, a.x - b.max_x, 0.0)
    dy = max(b.y - a.max_y, a.y - b.max_y, 0.0)
    return (dx * dx + dy * dy) ** 0.5


def _overlap(a: Rect, b: Rect) -> float:
    w = min(a.max_x, b.max_x) - max(a.x, b.x)
    h = min(a.max_y, b.max_y) - max(a.y, b.y)
    return w * h if w > 0 and h > 0 else 0.0


def _figure_for(region: Rect, figures: Sequence[Rect], reach: float) -> Rect | None:
    """The figure that belongs with a question: the nearest one within *reach* that
    adds something. Left out: regions mostly inside the question's area (its answer
    widget), and regions that overlap it and run past both its top and its bottom
    (a canvas or page area wrapped around the question, often up to the window's
    top). Ties go to the figure with the most area outside the question."""
    best = None
    for f in figures:
        area = f.w * f.h
        inside = _overlap(f, region)
        if area <= 0 or inside / area >= INSIDE_QUESTION:
            continue
        if inside > 0 and f.y < region.y and f.max_y > region.max_y:
            continue
        gap = _gap(region, f)
        if gap > reach:
            continue
        key = (gap, -(area - inside))
        if best is None or key < best[0]:
            best = (key, f)
    return best[1] if best else None


def question_crops(
    questions: Sequence[ParsedQuestion],
    block_rects: dict[str, Rect],
    figure_rects: Sequence[Rect],
    width: int,
    height: int,
    scale: float,
) -> list[tuple[ParsedQuestion, Rect]]:
    """(question, crop) in capture pixels, whole-pixel aligned, inside the image.
    A question with nothing to locate it by is left out."""
    crops = []
    pad = PAD_PT * scale
    for q in questions:
        parts = [
            block_rects[b] for b in (*q.question_block_ids, *q.target_block_ids) if b in block_rects
        ]
        if q.approx_bbox_norm is not None:
            nx, ny, nw, nh = q.approx_bbox_norm
            parts.append(Rect(nx * width, ny * height, nw * width, nh * height))
        if not parts:
            continue
        region = union_all(parts)
        figure = _figure_for(region, figure_rects, FIGURE_REACH * width)
        if figure is not None:
            region = region.union(figure)
        x0, y0 = max(0, int(region.x - pad)), max(0, int(region.y - pad))
        x1 = min(width, int(region.max_x + pad + 0.999))
        y1 = min(height, int(region.max_y + pad + 0.999))
        if x1 - x0 < 2 or y1 - y0 < 2:
            continue
        crops.append((q, Rect(x0, y0, x1 - x0, y1 - y0)))
    return crops


_UNSAFE_NAME = re.compile(r"[^\w\-. ]+")


def file_name(stamp: str, question_id: str, index: int, display: int = 0) -> str:
    """'Peek 2026-11-19 at 13.45.02 - A.png'; the question's id, made safe for a
    file name, or its number when the id has nothing usable."""
    label = _UNSAFE_NAME.sub("", question_id).strip(" .")[:24] or str(index + 1)
    screen = f" (screen {display + 1})" if display else ""
    return f"Peek {stamp}{screen} - {label}.png"


def _unique(folder: Path, name: str) -> Path:
    path = folder / name
    stem, n = path.stem, 2
    while path.exists():
        path = folder / f"{stem} ({n}).png"
        n += 1
    return path


class SaveError(Exception):
    """Short, safe message for the log and the menu."""


def save(
    cgimage,
    width: int,
    height: int,
    questions: Sequence[ParsedQuestion],
    block_rects: dict[str, Rect],
    figure_rects: Sequence[Rect],
    folder: str,
    scale: float,
    *,
    display: int = 0,
    when: float | None = None,
) -> list[Path]:
    """Write one PNG per question into *folder* (which must exist). Raises SaveError.
    Runs on a solve thread."""
    import Quartz

    from peek.debug import png_bytes

    target = Path(folder).expanduser()
    if not folder or not target.is_dir():
        raise SaveError("the folder for saved questions is missing")
    stamp = time.strftime("%Y-%m-%d at %H.%M.%S", time.localtime(when))
    written: list[Path] = []
    for index, (q, crop) in enumerate(
        question_crops(questions, block_rects, figure_rects, width, height, scale)
    ):
        piece = Quartz.CGImageCreateWithImageInRect(
            cgimage, Quartz.CGRectMake(crop.x, crop.y, crop.w, crop.h)
        )
        if piece is None:
            continue
        data = png_bytes(piece)
        path = _unique(target, file_name(stamp, q.id, index, display))
        try:
            fd, tmp = tempfile.mkstemp(prefix=".peek-", suffix=".png", dir=target)
            try:
                with os.fdopen(fd, "wb") as f:
                    f.write(data)
                os.replace(tmp, path)
            except BaseException:
                Path(tmp).unlink(missing_ok=True)
                raise
        except PermissionError:
            raise SaveError("Peek isn't allowed to write to that folder") from None
        except OSError as exc:
            raise SaveError(f"couldn't write to that folder ({exc.strerror or exc})") from None
        written.append(path)
    return written
