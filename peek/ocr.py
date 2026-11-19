"""Vision text recognition -> OCR blocks with top-left pixel boxes.

Runs on the pipeline's worker thread. `Recognizer.cancel()` may be called from
the main thread to stop a recognition whose capture has been superseded.
"""

from __future__ import annotations

import logging
import statistics
import threading
from dataclasses import dataclass

import objc
import Vision

from peek.geometry import Rect, reading_order, vision_to_pixels

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class OcrBlock:
    id: str
    text: str
    confidence: float
    box: Rect  # pixels in the captured image, top-left origin

    def to_json(self) -> dict:
        return {
            "id": self.id,
            "text": self.text,
            "confidence": round(self.confidence, 3),
            "box": self.box.as_list(),
        }


class OcrCancelled(Exception):
    pass


class Recognizer:
    """One recognition at a time; cancel() is thread-safe."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._request = None
        self._cancelled = False

    def cancel(self) -> None:
        with self._lock:
            self._cancelled = True
            if self._request is not None:
                self._request.cancel()

    def recognize(self, cgimage, width: int, height: int) -> list[OcrBlock]:
        with objc.autorelease_pool():
            request = Vision.VNRecognizeTextRequest.alloc().init()
            request.setRecognitionLevel_(Vision.VNRequestTextRecognitionLevelAccurate)
            request.setUsesLanguageCorrection_(True)
            request.setAutomaticallyDetectsLanguage_(True)
            with self._lock:
                if self._cancelled:
                    raise OcrCancelled
                self._request = request
            try:
                handler = Vision.VNImageRequestHandler.alloc().initWithCGImage_options_(cgimage, {})
                ok, error = handler.performRequests_error_([request], None)
            finally:
                with self._lock:
                    self._request = None
                    cancelled = self._cancelled
            if cancelled:
                raise OcrCancelled
            if not ok:
                raise RuntimeError(f"Vision text recognition failed: {error}")
            return _blocks_from_observations(request.results() or [], width, height)


def _box(observation_rect, width: int, height: int) -> Rect:
    o, s = observation_rect.origin, observation_rect.size
    return vision_to_pixels(o.x, o.y, s.width, s.height, width, height)


def _blocks_from_observations(observations, width: int, height: int) -> list[OcrBlock]:
    pieces: list[tuple[str, float, Rect]] = []
    for obs in observations:
        candidates = obs.topCandidates_(1)
        if not candidates:
            continue
        candidate = candidates[0]
        text = str(candidate.string()).strip()
        if not text:
            continue
        pieces.append((text, float(candidate.confidence()), _box(obs.boundingBox(), width, height)))

    if not pieces:
        return []
    line_height = statistics.median(p[2].h for p in pieces)
    ordered = [pieces[i] for i in reading_order([p[2] for p in pieces], line_height)]
    return [
        OcrBlock(id=f"b{i}", text=t, confidence=c, box=b)
        for i, (t, c, b) in enumerate(ordered, start=1)
    ]
