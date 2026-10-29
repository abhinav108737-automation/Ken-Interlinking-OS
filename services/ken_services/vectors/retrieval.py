"""The SEMANTIC channel's single entry point (ADR-0017 consequences, protocol.SemanticRetriever).

A vector store holds DERIVED points and cannot know a page's current version, so it returns
``stale=False`` for everything. This layer owns the step a store cannot do: re-check every hit
against ``pages.current_version`` and the stored embedding's content hash, drop stale hits by
default (``VectorFilters.include_stale`` can keep them for inspection) and mark the survivors
``stale=True``. A stale hit may never place a link -- the autonomy engine blocks it
deterministically (``BlockCode.STALE_VECTOR``); it is a filter, not a ranking penalty.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from typing import Final

from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ken_contracts.vectors import VectorFilters, VectorHit, VectorQuery, VectorStore
from ken_core.db.models.intelligence import PageEmbedding
from ken_core.db.models.inventory import Page
from ken_core.db.repositories import intelligence as repo
from ken_services.vectors.identity import VectorIdentity, page_version_id

__all__ = ["OVERFETCH", "PostgresSemanticRetriever", "check_freshness"]

OVERFETCH: Final = 2
"""Stale hits are dropped AFTER the store's limit, so a bounded over-fetch keeps ``limit``
meaningful when some drift exists. Drift is kept small by the indexer; this only absorbs it."""
_MAX_LIMIT: Final = 500


async def check_freshness(
    session: AsyncSession, *, hits: Sequence[VectorHit], identity: VectorIdentity
) -> list[VectorHit]:
    """Every hit, re-stamped with ``stale`` from PostgreSQL's current facts. Pages that are gone
    or unknown are returned stale too (the caller drops them unless asked to keep them)."""
    if not hits:
        return []
    ids = sorted({h.page_id for h in hits}, key=str)
    page_rows = (
        await session.execute(
            select(Page.id, Page.current_version, Page.gone_at).where(Page.id.in_(ids))
        )
    ).all()
    current = {r.id: (r.current_version, r.gone_at is not None) for r in page_rows}
    hash_rows = (
        await session.execute(
            select(PageEmbedding.page_id, PageEmbedding.page_version, PageEmbedding.input_hash)
            .join(
                Page,
                and_(
                    PageEmbedding.page_id == Page.id,
                    PageEmbedding.page_version == Page.current_version,
                ),
            )
            .where(
                PageEmbedding.page_id.in_(ids),
                PageEmbedding.model == identity.model,
                PageEmbedding.recipe_version == identity.recipe_version,
                PageEmbedding.recipe_fingerprint == identity.recipe_fingerprint,
            )
        )
    ).all()
    stored_hash = {r.page_id: r.input_hash for r in hash_rows}
    out: list[VectorHit] = []
    for hit in hits:
        version, gone = current.get(hit.page_id, (None, True))
        stale = (
            gone
            or version is None
            or hit.page_version_id != page_version_id(hit.page_id, version)
            or stored_hash.get(hit.page_id) != hit.content_hash
        )
        out.append(hit.model_copy(update={"stale": stale}))
    return out


class PostgresSemanticRetriever:
    """:class:`~ken_services.vectors.protocol.SemanticRetriever`."""

    def __init__(
        self,
        *,
        sessionmaker: async_sessionmaker[AsyncSession],
        store: VectorStore,
        identity: VectorIdentity,
    ) -> None:
        self._sessionmaker = sessionmaker
        self._store = store
        self._identity = identity

    async def search(self, *, query: VectorQuery) -> list[VectorHit]:
        include_stale = query.filters.include_stale
        # One identity per search: another model's or recipe's vectors are never compared.
        filters = query.filters.model_copy(
            update={
                "embedding_model": query.filters.embedding_model or self._identity.model,
                "embedding_version": query.filters.embedding_version
                or self._identity.embedding_version,
            }
        )
        fetch = query.limit if include_stale else min(_MAX_LIMIT, query.limit * OVERFETCH)
        raw = await self._store.search(
            query=query.model_copy(update={"filters": filters, "limit": fetch})
        )
        async with self._sessionmaker() as session:
            checked = await check_freshness(session, hits=raw, identity=self._identity)
        kept = checked if include_stale else [h for h in checked if not h.stale]
        return kept[: query.limit]

    async def similar_to_page(
        self, *, page_id: uuid.UUID, limit: int, score_threshold: float | None = None
    ) -> list[VectorHit]:
        async with self._sessionmaker() as session:
            rows = await repo.current_embeddings(
                session,
                model=self._identity.model,
                recipe_version=self._identity.recipe_version,
                recipe_fingerprint=self._identity.recipe_fingerprint,
                page_ids=[page_id],
            )
        if not rows:
            return []
        return await self.search(
            query=VectorQuery(
                vector=rows[0].vector,
                limit=limit,
                score_threshold=score_threshold,
                filters=VectorFilters(
                    exclude_page_ids=(page_id,),
                    embedding_model=self._identity.model,
                    embedding_version=self._identity.embedding_version,
                ),
            )
        )
