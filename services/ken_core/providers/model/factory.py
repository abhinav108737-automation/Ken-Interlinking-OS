"""Construct the configured ModelProvider.

FROZEN SIGNATURE: ``build_model_provider(settings) -> ModelProvider``. Callers
(the API lifespan, the worker) depend on this function and on the protocol
only, never on a concrete provider class.
"""

from __future__ import annotations

from ken_contracts.model import ModelProvider
from ken_core.config import ConfigurationError, Settings
from ken_core.providers.model.mock import MockModelProvider
from ken_core.providers.model.ollama import OllamaProvider


def build_model_provider(settings: Settings) -> ModelProvider:
    """Return the provider selected by ``KEN_MODEL_PROVIDER`` (``mock`` or ``ollama``)."""
    if settings.model_provider == "mock":
        return MockModelProvider(settings)
    if settings.model_provider == "ollama":
        return OllamaProvider(settings)
    raise ConfigurationError(  # pragma: no cover - Settings' Literal already rejects this
        f"Unknown model provider: {settings.model_provider!r}"
    )
