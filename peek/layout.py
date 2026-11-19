"""Where each answer goes: pure geometry, unit-tested, no AppKit.

Owner's answer style: an answer is plain
text in the question's own colour and size. Placement keeps the spec's rules:

- answer_box with a box: centred in the empty field / box. A blank line comes as
  the writing area whose bottom edge is the line; text that is taller than that
  area sits on the line. A short answer shrinks to fit the width.
- answer_box with only a label block (e.g. "Answer:"): just right of the label.
- choice: the option's letter in yellow, after the end of the question's last line.
- below_question: under the question's last line, left-aligned with the question.

A long answer (a free-body diagram described in words) wraps onto several lines,
as wide as the question when it goes under or over it, instead of being cut.

Answers never cover text on screen if a free spot exists: each kind has a short
list of candidate spots, and the first one that overlaps no OCR text, no other
answer and stays on screen wins.

Coordinates: inputs are capture pixels (top-left origin); outputs are points in
the overlay view of that display (flipped, top-left origin, the screen's size).
"""

from __future__ import annotations

import statistics
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from peek.geometry import Rect
from peek.schema import ParsedQuestion
from peek.textstyle import TextStyle

RGB = tuple[int, int, int]
Measure = Callable[[str, float], tuple[float, float]]  # (text, font size) -> (w, h) points
# Tight glyph height of a text in the system font, per point of font size (0.5 for
# "xxxx", 0.89 for a line with capitals and descenders).
InkHeight = Callable[[str], float]

YELLOW: RGB = (255, 204, 0)  # systemYellow (light); the multiple-choice letter
FALLBACK_RGB: RGB = (29, 29, 31)
FONT_PER_BOX_HEIGHT = 0.9  # OCR box height -> font size (measured 0.8-1.1)
# Vision box width vs the text's advance width, both at the true size (median 0.995
# on system-font text, 9-40 pt, 1x and 2x).
INK_TO_ADVANCE = 1.0
# Vision's box height vs the text's tight glyph height. Calibrated so system-font
# lines average a width/height estimate ratio of 1.0 (worksheet render, 12-18 pt;
# single lines still vary 0.83-1.19).
BOX_PER_INK = 1.21
# One line on its own: width and height estimates within this ratio count as the
# system font; outside it (monospace ~1.24, serif ~0.8) the height estimate is used.
WIDTH_AGREES = (0.82, 1.22)
# The page's font is judged from lines within this many line heights of the question.
PAGE_RATIO_REACH = 12
PAGE_RATIO_MIN_LINES = 3
# A page ratio inside this band is treated as the system font and the width estimate
# is used as is. Measured on real OCR: system 0.97 (14 pt) to 1.14 (28 pt), Menlo
# 1.29 (corrected), Times 0.89-0.94 (indistinguishable from noise, so Times pages
# come out 6-14% small; see README).
SYSTEM_FONT_BAND = (0.8, 1.2)
MIN_FONT, MAX_FONT, DEFAULT_FONT = 9.0, 40.0, 14.0
MAX_WIDTH = 420.0  # points; a longer answer wraps (under a question: at its width)
MAX_LINES = 6  # a wrapped answer longer than this is cut with an ellipsis
# Besides the spots next to the question, an answer may go under one of the next
# lines of text below it, this many of the answer's font sizes down at most.
FURTHER_BELOW_REACH = 8.0
EDGE = 4.0  # keep answers this far inside the screen
CLEAR = 1.5  # minimum gap between an answer and any text


@dataclass(frozen=True)
class DrawItem:
    question_id: str
    text: str
    rect: Rect  # view points, top-left origin; the text's measured box
    font_size: float
    rgb: RGB
    kind: str  # answer_box | choice | below_question
    spot: str  # which candidate was used (for the debug PNG and tests)


@dataclass(frozen=True)
class LayoutInput:
    questions: Sequence[ParsedQuestion]
    block_rects_px: dict[str, Rect]  # every OCR block on this display
    styles: dict[str, TextStyle]  # by question id, measured in capture pixels
    scale: float  # capture pixels per point
    view_width: float  # points
    view_height: float
    block_texts: dict[str, str] | None = None  # OCR text by block id (for font size)
    fixed_font_size: float | None = None  # Settings → Overlay → Text size, if not "match"


def _to_pt(r: Rect, scale: float) -> Rect:
    return Rect(r.x / scale, r.y / scale, r.w / scale, r.h / scale)


def _overlaps(a: Rect, b: Rect, margin: float = CLEAR) -> bool:
    return (
        a.x < b.max_x + margin
        and b.x < a.max_x + margin
        and a.y < b.max_y + margin
        and b.y < a.max_y + margin
    )


def _fit(text: str, font: float, max_w: float, measure: Measure) -> tuple[str, float, float]:
    """Cut *text* with an ellipsis until it is at most max_w wide."""
    w, h = measure(text, font)
    if w <= max_w:
        return text, w, h
    lo, hi = 1, len(text)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if measure(text[:mid].rstrip() + "…", font)[0] <= max_w:
            lo = mid
        else:
            hi = mid - 1
    cut = text[:lo].rstrip() + "…"
    w, h = measure(cut, font)
    return cut, w, h


def _first_line(text: str, font: float, max_w: float, measure: Measure) -> str:
    """The longest start of *text* that fits max_w, broken after a word if it can
    be (a word longer than the whole line is broken where it must)."""
    lo, hi = 1, len(text)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if measure(text[:mid].rstrip(), font)[0] <= max_w:
            lo = mid
        else:
            hi = mid - 1
    if lo < len(text) and not text[lo].isspace():
        space = text.rfind(" ", 0, lo)
        if space > 0:
            lo = space
    return text[:lo].rstrip()


def _wrap(text: str, font: float, max_w: float, measure: Measure) -> tuple[str, float, float]:
    """*text* on one line if it fits max_w; otherwise wrapped into at most MAX_LINES
    lines of at most max_w, the last one cut with an ellipsis if text is left over.

    Returns (the lines joined by newlines, the widest line's width, the height).
    The overlay draws line i at y + i * height / lines."""
    w, h = measure(text, font)
    if w <= max_w:
        return text, w, h
    lines: list[str] = []
    rest = text
    while rest:
        if len(lines) == MAX_LINES - 1:
            lines.append(_fit(rest, font, max_w, measure)[0])
            break
        line = _first_line(rest, font, max_w, measure)
        lines.append(line)
        rest = rest[len(line) :].lstrip()
    width = max(measure(line, font)[0] for line in lines)
    return "\n".join(lines), width, h * len(lines)


def _estimates(
    rect: Rect, text: str | None, measure: Measure, ink_height: InkHeight | None
) -> tuple[float | None, float]:
    """(width-based, height-based) font size of one OCR block, in points.

    Width: the text measured in the system font at the size being estimated (SF's
    width per point shrinks as it grows), iterated to a fixed point. None when the
    text is too short to measure. Height: the box height against this text's own
    glyph height (capitals, descenders), or a flat factor without ink_height."""
    clean = (text or "").strip()
    by_height = rect.h * FONT_PER_BOX_HEIGHT
    if ink_height is not None and clean:
        per_pt = ink_height(clean)
        if per_pt > 0:
            by_height = rect.h / (per_pt * BOX_PER_INK)
    if len(clean) < 6 or by_height <= 0:
        return None, by_height
    size = by_height
    for _ in range(3):
        per_pt = measure(clean, size)[0] / size
        if per_pt <= 0:
            return None, by_height
        size = rect.w * INK_TO_ADVANCE / per_pt
    return size, by_height


def font_size_for(
    rects: Sequence[Rect],
    texts: Sequence[str | None],
    height_px_per_pt: float | None,
    measure: Measure,
    ink_height: InkHeight | None = None,
    page_ratio: float | None = None,
) -> float | None:
    """The question's font size in points.

    page_ratio is the median width/height estimate ratio of the lines around the
    question: about 1 for system-font text, ~1.24 for monospace, ~0.85 for Times.
    Dividing the (precise) width estimate by it corrects for the page's font; one
    line's height is too noisy to do that on its own. Without a page ratio, a width
    estimate that disagrees with the height estimate falls back to the height."""
    estimates = []
    for rect, text in zip(rects, texts, strict=True):
        by_width, by_height = _estimates(rect, text, measure, ink_height)
        if by_height <= 0:
            continue
        if by_width is None:
            estimates.append(by_height)
        elif page_ratio is not None:
            in_band = SYSTEM_FONT_BAND[0] <= page_ratio <= SYSTEM_FONT_BAND[1]
            estimates.append(by_width if in_band else by_width / page_ratio)
        else:
            ratio = by_width / by_height
            estimates.append(by_width if WIDTH_AGREES[0] <= ratio <= WIDTH_AGREES[1] else by_height)
    if estimates:
        return statistics.median(estimates)
    return height_px_per_pt


def page_ratio_near(
    center_y: float,
    line_h: float,
    blocks: dict[str, Rect],
    texts: dict[str, str],
    measure: Measure,
    ink_height: InkHeight | None,
) -> float | None:
    """Median width/height estimate ratio of the lines within a few line heights
    of the question (at least PAGE_RATIO_MIN_LINES of them), clamped."""
    ratios = []
    for bid, rect in blocks.items():
        if abs(rect.mid_y - center_y) > PAGE_RATIO_REACH * max(line_h, 1.0):
            continue
        by_width, by_height = _estimates(rect, texts.get(bid), measure, ink_height)
        if by_width is not None and by_height > 0:
            ratios.append(by_width / by_height)
    if len(ratios) < PAGE_RATIO_MIN_LINES:
        return None
    return min(max(statistics.median(ratios), 0.7), 1.45)


def _intersection(a: Rect, b: Rect) -> float:
    w = min(a.max_x, b.max_x) - max(a.x, b.x)
    h = min(a.max_y, b.max_y) - max(a.y, b.y)
    return w * h if w > 0 and h > 0 else 0.0


def _is_blank(text: str | None) -> bool:
    """A block of underscores or dots (a fill-in line), not words."""
    return text is not None and not any(c.isalnum() for c in text)


def layout(
    inp: LayoutInput, measure: Measure, ink_height: InkHeight | None = None
) -> list[DrawItem]:
    blocks_pt = {bid: _to_pt(r, inp.scale) for bid, r in inp.block_rects_px.items()}
    texts_by_id = inp.block_texts or {}
    view = Rect(EDGE, EDGE, inp.view_width - 2 * EDGE, inp.view_height - 2 * EDGE)
    placed: list[Rect] = []
    items: list[DrawItem] = []
    question_blocks = {
        b for q in inp.questions for b in (*q.question_block_ids, *q.target_block_ids)
    }

    def clamp(r: Rect) -> Rect:
        x = min(max(r.x, view.x), max(view.x, view.max_x - r.w))
        y = min(max(r.y, view.y), max(view.y, view.max_y - r.h))
        return Rect(x, y, r.w, r.h)

    def cost(r: Rect, skip: frozenset[str] = frozenset()) -> float:
        """0 for a free spot; otherwise how much text/answers it would cover."""
        total = sum(_intersection(r, o) for o in placed if _overlaps(r, o))
        for bid, t in blocks_pt.items():
            if bid in skip or not _overlaps(r, t):
                continue
            total += max(_intersection(r, t), 1.0)
        return total

    for q in inp.questions:
        q_rects = [blocks_pt[b] for b in q.question_block_ids if b in blocks_pt]
        t_rects = [blocks_pt[b] for b in q.target_block_ids if b in blocks_pt]
        style = inp.styles.get(q.id)

        texts = [texts_by_id.get(b) for b in q.question_block_ids if b in blocks_pt]
        style_font = (
            style.line_height_px / inp.scale * FONT_PER_BOX_HEIGHT if style is not None else None
        )
        ratio = None
        if q_rects:
            ratio = page_ratio_near(
                statistics.fmean(r.mid_y for r in q_rects),
                statistics.median(r.h for r in q_rects),
                blocks_pt,
                texts_by_id,
                measure,
                ink_height,
            )
        if inp.fixed_font_size:
            font = float(inp.fixed_font_size)
        else:
            font = font_size_for(q_rects, texts, style_font, measure, ink_height, ratio)
            font = min(max(font or DEFAULT_FONT, MIN_FONT), MAX_FONT)
        rgb = YELLOW if q.target_kind == "choice" else (style.rgb if style else FALLBACK_RGB)
        gap = font * 0.4  # about one space
        _, w_full, h = _fit(q.answer, font, 10_000, measure)

        # 1. Inside the model's answer box, if that box is really empty.
        if q.target_kind == "answer_box" and q.approx_bbox_norm is not None:
            nx, ny, nw, nh = q.approx_bbox_norm
            area = Rect(
                nx * inp.view_width, ny * inp.view_height, nw * inp.view_width, nh * inp.view_height
            )
            if w_full <= MAX_WIDTH:
                # A short answer stays on one line and shrinks to fit the box.
                size, fitted = font, _fit(q.answer, font, MAX_WIDTH, measure)
                while size > MIN_FONT and fitted[1] > max(area.w - 8, 1):
                    size = max(MIN_FONT, size - 1)
                    fitted = _fit(q.answer, size, MAX_WIDTH, measure)
            else:
                # A long one keeps its size and wraps (the box is usually one line
                # tall, so it often doesn't fit and goes to the other spots).
                room = min(max(area.w - 8, MAX_WIDTH), max(view.w, 1))
                size, fitted = font, _wrap(q.answer, font, room, measure)
            text, w, hh = fitted
            x = area.x + (area.w - w) / 2
            # Taller than a blank line's writing area: sit on the line instead.
            y = area.y + (area.h - hh) / 2 if hh <= area.h else area.max_y - hh - 1
            rect = clamp(Rect(x, y, w, hh))
            own = frozenset(
                b
                for b in (*q.question_block_ids, *q.target_block_ids, *blocks_pt)
                # A wrapped answer may not grow over its own question's text.
                if (b in q.question_block_ids and w_full <= MAX_WIDTH)
                or b in q.target_block_ids
                or _is_blank(texts_by_id.get(b))
            )
            if cost(rect, own) == 0 or not (q_rects or t_rects):
                items.append(DrawItem(q.id, text, rect, size, rgb, q.target_kind, "in_box"))
                placed.append(rect)
                continue
            # The box overlaps text it shouldn't (a misplaced box): use the other spots.

        # Anchors: the question's last line (lowest; rightmost on that row) and first.
        last = first = None
        if q_rects:
            bottom = max(r.mid_y for r in q_rects)
            row = [r for r in q_rects if abs(r.mid_y - bottom) <= max(r.h for r in q_rects) / 2]
            last = max(row, key=lambda r: r.max_x)
            first = min(q_rects, key=lambda r: (r.y, r.x))
        left = min((r.x for r in q_rects), default=None)
        # Under or over the question, an answer may be as wide as the question.
        column = max(MAX_WIDTH, max(r.max_x for r in q_rects) - left) if q_rects else MAX_WIDTH

        # Each spot: (name, x, y_top_for(height)).
        spots: list[tuple[str, float, Callable[[float], float]]] = []
        if q.target_kind == "answer_box" and t_rects:
            label = max(t_rects, key=lambda r: r.max_x)
            spots.append(
                ("after_label", label.max_x + gap * 2, lambda hh, r=label: r.mid_y - hh / 2)
            )
        if q.target_kind == "choice" and last is not None:
            spots.append(("end_of_question", last.max_x + gap, lambda hh, r=last: r.mid_y - hh / 2))
        if q.target_kind == "choice" and t_rects:
            option = max(t_rects, key=lambda r: r.max_x)
            spots.append(
                ("after_option", option.max_x + gap, lambda hh, r=option: r.mid_y - hh / 2)
            )
        if last is not None and left is not None:
            bottom_y = max(r.max_y for r in q_rects)
            below = ("below_question", left, lambda hh, b=bottom_y, f=font: b + f * 0.25)
            end = ("end_of_question", last.max_x + gap, lambda hh, r=last: r.mid_y - hh / 2)
            above = ("above_question", left, lambda hh, r=first, f=font: r.y - hh - f * 0.25)
            spots += [below, above] if q.target_kind == "choice" else [below, end, above]
            if q.target_kind != "choice":
                # Under one of the next lines of text below the question (a link, an
                # instruction), when the gaps next to it are too small.
                # Never past the next question: there it would read as its answer.
                col_right = left + column

                def in_column(t: Rect, top=bottom_y, x0=left, x1=col_right) -> bool:
                    return top < t.y and t.x < x1 and t.max_x > x0

                own_ids = {*q.question_block_ids, *q.target_block_ids}
                next_question = min(
                    (
                        blocks_pt[b].y
                        for b in question_blocks - own_ids
                        if b in blocks_pt and in_column(blocks_pt[b])
                    ),
                    default=float("inf"),
                )
                reach = min(bottom_y + FURTHER_BELOW_REACH * font, next_question)
                under = sorted(
                    {
                        round(t.max_y, 1)
                        for bid, t in blocks_pt.items()
                        if bid not in question_blocks and in_column(t) and t.max_y <= reach
                    }
                )
                spots += [
                    ("further_below", left, lambda hh, b=y, f=font: b + f * 0.25) for y in under
                ]
        elif t_rects:
            anchor = t_rects[0]
            spots.append(
                ("below_target", anchor.x, lambda hh, r=anchor, f=font: r.max_y + f * 0.25)
            )
        if not spots:
            continue  # nothing to anchor to (parse_lenient should have dropped it)

        words = q.answer.split()
        best = None  # (damaged, cost, order, name, rect, text)
        for order, (name, x, y_for) in enumerate(spots):
            wide = name in ("below_question", "above_question", "further_below")
            room = min(column if wide else MAX_WIDTH, view.max_x - x)
            if q.target_kind == "choice":
                if w_full > room:
                    continue  # never cut a choice letter
                text, w, hh = q.answer, w_full, h
            else:
                if room < min(w_full, font * 3):
                    continue  # no useful room here
                text, w, hh = _wrap(q.answer, font, room, measure)
            rect = clamp(Rect(x, y_for(hh), w, hh))
            c = cost(rect)
            # Cut, broken mid-word, or a tall column beside the question: only if
            # no spot shows the answer whole.
            damaged = text.split() != words or (not wide and text.count("\n") >= 2)
            if best is None or (damaged, c, order) < best[:3]:
                best = (damaged, c, order, name, rect, text)
            if c == 0 and not damaged:
                break
        if best is None:  # every spot too narrow: last resort, cut to the screen
            name, x, y_for = spots[0]
            text, w, hh = _wrap(q.answer, font, max(view.w, 1), measure)
            best = (True, 0, 0, name, clamp(Rect(x, y_for(hh), w, hh)), text)
        _, _, _, name, rect, text = best
        items.append(DrawItem(q.id, text, rect, font, rgb, q.target_kind, name))
        placed.append(rect)
    return items
