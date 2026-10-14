"""Contract: every ModelProvider reports ONE embedding identity.

ADR-0010 section 2 and the ``ken_contracts.intelligence`` docstring make the
PROVIDER-REPORTED model the freshness/similarity key for stored vectors. That is
only sound if a provider says the same thing in both places it can say it:

1. ``capabilities().roles[EMBEDDING].model == EmbeddingResult.model``;
2. ``capabilities().embedding_dimensions == EmbeddingResult.dimensions`` once a
   dimension is known (Ollama learns it from the first successful embed);
3. ``capabilities().provider == EmbeddingResult.provider``.

Ollama canonical form: the configured ``KEN_MODEL_EMBEDDING`` string, verbatim,
in BOTH places -- even when the daemon lists or echoes ``<name>:latest``.

The mock is checked always; Ollama through ``httpx.MockTransport`` (no network).
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import httpx
import pytest

from ken_contracts.model import EmbeddingResult, ModelProvider, ModelRole, ProviderCapabilities
from ken_core.config import Settings
from ken_core.providers.model.mock import MockModelProvider
from ken_core.providers.model.ollama import OllamaProvider


def _embedding_role_model(caps: ProviderCapabilities) -> str | None:
    [status] = [s for s in caps.roles if s.role is ModelRole.EMBEDDING]
    assert status.status == "ok"
    return status.model


async def _assert_identity(provider: ModelProvider) -> EmbeddingResult:
    result = await provider.embed(texts=["internal linking", "market research reports"])
    caps = await provider.capabilities()
    assert caps.supports_embeddings is True
    assert _embedding_role_model(caps) == result.model
    assert caps.embedding_dimensions == result.dimensions
    assert caps.provider == result.provider == provider.name
    assert all(len(v) == result.dimensions for v in result.vectors)
    return result


# ------------------------------------------------------------------- mock
@pytest.mark.parametrize("dim", [8, 768])
async def test_mock_embedding_identity(dim: int) -> None:
    provider = MockModelProvider(Settings(_env_file=None, mock_embedding_dim=dim))
    # The mock knows its dimension before any embed.
    assert (await provider.capabilities()).embedding_dimensions == dim
    result = await _assert_identity(provider)
    assert result.model == "mock-embedding"
    assert result.dimensions == dim


# ----------------------------------------------------------------- ollama
CONFIGURED = "nomic-embed-text"


def _ollama(tag_name: str, echoed_model: str | None) -> OllamaProvider:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/tags":
            return httpx.Response(
                200,
                json={
                    "models": [
                        {"name": tag_name, "model": tag_name, "capabilities": ["embedding"]},
                        {"name": "qwen3:8b-q4_K_M", "capabilities": ["completion", "tools"]},
                    ]
                },
            )
        if request.url.path == "/api/version":
            return httpx.Response(200, json={"version": "0.35.0"})
        if request.url.path == "/api/embed":
            n = len(json.loads(request.content)["input"])
            body: dict[str, Any] = {"embeddings": [[0.6, 0.8, 0.0]] * n}
            if echoed_model is not None:
                body["model"] = echoed_model
            return httpx.Response(200, json=body)
        return httpx.Response(404, json={"error": "no route"})

    settings = Settings(
        _env_file=None,
        model_provider="ollama",
        ollama_base_url="http://ollama.test:11434",
        model_embedding=CONFIGURED,
    )
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return OllamaProvider(settings, client=client)


Case = tuple[str, str | None]
OLLAMA_CASES: dict[str, Case] = {
    "daemon-echoes-verbatim": (f"{CONFIGURED}:latest", CONFIGURED),
    "daemon-normalises-to-latest": (f"{CONFIGURED}:latest", f"{CONFIGURED}:latest"),
    "daemon-omits-model": (CONFIGURED, None),
}


@pytest.mark.parametrize("case", list(OLLAMA_CASES), ids=list(OLLAMA_CASES))
async def test_ollama_embedding_identity_uses_configured_string(case: str) -> None:
    tag_name, echoed = OLLAMA_CASES[case]
    provider = _ollama(tag_name, echoed)
    try:
        before = await provider.capabilities()
        assert _embedding_role_model(before) == CONFIGURED
        assert before.embedding_dimensions is None  # unknown until observed
        result = await _assert_identity(provider)
        assert result.model == CONFIGURED  # never the daemon's ":latest" spelling
        assert result.dimensions == 3
    finally:
        await provider.aclose()


@pytest.mark.parametrize(
    "build",
    [
        lambda: MockModelProvider(),
        lambda: _ollama(f"{CONFIGURED}:latest", f"{CONFIGURED}:latest"),
    ],
    ids=["mock", "ollama"],
)
async def test_identity_is_stable_across_calls(build: Callable[[], ModelProvider]) -> None:
    provider = build()
    try:
        first = await provider.embed(texts=["a"])
        second = await provider.embed(texts=["b", "c"])
        assert (first.model, first.dimensions, first.provider) == (
            second.model,
            second.dimensions,
            second.provider,
        )
    finally:
        await provider.aclose()
