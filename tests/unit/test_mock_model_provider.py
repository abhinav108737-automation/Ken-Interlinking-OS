"""MockModelProvider: deterministic, network-free, never fabricates validated values."""

from __future__ import annotations

import json
import math
import os
import subprocess
import sys

import pytest
from pydantic import BaseModel, ValidationError

from ken_contracts.model import (
    Message,
    ModelProvider,
    ModelProviderError,
    ModelRole,
    ModelSchemaViolationError,
    ModelUnavailableError,
)
from ken_core.config import Settings
from ken_core.providers.model.mock import MockModelProvider, deterministic_vector


class Verdict(BaseModel):
    label: str
    score: float


class Other(BaseModel):
    flag: bool


MSGS = [Message(role="system", content="be terse"), Message(role="user", content="hello")]


def _settings(**overrides: object) -> Settings:
    return Settings(_env_file=None, **overrides)  # type: ignore[arg-type]


@pytest.fixture
def provider() -> MockModelProvider:
    return MockModelProvider(_settings())


def test_conforms_to_protocol(provider: MockModelProvider) -> None:
    assert isinstance(provider, ModelProvider)
    assert provider.name == "mock"


async def test_complete_is_deterministic(provider: MockModelProvider) -> None:
    a = await provider.complete(role=ModelRole.FAST, messages=MSGS)
    b = await MockModelProvider(_settings()).complete(role=ModelRole.FAST, messages=MSGS)
    c = await provider.complete(
        role=ModelRole.FAST, messages=[Message(role="user", content="different")]
    )
    assert a.text == b.text
    assert a.text != c.text
    assert a.provider == "mock"
    assert a.role is ModelRole.FAST


async def test_embed_shape_normalisation_and_stability(provider: MockModelProvider) -> None:
    result = await provider.embed(texts=["alpha", "beta", "alpha"])
    assert result.dimensions == 768
    assert len(result.vectors) == 3
    for vector in result.vectors:
        assert len(vector) == 768
        assert math.isclose(math.fsum(v * v for v in vector), 1.0, rel_tol=1e-9)
    assert result.vectors[0] == result.vectors[2]
    assert result.vectors[0] != result.vectors[1]
    again = await MockModelProvider(_settings()).embed(texts=["alpha"])
    assert again.vectors[0] == result.vectors[0]


def _cos(a: list[float], b: list[float]) -> float:
    return math.fsum(x * y for x, y in zip(a, b, strict=True))


def test_feature_hashing_golden_vector() -> None:
    """Pins the algorithm: any change to tokenising/hashing/signing fails here.

    "internal linking" -> 3 features (2 unigrams + 1 bigram), each landing in a
    distinct bucket of 8, so every non-zero entry is +-1/sqrt(3).
    """
    s = 1 / math.sqrt(3)
    expected = [0.0, s, 0.0, 0.0, 0.0, 0.0, -s, s]
    assert deterministic_vector("internal linking", 8) == pytest.approx(expected, abs=1e-12)


def test_feature_hashing_is_lexically_meaningful() -> None:
    a = deterministic_vector("Internal linking improves SEO for market research reports", 768)
    b = deterministic_vector("Market research reports benefit from internal linking for SEO", 768)
    c = deterministic_vector("The cat sat quietly on the warm kitchen mat", 768)
    assert _cos(a, b) > 0.5
    assert _cos(a, b) > _cos(a, c)
    assert _cos(b, c) < _cos(a, b)
    # Bigrams carry some word order: same words, different order, still close but not equal.
    ab = deterministic_vector("research market", 768)
    ba = deterministic_vector("market research", 768)
    assert ab != ba
    assert 0.0 < _cos(ab, ba) < 1.0


def test_feature_hashing_normalises_case_and_unicode() -> None:
    composed = "caf\u00e9 reports"  # e-acute as one code point
    decomposed = "Cafe\u0301 REPORTS"  # e + combining acute; upper case
    assert deterministic_vector(composed, 64) == deterministic_vector(decomposed, 64)
    # Punctuation and whitespace are not features.
    assert deterministic_vector("market, research!", 64) == deterministic_vector(
        "  market   research ", 64
    )


@pytest.mark.parametrize("text", ["", "   ", "\n\t", "!!! ... ---"])
def test_feature_hashing_empty_text_is_fixed_basis_vector(text: str) -> None:
    assert deterministic_vector(text, 5) == [1.0, 0.0, 0.0, 0.0, 0.0]


def test_feature_hashing_rejects_non_positive_dimensions() -> None:
    with pytest.raises(ValueError, match="positive"):
        deterministic_vector("x", 0)


def test_feature_hashing_is_stable_across_processes() -> None:
    """Never Python's salted hash(): a different PYTHONHASHSEED gives the same vector."""
    code = (
        "import json; from ken_core.providers.model.mock import deterministic_vector as d; "
        "print(json.dumps(d('internal links between research pages', 32)))"
    )
    outputs = set()
    for seed in ("1", "2"):
        env = {**os.environ, "PYTHONHASHSEED": seed}
        proc = subprocess.run(  # noqa: S603 - fixed argv: this interpreter, a literal script
            [sys.executable, "-c", code], capture_output=True, text=True, env=env, check=True
        )
        outputs.add(proc.stdout.strip())
    assert len(outputs) == 1
    assert json.loads(outputs.pop()) == deterministic_vector(
        "internal links between research pages", 32
    )


async def test_embedding_dimension_comes_from_settings() -> None:
    provider = MockModelProvider(_settings(mock_embedding_dim=16))
    result = await provider.embed(texts=["x"])
    assert result.dimensions == 16
    assert len(result.vectors[0]) == 16
    caps = await provider.capabilities()
    assert caps.embedding_dimensions == 16


async def test_embed_rejects_non_embedding_role_and_empty_input(
    provider: MockModelProvider,
) -> None:
    with pytest.raises(ModelUnavailableError):
        await provider.embed(texts=["x"], role=ModelRole.FAST)
    with pytest.raises(ValueError, match="empty"):
        await provider.embed(texts=[])


async def test_registered_structured_value_from_instance_and_dict(
    provider: MockModelProvider,
) -> None:
    provider.register(Verdict, Verdict(label="relevant", score=0.9))
    provider.register(Other, {"flag": True})
    result = await provider.complete_structured(
        role=ModelRole.REASONING, messages=MSGS, schema=Verdict
    )
    assert isinstance(result.value, Verdict)
    assert result.value == Verdict(label="relevant", score=0.9)
    assert result.attempts == 1
    assert result.repaired is False
    assert Verdict.model_validate_json(result.completion.text) == result.value
    other = await provider.complete_structured(role=ModelRole.FAST, messages=MSGS, schema=Other)
    assert other.value.flag is True


def test_register_validates_value(provider: MockModelProvider) -> None:
    with pytest.raises(ValidationError):
        provider.register(Verdict, {"label": "x", "score": "not-a-number"})


async def test_unregistered_schema_is_a_violation(provider: MockModelProvider) -> None:
    with pytest.raises(ModelSchemaViolationError) as info:
        await provider.complete_structured(role=ModelRole.FAST, messages=MSGS, schema=Verdict)
    assert "Verdict" in str(info.value)


async def test_scripted_violation_applies_once(provider: MockModelProvider) -> None:
    provider.register(Verdict, {"label": "ok", "score": 1.0})
    provider.script_violation(Verdict, raw_text="garbage")
    with pytest.raises(ModelSchemaViolationError) as info:
        await provider.complete_structured(
            role=ModelRole.FAST, messages=MSGS, schema=Verdict, max_attempts=3
        )
    assert info.value.raw_text == "garbage"
    assert info.value.attempts == 3
    ok = await provider.complete_structured(role=ModelRole.FAST, messages=MSGS, schema=Verdict)
    assert ok.value.label == "ok"


async def test_calls_are_recorded(provider: MockModelProvider) -> None:
    provider.register(Other, {"flag": False})
    await provider.complete(role=ModelRole.FAST, messages=MSGS)
    await provider.complete_structured(role=ModelRole.REASONING, messages=MSGS, schema=Other)
    await provider.embed(texts=["a", "b"])
    await provider.health()
    methods = [c.method for c in provider.calls]
    assert methods == ["complete", "complete_structured", "embed", "health"]
    assert provider.calls[0].messages == tuple(MSGS)
    assert provider.calls[1].role is ModelRole.REASONING
    assert provider.calls[1].schema == "Other"
    assert provider.calls[2].texts == ("a", "b")
    provider.reset_calls()
    assert provider.calls == []


async def test_capabilities_and_health(provider: MockModelProvider) -> None:
    caps = await provider.capabilities()
    assert caps.provider == "mock"
    assert caps.supports_embeddings is True
    assert caps.embedding_dimensions == 768
    assert {s.role for s in caps.roles} == set(ModelRole)
    assert all(s.status == "ok" for s in caps.roles)
    health = await provider.health()
    assert health.reachable is True
    assert health.error is None


async def test_aclose_is_idempotent(provider: MockModelProvider) -> None:
    await provider.aclose()
    await provider.aclose()
    assert (await provider.health()).reachable is False
    with pytest.raises(ModelProviderError):
        await provider.complete(role=ModelRole.FAST, messages=MSGS)


async def test_recorded_calls_are_bounded() -> None:
    """A long-running process must not grow the call log forever (Phase 0 QA L3)."""
    from ken_core.providers.model.mock import MAX_RECORDED_CALLS

    provider = MockModelProvider()
    for _ in range(MAX_RECORDED_CALLS + 25):
        await provider.health()
    assert len(provider.calls) == MAX_RECORDED_CALLS
    await provider.capabilities()
    assert provider.calls[-1].method == "capabilities"
    assert len(provider.calls) == MAX_RECORDED_CALLS
