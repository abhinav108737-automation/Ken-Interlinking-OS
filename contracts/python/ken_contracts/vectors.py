"""Vector-store contracts (ADR-0017).

FROZEN CONTRACT. Implements docs/adr/ADR-0017-qdrant-vector-architecture.md section 3, which
**supersedes ADR-0003 / ADR-0010's choice of pgvector and overrides CLAUDE.md #5**.

The responsibility split is the whole point of this module:

* **PostgreSQL is the source of truth for everything**, including the embedding *values*
  (``page_embeddings``: the vector, the model, the recipe version, the content hash).
* **The vector store is a derived, rebuildable index.** It holds points only -- the vector
  plus the identifiers and retrieval metadata below. Nothing is stored ONLY here, so losing
  the store costs a reindex and never data. :meth:`VectorStore.rebuild_from` exists so that a
  test can drop the collection and refill it from PostgreSQL.

Two implementations are expected, chosen by ``build_vector_store(settings)`` from
``KEN_VECTOR_STORE`` exactly as the CMS and analytics factories work:
``QdrantVectorStore`` (HTTP, via ``qdrant-client``) and ``MemoryVectorStore`` (exact cosine
over a dict -- the default, so the test suite and CI need no container).

Protocol rules, as docs/CONTRACTS.md rule 2 requires of every provider: async, keyword-only,
Pydantic returns, ``aclose()``, typed exceptions.

**Freshness is explicit** (ADR-0017 section 4). A hit is STALE when its ``page_version_id`` is
not the page's current version, or its ``content_hash`` differs from the stored one. Retrieval
filters stale hits out by default; one that survives a filter is marked ``stale=True`` and may
never be used to place a link -- that is a deterministic block
(:attr:`~ken_contracts.autonomy.BlockCode.STALE_VECTOR`), not a judgement call.

**Who decides staleness.** A store holds DERIVED points and therefore cannot know a page's
current version: that fact lives in PostgreSQL. So a :class:`VectorStore` returns
``stale=False`` and the RETRIEVAL layer re-checks every hit against
``pages.current_version`` and ``page_embeddings.content_hash``, dropping stale hits unless
:attr:`VectorFilters.include_stale` asked for them and marking the survivors. The indexer
keeps drift small by calling :meth:`VectorStore.delete_stale` after each page is re-embedded;
``make qdrant-verify`` reports whatever drift remains, in both directions (orphaned points,
and current versions with no point).
"""

from __future__ import annotations

import math
import uuid
from collections.abc import Sequence
from enum import StrEnum
from typing import Any, Final, Protocol, runtime_checkable

from pydantic import AwareDatetime, Field, field_validator, model_validator

from ken_contracts.common import AsyncClosable, FrozenModel
from ken_contracts.model import ProviderHealth

__all__ = [
    "POINT_ID_NAMESPACE",
    "CollectionInfo",
    "CollectionStatus",
    "RebuildReport",
    "UpsertResult",
    "VectorDistance",
    "VectorFilters",
    "VectorHit",
    "VectorPoint",
    "VectorQuery",
    "VectorStore",
    "VectorStoreError",
    "VectorStoreNotConfiguredError",
    "VectorStoreUnavailableError",
    "cosine_similarity",
    "point_id",
]

MAX_VECTOR_DIMENSIONS: Final = 8192
"""A sanity bound only. The real dimension is whatever the EMBEDDING model reports at runtime
(``EmbeddingResult.dimensions``); nothing in this repository hard-codes 768 (ADR-0010)."""

MAX_SEARCH_LIMIT: Final = 500
MAX_BATCH_SIZE: Final = 1000

POINT_ID_NAMESPACE: Final = uuid.NAMESPACE_URL
"""The uuid5 namespace of :func:`point_id`. Frozen: changing it would orphan every point."""


# --------------------------------------------------------------------------- #
# Deterministic helpers
#
# Pure functions, implemented HERE rather than in one store, because every
# implementation -- Qdrant, memory, the indexer, the verifier and the drift
# report -- must compute them IDENTICALLY or re-indexing silently duplicates.
# --------------------------------------------------------------------------- #
def point_id(
    *,
    page_version_id: str,
    chunk_id: str | None,
    embedding_model: str,
    embedding_version: str,
) -> uuid.UUID:
    """The deterministic point id (ADR-0017 section 3).

    ``uuid5(NAMESPACE_URL, f"{page_version_id}:{chunk_id or '-'}:{model}:{version}")``.

    Re-indexing the same content therefore OVERWRITES its point instead of adding a second
    one, which is what makes :meth:`VectorStore.upsert` idempotent. The identity deliberately
    includes the embedding model and version: two models' vectors are never comparable, so
    they must never collide on one id.
    """
    key = f"{page_version_id}:{chunk_id or '-'}:{embedding_model}:{embedding_version}"
    return uuid.uuid5(POINT_ID_NAMESPACE, key)


def cosine_similarity(left: Sequence[float], right: Sequence[float]) -> float | None:
    """Exact cosine similarity in [-1, 1], or ``None`` when it is undefined.

    ``None`` means "no answer", not "zero": a zero vector has no direction, and a non-finite
    component makes the comparison meaningless. Callers must DROP such a pair rather than
    score it 0.0, which would read as "unrelated" instead of "unknown". Mismatched lengths are
    likewise ``None``: vectors of two models or two text recipes are never compared.
    """
    if len(left) != len(right) or not left:
        return None
    dot = 0.0
    left_norm = 0.0
    right_norm = 0.0
    for a, b in zip(left, right, strict=True):
        if not (math.isfinite(a) and math.isfinite(b)):
            return None
        dot += a * b
        left_norm += a * a
        right_norm += b * b
    if left_norm <= 0.0 or right_norm <= 0.0:
        return None
    score = dot / (math.sqrt(left_norm) * math.sqrt(right_norm))
    if not math.isfinite(score):
        return None
    return max(-1.0, min(1.0, score))


# --------------------------------------------------------------------------- #
# Vocabulary
# --------------------------------------------------------------------------- #
class VectorDistance(StrEnum):
    """The only metric this system uses is cosine; the enum exists so the collection can
    DECLARE it and a probe can detect a collection built with another one."""

    COSINE = "cosine"


class CollectionStatus(StrEnum):
    GREEN = "green"
    """Ready for queries."""
    YELLOW = "yellow"
    """Optimising or indexing; queries still answer."""
    GREY = "grey"
    """Reachable but not usable yet (e.g. just created, no points)."""
    RED = "red"
    """Broken; a rebuild is required."""
    MISSING = "missing"
    """The collection does not exist. Not an error: it is the pre-``ensure_collection`` state."""


# --------------------------------------------------------------------------- #
# Points and queries
# --------------------------------------------------------------------------- #
class VectorPoint(FrozenModel):
    """One derived point. Every field is a copy of something PostgreSQL owns."""

    point_id: uuid.UUID = Field(description="Exactly :func:`point_id` of the four fields below.")
    page_id: uuid.UUID
    page_version_id: str = Field(
        min_length=1,
        max_length=128,
        description=(
            "The page VERSION this vector describes, as text (``<page_id>:<version>`` in this "
            "system). Opaque to the store: round-trip it, never parse it."
        ),
    )
    chunk_id: str | None = Field(
        default=None,
        max_length=128,
        description="None for a whole-page vector; a stable chunk key once chunking exists.",
    )
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    embedding_model: str = Field(min_length=1, max_length=200)
    embedding_version: str = Field(
        min_length=1,
        max_length=64,
        description="The text-recipe identity (recipe version + fingerprint), not a model tag.",
    )
    vector: tuple[float, ...] = Field(min_length=1, max_length=MAX_VECTOR_DIMENSIONS)
    metadata: dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "Retrieval metadata only -- page type, language, commercial importance and the "
            "like. Never content, never a secret: the store is a derived index and its "
            "payloads are rebuilt from PostgreSQL."
        ),
    )

    @field_validator("vector")
    @classmethod
    def _finite(cls, value: tuple[float, ...]) -> tuple[float, ...]:
        if any(not math.isfinite(component) for component in value):
            raise ValueError("vector components must be finite")
        return value

    @model_validator(mode="after")
    def _point_id_is_deterministic(self) -> VectorPoint:
        expected = point_id(
            page_version_id=self.page_version_id,
            chunk_id=self.chunk_id,
            embedding_model=self.embedding_model,
            embedding_version=self.embedding_version,
        )
        if self.point_id != expected:
            raise ValueError("point_id must equal point_id(...) of this point's identity")
        return self

    @classmethod
    def build(
        cls,
        *,
        page_id: uuid.UUID,
        page_version_id: str,
        content_hash: str,
        embedding_model: str,
        embedding_version: str,
        vector: Sequence[float],
        chunk_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> VectorPoint:
        """Construct a point with its id computed. The ONLY way any caller should build one."""
        return cls(
            point_id=point_id(
                page_version_id=page_version_id,
                chunk_id=chunk_id,
                embedding_model=embedding_model,
                embedding_version=embedding_version,
            ),
            page_id=page_id,
            page_version_id=page_version_id,
            chunk_id=chunk_id,
            content_hash=content_hash,
            embedding_model=embedding_model,
            embedding_version=embedding_version,
            vector=tuple(float(component) for component in vector),
            metadata=dict(metadata or {}),
        )


class VectorFilters(FrozenModel):
    """Pre-filters applied INSIDE the store, so ``limit`` means "limit of eligible hits".

    Filtering after the fact is not equivalent: a top-k that is then filtered can come back
    empty while eligible neighbours existed just outside k.
    """

    page_ids: tuple[uuid.UUID, ...] | None = Field(
        default=None, description="Restrict to these pages (None = no restriction)."
    )
    exclude_page_ids: tuple[uuid.UUID, ...] = Field(
        default=(), description="Never return these (the source page itself, at minimum)."
    )
    embedding_model: str | None = Field(
        default=None, description="Compare only vectors of this model."
    )
    embedding_version: str | None = Field(
        default=None, description="Compare only vectors of this text-recipe identity."
    )
    content_hashes: tuple[str, ...] | None = None
    page_types: tuple[str, ...] | None = None
    languages: tuple[str, ...] | None = None
    include_stale: bool = Field(
        default=False,
        description=(
            "Default FALSE: a superseded point is excluded. Set true only to INSPECT drift "
            "(``make qdrant-verify``); a stale hit may never place a link (ADR-0017 s4)."
        ),
    )


class VectorQuery(FrozenModel):
    vector: tuple[float, ...] = Field(min_length=1, max_length=MAX_VECTOR_DIMENSIONS)
    limit: int = Field(ge=1, le=MAX_SEARCH_LIMIT)
    score_threshold: float | None = Field(
        default=None,
        ge=-1.0,
        le=1.0,
        description="Minimum cosine similarity; None = no floor. Applied by the store.",
    )
    filters: VectorFilters = Field(default_factory=VectorFilters)

    @field_validator("vector")
    @classmethod
    def _finite(cls, value: tuple[float, ...]) -> tuple[float, ...]:
        if any(not math.isfinite(component) for component in value):
            raise ValueError("query vector components must be finite")
        return value


class VectorHit(FrozenModel):
    """One neighbour. ``score`` is cosine SIMILARITY (higher is nearer), never a distance."""

    page_id: uuid.UUID
    page_version_id: str = Field(min_length=1, max_length=128)
    chunk_id: str | None = Field(default=None, max_length=128)
    score: float = Field(ge=-1.0, le=1.0)
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    stale: bool = Field(
        default=False,
        description=(
            "The point describes a superseded version or a changed content hash. True only "
            "when ``VectorFilters.include_stale`` asked for it, and such a hit may never "
            "place a link."
        ),
    )


class CollectionInfo(FrozenModel):
    """What the collection IS, as the store reports it -- the readiness probe's payload."""

    name: str = Field(min_length=1, max_length=200)
    vector_size: int = Field(ge=0, le=MAX_VECTOR_DIMENSIONS)
    distance: VectorDistance = VectorDistance.COSINE
    points: int = Field(ge=0)
    status: CollectionStatus
    embedding_model: str | None = Field(
        default=None, description="The model the points were built with, if the store knows."
    )
    embedding_version: str | None = None


class UpsertResult(FrozenModel):
    """Idempotent on ``point_id``: re-sending the same points changes nothing but
    ``updated``."""

    upserted: int = Field(ge=0)
    updated: int = Field(ge=0, description="Of ``upserted``, how many replaced an existing id.")
    skipped: int = Field(ge=0, description="Rejected points (wrong dimension, bad identity).")


class RebuildReport(FrozenModel):
    """``rebuild_from``: drop, recreate, fill. Proves the index is derived, not a store."""

    collection: str = Field(min_length=1, max_length=200)
    dropped: bool = Field(description="Whether an existing collection was removed first.")
    points_written: int = Field(ge=0)
    batches: int = Field(ge=0)
    vector_size: int = Field(ge=0, le=MAX_VECTOR_DIMENSIONS)
    duration_ms: float = Field(ge=0.0)
    completed_at: AwareDatetime


# --------------------------------------------------------------------------- #
# Errors
# --------------------------------------------------------------------------- #
class VectorStoreError(Exception):
    """Base class for every vector-store failure. Typed, never a sentinel return."""


class VectorStoreUnavailableError(VectorStoreError):
    """The backing service could not be reached. The message never carries a URL or a key."""


class VectorStoreNotConfiguredError(VectorStoreError):
    """The configured store cannot be built (e.g. ``KEN_QDRANT_URL`` is unset)."""


# --------------------------------------------------------------------------- #
# Protocol
# --------------------------------------------------------------------------- #
@runtime_checkable
class VectorStore(AsyncClosable, Protocol):
    """The ONLY route from business logic to a vector index (ADR-0017 section 3).

    Implementations must be idempotent on ``point_id`` and must never be the source of truth
    for anything: every point is reconstructible from ``page_embeddings``.
    """

    @property
    def name(self) -> str:
        """Stable implementation name for logs and probes, e.g. ``"qdrant"``."""
        ...

    async def ensure_collection(
        self, *, vector_size: int, distance: VectorDistance = VectorDistance.COSINE
    ) -> CollectionInfo:
        """Create the collection if it is absent; return what exists either way.

        Idempotent. A collection whose vector size or metric DIFFERS is a configuration
        error, not something to silently recreate: raise, so an operator rebuilds knowingly.
        """
        ...

    async def upsert(self, *, points: Sequence[VectorPoint]) -> UpsertResult:
        """Insert or replace points by ``point_id``. Idempotent."""
        ...

    async def search(self, *, query: VectorQuery) -> list[VectorHit]:
        """Nearest eligible neighbours, best score first, deterministically tie-broken by
        ``(page_id, chunk_id)``. Filters are applied before the limit."""
        ...

    async def delete_pages(self, *, page_ids: Sequence[uuid.UUID]) -> int:
        """Remove every point of these pages. Returns how many were removed."""
        ...

    async def delete_stale(self, *, page_id: uuid.UUID, keep_version_id: str) -> int:
        """Remove this page's points for every version EXCEPT ``keep_version_id``."""
        ...

    async def collection_info(self) -> CollectionInfo:
        """Current collection facts. ``CollectionStatus.MISSING`` when it does not exist."""
        ...

    async def health(self) -> ProviderHealth:
        """Reachability. Must NOT raise: an unreachable store is an answer, not an error."""
        ...

    async def rebuild_from(
        self, *, points: Sequence[VectorPoint], batch_size: int = 256
    ) -> RebuildReport:
        """Drop the collection, recreate it and fill it from ``points`` (which come from
        PostgreSQL). The vector size is taken from the points; an empty sequence is allowed
        and yields an empty collection."""
        ...
