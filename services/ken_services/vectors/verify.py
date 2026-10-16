"""``make qdrant-verify``: drift between PostgreSQL and the collection, BOTH directions
(ADR-0017 section 4). Read-only: it changes nothing.

* ``missing_vectors``  -- pages whose current version has an embedding but no point (retrieval
  silently cannot see them);
* ``orphaned_points``  -- points whose page is gone or unknown, or whose version is superseded
  (retrieval can see pages that no longer exist or old text);
* ``stale_points``     -- points of the current version whose content hash no longer matches the
  stored embedding's;
* ``identity_mismatch`` / ``dimension_mismatch`` -- the collection was built with another
  (model, embedding_version) or vector size: a rebuild is due, not quiet searching.
"""

from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ken_contracts.vectors import CollectionStatus, VectorStore
from ken_core.config import Settings
from ken_core.db.repositories import intelligence as repo
from ken_core.providers.vectors.base import InspectableVectorStore, PointRef
from ken_services.vectors.identity import VectorIdentity, page_version_id, resolve_identity
from ken_services.vectors.protocol import DriftReport

__all__ = ["PostgresVectorVerifier"]


class StoreNotInspectableError(RuntimeError):
    """The configured store cannot list its points, so drift cannot be reported."""

    def __init__(self) -> None:
        super().__init__("the vector store cannot enumerate its points")


class PostgresVectorVerifier:
    """:class:`~ken_services.vectors.protocol.VectorVerifier`."""

    def __init__(
        self,
        *,
        sessionmaker: async_sessionmaker[AsyncSession],
        store: VectorStore,
        settings: Settings,
        identity: VectorIdentity | None = None,
    ) -> None:
        self._sessionmaker = sessionmaker
        self._store = store
        self._settings = settings
        self._identity = identity

    async def verify(self) -> DriftReport:
        if not isinstance(self._store, InspectableVectorStore):
            raise StoreNotInspectableError
        info = await self._store.collection_info()
        refs: list[PointRef] = []
        if info.status is not CollectionStatus.MISSING:
            refs = [ref async for ref in self._store.scan_points()]

        async with self._sessionmaker() as session:
            identity = self._identity or await resolve_identity(session, self._settings)
            pages = await repo.list_pages(session)
            embeddings = (
                []
                if identity is None
                else await repo.current_embeddings(
                    session,
                    model=identity.model,
                    recipe_version=identity.recipe_version,
                    recipe_fingerprint=identity.recipe_fingerprint,
                    with_vectors=False,
                )
            )

        live_current = {p.id: page_version_id(p.id, p.current_version) for p in pages if not p.gone}
        expected_hash = {e.page_id: e.input_hash for e in embeddings}
        expected_version = {
            e.page_id: page_version_id(e.page_id, e.page_version) for e in embeddings
        }
        present_pages = {
            r.page_id for r in refs if r.page_version_id == live_current.get(r.page_id)
        }

        missing = sorted((pid for pid in expected_version if pid not in present_pages), key=str)
        orphaned: list[uuid.UUID] = []
        stale: list[uuid.UUID] = []
        for ref in refs:
            if live_current.get(ref.page_id) != ref.page_version_id:
                orphaned.append(ref.point_id)  # page gone/unknown, or version superseded
            elif ref.page_id in expected_hash and expected_hash[ref.page_id] != ref.content_hash:
                stale.append(ref.point_id)

        identity_mismatch = False
        if identity is not None and refs:
            identity_mismatch = any(
                r.embedding_model != identity.model
                or r.embedding_version != identity.embedding_version
                for r in refs
            )
        expected_size = 0
        if embeddings:
            expected_size = embeddings[0].dimensions
        dimension_mismatch = bool(
            expected_size
            and info.status is not CollectionStatus.MISSING
            and info.vector_size
            and info.vector_size != expected_size
        )
        return DriftReport(
            collection=info.name,
            collection_info=info,
            missing_vectors=tuple(missing),
            orphaned_points=tuple(sorted(orphaned, key=str)),
            stale_points=tuple(sorted(stale, key=str)),
            identity_mismatch=identity_mismatch,
            dimension_mismatch=dimension_mismatch,
        )
