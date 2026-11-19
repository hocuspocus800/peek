"""What Peek knows about each AI provider, without importing any SDK.

The app, Settings and the pipeline look providers up here; the SDK-backed
classes (claude.py, gemini.py, chatgpt.py) are imported only when a request is
made, so launching Peek never pays for SDKs it does not use.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from peek.providers.base import ModelInfo

CLAUDE_MODELS = (
    # The models offered in Settings, in the order shown (owner's choice, 2026-11-19).
    ModelInfo("claude-fable-5-2", "Claude Fable 5.2", "Most capable; slowest and priciest"),
    ModelInfo("claude-opus-5-5", "Claude Opus 5.5", "Very capable"),
    ModelInfo("claude-sonnet-5-5", "Claude Sonnet 5.5", "Fast and capable"),
    ModelInfo("claude-haiku-5-5", "Claude Haiku 5.5", "Fastest and cheapest"),
)

# Shown until the account's own list arrives (models.list), and if it fails.
# Gemini API models page, 2026-11-19: 3.8 Flash is the stable default (the Figma
# frame shows it selected); 4 Carbon is the strongest reasoning model.
GEMINI_MODELS = (
    ModelInfo("gemini-4-carbon", "Gemini 4 Carbon", "Most capable; slowest and priciest"),
    ModelInfo("gemini-4-neon", "Gemini 4 Neon", "Frontier, fast"),
    ModelInfo("gemini-4-hydrogen", "Gemini 4 Hydrogen", "Fast and capable"),
    ModelInfo("gemini-4-argon", "Gemini 4 Argon", "Efficient reasoning"),
    ModelInfo("gemini-3.8-flash", "Gemini 3.8 Flash", "Fast and capable; stable"),
    ModelInfo("gemini-3.5-flash-lite", "Gemini 3.5 Flash-Lite", "Fastest and cheapest"),
)


# Until the signed-in account's own catalog arrives (GET /v1/models); the SIWC docs'
# examples use these slugs. The account's list replaces them in Settings.
CHATGPT_MODELS = (
    ModelInfo("gpt-6.2-sol", "GPT-6.2 Sol"),
    ModelInfo("gpt-6.1-sol", "GPT-6.1 Sol"),
)


@dataclass(frozen=True)
class ProviderSpec:
    id: str  # settings.json value
    name: str  # "Claude"
    vendor: str  # "Anthropic", for messages ("Add your Anthropic API key")
    key_account: str | None  # Keychain account of its API key; None: sign-in (ChatGPT)
    key_prefix: str  # what a key starts with, for a friendly check before saving
    key_placeholder: str
    frame: str  # coordinates the model answers in: "pixels" or "normalized" (0-1000)
    default_model: str
    best_model: str  # Hard mode
    models: tuple[ModelInfo, ...]  # offered (Claude) or until the account's list loads
    fixed_models: bool  # True: exactly these (owner's choice); False: from the account
    model_prefix: str = ""  # what the account's model ids start with (not fixed lists)

    def create(self, credential: str):
        """The SDK-backed provider (imported here, on first use)."""
        if self.id == "claude":
            from peek.providers.claude import ClaudeProvider

            return ClaudeProvider(credential)
        if self.id == "gemini":
            from peek.providers.gemini import GeminiProvider

            return GeminiProvider(credential)
        if self.id == "chatgpt":
            from peek.providers.chatgpt import ChatGPTProvider

            return ChatGPTProvider(credential)
        raise KeyError(self.id)

    def accepts_model(self, model_id: str | None) -> bool:
        if not model_id:
            return False
        if self.fixed_models:
            return any(m.id == model_id for m in self.models)
        return model_id.startswith(self.model_prefix)

    def best_of(self, available: list[str] | None) -> str:
        """Hard mode's model. Fixed lists: the catalog's best. From the account: the
        newest full-size family (the version right after the prefix: 'gemini-4-…' is
        4, 'gpt-6.2-…' is 6.2; not mini/nano/lite), stable models before previews,
        experiments and dated snapshots; among equals the catalog's best, then the
        catalog's order, then the account's. No list: the catalog's best."""
        if self.fixed_models or not available:
            return self.best_model
        full = [m for m in available if not _SMALL.search(m)] or list(available)
        known = [m.id for m in self.models]

        def rank(m: str):
            return (
                not _UNSTABLE.search(m),
                self._family(m),
                m == self.best_model,
                -known.index(m) if m in known else -len(known),
                -available.index(m),
            )

        return max(full, key=rank)

    def _family(self, model_id: str) -> float:
        match = re.match(re.escape(self.model_prefix) + r"(\d+(?:\.\d+)?)", model_id)
        return float(match.group(1)) if match else 0.0

    def display_name(self, model_id: str) -> str:
        for m in self.models:
            if m.id == model_id:
                return m.name
        return pretty_model_name(model_id)


PROVIDERS: dict[str, ProviderSpec] = {
    "claude": ProviderSpec(
        id="claude",
        name="Claude",
        vendor="Anthropic",
        key_account="anthropic_api_key",
        key_prefix="sk-ant-",
        key_placeholder="Paste your Anthropic API key",
        frame="pixels",
        default_model="claude-sonnet-5-5",
        best_model="claude-fable-5-2",
        models=CLAUDE_MODELS,
        fixed_models=True,
    ),
    "gemini": ProviderSpec(
        id="gemini",
        name="Gemini",
        vendor="Google AI Studio",
        key_account="gemini_api_key",
        key_prefix="AIza",
        key_placeholder="Paste your Gemini API key",
        frame="normalized",
        default_model="gemini-3.8-flash",
        best_model="gemini-4-carbon",
        models=GEMINI_MODELS,
        fixed_models=False,
        model_prefix="gemini-",
    ),
    "chatgpt": ProviderSpec(
        id="chatgpt",
        name="ChatGPT",
        vendor="ChatGPT",
        key_account=None,  # Sign in with ChatGPT (providers/oauth_chatgpt.py)
        key_prefix="",
        key_placeholder="",
        frame="normalized",
        default_model="gpt-6.2-sol",
        best_model="gpt-6.2-sol",
        models=CHATGPT_MODELS,
        fixed_models=False,
        model_prefix="gpt-",
    ),
}

_SMALL = re.compile(r"(^|-)(mini|nano|lite|flash-lite|small)($|-)")


# Previews, experiments, dated or numbered snapshots ("-001", "-11-2026", "-0514").
_UNSTABLE = re.compile(r"preview|(^|-)exp($|-)|-\d{3,}($|-)|-\d{2}-\d{2,4}($|-)")


def spec(provider_id: str) -> ProviderSpec:
    return PROVIDERS.get(provider_id, PROVIDERS["claude"])


def pretty_model_name(model_id: str) -> str:
    """'gemini-3.8-flash-lite' -> 'Gemini 3.8 Flash-Lite' (for models the API lists
    that the catalog does not know)."""
    words = model_id.split("-")
    if words and words[0] == "gpt" and len(words) > 1:  # "gpt-6.2-sol" -> "GPT-6.2 Sol"
        words = [f"GPT-{words[1]}", *words[2:]]
    out: list[str] = []
    for w in words:
        if out and w == "lite":
            out[-1] += "-Lite"
        elif re.fullmatch(r"[\d.]+", w):
            out.append(w)
        else:
            out.append(w if any(ch.isupper() for ch in w) else w.capitalize())
    return " ".join(out)
