"""Vector indexing service interfaces (ADR-0017 sections 1, 4 and 5).

FROZEN (orchestrator). The vector workstream implements these; it does not change a
signature. The STORE itself is a frozen contract elsewhere
(:class:`ken_contracts.vectors.VectorStore`, built by
``ken_core.providers.vectors.factory.build_vector_store``); what lives here is the service
layer AROUND it -- the jobs that keep a derived index in step with PostgreSQL.

Rules (binding):

* **PostgreSQL is the source of truth; the index is derived.** Every method here reads
  ``page_embeddings`` (and ``pages`` / ``page_versions`` for freshness) and writes only to the
  store. Nothing is ever read from the store and then written back to PostgreSQL. A test must
  prove the whole collection can be dropped and rebuilt (:meth:`VectorRebuilder.rebuild`).
* **Indexing is idempotent**, because ``point_id`` is deterministic
  (:func:`ken_contracts.vectors.point_id`): running ``make qdrant-index`` twice changes
  nothing. Every implementation MUST use that helper rather than minting an id, or re-indexing
  silently duplicates.
* **Staleness is decided in PostgreSQL, never in the store.** A point is stale when its
  ``page_version_id`` is not the page's ``current_version`` or its ``content_hash`` differs
  from the stored embedding's. :meth:`VectorIndexer.index` calls
  ``VectorStore.delete_stale`` for each page it touches, so drift stays bounded; whatever
  remains is reported by :meth:`VectorVerifier.verify`, in BOTH directions.
* **A stale hit may never place a link.** Retrieval marks it
  (:attr:`ken_contracts.vectors.VectorHit.stale`) and the autonomy engine blocks it
  deterministically (:attr:`ken_contracts.autonomy.BlockCode.STALE_VECTOR`). That is a
  deterministic filter, not a ranking penalty.
* **One identity per collection.** Vectors of two embedding models, or two text recipes, are
  never comparable; an index run writes ONE (model, embedding_version) identity, and
  :meth:`VectorVerifier.verify` reports a collection built with another one as drift rather
  than quietly searching it.
* **No LLM, no clock-dependence, no randomness** in any of this: an index run over an
  unchanged corpus produces a byte-identical collection.
* **Every run is a ``job_runs`` row** of kind :attr:`~ken_contracts.jobs.JobKind.VECTOR_INDEX`
  and emits ``job.completed``, with a per-kind advisory lock (docs/CONTRACTS.md rule 14).
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Protocol

from ken_contracts.vectors import (
    CollectionInfo,
    RebuildReport,
    UpsertResult,
    VectorHit,
    VectorPoint,
    VectorQuery,
)

__all__ = [
    "DriftReport",
    "IndexReport",
    "SemanticRetriever",
    "VectorIndexer",
    "VectorRebuilder",
    "VectorVerifier",
]


@dataclass(frozen=True, slots=True)
class IndexReport:
    """What one indexing pass did. Counters only -- no page content, no vectors."""

    collection: str
    scanned: int
    """Current-version embeddings of the active identity considered."""
    upserted: int
    updated: int
    skipped: int
    stale_deleted: int
    """Superseded points removed for the pages this pass touched."""
    pages_deleted: int
    """Points removed because the page is gone from the inventory."""
    vector_size: int
    embedding_model: str | None = None
    embedding_version: str | None = None
    issues: tuple[str, ...] = field(default=())
    """Stable codes, never exception text (docs/CONTRACTS.md rule 10)."""

    @property
    def ok(self) -> bool:
        return not self.issues


@dataclass(frozen=True, slots=True)
class DriftReport:
    """Consistency between PostgreSQL and the collection, in BOTH directions (ADR-0017 s4).

    One direction alone is a false sense of safety: ``missing_vectors`` means retrieval
    silently cannot see those pages, while ``orphaned_points`` means it can see pages that no
    longer exist or versions that have been superseded. ``make qdrant-verify`` exits non-zero
    when either is non-empty.
    """

    collection: str
    collection_info: CollectionInfo
    missing_vectors: tuple[uuid.UUID, ...]
    """Pages whose current version has an embedding but no point."""
    orphaned_points: tuple[uuid.UUID, ...]
    """Point ids whose page is gone, or whose version is superseded."""
    stale_points: tuple[uuid.UUID, ...]
    """Point ids whose ``content_hash`` no longer matches the stored embedding."""
    identity_mismatch: bool
    """The collection was built with another (model, embedding_version): a rebuild is due."""
    dimension_mismatch: bool

    @property
    def in_sync(self) -> bool:
        return not (
            self.missing_vectors
            or self.orphaned_points
            or self.stale_points
            or self.identity_mismatch
            or self.dimension_mismatch
        )


class VectorIndexer(Protocol):
    """``make qdrant-index``: incremental, idempotent indexing from PostgreSQL."""

    async def index(
        self, *, page_ids: Sequence[uuid.UUID] | None = None, force: bool = False
    ) -> IndexReport:
        """Index the current-version embeddings of ``page_ids`` (or every page).

        ``force`` re-sends points whose content hash already matches, which changes nothing
        but the ``updated`` count: it is for repairing a collection, never for correctness.
        """
        ...

    async def points_for(self, *, page_ids: Sequence[uuid.UUID]) -> list[VectorPoint]:
        """Build the points these pages' CURRENT versions should have. Pure read; the one
        place that maps a ``page_embeddings`` row to a
        :class:`~ken_contracts.vectors.VectorPoint`, so the indexer, the rebuilder and the
        verifier cannot disagree about what a point should be."""
        ...

    async def upsert_points(self, *, points: Sequence[VectorPoint]) -> UpsertResult: ...


class VectorRebuilder(Protocol):
    """``make qdrant-rebuild``: drop the collection and refill it from PostgreSQL.

    Its existence is the proof that the index is derived. A test drops and rebuilds, then
    asserts retrieval returns the same hits.
    """

    async def rebuild(self, *, batch_size: int = 256) -> RebuildReport: ...


class VectorVerifier(Protocol):
    """``make qdrant-verify``: report drift, change nothing."""

    async def verify(self) -> DriftReport: ...


class SemanticRetriever(Protocol):
    """The SEMANTIC channel's single entry point (ADR-0017 consequences).

    It owns the step a store cannot do: re-checking every hit's freshness against
    ``pages.current_version`` and the stored ``content_hash``, dropping stale hits by default
    and marking any that a caller deliberately asked to keep.
    """

    async def search(self, *, query: VectorQuery) -> list[VectorHit]: ...

    async def similar_to_page(
        self, *, page_id: uuid.UUID, limit: int, score_threshold: float | None = None
    ) -> list[VectorHit]:
        """Neighbours of a page's CURRENT version, excluding the page itself."""
        ...
