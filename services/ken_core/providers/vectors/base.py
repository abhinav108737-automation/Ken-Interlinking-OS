"""Behaviour every vector store shares (ADR-0017 section 3).

The :class:`~ken_contracts.vectors.VectorStore` protocol is frozen and deliberately has no way
to LIST points: a derived index has no business being read back into PostgreSQL. But the
consistency check (``make qdrant-verify``, ADR-0017 section 4) has to know which points exist
to report orphans, and the incremental indexer has to know which points are already current.
:class:`InspectableVectorStore` is the one additive capability both stores implement for that:
a read-only scan of point REFERENCES (ids and identity fields, never vectors).

Nothing here is a source of truth. A reference is only ever compared with PostgreSQL.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from ken_contracts.vectors import VectorFilters, VectorHit, VectorPoint

__all__ = [
    "InspectableVectorStore",
    "PointRef",
    "point_matches",
    "sort_hits",
]


@dataclass(frozen=True, slots=True)
class PointRef:
    """What a scan reports about one stored point. No vector, no metadata."""

    point_id: uuid.UUID
    page_id: uuid.UUID
    page_version_id: str
    chunk_id: str | None
    content_hash: str
    embedding_model: str
    embedding_version: str


@runtime_checkable
class InspectableVectorStore(Protocol):
    """Optional read-only capability: enumerate stored point references."""

    def scan_points(self, *, batch_size: int = 512) -> AsyncIterator[PointRef]:
        """Every point reference, in ``point_id`` order, for drift reporting."""
        ...


def ref_of(point: VectorPoint) -> PointRef:
    return PointRef(
        point_id=point.point_id,
        page_id=point.page_id,
        page_version_id=point.page_version_id,
        chunk_id=point.chunk_id,
        content_hash=point.content_hash,
        embedding_model=point.embedding_model,
        embedding_version=point.embedding_version,
    )


def point_matches(point: VectorPoint, filters: VectorFilters) -> bool:
    """Whether ``point`` passes every pre-filter. ``include_stale`` is NOT consulted here:
    a store cannot know a page's current version, so freshness is the retrieval layer's job
    (``ken_services.vectors.retrieval``)."""
    if filters.page_ids is not None and point.page_id not in set(filters.page_ids):
        return False
    if point.page_id in set(filters.exclude_page_ids):
        return False
    if filters.embedding_model is not None and point.embedding_model != filters.embedding_model:
        return False
    if (
        filters.embedding_version is not None
        and point.embedding_version != filters.embedding_version
    ):
        return False
    if filters.content_hashes is not None and point.content_hash not in set(filters.content_hashes):
        return False
    if filters.page_types is not None and point.metadata.get("page_type") not in set(
        filters.page_types
    ):
        return False
    return filters.languages is None or point.metadata.get("language") in set(filters.languages)


def sort_hits(hits: list[VectorHit]) -> list[VectorHit]:
    """Best score first, ties broken by ``(page_id, chunk_id)`` -- the protocol's rule, applied
    identically by every implementation so two stores return the same order."""
    return sorted(hits, key=lambda h: (-h.score, str(h.page_id), h.chunk_id or ""))
