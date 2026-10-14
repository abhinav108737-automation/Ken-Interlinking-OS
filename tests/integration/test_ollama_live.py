"""Live smoke test against the host Ollama daemon.

Opt-in twice: marked ``live`` AND skipped unless ``KEN_RUN_LIVE_OLLAMA=1``.
Read-only use of already-installed models; never pulls a model. Phase 2
expects the configured embedding model (``KEN_MODEL_EMBEDDING``) to be pulled.

    KEN_RUN_LIVE_OLLAMA=1 .venv/bin/pytest tests/integration/test_ollama_live.py -q -m live
"""

from __future__ import annotations

import math
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


async def test_capabilities_all_roles_ok(provider: OllamaProvider) -> None:
    caps = await provider.capabilities()
    statuses = {s.role: s.status for s in caps.roles}
    assert statuses[ModelRole.FAST] == "ok"
    assert statuses[ModelRole.REASONING] == "ok"
    # Phase 2: the embedding model is a prerequisite (ADR-0010 section 2).
    embedding_model = provider._settings.role_model(ModelRole.EMBEDDING)
    assert statuses[ModelRole.EMBEDDING] == "ok", f"run: ollama pull {embedding_model}"
    assert caps.supports_embeddings is True
    assert caps.embedding_dimensions is None  # unknown until the first embed
    assert caps.supports_json_schema is True


def _cos(a: list[float], b: list[float]) -> float:
    dot = math.fsum(x * y for x, y in zip(a, b, strict=True))
    return dot / (math.sqrt(math.fsum(x * x for x in a)) * math.sqrt(math.fsum(y * y for y in b)))


async def test_embed_related_closer_than_unrelated(provider: OllamaProvider) -> None:
    # Prefixes come from settings (model conventions are configuration).
    prefix = provider._settings.embedding_document_prefix
    texts = [
        f"{prefix}Market size and growth forecast for the India electric vehicle battery industry",
        f"{prefix}India EV battery market analysis: revenue, share and outlook to 2030",
        f"{prefix}A slow-cooked tomato and basil pasta sauce recipe for weeknight dinners",
    ]
    result = await provider.embed(texts=texts)
    caps = await provider.capabilities()
    [status] = [s for s in caps.roles if s.role is ModelRole.EMBEDDING]

    assert result.dimensions > 0
    assert all(len(v) == result.dimensions for v in result.vectors)
    assert caps.embedding_dimensions == result.dimensions
    assert status.model == result.model  # one embedding identity (ADR-0010)
    related, unrelated = _cos(*result.vectors[:2]), _cos(result.vectors[0], result.vectors[2])
    assert related > unrelated
    print(
        f"\nmodel={result.model} dimensions={result.dimensions} related={related:.3f} "
        f"unrelated={unrelated:.3f} latency_ms={result.latency_ms:.0f}"
    )


async def test_embed_overlong_input_is_truncated_not_rejected(provider: OllamaProvider) -> None:
    # Far beyond nomic-embed-text's 2048-token context; truncate=true must absorb it.
    result = await provider.embed(texts=["internal linking research " * 4000])
    assert len(result.vectors) == 1
    assert result.dimensions > 0


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
