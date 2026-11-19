"""Capture -> OCR -> model -> parsed answers. Layout joins in milestone 5.

Two stages, so a slow model call never delays the next screenshot:

1. The capture worker (one daemon thread, jobs in order) takes every display's
   screenshot as soon as a job arrives, then runs OCR. Older queued jobs are
   skipped; a running Vision request is cancelled by `submit()`.
2. Each job's model calls run on their own daemon thread. `submit()` also cancels
   every older job: its open HTTP stream is closed, so a superseded request stops
   costing tokens.

Every job carries a generation id. Results go back with AppHelper.callAfter, and
the app drops any whose generation is not the latest; cancellation only saves
work and money, correctness never depends on it.

`analyze()` runs both halves in one call, for the offline self-test.
"""

from __future__ import annotations

import json
import logging
import queue
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import objc
from PyObjCTools import AppHelper

from peek import capture as capture_mod
from peek import debug, question_pictures
from peek.capture import CaptureError, DisplayTarget
from peek.figures import ZoomedFigure, figure_regions, zoomed_figures
from peek.geometry import Rect
from peek.imaging import HIGH_RES, STANDARD, encode_for_model, fit_size
from peek.ocr import OcrBlock, OcrCancelled, Recognizer
from peek.providers import catalog
from peek.providers.base import ProviderError
from peek.schema import (
    MalformedResponse,
    ParsedQuestion,
    SentBlock,
    build_user_text,
    figure_caption,
    parse_lenient,
)
from peek.store import SecretCache
from peek.textstyle import TextStyle, estimate_style

log = logging.getLogger(__name__)


NORMALIZED_EXTENT = 1000  # Gemini's box convention: 0-1000 across the image


class Superseded(Exception):
    """A newer capture was requested; this job's result would be dropped anyway."""


class CancelToken:
    """Cancellation for one job; cancel() may be called from any thread."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.cancelled = False
        self._closers: list[Callable[[], None]] = []

    def add_closer(self, close: Callable[[], None]) -> None:
        with self._lock:
            if not self.cancelled:
                self._closers.append(close)
                return
        _safe_close(close)  # cancelled already: close right away

    def cancel(self) -> None:
        with self._lock:
            if self.cancelled:
                return
            self.cancelled = True
            closers, self._closers = self._closers, []
        for close in closers:
            _safe_close(close)


def _safe_close(close: Callable[[], None]) -> None:
    try:
        close()
    except Exception:
        log.debug("Closing a superseded request failed", exc_info=True)


@dataclass(frozen=True)
class SolveConfig:
    """What to ask, snapshotted on the main thread when the capture starts."""

    provider: str  # "claude" | "gemini" (providers/catalog.py)
    model: str
    key_cache: SecretCache  # read off the main thread (the Keychain may prompt)
    show_work: bool = False  # Settings → Show work: ask for each question's working
    save_folder: str = ""  # Settings → Save questions: where the pictures go ("" = off)


@dataclass
class Analysis:
    blocks: list[OcrBlock]
    ocr_ms: float
    questions: list[ParsedQuestion] = field(default_factory=list)
    dropped: int = 0
    problems: list[str] = field(default_factory=list)
    sent_size: tuple[int, int] | None = None
    solve_ms: float = 0.0
    model_used: str | None = None
    fallback_used: bool = False
    error: ProviderError | None = None
    styles: dict[str, TextStyle] = field(default_factory=dict)  # by question id
    saved: int = 0  # Save questions: pictures written
    save_error: str | None = None  # short and safe, for the log and the menu


@dataclass
class DisplayResult:
    target: DisplayTarget
    width: int
    height: int
    scale: float  # image pixels per point
    analysis: Analysis
    capture_ms: float
    excluded_by: str
    debug_dir: str | None = None

    @property
    def blocks(self) -> list[OcrBlock]:
        return self.analysis.blocks

    @property
    def ocr_ms(self) -> float:
        return self.analysis.ocr_ms


@dataclass
class PipelineResult:
    generation: int
    provider: str = ""  # who was asked (the user may switch provider meanwhile)
    displays: list[DisplayResult] = field(default_factory=list)
    errors: list[Exception] = field(default_factory=list)  # CaptureError / ProviderError
    total_ms: float = 0.0

    @property
    def block_count(self) -> int:
        return sum(len(d.blocks) for d in self.displays)

    @property
    def questions(self) -> list[ParsedQuestion]:
        return [q for d in self.displays for q in d.analysis.questions]

    @property
    def dropped(self) -> int:
        return sum(d.analysis.dropped for d in self.displays)


def _sent_blocks(blocks: list[OcrBlock], sx: float, sy: float) -> list[SentBlock]:
    return [
        SentBlock(
            b.id,
            b.text,
            (
                round(b.box.x * sx),
                round(b.box.y * sy),
                round(b.box.max_x * sx),
                round(b.box.max_y * sy),
            ),
        )
        for b in blocks
    ]


# --- stage 1: OCR -------------------------------------------------------------------


def run_ocr(cgimage, width: int, height: int, recognizer: Recognizer) -> Analysis:
    started = time.monotonic()
    try:
        blocks = recognizer.recognize(cgimage, width, height)
    except OcrCancelled:
        raise Superseded from None
    return Analysis(blocks=blocks, ocr_ms=(time.monotonic() - started) * 1000)


# --- stage 2: the model ---------------------------------------------------------------


def solve_into(
    analysis: Analysis,
    cgimage,
    width: int,
    height: int,
    solve: SolveConfig,
    *,
    check: Callable[[], None] = lambda: None,
    token: CancelToken | None = None,
    debug_folder: Path | None = None,
    scale: float = 2.0,
    display_index: int = 0,
) -> None:
    """Ask the model about one capture and fill *analysis*. Never raises anything
    but Superseded: every failure ends up in analysis.error, with the OCR blocks
    kept. *scale* is capture pixels per point (diagram detection works in points).
    With Save questions on, each question's picture is written after parsing."""
    started = time.monotonic()
    blocks = analysis.blocks
    try:
        try:
            key = solve.key_cache.get()  # an API key, or a fresh ChatGPT access token
        except ProviderError:
            raise  # signed out, plan declined, network (ChatGPT credential)
        except Exception:
            log.exception("Could not read the API key from the Keychain")
            raise ProviderError(
                "keychain", "Peek couldn't read your API key from the Keychain; allow access"
            ) from None
        spec = catalog.spec(solve.provider)
        if not key:
            raise ProviderError("not_configured", f"Add your {spec.vendor} API key in Settings")
        provider = spec.create(key)
        normalized = spec.frame == "normalized"
        regions, figures = _figures(cgimage, width, height, blocks, scale)
        if debug_folder is not None:
            for k, fig in enumerate(figures, 1):
                (debug_folder / f"figure-{k}.{_ext(fig.image)}").write_bytes(fig.image.data)
        for tier_name, tier in (("high", HIGH_RES), ("standard", STANDARD)):
            sent = encode_for_model(cgimage, width, height, fit_size(width, height, *tier))
            views = [f for f in figures if f.adds_detail(sent.scale_x)]
            if len(views) < len(figures):
                log.info(
                    "%d diagram(s) already large in the %s screenshot; no zoomed view",
                    len(figures) - len(views),
                    tier_name,
                )
            # The frame the model answers in: the sent image's pixels, or 0-1000
            # across it (Gemini). cx/cy map capture pixels into it.
            if normalized:
                frame_w = frame_h = NORMALIZED_EXTENT
                cx, cy = NORMALIZED_EXTENT / width, NORMALIZED_EXTENT / height
            else:
                frame_w, frame_h = sent.width, sent.height
                cx, cy = sent.scale_x, sent.scale_y
            user_text = build_user_text(
                sent.width,
                sent.height,
                _sent_blocks(blocks, cx, cy),
                frame="normalized" if normalized else "pixels",
                figures=len(views),
            )
            captions = [
                figure_caption(k, _sent_box(fig.rect, cx, cy)) for k, fig in enumerate(views, 1)
            ]
            if debug_folder is not None:
                (debug_folder / f"sent-{tier_name}.{_ext(sent)}").write_bytes(sent.data)
                request = "\n".join([*captions, "", user_text]) if captions else user_text
                (debug_folder / f"request-{tier_name}.txt").write_text(request, encoding="utf-8")
            check()
            try:
                result = provider.solve(
                    sent,
                    user_text,
                    solve.model,
                    extra_images=[(c, f.image) for c, f in zip(captions, views, strict=True)],
                    on_stream=token.add_closer if token is not None else None,
                    show_work=solve.show_work,
                )
                break
            except ProviderError as exc:
                if token is not None and token.cancelled:
                    raise Superseded from None
                if exc.kind == "image_too_large" and tier is HIGH_RES:
                    log.warning("%s rejected the high-resolution size; retrying smaller", spec.name)
                    continue
                raise
        check()
        analysis.sent_size = (sent.width, sent.height)
        analysis.model_used = result.model
        analysis.fallback_used = result.fallback_used
        if debug_folder is not None:
            (debug_folder / "response.json").write_text(result.text, encoding="utf-8")
        try:
            parsed = parse_lenient(
                result.text,
                {b.id for b in blocks},
                frame_width=frame_w,
                frame_height=frame_h,
            )
        except MalformedResponse as exc:
            raise ProviderError(
                "malformed", f"{spec.name}'s answer could not be read ({exc})"
            ) from None
        analysis.questions = parsed.questions
        analysis.dropped = parsed.dropped
        analysis.problems = parsed.problems
        analysis.styles = _question_styles(cgimage, blocks, parsed.questions)
        if solve.save_folder and parsed.questions:
            check()
            _save_pictures(analysis, cgimage, width, height, regions, solve, scale, display_index)
        if debug_folder is not None:
            (debug_folder / "questions.json").write_text(
                json.dumps(
                    {
                        "model": result.model,
                        "fallback_used": result.fallback_used,
                        "sent_px": [sent.width, sent.height],
                        "input_tokens": result.input_tokens,
                        "output_tokens": result.output_tokens,
                        "dropped": parsed.dropped,
                        "problems": parsed.problems,
                        "questions": [q.to_json() for q in parsed.questions],
                    },
                    ensure_ascii=False,
                    indent=1,
                ),
                encoding="utf-8",
            )
        log.info(
            "Model %s: %d questions (%d dropped), %d in / %d out tokens%s",
            result.model,
            len(parsed.questions),
            parsed.dropped,
            result.input_tokens,
            result.output_tokens,
            ", fallback used" if result.fallback_used else "",
        )
    except Superseded:
        raise
    except ProviderError as exc:
        analysis.error = exc.with_traceback(None)
        log.warning("Solve failed (%s): %s", exc.kind, exc)
    except Exception:
        if token is not None and token.cancelled:
            raise Superseded from None
        log.exception("Solve step failed")
        analysis.error = ProviderError("failed", "Peek couldn't get answers; see the log")
    finally:
        analysis.solve_ms = (time.monotonic() - started) * 1000


def _figures(
    cgimage, width: int, height: int, blocks, scale: float
) -> tuple[list[Rect], list[ZoomedFigure]]:
    """Every diagram region on the capture, and enlarged copies of the ones worth
    sending; none if detection fails (the screenshot alone still gets an answer)."""
    started = time.monotonic()
    try:
        regions = figure_regions(cgimage, width, height, blocks, scale)
    except Exception:
        log.exception("Diagram detection failed; sending the screenshot alone")
        return [], []
    try:
        figures = zoomed_figures(cgimage, width, height, blocks, scale, regions=regions)
    except Exception:
        log.exception("Zooming the diagrams failed; sending the screenshot alone")
        return regions, []
    if figures:
        log.info(
            "%d diagram(s) to zoom found in %.0f ms; views %s",
            len(figures),
            (time.monotonic() - started) * 1000,
            [f"{f.image.width}x{f.image.height}" for f in figures],
        )
    return regions, figures


def _save_pictures(
    analysis: Analysis,
    cgimage,
    width: int,
    height: int,
    regions: list[Rect],
    solve: SolveConfig,
    scale: float,
    display_index: int,
) -> None:
    """Save questions: never fails the capture; a problem goes to analysis.save_error."""
    try:
        written = question_pictures.save(
            cgimage,
            width,
            height,
            analysis.questions,
            {b.id: b.box for b in analysis.blocks},
            regions,
            solve.save_folder,
            scale,
            display=display_index,
        )
    except question_pictures.SaveError as exc:
        analysis.save_error = str(exc)
        log.warning("Questions not saved: %s", exc)
        return
    except Exception:
        analysis.save_error = "Peek couldn't save the questions; see the log"
        log.exception("Saving question pictures failed")
        return
    analysis.saved = len(written)
    log.info("Saved %d question picture(s)", len(written))


def _sent_box(rect, sx: float, sy: float) -> tuple[int, int, int, int]:
    return (round(rect.x * sx), round(rect.y * sy), round(rect.max_x * sx), round(rect.max_y * sy))


def _ext(image) -> str:
    return "png" if image.media_type == "image/png" else "jpg"


def _question_styles(cgimage, blocks: list[OcrBlock], questions) -> dict[str, TextStyle]:
    """Each question's ink colour and line height, sampled from the capture."""
    by_id = {b.id: b for b in blocks}
    styles: dict[str, TextStyle] = {}
    for q in questions:
        boxes = [by_id[b].box for b in q.question_block_ids if b in by_id]
        try:
            style = estimate_style(cgimage, boxes)
        except Exception:
            log.exception("Could not sample the text style of %s", q.id)
            style = None
        if style is not None:
            styles[q.id] = style
    return styles


def analyze(
    cgimage,
    width: int,
    height: int,
    *,
    recognizer: Recognizer,
    solve: SolveConfig | None,
    debug_folder: Path | None = None,
    scale: float = 2.0,
) -> Analysis:
    """Both stages in one call (the offline self-test)."""
    analysis = run_ocr(cgimage, width, height, recognizer)
    if solve is not None:
        solve_into(analysis, cgimage, width, height, solve, debug_folder=debug_folder, scale=scale)
    return analysis


# --- the pipeline ------------------------------------------------------------------


@dataclass
class _Job:
    generation: int
    targets: list[DisplayTarget]
    on_done: Callable[[int, PipelineResult], None]
    save_debug: bool
    solve: SolveConfig | None
    token: CancelToken = field(default_factory=CancelToken)


@dataclass
class _Prepared:
    """One display after stage 1, waiting for its model call."""

    display: DisplayResult
    image: object  # CGImage, released after the model call
    folder: Path | None


class Pipeline:
    def __init__(self) -> None:
        self._queue: queue.Queue[_Job] = queue.Queue()
        self._latest = 0
        self._lock = threading.Lock()
        self._recognizer: Recognizer | None = None
        self._live: dict[int, CancelToken] = {}  # jobs submitted and not yet delivered
        self._thread = threading.Thread(target=self._worker, name="pipeline", daemon=True)
        self._thread.start()

    # --- main thread --------------------------------------------------------

    def submit(
        self,
        generation: int,
        targets: list[DisplayTarget],
        on_done: Callable[[int, PipelineResult], None],
        *,
        save_debug: bool = False,
        solve: SolveConfig | None = None,
    ) -> None:
        job = _Job(generation, targets, on_done, save_debug, solve)
        with self._lock:
            self._latest = generation
            if self._recognizer is not None:
                self._recognizer.cancel()
            older = [t for g, t in self._live.items() if g < generation]
            self._live[generation] = job.token
        for token in older:
            token.cancel()  # closes their open HTTP streams
        self._queue.put(job)

    # --- shared -------------------------------------------------------------

    def _check(self, generation: int) -> None:
        if generation != self._latest:
            raise Superseded

    def _finish(self, job: _Job, result: PipelineResult | None) -> None:
        with self._lock:
            self._live.pop(job.generation, None)
        if result is None:
            log.info("Capture g%d superseded", job.generation)
            return
        AppHelper.callAfter(job.on_done, job.generation, result)

    # --- stage 1: capture worker ----------------------------------------------

    def _worker(self) -> None:
        while True:
            job = self._queue.get()
            if job.generation != self._latest:
                self._finish(job, None)  # a newer capture was requested first
                continue
            with objc.autorelease_pool():
                try:
                    prepared, result = self._capture_and_ocr(job)
                except Superseded:
                    self._finish(job, None)
                    continue
                except Exception as exc:
                    log.exception("Capture g%d failed", job.generation)
                    self._finish(
                        job,
                        PipelineResult(job.generation, errors=[CaptureError("failed", str(exc))]),
                    )
                    continue
            if job.solve is None or not prepared:
                self._finish(job, result)
            else:
                threading.Thread(
                    target=self._solve_job,
                    args=(job, prepared, result),
                    name=f"solve-g{job.generation}",
                    daemon=True,
                ).start()

    def _capture_and_ocr(self, job: _Job) -> tuple[list[_Prepared], PipelineResult]:
        started = time.monotonic()
        result = PipelineResult(job.generation, job.solve.provider if job.solve else "")
        # Every screenshot first, so all displays show the moment of the double-tap.
        captures = []
        for target in job.targets:
            self._check(job.generation)
            try:
                captures.append(capture_mod.capture(target))
            except CaptureError as exc:
                log.warning("Capture of display %d failed: %s", target.display_id, exc)
                # Without its traceback: the frames would keep this job's objects alive.
                result.errors.append(exc.with_traceback(None))

        prepared: list[_Prepared] = []
        for index, cap in enumerate(captures):
            self._check(job.generation)
            recognizer = Recognizer()
            with self._lock:
                self._recognizer = recognizer
            try:
                analysis = run_ocr(cap.image, cap.width, cap.height, recognizer)
            finally:
                with self._lock:
                    self._recognizer = None
            self._check(job.generation)
            display = DisplayResult(
                target=cap.target,
                width=cap.width,
                height=cap.height,
                scale=cap.scale,
                analysis=analysis,
                capture_ms=cap.elapsed_ms,
                excluded_by=cap.excluded_by,
            )
            folder = debug.new_capture_dir(job.generation, index) if job.save_debug else None
            if folder is not None:
                display.debug_dir = self._save_capture(folder, cap, display)
            result.displays.append(display)
            prepared.append(_Prepared(display, cap.image if job.solve else None, folder))
            cap.image = None
        result.total_ms = (time.monotonic() - started) * 1000
        return prepared, result

    # --- stage 2: model calls, one thread per job -------------------------------

    def _solve_job(self, job: _Job, prepared: list[_Prepared], result: PipelineResult) -> None:
        started = time.monotonic()
        with objc.autorelease_pool():
            try:
                for index, item in enumerate(prepared):
                    d = item.display
                    solve_into(
                        d.analysis,
                        item.image,
                        d.width,
                        d.height,
                        job.solve,
                        check=lambda gen=job.generation: self._check(gen),
                        token=job.token,
                        debug_folder=item.folder,
                        scale=d.scale,
                        display_index=index,
                    )
                    item.image = None  # free the full-resolution screenshot
                    if d.analysis.error is not None:
                        result.errors.append(d.analysis.error)
            except Superseded:
                self._finish(job, None)
                return
            except Exception as exc:  # solve_into should not raise; be safe anyway
                log.exception("Solve g%d failed", job.generation)
                result.errors.append(ProviderError("failed", f"Peek couldn't get answers ({exc})"))
            finally:
                for item in prepared:
                    item.image = None
        result.total_ms += (time.monotonic() - started) * 1000
        self._finish(job, result)

    @staticmethod
    def _save_capture(folder: Path, cap, display: DisplayResult) -> str | None:
        try:
            debug.write_capture(
                folder,
                debug.png_bytes(cap.image),
                display.blocks,
                {
                    "display_id": display.target.display_id,
                    "screen_frame_points": display.target.frame.as_list(),
                    "backing_scale": display.target.backing_scale,
                    "image_px": [display.width, display.height],
                    "scale_px_per_pt": round(display.scale, 4),
                    "point_pixel_scale": cap.point_pixel_scale,
                    "excluded_by": display.excluded_by,
                    "capture_ms": round(display.capture_ms, 1),
                    "ocr_ms": round(display.analysis.ocr_ms, 1),
                    "blocks": len(display.blocks),
                },
            )
            return str(folder)
        except Exception:
            log.exception("Could not write debug capture")
            return None
