"""build_model_provider: selection by KEN_MODEL_PROVIDER."""

from __future__ import annotations

import pytest

from ken_contracts.model import ModelProvider
from ken_core.config import Settings
from ken_core.providers.model import build_model_provider
from ken_core.providers.model.mock import MockModelProvider
from ken_core.providers.model.ollama import OllamaProvider


def _settings(**overrides: object) -> Settings:
    return Settings(_env_file=None, **overrides)  # type: ignore[arg-type]


async def test_mock_is_selected() -> None:
    provider = build_model_provider(_settings(model_provider="mock"))
    assert isinstance(provider, MockModelProvider)
    assert isinstance(provider, ModelProvider)
    await provider.aclose()


async def test_ollama_is_selected_without_network() -> None:
    # Construction performs no I/O; nothing is called before aclose.
    provider = build_model_provider(_settings(model_provider="ollama"))
    assert isinstance(provider, OllamaProvider)
    assert isinstance(provider, ModelProvider)
    await provider.aclose()


async def test_default_is_mock(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("KEN_MODEL_PROVIDER", raising=False)
    provider = build_model_provider(_settings())
    assert isinstance(provider, MockModelProvider)
    await provider.aclose()
