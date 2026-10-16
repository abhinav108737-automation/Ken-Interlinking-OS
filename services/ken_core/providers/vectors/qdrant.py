"""Qdrant vector store over the local HTTP API (ADR-0017 sections 3-5).

A DERIVED, rebuildable index. PostgreSQL keeps every embedding value; nothing is stored only
here, so dropping the collection costs a reindex and never data (``rebuild_from``).

Design points, each tied to a rule in the ADR or the frozen contract:

* **Deterministic ids.** Every point is written under ``VectorPoint.point_id`` (a ``uuid5`` of
  version, chunk, model and recipe), so re-indexing overwrites instead of duplicating.
* **Pre-filtering.** Filters are Qdrant payload conditions, applied INSIDE the engine before
  the limit, so ``limit`` means "limit of eligible hits".
* **Deterministic order.** Qdrant makes no promise about the order of equal scores, so hits are
  re-sorted by ``(-score, page_id, chunk_id)``, the protocol's tie-break.
* **No silent recreation.** A collection with another vector size or metric raises; an operator
  rebuilds knowingly (``make qdrant-rebuild``).
* **Honest unavailability.** Transport failures become
  :class:`~ken_contracts.vectors.VectorStoreUnavailableError` whose message carries neither the
  URL nor the API key. :meth:`health` never raises.
* **Zero vectors are not indexed.** A zero vector has no direction under cosine; counting it as
  ``skipped`` keeps the store and the memory store answering identically.
"""

from __future__ import annotations

import math
import time
import uuid
from collections.abc import AsyncIterator, Sequence
from datetime import UTC, datetime
from typing import Any, Final

from pydantic import SecretStr
from qdrant_client import AsyncQdrantClient, models

from ken_contracts.model import ProviderHealth
from ken_contracts.vectors import (
    MAX_BATCH_SIZE,
    CollectionInfo,
    CollectionStatus,
    RebuildReport,
    UpsertResult,
    VectorDistance,
    VectorFilters,
    VectorHit,
    VectorPoint,
    VectorQuery,
    VectorStoreError,
    VectorStoreUnavailableError,
)
from ken_core.providers.vectors.base import PointRef, sort_hits

__all__ = ["NAME", "QdrantVectorStore"]

NAME: Final = "qdrant"
_INDEXED_FIELDS: Final = (
    "page_id",
    "page_version_id",
    "content_hash",
    "embedding_model",
    "embedding_version",
    "page_type",
    "language",
)
"""Payload fields filters use; each gets a keyword index so pre-filtering stays cheap."""
_SAMPLE: Final = 256
"""Points sampled to report the model/version a collection was built with."""

_STATUS: Final = {
    "green": CollectionStatus.GREEN,
    "yellow": CollectionStatus.YELLOW,
    "grey": CollectionStatus.GREY,
    "red": CollectionStatus.RED,
}


def _unavailable() -> VectorStoreUnavailableError:
    """Never carries the URL, the key or the client's own text (docs/CONTRACTS.md rule 10)."""
    return VectorStoreUnavailableError("the vector store could not be reached")


def _is_zero(vector: Sequence[float]) -> bool:
    return not any(component != 0.0 for component in vector)


class QdrantVectorStore:
    """Structurally a :class:`~ken_contracts.vectors.VectorStore` (and an
    :class:`~ken_core.providers.vectors.base.InspectableVectorStore`)."""

    def __init__(
        self,
        *,
        url: str,
        collection: str,
        api_key: SecretStr | str | None = None,
        timeout_s: float = 10.0,
        client: AsyncQdrantClient | None = None,
    ) -> None:
        self._collection = collection
        self._timeout_s = timeout_s
        key = api_key.get_secret_value() if isinstance(api_key, SecretStr) else api_key
        self._client = client or AsyncQdrantClient(
            url=url,
            api_key=key,
            timeout=max(1, math.ceil(timeout_s)),
            prefer_grpc=False,
            check_compatibility=False,
        )
        self._vector_size: int | None = None

    @property
    def name(self) -> str:
        return NAME

    # ------------------------------------------------------------- plumbing
    async def _exists(self) -> bool:
        try:
            return bool(await self._client.collection_exists(self._collection))
        except Exception as exc:
            raise _unavailable() from exc

    async def _known_size(self) -> int | None:
        """Vector size of the existing collection, or ``None`` when it does not exist."""
        if self._vector_size is not None:
            return self._vector_size
        if not await self._exists():
            return None
        try:
            info = await self._client.get_collection(self._collection)
        except Exception as exc:
            raise _unavailable() from exc
        size, distance = self._params(info)
        if distance is not models.Distance.COSINE:
            raise VectorStoreError(
                "the collection exists with a different distance metric; rebuild it explicitly"
            )
        self._vector_size = size
        return size

    @staticmethod
    def _params(info: Any) -> tuple[int, models.Distance | None]:
        vectors = info.config.params.vectors
        if isinstance(vectors, dict):  # named vectors are not used by this system
            raise VectorStoreError("the collection uses named vectors; rebuild it explicitly")
        return int(vectors.size), vectors.distance

    async def _create(self, size: int) -> None:
        try:
            await self._client.create_collection(
                collection_name=self._collection,
                vectors_config=models.VectorParams(size=size, distance=models.Distance.COSINE),
            )
            for field in _INDEXED_FIELDS:
                await self._client.create_payload_index(
                    collection_name=self._collection,
                    field_name=field,
                    field_schema=models.PayloadSchemaType.KEYWORD,
                )
        except Exception as exc:
            raise _unavailable() from exc
        self._vector_size = size

    # ------------------------------------------------------------ collection
    async def ensure_collection(
        self, *, vector_size: int, distance: VectorDistance = VectorDistance.COSINE
    ) -> CollectionInfo:
        if vector_size <= 0:
            raise VectorStoreError("vector_size must be positive")
        if distance is not VectorDistance.COSINE:  # pragma: no cover - single-member enum
            raise VectorStoreError("only the cosine metric is supported")
        existing = await self._known_size()
        if existing is None:
            await self._create(vector_size)
        elif existing != vector_size:
            # Never silently recreate: an operator must rebuild knowingly (ADR-0017 s3).
            raise VectorStoreError(
                "the collection exists with a different vector size; rebuild it explicitly"
            )
        return await self.collection_info()

    async def collection_info(self) -> CollectionInfo:
        if not await self._exists():
            return CollectionInfo(
                name=self._collection,
                vector_size=0,
                points=0,
                status=CollectionStatus.MISSING,
            )
        try:
            info = await self._client.get_collection(self._collection)
            size, _ = self._params(info)
            points, _next = await self._client.scroll(
                collection_name=self._collection,
                limit=_SAMPLE,
                with_payload=["embedding_model", "embedding_version"],
                with_vectors=False,
            )
        except VectorStoreError:
            raise
        except Exception as exc:
            raise _unavailable() from exc
        models_seen = {(p.payload or {}).get("embedding_model") for p in points}
        versions_seen = {(p.payload or {}).get("embedding_version") for p in points}
        count = int(info.points_count or 0)
        status = _STATUS.get(str(getattr(info.status, "value", info.status)), CollectionStatus.RED)
        if status is CollectionStatus.GREEN and count == 0:
            status = CollectionStatus.GREY
        return CollectionInfo(
            name=self._collection,
            vector_size=size,
            points=count,
            status=status,
            embedding_model=next(iter(models_seen)) if len(models_seen) == 1 else None,
            embedding_version=next(iter(versions_seen)) if len(versions_seen) == 1 else None,
        )

    # ----------------------------------------------------------------- write
    @staticmethod
    def _payload(point: VectorPoint) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "page_id": str(point.page_id),
            "page_version_id": point.page_version_id,
            "chunk_id": point.chunk_id,
            "content_hash": point.content_hash,
            "embedding_model": point.embedding_model,
            "embedding_version": point.embedding_version,
            "metadata": dict(point.metadata),
        }
        for key in ("page_type", "language"):
            value = point.metadata.get(key)
            if isinstance(value, str):
                payload[key] = value
        return payload

    async def upsert(self, *, points: Sequence[VectorPoint]) -> UpsertResult:
        if len(points) > MAX_BATCH_SIZE:
            raise VectorStoreError("too many points in one upsert")
        if not points:
            return UpsertResult(upserted=0, updated=0, skipped=0)
        size = await self._known_size()
        if size is None:
            await self._create(len(points[0].vector))
            size = self._vector_size
        accepted = [p for p in points if len(p.vector) == size and not _is_zero(p.vector)]
        skipped = len(points) - len(accepted)
        if not accepted:
            return UpsertResult(upserted=0, updated=0, skipped=skipped)
        try:
            existing = await self._client.retrieve(
                collection_name=self._collection,
                ids=[str(p.point_id) for p in accepted],
                with_payload=False,
                with_vectors=False,
            )
            await self._client.upsert(
                collection_name=self._collection,
                points=[
                    models.PointStruct(
                        id=str(p.point_id), vector=list(p.vector), payload=self._payload(p)
                    )
                    for p in accepted
                ],
                wait=True,
            )
        except Exception as exc:
            raise _unavailable() from exc
        return UpsertResult(upserted=len(accepted), updated=len(existing), skipped=skipped)

    async def _delete_where(self, flt: models.Filter) -> int:
        try:
            before = (
                await self._client.count(
                    collection_name=self._collection, count_filter=flt, exact=True
                )
            ).count
            if before:
                await self._client.delete(
                    collection_name=self._collection,
                    points_selector=models.FilterSelector(filter=flt),
                    wait=True,
                )
        except Exception as exc:
            raise _unavailable() from exc
        return int(before)

    async def delete_pages(self, *, page_ids: Sequence[uuid.UUID]) -> int:
        if not page_ids or not await self._exists():
            return 0
        return await self._delete_where(
            models.Filter(
                must=[
                    models.FieldCondition(
                        key="page_id", match=models.MatchAny(any=[str(p) for p in page_ids])
                    )
                ]
            )
        )

    async def delete_stale(self, *, page_id: uuid.UUID, keep_version_id: str) -> int:
        if not await self._exists():
            return 0
        return await self._delete_where(
            models.Filter(
                must=[
                    models.FieldCondition(
                        key="page_id", match=models.MatchValue(value=str(page_id))
                    )
                ],
                must_not=[
                    models.FieldCondition(
                        key="page_version_id", match=models.MatchValue(value=keep_version_id)
                    )
                ],
            )
        )

    async def rebuild_from(
        self, *, points: Sequence[VectorPoint], batch_size: int = 256
    ) -> RebuildReport:
        if batch_size < 1 or batch_size > MAX_BATCH_SIZE:
            raise VectorStoreError("batch_size must be between 1 and the maximum batch size")
        started = time.monotonic()
        dropped = await self._exists()
        try:
            if dropped:
                await self._client.delete_collection(self._collection)
        except Exception as exc:
            raise _unavailable() from exc
        self._vector_size = None
        size = len(points[0].vector) if points else 0
        if size:
            await self._create(size)
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

    # ------------------------------------------------------------------ read
    @staticmethod
    def _filter(filters: VectorFilters) -> models.Filter | None:
        must: list[models.Condition] = []
        must_not: list[models.Condition] = []

        def any_of(key: str, values: Sequence[str]) -> models.FieldCondition:
            return models.FieldCondition(key=key, match=models.MatchAny(any=list(values)))

        if filters.page_ids is not None:
            must.append(any_of("page_id", [str(p) for p in filters.page_ids]))
        if filters.exclude_page_ids:
            must_not.append(any_of("page_id", [str(p) for p in filters.exclude_page_ids]))
        if filters.embedding_model is not None:
            must.append(
                models.FieldCondition(
                    key="embedding_model", match=models.MatchValue(value=filters.embedding_model)
                )
            )
        if filters.embedding_version is not None:
            must.append(
                models.FieldCondition(
                    key="embedding_version",
                    match=models.MatchValue(value=filters.embedding_version),
                )
            )
        if filters.content_hashes is not None:
            must.append(any_of("content_hash", filters.content_hashes))
        if filters.page_types is not None:
            must.append(any_of("page_type", filters.page_types))
        if filters.languages is not None:
            must.append(any_of("language", filters.languages))
        if not must and not must_not:
            return None
        return models.Filter(must=must or None, must_not=must_not or None)

    async def search(self, *, query: VectorQuery) -> list[VectorHit]:
        if _is_zero(query.vector):
            return []  # no direction, no neighbours (same as the memory store)
        size = await self._known_size()
        if size is None:
            return []
        if len(query.vector) != size:
            raise VectorStoreError("the query vector has a different dimension than the collection")
        try:
            response = await self._client.query_points(
                collection_name=self._collection,
                query=list(query.vector),
                query_filter=self._filter(query.filters),
                limit=query.limit,
                score_threshold=query.score_threshold,
                with_payload=True,
                with_vectors=False,
            )
        except Exception as exc:
            raise _unavailable() from exc
        hits: list[VectorHit] = []
        for scored in response.points:
            payload = scored.payload or {}
            try:
                hits.append(
                    VectorHit(
                        page_id=uuid.UUID(str(payload["page_id"])),
                        page_version_id=str(payload["page_version_id"]),
                        chunk_id=payload.get("chunk_id"),
                        score=max(-1.0, min(1.0, float(scored.score))),
                        content_hash=str(payload["content_hash"]),
                        stale=False,  # freshness is decided against PostgreSQL, not here
                    )
                )
            except (KeyError, ValueError):
                continue  # a point without our payload is not ours to return
        return sort_hits(hits)[: query.limit]

    async def scan_points(self, *, batch_size: int = 512) -> AsyncIterator[PointRef]:
        """Read-only enumeration for drift reporting (``InspectableVectorStore``)."""
        if not await self._exists():
            return
        offset: Any = None
        refs: list[PointRef] = []
        while True:
            try:
                batch, offset = await self._client.scroll(
                    collection_name=self._collection,
                    limit=batch_size,
                    offset=offset,
                    with_payload=True,
                    with_vectors=False,
                )
            except Exception as exc:
                raise _unavailable() from exc
            for record in batch:
                payload = record.payload or {}
                try:
                    refs.append(
                        PointRef(
                            point_id=uuid.UUID(str(record.id)),
                            page_id=uuid.UUID(str(payload["page_id"])),
                            page_version_id=str(payload["page_version_id"]),
                            chunk_id=payload.get("chunk_id"),
                            content_hash=str(payload["content_hash"]),
                            embedding_model=str(payload["embedding_model"]),
                            embedding_version=str(payload["embedding_version"]),
                        )
                    )
                except (KeyError, ValueError):
                    continue
            if offset is None:
                break
        for ref in sorted(refs, key=lambda r: str(r.point_id)):
            yield ref

    async def health(self) -> ProviderHealth:
        started = time.monotonic()
        try:
            info = await self.collection_info()
        except Exception as exc:  # an unreachable store is an answer, not an error
            return ProviderHealth(
                provider=NAME,
                reachable=False,
                latency_ms=(time.monotonic() - started) * 1000.0,
                detail={"collection": self._collection},
                error=type(exc).__name__,
            )
        return ProviderHealth(
            provider=NAME,
            reachable=True,
            latency_ms=(time.monotonic() - started) * 1000.0,
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
        """Idempotent."""
        try:
            await self._client.close()
        except Exception:  # closing twice, or a dead transport, is not an error
            return None
