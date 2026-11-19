"""Claude via the Anthropic Messages API (anthropic SDK 2.x).

- Structured output with `output_config.format` (json_schema). Forced tool calls
  return 400 on current models, so they are not used.
- No `thinking` parameter: all four models think adaptively by default (Fable and
  Opus 5.5 always do). No sampling parameters (400 on the current API version).
- Fable 5.2, Opus 5.5 and Sonnet 5.5 decline some requests on safety grounds
  (`stop_reason: "refusal"`). Peek opts into the server-side fallback, which re-runs
  a declined request on another Claude model in the same call, billed at that
  model's rates. Haiku 5.5 has no server-side fallback.
- The image carries `transformations.oversized_image: "error"`, so Claude never
  silently resizes it (which would shift the coordinates it returns).
"""

from __future__ import annotations

import base64
import contextlib
import logging
from collections.abc import Callable, Sequence

import anthropic
import httpx2

from peek.imaging import SentImage
from peek.providers.base import ModelInfo, ProviderError, SolveResult, VisionProvider
from peek.providers.catalog import CLAUDE_MODELS, PROVIDERS
from peek.schema import PORTABLE_SCHEMA, SYSTEM_PROMPT

log = logging.getLogger(__name__)

# The models offered in Settings (owner's choice, 2026-11-19): see catalog.py.
MODELS = list(CLAUDE_MODELS)
DEFAULT_MODEL = PROVIDERS["claude"].default_model
BEST_MODEL = PROVIDERS["claude"].best_model  # what Hard mode switches to
_NO_SERVER_FALLBACK = {"claude-haiku-5-5"}
_FALLBACK_BETA = "server-side-fallback-2026-07-01"

MAX_TOKENS = 32_000  # thinking counts against this; streaming keeps long turns safe
TIMEOUT = anthropic.Timeout(120.0, connect=10.0)


def _image_block(image: SentImage) -> dict:
    return {
        "type": "image",
        "source": {
            "type": "base64",
            "media_type": image.media_type,
            "data": base64.standard_b64encode(image.data).decode("ascii"),
        },
        "transformations": {"oversized_image": "error"},
    }


def display_name(model_id: str) -> str:
    return PROVIDERS["claude"].display_name(model_id)


class ClaudeProvider(VisionProvider):
    name = "claude"

    def __init__(self, api_key: str) -> None:
        if not api_key:
            raise ProviderError("not_configured", "Add your Anthropic API key in Settings")
        self._client = anthropic.Anthropic(api_key=api_key, timeout=TIMEOUT, max_retries=1)

    def list_models(self) -> list[ModelInfo]:
        return list(MODELS)

    def test_connection(self, model: str) -> None:
        """models.retrieve: checks the key and that the model is available to this
        account, without running (or paying for) any inference."""
        try:
            info = self._client.models.retrieve(model)
        except anthropic.APIError as exc:
            raise _map_error(exc) from None
        except httpx2.TimeoutException:
            raise ProviderError("timeout", "Anthropic did not answer in time") from None
        except httpx2.HTTPError:
            raise ProviderError("network", "Can't reach Anthropic; check your connection") from None
        caps = getattr(info, "capabilities", None)
        image = getattr(caps, "image_input", None) if caps is not None else None
        if image is not None and getattr(image, "supported", True) is False:
            raise ProviderError(
                "model_unavailable", f"{display_name(model)} does not accept images"
            )

    def solve(
        self,
        image: SentImage,
        user_text: str,
        model: str,
        *,
        extra_images: Sequence[tuple[str, SentImage]] = (),
        on_stream: Callable[[Callable[[], None]], None] | None = None,
    ) -> SolveResult:
        """extra_images are (caption, image) pairs sent after the screenshot, each
        caption just before its image (the zoomed figure views). on_stream receives
        the open stream's close() so the pipeline can abort a request whose capture
        has been superseded."""
        content = [_image_block(image)]
        for caption, extra in extra_images:
            content += [{"type": "text", "text": caption}, _image_block(extra)]
        content.append({"type": "text", "text": user_text})
        kwargs = dict(
            model=model,
            max_tokens=MAX_TOKENS,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": content}],
            output_config={"format": {"type": "json_schema", "schema": PORTABLE_SCHEMA}},
        )
        try:
            if model in _NO_SERVER_FALLBACK:
                manager = self._client.messages.stream(**kwargs)
            else:
                manager = self._client.beta.messages.stream(
                    betas=[_FALLBACK_BETA], fallbacks="default", **kwargs
                )
            with manager as stream:
                if on_stream is not None:
                    on_stream(stream.close)
                message = stream.get_final_message()
        except anthropic.APIError as exc:
            raise _map_error(exc) from None
        # Once the response has started, the SDK lets transport errors through
        # unwrapped (a Wi-Fi change or sleep in the middle of a long answer).
        except httpx2.TimeoutException:
            raise ProviderError("timeout", "Claude did not answer in time; try again") from None
        except httpx2.HTTPError:
            raise ProviderError("network", "Lost the connection to Anthropic; try again") from None

        if message.stop_reason == "refusal":
            details = getattr(message, "stop_details", None)
            category = getattr(details, "category", None) if details else None
            raise ProviderError(
                "refused",
                "Claude declined to answer this screen"
                + (f" (safety category: {category})" if category else ""),
            )
        if message.stop_reason == "max_tokens":
            raise ProviderError("truncated", "Claude's answer was cut off; try fewer questions")
        text = "".join(b.text for b in message.content if getattr(b, "type", None) == "text")
        if not text.strip():
            raise ProviderError("malformed", "Claude returned no answer")
        usage = getattr(message, "usage", None)
        iterations = getattr(usage, "iterations", None) or []
        return SolveResult(
            text=text,
            model=getattr(message, "model", model) or model,
            input_tokens=getattr(usage, "input_tokens", 0) or 0,
            output_tokens=getattr(usage, "output_tokens", 0) or 0,
            fallback_used=any(getattr(i, "type", "") == "fallback_message" for i in iterations),
        )


def _map_error(exc: anthropic.APIError) -> ProviderError:
    """Most specific first. Messages are Peek's own copy, never the server body."""
    if isinstance(exc, anthropic.APITimeoutError):
        return ProviderError("timeout", "Claude did not answer in time; try again")
    if isinstance(exc, anthropic.APIConnectionError):
        return ProviderError("network", "Can't reach Anthropic; check your connection")
    if isinstance(exc, anthropic.AuthenticationError):
        return ProviderError("bad_key", "Anthropic rejected the API key")
    if isinstance(exc, anthropic.PermissionDeniedError):
        return ProviderError("permission", "This API key may not use that model")
    if isinstance(exc, anthropic.NotFoundError):
        return ProviderError("model_unavailable", "That model isn't available to this API key")
    if isinstance(exc, anthropic.PaymentRequiredError):
        return ProviderError("no_credit", "Your Anthropic account is out of credit")
    if isinstance(
        exc, anthropic.EnforcedSpendLimitReachedError | anthropic.ContractSpendLimitReachedError
    ):
        return ProviderError("spend_limit", "Your Anthropic spend limit has been reached")
    if isinstance(exc, anthropic.RateLimitError):
        retry_after = None
        with contextlib.suppress(TypeError, ValueError, AttributeError):
            retry_after = float(exc.response.headers.get("retry-after"))
        return ProviderError(
            "rate_limited",
            "Anthropic rate limit reached; try again shortly",
            retry_after=retry_after,
        )
    if isinstance(exc, anthropic.OverloadedError):
        return ProviderError("overloaded", "Claude is overloaded right now; try again")
    if isinstance(exc, anthropic.BadRequestError):
        message = str(getattr(exc, "message", "") or "").lower()
        if "credit balance" in message:
            return ProviderError("no_credit", "Your Anthropic account is out of credit")
        if "spend limit" in message:
            return ProviderError("spend_limit", "Your Anthropic spend limit has been reached")
        if "image" in message and (
            "downsized" in message or "too large" in message or "exceed" in message
        ):
            return ProviderError("image_too_large", "The screenshot was too large for Claude")
        log.warning("Anthropic rejected the request (400, type %s)", getattr(exc, "type", None))
        return ProviderError("failed", "Anthropic rejected the request; see the log")
    if isinstance(exc, anthropic.APIStatusError) and exc.status_code >= 500:
        return ProviderError("server", "Anthropic had a server error; try again")
    # Errors sent as an event inside a 200 stream carry only their type.
    error_type = getattr(exc, "type", None)
    if error_type == "overloaded_error":
        return ProviderError("overloaded", "Claude is overloaded right now; try again")
    if error_type == "rate_limit_error":
        return ProviderError("rate_limited", "Anthropic rate limit reached; try again shortly")
    if error_type == "api_error":
        return ProviderError("server", "Anthropic had a server error; try again")
    status = getattr(exc, "status_code", None)
    log.warning("Unexpected Anthropic error %s (status %s)", type(exc).__name__, status)
    return ProviderError("failed", "Claude request failed; see the log")
