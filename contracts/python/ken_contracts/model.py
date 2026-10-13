"""ModelProvider protocol and its typed payloads.

FROZEN CONTRACT.

Architecture rules this module enforces (CLAUDE.md "Model Design Rule",
TRD.md sections 6, 7 and 24):

1. Callers select a model by ROLE (fast / reasoning / embedding), never by
   model name. Role-to-name resolution lives inside the provider, driven by
   MODEL_FAST / MODEL_REASONING / MODEL_EMBEDDING settings. No business logic
   may hardcode a model.
2. ``complete_structured`` is first-class, which makes TRD.md section 6's typed
   boundary the DEFAULT call shape rather than something each agent
   reimplements. Raw text crossing into application logic is the exception,
   not the rule.
3. No streaming in the base protocol. A ``StreamingModelProvider`` can be
   added later as a separate protocol without touching this one.
4. Agents never hold a provider's transport credentials; they receive narrow
   tools that call providers on their behalf.
"""

from __future__ import annotations

from collections.abc import Sequence
from enum import StrEnum
from typing import Any, Literal, Protocol, runtime_checkable

from pydantic import BaseModel, Field

from ken_contracts.common import AsyncClosable, ContractModel


class ModelRole(StrEnum):
    """The configured role a call needs, resolved to a model by the provider."""

    FAST = "fast"
    REASONING = "reasoning"
    EMBEDDING = "embedding"


class Message(ContractModel):
    """One chat message."""

    role: Literal["system", "user", "assistant", "tool"]
    content: str


class GenerationOptions(ContractModel):
    """Per-call generation controls.

    ``temperature`` defaults to 0.0 because this system prefers reproducible
    decisions; ``think`` defaults to False because the locally installed
    qwen3 model advertises a "thinking" capability whose ``<think>`` output
    would otherwise corrupt structured parsing.
    """

    temperature: float = Field(default=0.0, ge=0.0, le=2.0)
    top_p: float | None = Field(default=None, gt=0.0, le=1.0)
    max_tokens: int | None = Field(default=None, gt=0)
    seed: int | None = None
    stop: list[str] = Field(default_factory=list)
    timeout_s: float = Field(default=60.0, gt=0.0)
    think: bool = Field(
        default=False,
        description="Request model-side reasoning tokens. Keep false for structured calls.",
    )


class TokenUsage(ContractModel):
    prompt_tokens: int | None = None
    completion_tokens: int | None = None


class Completion(ContractModel):
    """Raw text result. Crossing into application logic requires validation."""

    text: str
    model: str = Field(description="The concrete model that served the call.")
    role: ModelRole
    finish_reason: str | None = None
    usage: TokenUsage | None = None
    latency_ms: float = Field(ge=0.0)
    provider: str


class StructuredResult[T: BaseModel](ContractModel):
    """A model output that has already passed its Pydantic boundary.

    ``attempts`` and ``repaired`` are recorded because a provider that needed
    several tries to produce valid output is a quality signal the evaluation
    subsystem will want in later phases.
    """

    value: T
    completion: Completion
    attempts: int = Field(ge=1)
    repaired: bool = Field(
        default=False, description="True when the raw output needed cleanup before validating."
    )


class EmbeddingResult(ContractModel):
    vectors: list[list[float]]
    model: str
    dimensions: int = Field(gt=0)
    latency_ms: float = Field(ge=0.0)
    provider: str


class ModelRoleStatus(ContractModel):
    """Whether one configured role is actually satisfiable right now."""

    role: ModelRole
    model: str | None
    status: Literal["ok", "missing", "unknown"]


class ProviderCapabilities(ContractModel):
    """What a provider can do, discovered at runtime rather than assumed.

    ``embedding_dimensions`` exists so that the Phase 2 migration which creates
    the first pgvector column reads the dimension from here instead of
    hardcoding a number. Note that a completion model's hidden size is NOT an
    embedding dimension.
    """

    provider: str
    supports_tools: bool
    supports_json_schema: bool
    supports_embeddings: bool
    embedding_dimensions: int | None = None
    models_available: list[str] = Field(default_factory=list)
    roles: list[ModelRoleStatus] = Field(default_factory=list)


class ProviderHealth(ContractModel):
    """Reachability of a provider's backing service. Shared with CMSProvider."""

    provider: str
    reachable: bool
    latency_ms: float | None = None
    detail: dict[str, Any] = Field(default_factory=dict)
    error: str | None = None


# --------------------------------------------------------------------------- #
# Errors
# --------------------------------------------------------------------------- #
class ModelProviderError(Exception):
    """Base class for every model-provider failure."""


class ModelUnavailableError(ModelProviderError):
    """The model server could not be reached, or the role has no model."""


class ModelTimeoutError(ModelProviderError):
    """The model server did not answer within the configured timeout."""


class ModelSchemaViolationError(ModelProviderError):
    """Output could not be validated against the requested schema.

    Carries the raw text so the failure is diagnosable and can become an
    evaluation fixture, rather than being discarded.
    """

    def __init__(self, message: str, *, raw_text: str, attempts: int) -> None:
        super().__init__(message)
        self.raw_text = raw_text
        self.attempts = attempts


# --------------------------------------------------------------------------- #
# Protocol
# --------------------------------------------------------------------------- #
@runtime_checkable
class ModelProvider(AsyncClosable, Protocol):
    """The only sanctioned route to inference.

    Business logic depends on this protocol, never on Ollama or any other
    concrete server (TRD.md section 7: "Agents cannot call Ollama directly").
    """

    name: str

    async def complete(
        self,
        *,
        role: ModelRole,
        messages: Sequence[Message],
        options: GenerationOptions | None = None,
    ) -> Completion:
        """Generate free-form text. The caller is responsible for validating it."""
        ...

    async def complete_structured[S: BaseModel](
        self,
        *,
        role: ModelRole,
        messages: Sequence[Message],
        schema: type[S],
        options: GenerationOptions | None = None,
        max_attempts: int = 2,
    ) -> StructuredResult[S]:
        """Generate output already validated against ``schema``.

        Raises:
            ModelSchemaViolationError: output never validated within ``max_attempts``.
        """
        ...

    async def embed(
        self,
        *,
        texts: Sequence[str],
        role: ModelRole = ModelRole.EMBEDDING,
    ) -> EmbeddingResult:
        """Embed one or more texts."""
        ...

    async def capabilities(self) -> ProviderCapabilities:
        """Report what this provider can currently do."""
        ...

    async def health(self) -> ProviderHealth:
        """Report reachability without raising."""
        ...
