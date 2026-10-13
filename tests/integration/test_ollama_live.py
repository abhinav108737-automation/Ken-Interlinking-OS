"""Live smoke test against the host Ollama daemon.

Opt-in twice: marked ``live`` AND skipped unless ``KEN_RUN_LIVE_OLLAMA=1``.
Read-only use of already-installed models; never pulls a model.

    KEN_RUN_LIVE_OLLAMA=1 .venv/bin/pytest tests/integration/test_ollama_live.py -q -m live
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator

import pytest
from pydantic import BaseModel, Field

from ken_contracts.model import GenerationOptions, Message, ModelRole
from ken_core.config import Settings
from ken_core.providers.model.ollama import OllamaProvider

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        os.environ.get("KEN_RUN_LIVE_OLLAMA") != "1",
        reason="set KEN_RUN_LIVE_OLLAMA=1 to run against a live Ollama daemon",
    ),
]


class Capital(BaseModel):
    country: str = Field(min_length=1)
    capital: str = Field(min_length=1)


@pytest.fixture
async def provider() -> AsyncIterator[OllamaProvider]:
    # Model names come from KEN_MODEL_* (or Settings defaults), never from this test.
    settings = Settings(_env_file=None, model_provider="ollama")
    instance = OllamaProvider(settings)
    yield instance
    await instance.aclose()


async def test_health_reachable(provider: OllamaProvider) -> None:
    health = await provider.health()
    assert health.reachable is True, health.error
    assert health.latency_ms is not None


async def test_capabilities_fast_reasoning_ok_embedding_missing(provider: OllamaProvider) -> None:
    caps = await provider.capabilities()
    statuses = {s.role: s.status for s in caps.roles}
    assert statuses[ModelRole.FAST] == "ok"
    assert statuses[ModelRole.REASONING] == "ok"
    # Phase 0: no embedding model is installed, and none may be pulled.
    assert statuses[ModelRole.EMBEDDING] == "missing"
    assert caps.supports_embeddings is False
    assert caps.embedding_dimensions is None
    assert caps.supports_json_schema is True


async def test_complete_structured_tiny_schema(provider: OllamaProvider) -> None:
    result = await provider.complete_structured(
        role=ModelRole.FAST,
        messages=[
            Message(role="system", content="Reply with JSON only."),
            Message(role="user", content="Give the capital city of France."),
        ],
        schema=Capital,
        options=GenerationOptions(max_tokens=64, seed=1, timeout_s=300.0),
    )
    assert isinstance(result.value, Capital)
    assert result.attempts >= 1
    assert "<think>" not in result.completion.text
    print(  # visible with -s; useful when debugging a live run
        f"\nvalue={result.value!r} attempts={result.attempts} repaired={result.repaired} "
        f"model={result.completion.model} latency_ms={result.completion.latency_ms:.0f}"
    )
