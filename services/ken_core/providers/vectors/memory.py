"""In-process vector store: exact cosine over a dict (ADR-0017 section 3).

This is the TEST/CI store (``KEN_VECTOR_STORE=memory``, selected explicitly by the test
factories; the code default is ``qdrant``): the whole test suite and CI run with no container,
and nothing about retrieval quality changes -- an exact cosine scan over the ~65-page demo
corpus is not an approximation of a nearest-neighbour index, it is the exact answer the index
approximates.

It is a store, never a source of truth: every point is rebuilt from ``page_embeddings``. The
contents live for the lifetime of the process, which is also why
:meth:`MemoryVectorStore.rebuild_from` is the normal way to fill it.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import AsyncIterator, Sequence
from datetime import UTC, datetime
from typing import Final

from ken_contracts.model import ProviderHealth
from ken_contracts.vectors import (
    MAX_BATCH_SIZE,
    CollectionInfo,
    CollectionStatus,
    RebuildReport,
    UpsertResult,
    VectorDistance,
    VectorHit,
    VectorPoint,
    VectorQuery,
    VectorStoreError,
    cosine_similarity,
)
from ken_core.providers.vectors.base import PointRef, point_matches, ref_of, sort_hits

__all__ = ["NAME", "MemoryVectorStore"]

NAME: Final = "memory"


class MemoryVectorStore:
    """Exact cosine over a dict keyed by ``point_id`` (so upserts are idempotent).

    Structurally a :class:`~ken_contracts.vectors.VectorStore`, like every other provider in
    this package: a test asserts ``isinstance(store, VectorStore)`` rather than inheriting
    from the protocol.
    """

    def __init__(self, *, collection: str = "ken_pages") -> None:
        self._collection = collection
        self._points: dict[uuid.UUID, VectorPoint] = {}
        self._vector_size: int | None = None
        self._distance = VectorDistance.COSINE
        self._exists = False

    @property
    def name(self) -> str:
        return NAME

    # ------------------------------------------------------------- collection
    async def ensure_collection(
        self, *, vector_size: int, distance: VectorDistance = VectorDistance.COSINE
    ) -> CollectionInfo:
        if vector_size <= 0:
            raise VectorStoreError("vector_size must be positive")
        if self._exists and self._vector_size not in (None, vector_size):
            # Never silently recreate: an operator must rebuild knowingly (ADR-0017 s3).
            raise VectorStoreError(
                "the collection exists with a different vector size; rebuild it explicitly"
            )
        if self._exists and distance is not self._distance:
            raise VectorStoreError(
                "the collection exists with a different distance metric; rebuild it explicitly"
            )
        self._exists = True
        self._vector_size = vector_size
        self._distance = distance
        return await self.collection_info()

    async def collection_info(self) -> CollectionInfo:
        if not self._exists:
            return CollectionInfo(
                name=self._collection,
                vector_size=0,
                distance=self._distance,
                points=0,
                status=CollectionStatus.MISSING,
            )
        models = {point.embedding_model for point in self._points.values()}
        versions = {point.embedding_version for point in self._points.values()}
        return CollectionInfo(
            name=self._collection,
            vector_size=self._vector_size or 0,
            distance=self._distance,
            points=len(self._points),
            status=CollectionStatus.GREEN if self._points else CollectionStatus.GREY,
            embedding_model=next(iter(models)) if len(models) == 1 else None,
            embedding_version=next(iter(versions)) if len(versions) == 1 else None,
        )

    # ------------------------------------------------------------------ write
    async def upsert(self, *, points: Sequence[VectorPoint]) -> UpsertResult:
        if len(points) > MAX_BATCH_SIZE:
            raise VectorStoreError("too many points in one upsert")
        if not self._exists and points:
            await self.ensure_collection(vector_size=len(points[0].vector))
        upserted = 0
        updated = 0
        skipped = 0
        for point in points:
            if not any(component != 0.0 for component in point.vector):
                # A zero vector has no direction under cosine; the Qdrant store skips it too.
                skipped += 1
                continue
            if self._vector_size is not None and len(point.vector) != self._vector_size:
                # A vector of another dimension is never comparable: drop it, loudly in the
                # result rather than silently in the data.
                skipped += 1
                continue
            if point.point_id in self._points:
                updated += 1
            self._points[point.point_id] = point
            upserted += 1
        return UpsertResult(upserted=upserted, updated=updated, skipped=skipped)

    async def delete_pages(self, *, page_ids: Sequence[uuid.UUID]) -> int:
        wanted = set(page_ids)
        doomed = [pid for pid, point in self._points.items() if point.page_id in wanted]
        for point_id in doomed:
            del self._points[point_id]
        return len(doomed)

    async def delete_stale(self, *, page_id: uuid.UUID, keep_version_id: str) -> int:
        doomed = [
            pid
            for pid, point in self._points.items()
            if point.page_id == page_id and point.page_version_id != keep_version_id
        ]
        for point_id in doomed:
            del self._points[point_id]
        return len(doomed)

    async def rebuild_from(
        self, *, points: Sequence[VectorPoint], batch_size: int = 256
    ) -> RebuildReport:
        if batch_size < 1:
            raise VectorStoreError("batch_size must be positive")
        started = time.monotonic()
        dropped = self._exists
        self._points.clear()
        self._exists = False
        self._vector_size = None
        size = len(points[0].vector) if points else 0
        if size:
            await self.ensure_collection(vector_size=size)
        batches = 0
        written = 0
        for start in range(0, len(points), batch_size):
            result = await self.upsert(points=points[start : start + batch_size])
            written += result.upserted
            batches += 1
        return RebuildReport(
            collection=self._collection,
            dropped=dropped,
            points_written=written,
            batches=batches,
            vector_size=size,
            duration_ms=(time.monotonic() - started) * 1000.0,
            completed_at=datetime.now(UTC),
        )

    # ------------------------------------------------------------------- read
    async def search(self, *, query: VectorQuery) -> list[VectorHit]:
        scored: list[tuple[float, VectorPoint]] = []
        for point in self._points.values():
            if not point_matches(point, query.filters):
                continue
            score = cosine_similarity(query.vector, point.vector)
            if score is None:
                # A zero vector has no direction, and a dimension mismatch is not a
                # comparison: no answer is not the same as a score of zero.
                continue
            if query.score_threshold is not None and score < query.score_threshold:
                continue
            scored.append((score, point))
        hits = [
            VectorHit(
                page_id=point.page_id,
                page_version_id=point.page_version_id,
                chunk_id=point.chunk_id,
                score=score,
                content_hash=point.content_hash,
                # A derived store cannot know the page's current version: the retrieval
                # layer re-checks freshness against PostgreSQL (ADR-0017 s4).
                stale=False,
            )
            for score, point in scored
        ]
        return sort_hits(hits)[: query.limit]

    async def scan_points(self, *, batch_size: int = 512) -> AsyncIterator[PointRef]:
        """Read-only enumeration for drift reporting (``InspectableVectorStore``)."""
        for point_id in sorted(self._points, key=str):
            yield ref_of(self._points[point_id])

    async def health(self) -> ProviderHealth:
        info = await self.collection_info()
        return ProviderHealth(
            provider=NAME,
            reachable=True,
            latency_ms=0.0,
            detail={
                "collection": info.name,
                "points": info.points,
                "vector_size": info.vector_size,
                "status": info.status.value,
                "embedding_model": info.embedding_model,
                "embedding_version": info.embedding_version,
            },
        )

    async def aclose(self) -> None:
        """Idempotent, as :class:`~ken_contracts.common.AsyncClosable` requires."""
        self._points.clear()
