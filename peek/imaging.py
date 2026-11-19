"""Resize and encode a capture for a model, so it sees exactly the pixels we send.

If a provider resizes the image again on its side, coordinates the model returns
would be in a frame we don't know. So the image is pre-sized to what the model
processes, and OCR boxes are converted into that same frame.

Claude (docs, "vision"): an image is limited by its long edge *and* by visual
tokens = ceil(w/28) * ceil(h/28). High-resolution tier (Claude 4.7 and later,
which covers all four models Peek offers): 2576 px / 4784 tokens. Standard tier:
1568 px / 1568 tokens.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

PATCH = 28
HIGH_RES = (2576, 4784)
STANDARD = (1568, 1568)
# Anthropic limits each image by its *base64* size: 5 MiB. Keep a small margin.
MAX_IMAGE_BASE64_BYTES = 5 * 1024 * 1024 - 65_536


def base64_size(raw_bytes: int) -> int:
    return 4 * math.ceil(raw_bytes / 3)


def fits_budget(raw_bytes: int) -> bool:
    return base64_size(raw_bytes) <= MAX_IMAGE_BASE64_BYTES


def visual_tokens(width: int, height: int) -> int:
    return math.ceil(width / PATCH) * math.ceil(height / PATCH)


def fit_size(width: int, height: int, max_edge: int, max_tokens: int) -> tuple[int, int]:
    """Largest size with the same aspect ratio that fits both limits (never upscales)."""
    if width <= 0 or height <= 0:
        raise ValueError("empty image")
    scale = min(1.0, max_edge / max(width, height))

    def size(s: float) -> tuple[int, int]:
        return max(1, int(width * s)), max(1, int(height * s))

    if visual_tokens(*size(scale)) <= max_tokens:
        return size(scale)
    lo, hi = 0.0, scale
    for _ in range(40):  # binary search on the scale factor
        mid = (lo + hi) / 2
        if visual_tokens(*size(mid)) <= max_tokens:
            lo = mid
        else:
            hi = mid
    return size(lo)


@dataclass(frozen=True)
class SentImage:
    """The image exactly as sent; its size is the coordinate frame of the request."""

    data: bytes
    media_type: str  # image/png | image/jpeg
    width: int
    height: int
    scale_x: float  # sent pixels per capture pixel
    scale_y: float


def encode_for_model(cgimage, src_width: int, src_height: int, size: tuple[int, int]) -> SentImage:
    """Resize with high-quality interpolation into an opaque sRGB bitmap and encode
    as PNG (crisp text). If that is over the base64 budget, fall back to JPEG at
    decreasing quality, then to smaller sizes; never return an image over budget.
    scale_x/scale_y always describe the size actually sent."""
    import Quartz
    from Foundation import NSMutableData

    space = Quartz.CGColorSpaceCreateWithName(Quartz.kCGColorSpaceSRGB)

    def resize(width: int, height: int):
        ctx = Quartz.CGBitmapContextCreate(
            None, width, height, 8, 0, space, Quartz.kCGImageAlphaNoneSkipLast
        )
        if ctx is None:
            raise RuntimeError("could not create a bitmap context")
        Quartz.CGContextSetInterpolationQuality(ctx, Quartz.kCGInterpolationHigh)
        Quartz.CGContextDrawImage(ctx, Quartz.CGRectMake(0, 0, width, height), cgimage)
        return Quartz.CGBitmapContextCreateImage(ctx)

    def encode(image, uti: str, properties) -> bytes:
        data = NSMutableData.alloc().init()
        dest = Quartz.CGImageDestinationCreateWithData(data, uti, 1, None)
        Quartz.CGImageDestinationAddImage(dest, image, properties)
        if not Quartz.CGImageDestinationFinalize(dest):
            raise RuntimeError(f"{uti} encoding failed")
        return bytes(data)

    width, height = size
    for _attempt in range(6):
        resized = resize(width, height)
        data, media_type = encode(resized, "public.png", None), "image/png"
        if not fits_budget(len(data)):
            for quality in (0.9, 0.8, 0.65, 0.5):
                data = encode(
                    resized,
                    "public.jpeg",
                    {Quartz.kCGImageDestinationLossyCompressionQuality: quality},
                )
                media_type = "image/jpeg"
                if fits_budget(len(data)):
                    break
        if fits_budget(len(data)):
            return SentImage(
                data=data,
                media_type=media_type,
                width=width,
                height=height,
                scale_x=width / src_width,
                scale_y=height / src_height,
            )
        width, height = max(1, int(width * 0.85)), max(1, int(height * 0.85))
    raise RuntimeError("could not encode the screenshot under the size limit")
