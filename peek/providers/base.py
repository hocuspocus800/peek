"""What every provider implements, and the errors the UI knows how to show."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import ClassVar

from peek.imaging import SentImage


@dataclass(frozen=True)
class ModelInfo:
    id: str
    name: str  # display name, e.g. "Claude Sonnet 5.5"
    note: str = ""


@dataclass(frozen=True)
class SolveResult:
    text: str  # the model's raw JSON
    model: str  # the model that answered (a refusal fallback may differ)
    input_tokens: int = 0
    output_tokens: int = 0
    fallback_used: bool = False


class ProviderError(Exception):
    """kind is one of KINDS; message is safe to show and to log (no secrets, no
    screen text, no raw server bodies)."""

    KINDS: ClassVar[frozenset[str]] = frozenset(
        {
            "not_configured",
            "bad_key",
            "permission",
            "no_credit",
            "spend_limit",
            "rate_limited",
            "overloaded",
            "model_unavailable",
            "image_too_large",
            "refused",
            "truncated",
            "malformed",
            "timeout",
            "network",
            "server",
            "keychain",
            "signed_out",  # ChatGPT: no sign-in, or it expired
            "plan_declined",  # ChatGPT: signed in without plan usage
            "usage_limit",  # ChatGPT: the plan's (or Peek's) usage limit is reached
            "not_eligible",  # ChatGPT: the plan can't be used by apps
            "failed",
        }
    )

    def __init__(self, kind: str, message: str, *, retry_after: float | None = None) -> None:
        assert kind in self.KINDS, kind
        super().__init__(message)
        self.kind = kind
        self.retry_after = retry_after


class VisionProvider(ABC):
    name: str

    @abstractmethod
    def list_models(self) -> list[ModelInfo]: ...

    @abstractmethod
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
        """Blocking; runs on a solve thread. extra_images are (caption, image) pairs
        sent after the screenshot. show_work asks for each question's working too
        (schema.request_format). Raises ProviderError."""

    @abstractmethod
    def test_connection(self, model: str) -> None:
        """Cheap check that the credentials work and the model is available."""
