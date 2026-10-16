"""Primitives shared by every other contract module.

FROZEN CONTRACT.

Three things are established here, in order of how often they are needed:

1. **Base models.** Every contract model inherits :class:`ContractModel` or
   :class:`FrozenModel`, both of which set ``extra="forbid"``. Inheriting
   rather than repeating ``model_config`` on each model means a model added in
   a later phase cannot silently drop unknown keys, which would be a hole in
   TRD.md section 6's typed boundary.
2. **Identifier conventions.** MULTI_AGENT_ORCHESTRATION.md freezes "database
   identifiers" as a shared contract. The aliases below record which ids are
   internally generated UUIDs and which are opaque strings owned by an
   external system.
3. **Lifecycle.** :class:`AsyncClosable` lets provider protocols be released
   without a caller having to downcast to the concrete implementation.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Protocol, runtime_checkable
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

# --------------------------------------------------------------------------- #
# Identifier conventions
#
# Opaque external identifiers are strings because they are owned by the CMS or
# another upstream system: we must round-trip them exactly and must never
# parse, compare or generate them.
#
# Internally generated identifiers are UUIDs because we mint them ourselves
# and need global uniqueness without coordinating with any external system.
# --------------------------------------------------------------------------- #
type PageId = str
"""Opaque page identifier owned by the CMS."""

type DeploymentId = str
"""Opaque deployment identifier returned by the CMS."""

type LinkId = str
"""Identifier of a deployed internal link, used to join analytics."""

type OperationId = UUID
"""Idempotency key we generate for a mutation. Replay must be detectable."""

type CorrelationId = UUID
"""Ties related events and log lines to one causing request or agent run."""


STORABLE_QUERY_PATTERN = r"^[^\x00-\x1f\x7f]*$"
"""For free-text query parameters: no NUL or other control characters (a 422, not a 500).
A lone surrogate cannot arrive through a URL: percent-decoding invalid UTF-8 never yields
one."""


def reject_unstorable_text(value: str) -> str:
    """PostgreSQL text rejects NUL, and a lone UTF-16 surrogate cannot be UTF-8 encoded: both
    would surface as a 500 deep in the stack. Reject them -- and other C0 controls except tab,
    newline and carriage return -- at the boundary (a 422), Phase 5 QA L1."""
    for char in value:
        code = ord(char)
        if (code < 0x20 and char not in "\t\n\r") or code == 0x7F or 0xD800 <= code <= 0xDFFF:
            raise ValueError("text contains a character that cannot be stored")
    return value


class ContractModel(BaseModel):
    """Base for mutable contract models.

    ``extra="forbid"`` is deliberate: an unexpected key is a contract
    violation that should fail loudly at the boundary rather than be silently
    dropped (TRD.md section 6).
    """

    # json_schema_serialization_defaults_required: in the RESPONSE (serialization)
    # schema, a field with a default is always present, so it is marked required.
    # Without it the generated TypeScript made every defaulted field optional
    # (``detail?:``), and clients had to guard against keys the API always sends.
    model_config = ConfigDict(extra="forbid", json_schema_serialization_defaults_required=True)


class FrozenModel(ContractModel):
    """Base for contract models that must not be mutated after construction."""

    model_config = ConfigDict(
        frozen=True, extra="forbid", json_schema_serialization_defaults_required=True
    )


@runtime_checkable
class AsyncClosable(Protocol):
    """Releases resources held by a provider.

    Present on every provider protocol so that a caller holding only the
    protocol can release an underlying HTTP client or connection pool. Without
    it, later phases must either leak connections or downcast to the concrete
    provider class, which defeats the abstraction.

    Implementations must be idempotent: closing twice is not an error.
    """

    async def aclose(self) -> None: ...


class HealthStatus(StrEnum):
    """Status of a single dependency check.

    Distinct from :class:`~ken_contracts.health.ReadinessStatus`: a dependency
    is *described* by this enum, whereas the service as a whole is *judged* by
    an aggregate verdict.
    """

    OK = "ok"
    DEGRADED = "degraded"
    UNAVAILABLE = "unavailable"
    ERROR = "error"
    SKIPPED = "skipped"


class CheckKind(StrEnum):
    """Category of a readiness check, so clients can group them in a UI."""

    DATABASE = "database"
    SCHEMA = "schema"
    EXTENSION = "extension"
    """A PostgreSQL extension. No check uses it since ADR-0017 removed pgvector; the value
    stays so a stored or client-side kind from an older build still parses."""
    MODEL_SERVER = "model_server"
    CMS = "cms"
    VECTOR_STORE = "vector_store"
    """ADR-0017: the derived semantic index (Qdrant, or the in-memory store in tests)."""


class ServiceIdentity(FrozenModel):
    """Who is answering. Present in every health, readiness and meta payload."""

    service: str = Field(description="Logical service name, e.g. 'ken-api'.")
    version: str = Field(description="Application version from the distribution metadata.")
    environment: str = Field(description="Deployment environment, e.g. 'local'.")
    git_sha: str | None = Field(
        default=None, description="Short commit SHA when discoverable, else null."
    )
