"""Deterministic, network-free ModelProvider for tests, CI and local development.

Design rules:

* **Deterministic.** Every output is a pure function of the inputs (sha256 for
  completions, blake2b feature hashing for embeddings), so a test that passes
  once passes always, on every machine.
* **Lexically meaningful embeddings.** :func:`deterministic_vector` is feature
  hashing over word uni- and bigrams (ADR-0010 section 2), so related texts
  really are closer and retrieval tests can assert similarity without a model.
* **One embedding identity.** ``capabilities().roles[EMBEDDING].model`` and
  ``EmbeddingResult.model`` are the same string (``mock-embedding``), and
  ``capabilities().embedding_dimensions`` equals ``EmbeddingResult.dimensions``.
* **No fabricated structured values.** ``complete_structured`` only returns a
  value that a test explicitly registered *and* that passed the schema's own
  validation. An unregistered schema is a :class:`ModelSchemaViolationError`,
  never a ``model_construct`` placeholder -- a mock that bypassed validation
  would hide exactly the class of bug TRD.md section 6 exists to prevent.
* **Observable.** Every call is recorded in :attr:`MockModelProvider.calls`.
"""

from __future__ import annotations

import hashlib
import itertools
import json
import math
import re
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel

from ken_contracts.model import (
    Completion,
    EmbeddingResult,
    GenerationOptions,
    Message,
    ModelProviderError,
    ModelRole,
    ModelRoleStatus,
    ModelSchemaViolationError,
    ModelUnavailableError,
    ProviderCapabilities,
    ProviderHealth,
    StructuredResult,
    TokenUsage,
)
from ken_core.config import Settings

PROVIDER_NAME = "mock"
MAX_RECORDED_CALLS = 1000
"""Newest calls kept in ``MockModelProvider.calls``; older ones are dropped."""


@dataclass(frozen=True, slots=True)
class MockCall:
    """One recorded provider call, for test assertions."""

    method: str
    role: ModelRole | None = None
    messages: tuple[Message, ...] = ()
    texts: tuple[str, ...] = ()
    schema: str | None = None


def _messages_digest(messages: Sequence[Message]) -> str:
    payload = json.dumps(
        [m.model_dump(mode="json") for m in messages], sort_keys=True, ensure_ascii=False
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


_TOKEN = re.compile(r"\w+")
_FEATURE_HASH_PERSON = b"ken-mock-embed"
"""blake2b personalisation: domain-separates these hashes from any other use."""


def _features(text: str) -> list[str]:
    """Unigrams and adjacent-token bigrams of NFC-normalised, lower-cased ``text``.

    Tokens are ``\\w+`` runs, so a bigram (``"a b"``, joined by one space) can
    never collide with a unigram.
    """
    tokens = _TOKEN.findall(unicodedata.normalize("NFC", text).lower())
    return [*tokens, *(f"{a} {b}" for a, b in itertools.pairwise(tokens))]


def deterministic_vector(text: str, dimensions: int) -> list[float]:
    """A deterministic, LEXICALLY MEANINGFUL, L2-normalised vector (feature hashing).

    ADR-0010 section 2. Each unigram and bigram of ``text`` is hashed with
    blake2b (8 bytes; never Python's per-process salted ``hash()``) to an
    unsigned 64-bit integer ``h``. ``h % dimensions`` picks the bucket and the
    top bit of ``h`` picks a +1/-1 sign (the signed "hashing trick", which keeps
    collisions unbiased). Counts are summed, then the vector is L2-normalised.

    Consequences: the same text yields the bit-identical vector in every
    process and on every machine; texts that share words and phrases have a
    higher cosine similarity than texts that do not; word order matters only
    through bigrams.

    Empty or token-free text (``""``, whitespace, punctuation only) maps to the
    fixed unit basis vector ``e0 = [1.0, 0.0, ..., 0.0]``. A non-empty feature
    list can never sum to the zero vector (2n-1 features is odd), but the same
    fallback guards that case too.
    """
    if dimensions <= 0:
        raise ValueError("dimensions must be positive")
    values = [0.0] * dimensions
    for feature in _features(text):
        digest = hashlib.blake2b(
            feature.encode("utf-8"), digest_size=8, person=_FEATURE_HASH_PERSON
        ).digest()
        h = int.from_bytes(digest, "big")
        values[h % dimensions] += 1.0 if h >> 63 else -1.0
    norm = math.sqrt(math.fsum(v * v for v in values))
    if norm == 0.0:
        return [1.0] + [0.0] * (dimensions - 1)
    return [v / norm for v in values]


class MockModelProvider:
    """Deterministic ModelProvider. The default in tests and CI."""

    name: str = PROVIDER_NAME

    def __init__(self, settings: Settings | None = None, *, dimensions: int | None = None) -> None:
        if dimensions is None:
            dimensions = settings.mock_embedding_dim if settings is not None else 768
        if dimensions <= 0:
            raise ValueError("dimensions must be positive")
        self.dimensions = dimensions
        self.calls: list[MockCall] = []
        self.closed = False
        self._registered: dict[type[BaseModel], str] = {}
        self._scripted_violations: dict[type[BaseModel], list[str]] = {}

    # ------------------------------------------------------------- scripting
    def register(self, schema: type[BaseModel], value: BaseModel | Mapping[str, Any]) -> None:
        """Register the value ``complete_structured(schema=schema)`` returns.

        ``value`` is validated through ``schema`` now, so an invalid
        registration fails in the test that wrote it rather than later.
        """
        if isinstance(value, BaseModel):
            validated = schema.model_validate(value.model_dump(mode="json"))
        else:
            validated = schema.model_validate(dict(value))
        self._registered[schema] = validated.model_dump_json()

    def script_violation(self, schema: type[BaseModel], *, raw_text: str = "not json") -> None:
        """Make the next ``complete_structured`` call for ``schema`` raise."""
        self._scripted_violations.setdefault(schema, []).append(raw_text)

    def reset_calls(self) -> None:
        self.calls.clear()

    def _record(self, call: MockCall) -> None:
        """Append to :attr:`calls`, keeping only the newest ``MAX_RECORDED_CALLS``.

        Bounded because a long-running Mode B process calls ``health`` and
        ``capabilities`` on every ``/ready`` and worker tick (Phase 0 QA L3).
        """
        self.calls.append(call)
        overflow = len(self.calls) - MAX_RECORDED_CALLS
        if overflow > 0:
            del self.calls[:overflow]

    # -------------------------------------------------------------- protocol
    def _ensure_open(self) -> None:
        if self.closed:
            raise ModelProviderError("MockModelProvider is closed")

    @staticmethod
    def _model_for(role: ModelRole) -> str:
        return f"mock-{ModelRole(role).value}"

    async def complete(
        self,
        *,
        role: ModelRole,
        messages: Sequence[Message],
        options: GenerationOptions | None = None,
    ) -> Completion:
        self._ensure_open()
        self._record(MockCall(method="complete", role=role, messages=tuple(messages)))
        digest = _messages_digest(messages)
        return Completion(
            text=f"mock completion {digest[:16]}",
            model=self._model_for(role),
            role=role,
            finish_reason="stop",
            usage=TokenUsage(prompt_tokens=len(messages), completion_tokens=3),
            latency_ms=0.0,
            provider=PROVIDER_NAME,
        )

    async def complete_structured[S: BaseModel](
        self,
        *,
        role: ModelRole,
        messages: Sequence[Message],
        schema: type[S],
        options: GenerationOptions | None = None,
        max_attempts: int = 2,
    ) -> StructuredResult[S]:
        self._ensure_open()
        if max_attempts < 1:
            raise ValueError("max_attempts must be >= 1")
        self._record(
            MockCall(
                method="complete_structured",
                role=role,
                messages=tuple(messages),
                schema=schema.__name__,
            )
        )
        scripted = self._scripted_violations.get(schema)
        if scripted:
            raw = scripted.pop(0)
            raise ModelSchemaViolationError(
                f"Scripted schema violation for {schema.__name__}",
                raw_text=raw,
                attempts=max_attempts,
            )
        raw_json = self._registered.get(schema)
        if raw_json is None:
            raise ModelSchemaViolationError(
                f"No value registered with MockModelProvider for schema {schema.__name__}",
                raw_text="",
                attempts=1,
            )
        # Re-validated on every call: the caller always receives a fresh
        # instance that has genuinely passed the schema.
        value = schema.model_validate_json(raw_json)
        completion = Completion(
            text=raw_json,
            model=self._model_for(role),
            role=role,
            finish_reason="stop",
            latency_ms=0.0,
            provider=PROVIDER_NAME,
        )
        return StructuredResult(value=value, completion=completion, attempts=1, repaired=False)

    async def embed(
        self,
        *,
        texts: Sequence[str],
        role: ModelRole = ModelRole.EMBEDDING,
    ) -> EmbeddingResult:
        self._ensure_open()
        self._record(MockCall(method="embed", role=role, texts=tuple(texts)))
        if ModelRole(role) is not ModelRole.EMBEDDING:
            # Mirrors the real provider: never embed with a completion model.
            raise ModelUnavailableError(f"Role {role.value!r} is not an embedding role")
        if not texts:
            raise ValueError("texts must not be empty")
        return EmbeddingResult(
            vectors=[deterministic_vector(t, self.dimensions) for t in texts],
            model=self._model_for(ModelRole.EMBEDDING),
            dimensions=self.dimensions,
            latency_ms=0.0,
            provider=PROVIDER_NAME,
        )

    async def capabilities(self) -> ProviderCapabilities:
        self._record(MockCall(method="capabilities"))
        models = [self._model_for(r) for r in ModelRole]
        return ProviderCapabilities(
            provider=PROVIDER_NAME,
            supports_tools=False,
            supports_json_schema=True,
            supports_embeddings=True,
            embedding_dimensions=self.dimensions,
            models_available=models,
            roles=[
                ModelRoleStatus(role=r, model=self._model_for(r), status="ok") for r in ModelRole
            ],
        )

    async def health(self) -> ProviderHealth:
        self._record(MockCall(method="health"))
        return ProviderHealth(
            provider=PROVIDER_NAME,
            reachable=not self.closed,
            latency_ms=0.0,
            detail={"deterministic": True},
            error="provider closed" if self.closed else None,
        )

    async def aclose(self) -> None:
        self.closed = True
