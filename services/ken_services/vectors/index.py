"""Idempotent incremental indexing and full rebuild from PostgreSQL (ADR-0017 sections 1, 4, 5).

``page_embeddings`` is the source of truth and is immutable; the collection is DERIVED. Nothing
is read from the store and written back to PostgreSQL. No LLM, no clock, no randomness: an index
run over an unchanged corpus produces a byte-identical collection and a second run upserts
nothing (point ids are deterministic, so even a forced re-send only bumps ``updated``).

One run:

1. resolve the single embedding identity (identity.py);
2. read the current-version embeddings of that identity for live pages;
3. ``ensure_collection`` with the dimension those vectors have (never an assumed constant);
4. scan the points already stored and send only those missing or whose content hash changed
   (``force`` sends all);
5. for every touched page call ``delete_stale`` so superseded versions do not linger, and drop the
   points of pages that are gone or have no current embedding of this identity.

Every pass is a ``job_runs`` row of kind ``vector_index`` with a per-kind advisory lock.
"""

from __future__ import annotations

import uuid
from collections import Counter
from collections.abc import Callable, Sequence
from datetime import datetime
from typing import Final

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ken_contracts.inventory import RunStatus
from ken_contracts.jobs import JobKind, JobRunDetail
from ken_contracts.vectors import (
    RebuildReport,
    UpsertResult,
    VectorPoint,
    VectorStore,
)
from ken_core.config import Settings
from ken_core.db.repositories import intelligence as repo
from ken_core.logging import get_logger
from ken_core.providers.vectors.base import InspectableVectorStore, PointRef
from ken_services.jobs.runner import JobContext, run_job, utc_now
from ken_services.vectors.identity import (
    VectorIdentity,
    point_from_embedding,
    resolve_identity,
)
from ken_services.vectors.protocol import IndexReport

__all__ = [
    "INDEX_COUNTERS",
    "PostgresVectorIndexer",
    "PostgresVectorRebuilder",
    "run_index_job",
    "run_rebuild_job",
]

logger = get_logger("ken_services.vectors.index")

INDEX_COUNTERS: Final = (
    "scanned",
    "upserted",
    "updated",
    "skipped",
    "stale_deleted",
    "pages_deleted",
)
ISSUE_NO_EMBEDDINGS: Final = "no_embeddings"
ISSUE_STORE_NOT_INSPECTABLE: Final = "store_not_inspectable"
ISSUE_DIMENSION_MISMATCH: Final = "dimension_mismatch"
DEFAULT_BATCH: Final = 256


class PostgresVectorIndexer:
    """:class:`~ken_services.vectors.protocol.VectorIndexer` over PostgreSQL + a store."""

    def __init__(
        self,
        *,
        sessionmaker: async_sessionmaker[AsyncSession],
        store: VectorStore,
        settings: Settings,
        identity: VectorIdentity | None = None,
        batch_size: int = DEFAULT_BATCH,
    ) -> None:
        self._sessionmaker = sessionmaker
        self._store = store
        self._settings = settings
        self._identity = identity
        self._batch = batch_size

    async def _identity_for(self, session: AsyncSession) -> VectorIdentity | None:
        if self._identity is not None:
            return self._identity
        return await resolve_identity(session, self._settings)

    # ---------------------------------------------------------------- points
    async def points_for(self, *, page_ids: Sequence[uuid.UUID]) -> list[VectorPoint]:
        async with self._sessionmaker() as session:
            identity = await self._identity_for(session)
            if identity is None:
                return []
            rows = await repo.current_embeddings(
                session,
                model=identity.model,
                recipe_version=identity.recipe_version,
                recipe_fingerprint=identity.recipe_fingerprint,
                page_ids=page_ids,
            )
        return [point_from_embedding(row) for row in rows]

    async def upsert_points(self, *, points: Sequence[VectorPoint]) -> UpsertResult:
        upserted = updated = skipped = 0
        for start in range(0, len(points), self._batch):
            result = await self._store.upsert(points=points[start : start + self._batch])
            upserted += result.upserted
            updated += result.updated
            skipped += result.skipped
        return UpsertResult(upserted=upserted, updated=updated, skipped=skipped)

    # ----------------------------------------------------------------- index
    async def index(
        self, *, page_ids: Sequence[uuid.UUID] | None = None, force: bool = False
    ) -> IndexReport:
        scope = None if page_ids is None else set(page_ids)
        async with self._sessionmaker() as session:
            identity = await self._identity_for(session)
            if identity is None:
                return IndexReport(
                    collection=(await self._store.collection_info()).name,
                    scanned=0,
                    upserted=0,
                    updated=0,
                    skipped=0,
                    stale_deleted=0,
                    pages_deleted=0,
                    vector_size=0,
                    issues=(ISSUE_NO_EMBEDDINGS,),
                )
            rows = await repo.current_embeddings(
                session,
                model=identity.model,
                recipe_version=identity.recipe_version,
                recipe_fingerprint=identity.recipe_fingerprint,
                page_ids=None if scope is None else sorted(scope, key=str),
            )
            pages = await repo.list_pages(
                session, page_ids=None if scope is None else sorted(scope, key=str)
            )

        issues: list[str] = []
        # One dimension per collection: the most common one wins, the rest are reported.
        dimensions = Counter(len(r.vector) for r in rows)
        size = max(dimensions, key=lambda d: (dimensions[d], -d)) if dimensions else 0
        usable = [r for r in rows if len(r.vector) == size]
        if len(usable) != len(rows):
            issues.append(ISSUE_DIMENSION_MISMATCH)
        points = [point_from_embedding(row) for row in usable]

        existing: dict[uuid.UUID, PointRef] = {}
        scanner = self._store if isinstance(self._store, InspectableVectorStore) else None
        inspectable = scanner is not None
        if size:
            await self._store.ensure_collection(vector_size=size)
        if scanner is not None:
            async for ref in scanner.scan_points():
                if scope is None or ref.page_id in scope:
                    existing[ref.point_id] = ref
        else:
            issues.append(ISSUE_STORE_NOT_INSPECTABLE)

        to_send = [
            p
            for p in points
            if force
            or not inspectable
            or p.point_id not in existing
            or existing[p.point_id].content_hash != p.content_hash
        ]
        sent = await self.upsert_points(points=to_send)

        current_ids = {p.page_id: p.page_version_id for p in points}
        # Superseded versions go for every page re-sent this pass and for any page whose
        # stored points describe an older version, so drift stays bounded (ADR-0017 s4).
        touched = {p.page_id for p in to_send} | {
            r.page_id
            for r in existing.values()
            if r.page_id in current_ids and r.page_version_id != current_ids[r.page_id]
        }
        stale_deleted = 0
        for page_id in sorted(touched, key=str):
            stale_deleted += await self._store.delete_stale(
                page_id=page_id, keep_version_id=current_ids[page_id]
            )
        # Pages that are gone, or have no current embedding of THIS identity, keep no points.
        live = {p.id for p in pages if not p.gone}
        doomed = {
            ref.page_id
            for ref in existing.values()
            if ref.page_id not in current_ids or ref.page_id not in live
        }
        pages_deleted = await self._store.delete_pages(page_ids=sorted(doomed, key=str))
        info = await self._store.collection_info()
        return IndexReport(
            collection=info.name,
            scanned=len(points),
            upserted=sent.upserted,
            updated=sent.updated,
            skipped=sent.skipped,
            stale_deleted=stale_deleted,
            pages_deleted=pages_deleted,
            vector_size=size,
            embedding_model=identity.model,
            embedding_version=identity.embedding_version,
            issues=tuple(issues),
        )


class PostgresVectorRebuilder:
    """:class:`~ken_services.vectors.protocol.VectorRebuilder`: drop, recreate, refill."""

    def __init__(
        self,
        *,
        sessionmaker: async_sessionmaker[AsyncSession],
        store: VectorStore,
        settings: Settings,
        identity: VectorIdentity | None = None,
    ) -> None:
        self._indexer = PostgresVectorIndexer(
            sessionmaker=sessionmaker, store=store, settings=settings, identity=identity
        )
        self._store = store
        self._sessionmaker = sessionmaker

    async def rebuild(self, *, batch_size: int = DEFAULT_BATCH) -> RebuildReport:
        async with self._sessionmaker() as session:
            pages = await repo.list_pages(session)
        points = await self._indexer.points_for(page_ids=[p.id for p in pages if not p.gone])
        dims = Counter(len(p.vector) for p in points)
        if dims:
            size = max(dims, key=lambda d: (dims[d], -d))
            points = [p for p in points if len(p.vector) == size]
        return await self._store.rebuild_from(points=points, batch_size=batch_size)


# --------------------------------------------------------------------------- #
# Recorded jobs (JobKind.VECTOR_INDEX)
# --------------------------------------------------------------------------- #
async def run_index_job(
    *,
    sessionmaker: async_sessionmaker[AsyncSession],
    store: VectorStore,
    settings: Settings,
    triggered_by: str,
    force: bool = False,
    clock: Callable[[], datetime] = utc_now,
) -> tuple[JobRunDetail, IndexReport | None]:
    """One incremental indexing pass as a ``vector_index`` job run."""
    holder: list[IndexReport] = []

    async def body(job: JobContext) -> RunStatus:
        report = await PostgresVectorIndexer(
            sessionmaker=sessionmaker, store=store, settings=settings
        ).index(force=force)
        holder.append(report)
        for name in INDEX_COUNTERS:
            job.increment(name, int(getattr(report, name)))
        return RunStatus.SUCCEEDED if report.ok else RunStatus.COMPLETED_WITH_ERRORS

    detail = await run_job(
        kind=JobKind.VECTOR_INDEX,
        sessionmaker=sessionmaker,
        triggered_by=triggered_by,
        body=body,
        config_snapshot={"mode": "index", "force": force, "store": store.name},
        counter_names=INDEX_COUNTERS,
        clock=clock,
    )
    return detail, (holder[0] if holder else None)


async def run_rebuild_job(
    *,
    sessionmaker: async_sessionmaker[AsyncSession],
    store: VectorStore,
    settings: Settings,
    triggered_by: str,
    batch_size: int = DEFAULT_BATCH,
    clock: Callable[[], datetime] = utc_now,
) -> tuple[JobRunDetail, RebuildReport | None]:
    """A full drop-and-refill as a ``vector_index`` job run (``make qdrant-rebuild``)."""
    holder: list[RebuildReport] = []

    async def body(job: JobContext) -> RunStatus:
        report = await PostgresVectorRebuilder(
            sessionmaker=sessionmaker, store=store, settings=settings
        ).rebuild(batch_size=batch_size)
        holder.append(report)
        job.increment("scanned", report.points_written)
        job.increment("upserted", report.points_written)
        return RunStatus.SUCCEEDED

    detail = await run_job(
        kind=JobKind.VECTOR_INDEX,
        sessionmaker=sessionmaker,
        triggered_by=triggered_by,
        body=body,
        config_snapshot={"mode": "rebuild", "batch_size": batch_size, "store": store.name},
        counter_names=INDEX_COUNTERS,
        clock=clock,
    )
    return detail, (holder[0] if holder else None)
