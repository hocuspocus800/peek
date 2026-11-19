"""--debug artifacts: the capture, OCR boxes and timings, for tuning placement.

Only ever called with --debug. Each capture gets its own folder in
~/Library/Application Support/Peek/debug/; the newest KEEP folders are kept.
Later milestones add the model's JSON and the computed answer rects.
"""

from __future__ import annotations

import io
import json
import logging
import shutil
import time
from pathlib import Path

import Quartz
from Foundation import NSMutableData
from PIL import Image, ImageDraw, ImageFont

from peek.ocr import OcrBlock
from peek.paths import debug_dir, ensure_private_dir

log = logging.getLogger(__name__)

KEEP = 20
BOX_COLOR = (255, 45, 85)  # magenta-red, readable on light and dark content
LABEL_BG = (255, 45, 85, 200)


def png_bytes(cgimage) -> bytes:
    """Encode a CGImage as PNG with ImageIO (no Pillow round trip)."""
    data = NSMutableData.alloc().init()
    dest = Quartz.CGImageDestinationCreateWithData(data, "public.png", 1, None)
    Quartz.CGImageDestinationAddImage(dest, cgimage, None)
    if not Quartz.CGImageDestinationFinalize(dest):
        raise RuntimeError("PNG encoding failed")
    return bytes(data)


def new_capture_dir(generation: int, index: int = 0) -> Path:
    root = ensure_private_dir(debug_dir())
    stamp = time.strftime("%Y%m%d-%H%M%S")
    path = root / f"{stamp}-g{generation}-d{index}"
    path.mkdir(mode=0o700, exist_ok=True)
    _prune(root, keep=path)
    return path


def _prune(root: Path, keep: Path) -> None:
    """Keep the KEEP most recently modified folders (by mtime, not by their
    local-time names, which go backwards after a time-zone change)."""
    others = [p for p in root.iterdir() if p.is_dir() and p != keep]
    others.sort(key=lambda p: p.stat().st_mtime)
    for old in others[: max(0, len(others) - (KEEP - 1))]:
        shutil.rmtree(old, ignore_errors=True)


def annotate(png: bytes, blocks: list[OcrBlock]) -> Image.Image:
    """The capture with every OCR box outlined and labelled with its id."""
    base = Image.open(io.BytesIO(png)).convert("RGB")
    overlay = Image.new("RGBA", base.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    scale = max(1, round(base.width / 1600))  # thicker lines on Retina captures
    try:
        font = ImageFont.load_default(size=11 * scale)
    except TypeError:  # Pillow without FreeType sizes
        font = ImageFont.load_default()
    for block in blocks:
        b = block.box
        draw.rectangle([b.x, b.y, b.max_x, b.max_y], outline=BOX_COLOR, width=scale)
        label = block.id
        tw = draw.textlength(label, font=font)
        th = 12 * scale
        ly = max(0, b.y - th - 1)
        draw.rectangle([b.x, ly, b.x + tw + 4 * scale, ly + th], fill=LABEL_BG)
        draw.text((b.x + 2 * scale, ly), label, fill=(255, 255, 255), font=font)
    return Image.alpha_composite(base.convert("RGBA"), overlay).convert("RGB")


def write_layout(folder: Path, items, scale: float) -> None:
    """layout.png: the capture with each answer's computed rect and text drawn in."""
    capture = folder / "capture.png"
    if not capture.exists():
        return
    base = Image.open(capture).convert("RGBA")
    overlay = Image.new("RGBA", base.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    for item in items:
        r = item.rect
        box = [r.x * scale, r.y * scale, r.max_x * scale, r.max_y * scale]
        draw.rectangle(box, outline=(0, 160, 255, 255), width=max(1, round(scale)))
        try:
            font = ImageFont.load_default(size=max(10, round(item.font_size * scale * 0.9)))
        except TypeError:
            font = ImageFont.load_default()
        lines = item.text.split("\n")  # a wrapped answer, spaced like the overlay
        for i, line in enumerate(lines):
            y = box[1] + i * (box[3] - box[1]) / len(lines)
            draw.text((box[0], y), line, fill=(*item.rgb, 255), font=font)
    Image.alpha_composite(base, overlay).convert("RGB").save(folder / "layout.png")
    (folder / "layout.json").write_text(
        json.dumps(
            [
                {
                    "question": i.question_id,
                    "text": i.text,
                    "rect_pt": i.rect.as_list(),
                    "font_size": round(i.font_size, 1),
                    "rgb": list(i.rgb),
                    "kind": i.kind,
                    "spot": i.spot,
                }
                for i in items
            ],
            ensure_ascii=False,
            indent=1,
        ),
        encoding="utf-8",
    )


def write_capture(folder: Path, png: bytes, blocks: list[OcrBlock], info: dict) -> None:
    (folder / "capture.png").write_bytes(png)
    # Explicit UTF-8: an app started by LaunchServices has no locale, so Python's
    # default text encoding there is ASCII and screen text like "•" would fail.
    (folder / "ocr.json").write_text(
        json.dumps([b.to_json() for b in blocks], ensure_ascii=False, indent=1),
        encoding="utf-8",
    )
    (folder / "info.json").write_text(json.dumps(info, indent=1), encoding="utf-8")
    annotate(png, blocks).save(folder / "annotated.png", optimize=False)
    log.info("Debug capture written to %s", folder)
