"""AI providers: Claude (milestone 4), Gemini (8), ChatGPT (9). See catalog.py."""

from __future__ import annotations


def best_model(provider: str, available: list[str] | None = None) -> str | None:
    """The most capable model a provider offers: what Hard mode switches to.
    *available*: the account's model ids, for providers whose list comes from it."""
    from peek.providers.catalog import PROVIDERS

    spec = PROVIDERS.get(provider)
    return spec.best_of(available) if spec is not None else None


def effective_model(
    provider: str, selected: str, hard_mode: bool, available: list[str] | None = None
) -> str:
    """Hard mode uses the provider's best model; off, the user's own choice. The
    choice itself is never overwritten, so turning Hard mode off restores it."""
    if hard_mode:
        return best_model(provider, available) or selected
    return selected
