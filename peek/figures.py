"""Find diagrams on a screenshot, so the model can be sent a zoomed-in copy.

A diagram is a region dense with lines and edges that OCR did not read as text:
the screenshot is reduced to a grid of cells, each cell gets an edge density,
cells covered by OCR text are set aside, and the remaining busy cells are grown
into connected regions. Big enough regions are diagrams. Labels inside a diagram
(dimensions such as "300 mm") stay in its crop because the crop is the region's
bounding box, padded.

`find_figure_regions` is pure Python (grid in, rectangles out) and unit-tested;
`edge_grid` and `zoomed_figures` talk to Quartz and Pillow.

Tuned on real captures (Mastering-style homework pages, a PDF worksheet, plain
text pages): every figure found, browser chrome and text-only pages left alone.
"""

from __future__ import annotations

import math
import re
from collections import deque
from collections.abc import Sequence
from dataclasses import dataclass

from peek.geometry import Rect
from peek.imaging import HIGH_RES, STANDARD, SentImage, encode_for_model, fit_size

CELL_PT = 12  # grid cell size
REDUCE_PT = 2  # edges are found on a copy with one pixel per 2 pt
EDGE_THRESHOLD = 28  # gradient (0-255) above which a pixel counts as an edge
MIN_STROKE_CELLS = 40  # largest unbroken run of busy cells a drawing must have
BUSY_CELL = 0.06  # fraction of edge pixels that makes a cell part of a drawing
MIN_SIDE_PT = 90  # a diagram is at least this big in both directions...
MIN_AREA_FRACTION = 0.015  # ...and covers at least this much of the screen
MAX_AREA_FRACTION = 0.6  # a region this large is the screen itself (video, photo)
MAX_ASPECT = 6.0  # toolbars and rules are long and thin
MIN_FILL = 0.05  # busy cells / cells in the bounding box (real figures: 0.15-0.3)...
MAX_FILL = 0.6  # ...a photo or texture is busy nearly everywhere
MIN_FLAT = 0.66  # see flatness(); filled bar charts measured ~0.7
MIN_FLAT_LABELLED = 0.58  # with labels inside: shaded illustrations 0.59-0.64,
MIN_LABELS = 0.02  # ...while pictures with text in them measured at most 0.56
MIN_DETAIL_CELLS = 25  # cells of curves/diagonals/arrowheads/corners a drawing has
# Second pass (rectilinear drawings), stricter: labelled drawings measured text
# 0-0.23 and texture 0-0.33; the forms and answer areas that remain after the
# flatness check, 0.29 and up.
MAX_AREA_RECTILINEAR = 0.35
MAX_TEXT_RECTILINEAR = 0.2
MAX_TEXT_DETAIL = 0.3  # first pass: real figures measured at most 0.23
MAX_FILL_DETAIL = 0.4  # first pass: real figures at most 0.26; emoji grids 0.43+
MAX_FILL_SMALL = 0.5  # a small figure (< SMALL_AREA of the screen) is denser
SMALL_AREA = 0.06
MAX_TALL = 3.5  # height/width: real figures at most 1.4; minimaps, strips 3.7+
# Text masking (see is_text)
TALL_TEXT = 2.5  # a box this many median line heights tall is checked as a heading
TALL_TEXT_LIMIT = 4.0  # bare rects (no text known) taller than this are not text
HEADING_PER_CHAR = (0.35, 0.68)
MAX_HEADING = 10.0  # median line heights; a 96 pt title over 14 pt text is ~6
MAX_TEXT_PER_CHAR = 1.1
MAX_TEXTURE_RECTILINEAR = 0.35
MAX_FIGURES = 2
TEXTURE_CELL = 0.3  # a cell this full of edges is texture (photo), not line work
LONG_LINE_PT = 60  # straight edges this long are left out of the detail map
CHROME_LINE_FRACTION = 0.3  # straight edges this long (of the screen) are UI chrome
ZOOM_LONG_SIDE = 1568  # zoomed views are enlarged towards this long side...
MAX_ZOOM = 3.0  # ...but never more than 3x
MIN_ZOOM_GAIN = 1.25  # a view is sent only if this much larger than in the screenshot
PAD_FRACTION = 0.04


@dataclass(frozen=True)
class EdgeGrid:
    cols: int
    rows: int
    # Row-major fractions of edge pixels per cell. `density` has only window-scale
    # straight lines removed (panel borders, rules across the page); `detail` has
    # every straight horizontal/vertical line of LONG_LINE_PT or more removed, so
    # what is left is curves, diagonals, arrowheads, ticks, corners and texture.
    density: list[float]
    detail: list[float]
    cell_px: float  # capture pixels per cell
    # The grayscale copy the edges were found on (row-major bytes, gray_width wide,
    # gray_px capture pixels per pixel), for flatness. None in pure-grid tests.
    gray: bytes | None = None
    gray_width: int = 0
    gray_px: float = 1.0


@dataclass(frozen=True)
class Candidate:
    """One connected region and the measurements the filters look at."""

    rect: Rect  # capture pixels, unpadded
    stroke: int  # largest 8-connected group of busy cells
    detail: int  # busy cells in the detail map (not straight lines, not text)
    fill: float  # busy cells / cells in the bounding box
    text: float  # OCR text cells / cells in the bounding box
    texture: float  # busy cells with a very high edge density / busy cells
    flat: float  # share of the region's pixels at its background gray
    reason: str  # "" if accepted, else why not


def _box(item) -> Rect:
    """Text items are OCR blocks (with .box and .text) or bare Rects (tests)."""
    return getattr(item, "box", item)


def _median_height(items: Sequence) -> float:
    heights = sorted(_box(i).h for i in items)
    return heights[len(heights) // 2] if heights else 0.0


def is_text(item, median_h: float) -> bool:
    """Is this OCR block really a line of text? Vision also "reads" line art:
    hatching, a zigzag spring or resistor ('1171111', 'ww', 'TITT') gives a box far
    wider per character than any text; a chart or a spring can come back as one
    tall block ('Illi', 'minu') whose width per character is unlike a heading's.
    Such blocks are not masked, so the drawing under them still counts.

    Width per character, in box heights: text 0.3-0.8 (a lone digit's box can be
    wider: single characters are always trusted). Measured 2026-11-19."""
    box, text = _box(item), getattr(item, "text", None)
    if text is None:  # a bare rect: only the size can be judged
        return box.h <= TALL_TEXT_LIMIT * median_h if median_h else True
    n = len(text.strip())
    if n == 0 or box.h <= 0:
        return False
    per_char = box.w / (box.h * n)
    if median_h and box.h > MAX_HEADING * median_h:
        return False  # no heading is this many lines tall: a chart read as "text"
    if median_h and box.h > TALL_TEXT * median_h:
        return HEADING_PER_CHAR[0] <= per_char <= HEADING_PER_CHAR[1]
    return n < 2 or per_char <= MAX_TEXT_PER_CHAR


def _text_mask(grid: EdgeGrid, text_boxes_px: Sequence) -> list[bool]:
    cols, rows, cell = grid.cols, grid.rows, grid.cell_px
    text = [False] * (cols * rows)
    median_h = _median_height(text_boxes_px)
    for item in text_boxes_px:
        if not is_text(item, median_h):
            continue
        box = _box(item)
        pad = box.h * 0.25  # glyph edges spill past Vision's box
        c0, c1 = int((box.x - pad) // cell), int((box.max_x + pad) // cell)
        r0, r1 = int((box.y - pad) // cell), int((box.max_y + pad) // cell)
        for r in range(max(r0, 0), min(r1, rows - 1) + 1):
            for c in range(max(c0, 0), min(c1, cols - 1) + 1):
                text[r * cols + c] = True
    return text


def flatness(grid: EdgeGrid, rect: Rect) -> float:
    """Share of the region's pixels within one 8-level step of its most common gray.
    Line drawings are mostly page background (measured 0.77-0.97); photographs,
    thumbnails and image cards spread over many grays (0.12-0.61)."""
    if grid.gray is None or grid.gray_width <= 0:
        return 1.0
    gw, gp = grid.gray_width, grid.gray_px
    gh = len(grid.gray) // gw
    x0, x1 = max(0, int(rect.x / gp)), min(gw, int(rect.max_x / gp))
    y0, y1 = max(0, int(rect.y / gp)), min(gh, int(rect.max_y / gp))
    if x1 <= x0 or y1 <= y0:
        return 1.0
    bins = [0] * 32
    for y in range(y0, y1):
        for v in grid.gray[y * gw + x0 : y * gw + x1]:
            bins[v >> 3] += 1
    mode = max(range(32), key=bins.__getitem__)
    return sum(bins[max(0, mode - 1) : mode + 2]) / ((x1 - x0) * (y1 - y0))


def candidate_regions(
    grid: EdgeGrid,
    text_boxes_px: Sequence,  # OCR blocks (.box, .text) or bare Rects
    image_width: int,
    image_height: int,
    scale: float,
    *,
    rectilinear: bool = False,
) -> list[Candidate]:
    """Every connected busy region, measured and judged (see find_figure_regions).
    rectilinear=False groups cells of the detail map, True of the density map."""
    cols, rows, cell = grid.cols, grid.rows, grid.cell_px
    text = _text_mask(grid, text_boxes_px)
    source = grid.density if rectilinear else grid.detail
    busy = [d >= BUSY_CELL and not t for d, t in zip(source, text, strict=True)]
    detail = [d >= BUSY_CELL and not t for d, t in zip(grid.detail, text, strict=True)]
    # Close small gaps between strokes: a cell joins if a busy cell is within 2 cells.
    grown = [False] * len(busy)
    for r in range(rows):
        for c in range(cols):
            if not busy[r * cols + c]:
                continue
            for rr in range(max(r - 2, 0), min(r + 2, rows - 1) + 1):
                for cc in range(max(c - 2, 0), min(c + 2, cols - 1) + 1):
                    grown[rr * cols + cc] = True

    seen = [False] * len(grown)
    screen_area = image_width * image_height
    out: list[Candidate] = []
    for start in range(len(grown)):
        if not grown[start] or seen[start]:
            continue
        queue, cells, hits, details, dense = deque([start]), [], 0, 0, 0
        seen[start] = True
        while queue:
            i = queue.popleft()
            cells.append(i)
            hits += busy[i]
            details += detail[i]
            dense += busy[i] and grid.detail[i] >= TEXTURE_CELL
            r, c = divmod(i, cols)
            for rr, cc in ((r - 1, c), (r + 1, c), (r, c - 1), (r, c + 1)):
                if 0 <= rr < rows and 0 <= cc < cols:
                    j = rr * cols + cc
                    if grown[j] and not seen[j]:
                        seen[j] = True
                        queue.append(j)
        if hits == 0:
            continue
        rs = [i // cols for i in cells]
        cs = [i % cols for i in cells]
        x0, x1 = min(cs) * cell, (max(cs) + 1) * cell
        y0, y1 = min(rs) * cell, (max(rs) + 1) * cell
        w, h = min(x1, image_width) - x0, min(y1, image_height) - y0
        c0, c1, r0, r1 = min(cs), max(cs), min(rs), max(rs)
        box_cells = (c1 - c0 + 1) * (r1 - r0 + 1)
        fill = hits / box_cells
        texts = (
            sum(text[r * cols + c] for r in range(r0, r1 + 1) for c in range(c0, c1 + 1))
            / box_cells
        )
        # Labels well inside the region (its middle, a fifth in from each side):
        # an illustration's own labels, not a paragraph wrapped around a picture.
        ir0, ir1 = r0 + (r1 - r0) // 5, r1 - (r1 - r0) // 5
        ic0, ic1 = c0 + (c1 - c0) // 5, c1 - (c1 - c0) // 5
        inner = sum(
            text[r * cols + c] for r in range(ir0, ir1 + 1) for c in range(ic0, ic1 + 1)
        ) / max((ir1 - ir0 + 1) * (ic1 - ic0 + 1), 1)
        area = (w * h) / screen_area
        stroke = _largest_stroke(cells, busy, cols)
        if min(w, h) / scale < MIN_SIDE_PT:
            reason = "small"
        elif not MIN_AREA_FRACTION <= area <= MAX_AREA_FRACTION:
            reason = "area"
        elif max(w, h) / max(min(w, h), 1) > MAX_ASPECT or h > MAX_TALL * w:
            reason = "thin"  # rules, toolbars; minimaps, thumbnail strips, chat columns
        elif stroke < MIN_STROKE_CELLS:
            # Drawings are made of strokes that run on unbroken; window chrome
            # (tab favicons, toolbar buttons) is small separate blobs that only
            # the gap closing joined together.
            reason = "blobs"
        elif details < MIN_DETAIL_CELLS:
            # Only straight frames: text fields, buttons, cards, tables.
            reason = "frames"
        elif not MIN_FILL <= fill <= _max_fill(rectilinear, area):
            # Too sparse to be one drawing, or texture everywhere (a photo, an
            # emoji grid).
            reason = "fill"
        elif not rectilinear and texts > MAX_TEXT_DETAIL:
            reason = "text"  # chat columns, spreadsheets: text with icons between
        elif rectilinear and area > MAX_AREA_RECTILINEAR:
            reason = "area"  # straight lines join whole page layouts together
        elif rectilinear and texts > MAX_TEXT_RECTILINEAR:
            reason = "text"  # forms, toolbars, cards: boxes around text
        elif rectilinear and dense / hits > MAX_TEXTURE_RECTILINEAR:
            reason = "texture"  # pictures inside a page layout
        elif (flat := flatness(grid, Rect(x0, y0, w, h))) < MIN_FLAT and not (
            inner >= MIN_LABELS and flat >= MIN_FLAT_LABELLED
        ):
            # Many grays: a photograph, thumbnails, window chrome. A shaded or
            # coloured textbook illustration gets a little more room when it has
            # labels in it ("m = 5.0 kg", "30°"); photos rarely contain text.
            reason = "photo"
        else:
            reason = ""
        out.append(
            Candidate(
                Rect(x0, y0, w, h),
                stroke,
                details,
                round(fill, 3),
                round(texts, 3),
                round(dense / hits, 3),
                round(flat, 3) if not reason or reason == "photo" else -1.0,
                reason,
            )
        )
    return out


def find_figure_regions(
    grid: EdgeGrid,
    text_boxes_px: Sequence,  # OCR blocks (.box, .text) or bare Rects
    image_width: int,
    image_height: int,
    scale: float,
    *,
    limit: int | None = MAX_FIGURES,
) -> list[Rect]:
    """Diagram regions in capture pixels, padded, largest first (at most *limit*).

    Two passes. The first groups the detail map (no long straight lines), which
    finds drawings with curves, diagonals and shading and never joins a figure to
    the window around it. The second groups the density map, which keeps straight
    lines shorter than window chrome, to find rectilinear drawings (a dimensioned
    plate, a block or circuit diagram, a bar chart); it is stricter about text and
    texture, because forms and page layouts are made of the same straight lines.
    A second-pass region that overlaps first-pass figures widens them into one (a
    beam's straight outline, a load arrow), unless the union would break the size
    limits; then the first-pass figures stay as they are."""
    args = (grid, text_boxes_px, image_width, image_height, scale)
    accepted = [c.rect for c in candidate_regions(*args) if not c.reason]
    for c in candidate_regions(*args, rectilinear=True):
        if c.reason:
            continue
        merged, absorbed = c.rect, []
        grew = True
        while grew:  # a union can reach regions the candidate alone did not touch
            grew = False
            for r in accepted:
                if r not in absorbed and _overlaps(r, merged):
                    merged, grew = merged.union(r), True
                    absorbed.append(r)
        if absorbed and not _within_limits(merged, image_width, image_height):
            continue
        accepted = [r for r in accepted if r not in absorbed] + [merged]
    accepted = [_with_labels(r, text_boxes_px, image_width, image_height) for r in accepted]
    accepted.sort(key=lambda r: r.w * r.h, reverse=True)
    regions = []
    for r in accepted[:limit]:
        pad_x, pad_y = r.w * PAD_FRACTION, r.h * PAD_FRACTION
        x0, y0 = max(0.0, r.x - pad_x), max(0.0, r.y - pad_y)
        x1, y1 = min(image_width, r.max_x + pad_x), min(image_height, r.max_y + pad_y)
        regions.append(Rect(x0, y0, x1 - x0, y1 - y0))
    return regions


def _with_labels(rect: Rect, text_items: Sequence, image_width: int, image_height: int) -> Rect:
    """The region widened to the labels around its line work: text cells are never
    busy, so an axis title ('t (s)'), a substituent ('CH3') or a dimension at the
    edge would otherwise be cut in half. Takes text lines that touch the region or
    sit within one line height of it, but not paragraphs (wider than the region)
    and not if the result would break the size limits."""
    median_h = _median_height(text_items)
    grown = rect
    for item in text_items:
        box = _box(item)
        if box.w > rect.w * 0.8 or not is_text(item, median_h):
            continue
        reach = max(box.h, median_h)
        near = (
            box.max_x >= rect.x - reach
            and box.x <= rect.max_x + reach
            and box.max_y >= rect.y - reach
            and box.y <= rect.max_y + reach
        )
        if near:
            grown = grown.union(box)
    area = grown.w * grown.h / (image_width * image_height)
    tall_ok = grown.h <= MAX_TALL * grown.w
    if (
        area > MAX_AREA_FRACTION
        or not tall_ok
        or max(grown.w, grown.h) > MAX_ASPECT * min(grown.w, grown.h)
    ):
        return rect
    return grown


def _within_limits(r: Rect, image_width: int, image_height: int) -> bool:
    area = r.w * r.h / (image_width * image_height)
    return area <= MAX_AREA_RECTILINEAR and max(r.w, r.h) / max(min(r.w, r.h), 1) <= MAX_ASPECT


def _max_fill(rectilinear: bool, area: float) -> float:
    if rectilinear:
        return MAX_FILL
    return MAX_FILL_SMALL if area < SMALL_AREA else MAX_FILL_DETAIL


def _overlaps(a: Rect, b: Rect) -> bool:
    return a.x < b.max_x and b.x < a.max_x and a.y < b.max_y and b.y < a.max_y


def _largest_stroke(cells: list[int], busy: list[bool], cols: int) -> int:
    """Size of the largest 8-connected group of busy cells among *cells*."""
    todo = {i for i in cells if busy[i]}
    best = 0
    while todo:
        queue, size = [todo.pop()], 0
        while queue:
            i = queue.pop()
            size += 1
            r, c = divmod(i, cols)
            for dr in (-1, 0, 1):
                for dc in (-1, 0, 1):
                    if dc and not 0 <= c + dc < cols:
                        continue
                    j = (r + dr) * cols + c + dc
                    if j in todo:
                        todo.remove(j)
                        queue.append(j)
        best = max(best, size)
    return best


# --- Quartz / Pillow ----------------------------------------------------------------


def _gray(cgimage, width: int, height: int):
    """The capture reduced to width x height grayscale, as a Pillow image."""
    import Quartz
    from PIL import Image

    space = Quartz.CGColorSpaceCreateDeviceGray()
    ctx = Quartz.CGBitmapContextCreate(
        None, width, height, 8, width, space, Quartz.kCGImageAlphaNone
    )
    if ctx is None:
        raise RuntimeError("could not create a grayscale context")
    Quartz.CGContextSetInterpolationQuality(ctx, Quartz.kCGInterpolationMedium)
    Quartz.CGContextDrawImage(ctx, Quartz.CGRectMake(0, 0, width, height), cgimage)
    image = Quartz.CGBitmapContextCreateImage(ctx)
    data = bytes(Quartz.CGDataProviderCopyData(Quartz.CGImageGetDataProvider(image)))
    stride = int(Quartz.CGImageGetBytesPerRow(image))
    return Image.frombytes("L", (width, height), data, "raw", "L", stride)


def remove_long_runs(data: bytes, width: int, height: int, min_run: int) -> bytes:
    """Zero every horizontal run of 255s at least *min_run* long (row-major 8-bit)."""
    out = bytearray(data)
    pattern = re.compile(rb"\xff{%d,}" % min_run)
    for y in range(height):
        row = y * width
        for m in pattern.finditer(data, row, row + width):
            out[m.start() : m.end()] = bytes(m.end() - m.start())
    return bytes(out)


def _strip_lines(binary, horizontal_run: int, vertical_run: int):
    """The edge image without straight horizontal/vertical runs of those lengths."""
    from PIL import Image

    w, h = binary.size
    data = remove_long_runs(binary.tobytes(), w, h, horizontal_run)
    flipped = Image.frombytes("L", (w, h), data).transpose(Image.Transpose.TRANSPOSE)
    data = remove_long_runs(flipped.tobytes(), h, w, vertical_run)
    return Image.frombytes("L", (h, w), data).transpose(Image.Transpose.TRANSPOSE)


def edge_grid(cgimage, image_width: int, image_height: int, scale: float) -> EdgeGrid:
    """Edge density per CELL_PT cell, found on a grayscale copy at REDUCE_PT per pixel.

    Straight horizontal and vertical edges longer than CHROME_LINE_FRACTION of the
    screen are removed first: they are panel borders, page margins and rules, and
    they would otherwise join every drawing on the screen into one region the size
    of the window. Shorter straight lines stay (a beam, a plate's outline, a block
    diagram's boxes) but are kept out of the `detail` map.
    """
    from PIL import ImageFilter

    reduce = max(1.0, REDUCE_PT * scale)  # capture pixels per reduced pixel
    w, h = max(1, round(image_width / reduce)), max(1, round(image_height / reduce))
    gray = _gray(cgimage, w, h)
    edges = gray.filter(ImageFilter.FIND_EDGES)
    binary = edges.point(lambda v: 255 if v > EDGE_THRESHOLD else 0)
    chrome = _strip_lines(
        binary,
        max(LONG_LINE_PT // REDUCE_PT, round(w * CHROME_LINE_FRACTION)),
        max(LONG_LINE_PT // REDUCE_PT, round(h * CHROME_LINE_FRACTION)),
    )
    straight = LONG_LINE_PT // REDUCE_PT
    detail = _strip_lines(chrome, straight, straight)
    cell_small = CELL_PT // REDUCE_PT
    cols, rows = math.ceil(w / cell_small), math.ceil(h / cell_small)

    def per_cell(image) -> list[float]:
        # Box-average down to one value per cell (Pillow's reduce: mean of each block).
        padded = image.crop((0, 0, cols * cell_small, rows * cell_small))
        return [v / 255 for v in padded.reduce(cell_small).tobytes()]

    return EdgeGrid(
        cols,
        rows,
        per_cell(chrome),
        per_cell(detail),
        cell_small * image_width / w,
        gray=gray.tobytes(),
        gray_width=w,
        gray_px=image_width / w,
    )


def zoom_size(width: int, height: int) -> tuple[int, int]:
    """Size to send a figure crop at: enlarged (up to MAX_ZOOM) towards ZOOM_LONG_SIDE,
    within the standard tier so each zoomed view costs at most ~1568 tokens.

    Enlarging adds no detail, but the model reads images in 28 px patches: a thin
    line or a "300 mm" label spread over more patches is read more reliably."""
    factor = max(1.0, min(MAX_ZOOM, ZOOM_LONG_SIDE / max(width, height, 1)))
    return fit_size(round(width * factor), round(height * factor), *STANDARD)


@dataclass(frozen=True)
class ZoomedFigure:
    rect: Rect  # the region, in capture pixels
    image: SentImage

    def gain(self, sent_scale: float) -> float:
        """How many times larger the zoomed view is than the same region in a
        screenshot sent at *sent_scale* (sent pixels per capture pixel)."""
        return self.image.width / max(self.rect.w * sent_scale, 1.0)

    def adds_detail(self, sent_scale: float) -> bool:
        """A big figure is already large in the screenshot: a "zoomed" view capped
        at the standard tier could even be smaller. Only send views that are."""
        return self.gain(sent_scale) >= MIN_ZOOM_GAIN


def figure_regions(
    cgimage, width: int, height: int, text_boxes_px: Sequence, scale: float
) -> list[Rect]:
    """Every diagram on the capture, in capture pixels, largest first (no cap)."""
    return find_figure_regions(
        edge_grid(cgimage, width, height, scale),
        text_boxes_px,
        width,
        height,
        scale,
        limit=None,  # the cap applies to the views that are worth sending
    )


def zoomed_figures(
    cgimage,
    width: int,
    height: int,
    text_boxes_px: Sequence,
    scale: float,
    *,
    regions: Sequence[Rect] | None = None,
) -> list[ZoomedFigure]:
    """Detect diagrams (or take *regions* from figure_regions) and encode an
    enlarged copy of each, ready to send."""
    import Quartz

    if regions is None:
        regions = figure_regions(cgimage, width, height, text_boxes_px, scale)
    # Judged against the screenshot as normally sent (high-resolution tier). A view
    # that adds nothing there is not encoded at all; the rare standard-tier retry
    # then goes without it.
    screenshot_scale = fit_size(width, height, *HIGH_RES)[0] / width
    figures = []
    for rect in regions:
        if len(figures) == MAX_FIGURES:
            break
        x, y = int(rect.x), int(rect.y)
        w, h = int(rect.max_x) - x, int(rect.max_y) - y
        if w <= 0 or h <= 0 or zoom_size(w, h)[0] < MIN_ZOOM_GAIN * w * screenshot_scale:
            continue
        cropped = Quartz.CGImageCreateWithImageInRect(cgimage, Quartz.CGRectMake(x, y, w, h))
        if cropped is None:
            continue
        figures.append(
            ZoomedFigure(Rect(x, y, w, h), encode_for_model(cropped, w, h, zoom_size(w, h)))
        )
    return figures
