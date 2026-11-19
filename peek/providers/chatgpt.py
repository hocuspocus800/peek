"""ChatGPT via Sign in with ChatGPT: the user's plan, the public Responses API.

Per the SIWC docs (models-and-inference, preview-limitations, errors-and-recovery):
- `OpenAI(api_key=<access token>, base_url="https://api.openai.com/v1",
  max_retries=0)`; `store=False`, `stream=True` on every request; `instructions`
  instead of a system message; `input` as an array; images inline (data URLs: the
  Files API is not available on this route).
- None of the unsupported fields (temperature, top_p, max_output_tokens,
  metadata, truncation, user, previous_response_id, …).
- Success only on `response.completed`; `response.failed` carries the error code
  (usage limits can arrive mid-stream); `response.incomplete` is a cut-off answer.
- Models: `GET /v1/models` with the token returns `{"models": [{slug,
  display_name, visibility}]}`; keep `visibility == "list"`, in server order.
- Structured output: `text.format` json_schema. The SIWC docs neither list it as
  supported nor as unsupported; if the route rejects it
  (`subscription_sharing_unsupported_capability`), Peek asks once more without
  it and relies on the prompt and the lenient parser.
- Coordinates: normalized 0-1000 (like Gemini), independent of how the model
  resizes the image.
"""

from __future__ import annotations

import base64
import logging
from collections.abc import Callable, Sequence

from peek.imaging import SentImage
from peek.providers.base import ModelInfo, ProviderError, SolveResult, VisionProvider
from peek.providers.catalog import PROVIDERS
from peek.schema import request_format

log = logging.getLogger(__name__)

BASE_URL = "https://api.openai.com/v1"
TIMEOUT_S = 120.0
SPEC = PROVIDERS["chatgpt"]
JSON_ONLY = "\n\nReply with one JSON object only, matching this JSON Schema, and nothing else:\n"


class UnsupportedCapability(Exception):
    def __init__(self, param: str) -> None:
        super().__init__(param)
        self.param = param


class ChatGPTProvider(VisionProvider):
    name = "chatgpt"

    def __init__(self, access_token: str) -> None:
        if not access_token:
            raise ProviderError("signed_out", "Sign in with ChatGPT in Settings")
        from openai import OpenAI

        self._token = access_token
        self._client = OpenAI(
            api_key=access_token, base_url=BASE_URL, max_retries=0, timeout=TIMEOUT_S
        )

    # --- models ---------------------------------------------------------------

    def list_models(self) -> list[ModelInfo]:
        """The signed-in account's catalog (not the standard /v1/models shape)."""
        import httpx2

        try:
            response = httpx2.get(
                f"{BASE_URL}/models",
                headers={"Authorization": f"Bearer {self._token}"},
                timeout=30.0,
                follow_redirects=False,
            )
        except httpx2.TimeoutException:
            raise ProviderError("timeout", "OpenAI did not answer in time") from None
        except httpx2.HTTPError:
            raise ProviderError("network", "Can't reach OpenAI; check your connection") from None
        if response.status_code == 401:
            raise ProviderError("signed_out", "Your ChatGPT sign-in isn't valid; sign in again")
        if response.status_code >= 400:
            raise ProviderError("failed", f"OpenAI couldn't list models ({response.status_code})")
        try:
            data = response.json()
        except ValueError:
            data = {}
        models = data.get("models") if isinstance(data, dict) else None
        out = []
        for m in models or []:
            if isinstance(m, dict) and m.get("visibility") == "list" and m.get("slug"):
                out.append(ModelInfo(str(m["slug"]), str(m.get("display_name") or m["slug"])))
        return out

    def test_connection(self, model: str) -> None:
        models = self.list_models()
        if models and model and all(m.id != model for m in models):
            raise ProviderError("model_unavailable", "That model isn't available to this account")

    # --- solve ------------------------------------------------------------------

    def solve(
        self,
        image: SentImage,
        user_text: str,
        model: str,
        *,
        extra_images: Sequence[tuple[str, SentImage]] = (),
        on_stream: Callable[[Callable[[], None]], None] | None = None,
        show_work: bool = False,
    ) -> SolveResult:
        content = [_image(image)]
        for caption, extra in extra_images:
            content += [{"type": "input_text", "text": caption}, _image(extra)]
        content.append({"type": "input_text", "text": user_text})
        system, schema = request_format(show_work)
        try:
            return self._stream(model, content, system=system, schema=schema, on_stream=on_stream)
        except UnsupportedCapability as exc:
            log.warning("ChatGPT route rejected %s; asking without a JSON schema", exc.param)
            content[-1] = {
                "type": "input_text",
                "text": user_text + JSON_ONLY + _schema_text(schema),
            }
            try:
                return self._stream(model, content, system=system, schema=None, on_stream=on_stream)
            except UnsupportedCapability as again:
                raise ProviderError(
                    "failed", f"ChatGPT rejected part of the request ({again.param})"
                ) from None

    def _stream(
        self, model, content, *, system: str, schema: dict | None, on_stream
    ) -> SolveResult:
        """schema None: prompted JSON (the route refused a JSON schema)."""
        kwargs = {
            "model": model,
            "instructions": system,
            "input": [{"role": "user", "content": content}],
            "store": False,
            "stream": True,
        }
        if schema is not None:
            kwargs["text"] = {
                "format": {
                    "type": "json_schema",
                    "name": "screen_answers",
                    "schema": schema,
                    "strict": True,
                }
            }
        text: list[str] = []
        completed = None
        try:
            stream = self._client.responses.create(**kwargs)
            if on_stream is not None:
                on_stream(stream.close)
            with stream:
                for event in stream:
                    kind = getattr(event, "type", "")
                    if kind == "response.output_text.delta":
                        text.append(event.delta)
                    elif kind == "error":  # a stream-level error event
                        code = getattr(event, "code", None) or "unknown_error"
                        if code == "subscription_sharing_unsupported_capability":
                            param = getattr(event, "param", "") or "a field"
                            raise UnsupportedCapability(str(param))
                        raise _failed(code)
                    elif kind == "response.failed":
                        error = getattr(event.response, "error", None)
                        raise _failed(getattr(error, "code", None) or "unknown_error")
                    elif kind == "response.incomplete":
                        details = getattr(event.response, "incomplete_details", None)
                        reason = getattr(details, "reason", "") or ""
                        if "content_filter" in reason:
                            raise ProviderError("refused", "ChatGPT declined to answer this screen")
                        raise ProviderError("truncated", "ChatGPT's answer was cut off")
                    elif kind == "response.completed":
                        completed = event.response
        except (ProviderError, UnsupportedCapability):
            raise
        except Exception as exc:
            raise _map_error(exc) from None
        if completed is None:
            raise ProviderError("network", "ChatGPT's answer stopped before it finished")
        body = "".join(text) or (getattr(completed, "output_text", "") or "")
        refusal = _refusal(completed)
        if refusal and not body.strip():
            raise ProviderError("refused", "ChatGPT declined to answer this screen")
        if not body.strip():
            raise ProviderError("malformed", "ChatGPT returned an empty answer")
        usage = getattr(completed, "usage", None)
        return SolveResult(
            body,
            str(getattr(completed, "model", None) or model),
            int(getattr(usage, "input_tokens", 0) or 0),
            int(getattr(usage, "output_tokens", 0) or 0),
            False,
        )


def _image(image: SentImage) -> dict:
    data = base64.standard_b64encode(image.data).decode("ascii")
    return {
        "type": "input_image",
        "image_url": f"data:{image.media_type};base64,{data}",
        "detail": "high",
    }


def _schema_text(schema: dict) -> str:
    import json

    return json.dumps(schema, separators=(",", ":"))


def _refusal(response) -> bool:
    for item in getattr(response, "output", None) or []:
        for part in getattr(item, "content", None) or []:
            if getattr(part, "type", "") == "refusal":
                return True
    return False


def _failed(code: str) -> ProviderError:
    """A `response.failed` event's error code (also used for HTTP error bodies)."""
    if code == "subscription_sharing_usage_limit_exceeded":
        # Docs: the limit may be the plan's or one set for this app; don't say which.
        return ProviderError("usage_limit", "ChatGPT usage limit reached")
    if code in ("subscription_sharing_usage_unavailable", "subscription_sharing_user_unavailable"):
        return ProviderError("overloaded", "ChatGPT plan usage is unavailable right now; try later")
    if code == "subscription_sharing_user_not_eligible":
        return ProviderError(
            "not_eligible", "This ChatGPT plan can't be used with Sign in with ChatGPT"
        )
    if code in ("subscription_sharing_invalid_user", "invalid_api_key"):
        return ProviderError("signed_out", "Your ChatGPT sign-in isn't valid; sign in again")
    if code in ("chatpass_v2_scope_not_authorized", "chatpass_v2_invalid_authorization_context"):
        return ProviderError("plan_declined", "ChatGPT plan usage isn't allowed; see Settings")
    if code == "subscription_sharing_route_not_supported":
        return ProviderError("not_eligible", "ChatGPT plan usage isn't available here")
    if code in ("rate_limit_exceeded",):
        return ProviderError("rate_limited", "ChatGPT is rate limiting requests; try again shortly")
    if code in ("server_error", "overloaded"):
        return ProviderError("overloaded", "ChatGPT is busy; try again")
    return ProviderError("failed", f"ChatGPT couldn't answer ({code})")


def _map_error(exc: Exception) -> ProviderError | UnsupportedCapability:
    import openai

    if isinstance(exc, openai.APIStatusError):
        status = exc.status_code
        body = exc.body if isinstance(exc.body, dict) else {}
        error = body.get("error") if isinstance(body.get("error"), dict) else body
        code = str(error.get("code") or "") if isinstance(error, dict) else ""
        if code == "subscription_sharing_unsupported_capability":
            return UnsupportedCapability(str(error.get("param") or "a field"))
        if code:
            return _failed(code)
        if status == 401:
            return ProviderError("signed_out", "Your ChatGPT sign-in isn't valid; sign in again")
        if status == 403:
            return ProviderError("not_eligible", "OpenAI didn't allow this request (403)")
        if status == 404:
            return ProviderError("model_unavailable", "That model isn't available to this account")
        if status == 429:
            return ProviderError("rate_limited", "ChatGPT is rate limiting requests; try again")
        if status in (500, 502, 503, 504):
            return ProviderError("overloaded", "ChatGPT is busy; try again")
        return ProviderError("failed", f"ChatGPT couldn't answer ({status})")
    if isinstance(exc, openai.APITimeoutError):
        return ProviderError("timeout", "ChatGPT did not answer in time; try again")
    if isinstance(exc, openai.APIConnectionError):
        return ProviderError("network", "Can't reach OpenAI; check your connection")
    if isinstance(exc, openai.APIError):  # e.g. an error event inside the stream
        body = exc.body if isinstance(exc.body, dict) else {}
        inner = body.get("error") if isinstance(body.get("error"), dict) else body
        code = str(inner.get("code") or getattr(exc, "code", "") or "")
        if code == "subscription_sharing_unsupported_capability":
            return UnsupportedCapability(str(inner.get("param") or "a field"))
        if code:
            return _failed(code)
    if isinstance(exc, openai.APITimeoutError):
        return ProviderError("timeout", "ChatGPT did not answer in time; try again")
    if isinstance(exc, openai.APIConnectionError):
        return ProviderError("network", "Can't reach OpenAI; check your connection")
    log.exception("Unexpected ChatGPT error", exc_info=exc)
    return ProviderError("failed", "ChatGPT request failed; see the log")
