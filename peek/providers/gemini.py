"""Gemini via a Google AI Studio API key (google-genai SDK, generate_content).

- API key only. Google does not allow third-party apps to use a Google account or
  Gemini subscription sign-in for model access (spec), so there is none.
- Strict JSON: `response_mime_type="application/json"` + `response_json_schema`
  (the same portable schema as Claude).
- Coordinates: Gemini is trained on boxes normalized to 0-1000, so the request
  states that frame (schema.build_user_text(frame="normalized")) and the pipeline
  converts OCR boxes and zoomed-view captions into it. Then the image's size on
  Google's side does not matter.
- `media_resolution=HIGH`: the most image tokens per image (small text, figures).
- Streaming, so a superseded capture stops reading (the SDK has no way to abort a
  request already waiting for its first byte; that one finishes on its own).
- No sampling parameters (Gemini 3+ recommend the defaults).
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Sequence

from peek.imaging import SentImage
from peek.providers.base import ModelInfo, ProviderError, SolveResult, VisionProvider
from peek.providers.catalog import PROVIDERS, pretty_model_name
from peek.schema import PORTABLE_SCHEMA, SYSTEM_PROMPT

log = logging.getLogger(__name__)

# google-genai also sends this as the server's deadline (X-Server-Timeout), and the
# whole answer, thinking included, must fit in it: generous for Hard mode.
TIMEOUT_MS = 300_000
SPEC = PROVIDERS["gemini"]
# Model families the API lists that cannot answer a screenshot with text.
_NOT_FOR_US = ("image", "tts", "embedding", "audio", "live", "robotics", "computer-use", "veo")


class GeminiProvider(VisionProvider):
    name = "gemini"

    def __init__(self, api_key: str) -> None:
        if not api_key:
            raise ProviderError("not_configured", "Add your Gemini API key in Settings")
        from google import genai
        from google.genai import types

        self._types = types
        self._client = genai.Client(
            api_key=api_key,
            http_options=types.HttpOptions(
                timeout=TIMEOUT_MS,
                # No automatic retries: a retried request (timeouts included) is
                # billed again, and the user can press ⌃⌃ again just as well.
                retry_options=types.HttpRetryOptions(attempts=1),
            ),
        )

    # --- models ---------------------------------------------------------------

    def list_models(self) -> list[ModelInfo]:
        """The account's models that can answer a screenshot, catalog order first."""
        try:
            listed = list(self._client.models.list())
        except Exception as exc:
            raise _map_error(exc) from None
        found: dict[str, ModelInfo] = {}
        for m in listed:
            model_id = str(m.name or "").removeprefix("models/")
            actions = m.supported_actions or []
            if not model_id.startswith("gemini-") or "generateContent" not in actions:
                continue
            if any(word in model_id for word in _NOT_FOR_US):
                continue
            found[model_id] = ModelInfo(
                model_id, SPEC.display_name(model_id) if _known(model_id) else _name(m, model_id)
            )
        ordered = [found.pop(m.id) for m in SPEC.models if m.id in found]
        return ordered + sorted(found.values(), key=lambda m: m.id, reverse=True)

    def test_connection(self, model: str) -> None:
        """models.get: checks the key and the model without running (or paying for)
        any inference."""
        try:
            info = self._client.models.get(model=model)
        except Exception as exc:
            raise _map_error(exc) from None
        actions = getattr(info, "supported_actions", None)
        if actions and "generateContent" not in actions:
            raise ProviderError(
                "model_unavailable", f"{SPEC.display_name(model)} can't answer screenshots"
            )

    # --- solve ------------------------------------------------------------------

    def solve(
        self,
        image: SentImage,
        user_text: str,
        model: str,
        *,
        extra_images: Sequence[tuple[str, SentImage]] = (),
        on_stream: Callable[[Callable[[], None]], None] | None = None,
    ) -> SolveResult:
        types = self._types
        parts = [types.Part.from_bytes(data=image.data, mime_type=image.media_type)]
        for caption, extra in extra_images:
            parts.append(types.Part.from_text(text=caption))
            parts.append(types.Part.from_bytes(data=extra.data, mime_type=extra.media_type))
        parts.append(types.Part.from_text(text=user_text))
        config = types.GenerateContentConfig(
            system_instruction=SYSTEM_PROMPT,
            response_mime_type="application/json",
            response_json_schema=PORTABLE_SCHEMA,
            media_resolution=types.MediaResolution.MEDIA_RESOLUTION_HIGH,
        )
        cancelled = threading.Event()
        if on_stream is not None:
            on_stream(cancelled.set)
        text: list[str] = []
        finish = block = None
        used_model, prompt_tokens, output_tokens = model, 0, 0
        try:
            stream = self._client.models.generate_content_stream(
                model=model, contents=[types.Content(role="user", parts=parts)], config=config
            )
            for chunk in stream:
                if cancelled.is_set():
                    raise ProviderError("failed", "Cancelled")
                feedback = getattr(chunk, "prompt_feedback", None)
                if feedback is not None and feedback.block_reason:
                    reason = feedback.block_reason
                    block = str(getattr(reason, "name", reason))
                for cand in chunk.candidates or []:
                    if cand.finish_reason:
                        finish = cand.finish_reason
                    content = cand.content
                    for part in (content.parts if content and content.parts else []) or []:
                        if part.text and not part.thought:
                            text.append(part.text)
                usage = chunk.usage_metadata
                if usage is not None:
                    prompt_tokens = usage.prompt_token_count or prompt_tokens
                    output_tokens = usage.candidates_token_count or output_tokens
                used_model = chunk.model_version or used_model
        except ProviderError:
            raise
        except Exception as exc:
            raise _map_error(exc) from None

        finish_name = getattr(finish, "name", str(finish or ""))
        if block or finish_name in _REFUSALS:
            raise ProviderError(
                "refused",
                "Gemini declined to answer this screen"
                + (f" ({(block or finish_name).lower()})" if block or finish_name else ""),
            )
        if finish_name == "MAX_TOKENS":
            raise ProviderError("truncated", "Gemini's answer was cut off; try fewer questions")
        body = "".join(text)
        if not body.strip():
            raise ProviderError("malformed", "Gemini returned an empty answer")
        return SolveResult(body, str(used_model), prompt_tokens, output_tokens, False)


_REFUSALS = {
    "SAFETY",
    "BLOCKLIST",
    "PROHIBITED_CONTENT",
    "SPII",
    "IMAGE_SAFETY",
    "IMAGE_PROHIBITED_CONTENT",
    "RECITATION",
    "MODEL_ARMOR",
}


def _known(model_id: str) -> bool:
    return any(m.id == model_id for m in SPEC.models)


def _name(model, model_id: str) -> str:
    display = str(getattr(model, "display_name", "") or "")
    return display if display.lower().startswith("gemini") else pretty_model_name(model_id)


def _map_error(exc: Exception) -> ProviderError:
    """google-genai and transport errors -> ProviderError (no server bodies)."""
    from google.genai import errors

    if isinstance(exc, errors.APIError):
        code = int(getattr(exc, "code", 0) or 0)
        status = str(getattr(exc, "status", "") or "")
        message = str(getattr(exc, "message", "") or "")
        if code == 400 and ("API key" in message or "API_KEY" in message):
            return ProviderError("bad_key", "Google rejected this API key")
        if code in (401, 403):
            if "leaked" in message.lower():
                return ProviderError("bad_key", "Google blocked this key (reported as leaked)")
            return ProviderError("permission", "This key isn't allowed to use Gemini")
        if code == 404:
            return ProviderError("model_unavailable", "That Gemini model isn't available")
        if code == 429 or status == "RESOURCE_EXHAUSTED":
            return ProviderError("rate_limited", "Gemini rate limit reached; try again shortly")
        if code == 413:
            return ProviderError("image_too_large", "The screenshot was too large for Gemini")
        if code == 504 or status == "DEADLINE_EXCEEDED":
            return ProviderError("timeout", "Gemini took too long to answer")
        if code in (500, 503):
            return ProviderError("overloaded", "Gemini is busy; try again")
        if 400 <= code < 500:
            log.warning("Gemini request rejected: %s %s", code, status)
            return ProviderError("failed", f"Gemini couldn't process the request ({code})")
        return ProviderError("server", f"Gemini had a problem ({code or status})")
    for module in ("httpx", "httpx2"):
        try:
            mod = __import__(module)
        except ImportError:
            continue
        if isinstance(exc, mod.TimeoutException):
            return ProviderError("timeout", "Gemini did not answer in time; try again")
        if isinstance(exc, mod.HTTPError):
            return ProviderError("network", "Can't reach Google; check your connection")
    log.exception("Unexpected Gemini error", exc_info=exc)
    return ProviderError("failed", "Gemini request failed; see the log")
