"""One behavioural contract, run against EVERY VectorStore implementation (ADR-0017 s3).

``memory`` always runs. ``qdrant`` runs against the local container when it is reachable and
skips otherwise; it uses a collection prefixed ``ken_wsa_`` and deletes it afterwards, so a
shared container is never clobbered.
"""

from __future__ import annotations

import urllib.request
import uuid
from collections.abc import AsyncIterator

import pytest

from ken_contracts.vectors import (
    CollectionStatus,
    VectorDistance,
    VectorFilters,
    VectorHit,
    VectorPoint,
    VectorQuery,
    VectorStore,
    VectorStoreError,
    VectorStoreUnavailableError,
)
from ken_core.providers.vectors.base import InspectableVectorStore
from ken_core.providers.vectors.memory import MemoryVectorStore
from ken_core.providers.vectors.qdrant import QdrantVectorStore

QDRANT_URL = "http://127.0.0.1:6333"
H = "a" * 64
H2 = "b" * 64
MODEL = "m1"
VERSION = "r1-abc"


def _qdrant_up() -> bool:
    try:
        with urllib.request.urlopen(f"{QDRANT_URL}/readyz", timeout=1) as response:  # noqa: S310
            return bool(response.status == 200)
    except Exception:
        return False


QDRANT_REACHABLE = _qdrant_up()


def pid(n: int) -> uuid.UUID:
    return uuid.UUID(int=n)


def point(
    n: int,
    vector: tuple[float, ...],
    *,
    version: int = 1,
    model: str = MODEL,
    content_hash: str = H,
    page_type: str | None = None,
    language: str | None = None,
) -> VectorPoint:
    metadata: dict[str, str] = {}
    if page_type:
        metadata["page_type"] = page_type
    if language:
        metadata["language"] = language
    return VectorPoint.build(
        page_id=pid(n),
        page_version_id=f"{pid(n)}:{version}",
        content_hash=content_hash,
        embedding_model=model,
        embedding_version=VERSION,
        vector=vector,
        metadata=metadata,
    )


@pytest.fixture(
    params=[
        "memory",
        pytest.param(
            "qdrant",
            marks=pytest.mark.skipif(not QDRANT_REACHABLE, reason="Qdrant is not reachable"),
        ),
    ]
)
async def store(request: pytest.FixtureRequest) -> AsyncIterator[VectorStore]:
    name = f"ken_wsa_{uuid.uuid4().hex[:12]}"
    if request.param == "memory":
        instance: VectorStore = MemoryVectorStore(collection=name)
    else:
        instance = QdrantVectorStore(url=QDRANT_URL, collection=name, timeout_s=5.0)
    try:
        yield instance
    finally:
        if isinstance(instance, QdrantVectorStore):
            # Remove the collection this test created (shared container hygiene).
            await instance.rebuild_from(points=[])
            await instance._client.delete_collection(name)
        await instance.aclose()


def query(
    vector: tuple[float, ...],
    *,
    limit: int = 10,
    score_threshold: float | None = None,
    filters: VectorFilters | None = None,
) -> VectorQuery:
    return VectorQuery(
        vector=vector,
        limit=limit,
        score_threshold=score_threshold,
        filters=filters if filters is not None else VectorFilters(),
    )


async def test_satisfies_the_frozen_protocol(store: VectorStore) -> None:
    assert isinstance(store, VectorStore)
    assert isinstance(store, InspectableVectorStore)


async def test_missing_collection_is_a_status_not_an_error(store: VectorStore) -> None:
    info = await store.collection_info()
    assert info.status is CollectionStatus.MISSING
    assert await store.search(query=query((1.0, 0.0))) == []


async def test_ensure_collection_is_idempotent_and_refuses_a_different_size(
    store: VectorStore,
) -> None:
    first = await store.ensure_collection(vector_size=3)
    second = await store.ensure_collection(vector_size=3)
    assert first.vector_size == second.vector_size == 3
    assert first.distance is VectorDistance.COSINE
    with pytest.raises(VectorStoreError):
        await store.ensure_collection(vector_size=4)


async def test_upsert_is_idempotent_on_the_deterministic_point_id(store: VectorStore) -> None:
    points = [point(1, (1.0, 0.0)), point(2, (0.0, 1.0))]
    first = await store.upsert(points=points)
    again = await store.upsert(points=points)
    assert (first.upserted, first.updated) == (2, 0)
    assert (again.upserted, again.updated) == (2, 2)
    assert (await store.collection_info()).points == 2  # overwritten, never duplicated


async def test_search_orders_by_cosine_and_breaks_ties_by_page_id(store: VectorStore) -> None:
    await store.upsert(
        points=[
            point(3, (1.0, 0.0)),
            point(2, (1.0, 0.0)),  # identical to 3: a tie
            point(1, (0.6, 0.8)),
            point(4, (0.0, 1.0)),
        ]
    )
    hits = await store.search(query=query((1.0, 0.0)))
    assert [h.page_id for h in hits] == [pid(2), pid(3), pid(1), pid(4)]
    assert hits[0].score == pytest.approx(1.0, abs=1e-5)
    assert hits[2].score == pytest.approx(0.6, abs=1e-5)
    assert all(not h.stale for h in hits)  # a store never decides staleness


async def test_limit_and_score_threshold(store: VectorStore) -> None:
    await store.upsert(points=[point(1, (1.0, 0.0)), point(2, (0.6, 0.8)), point(3, (0.0, 1.0))])
    assert [h.page_id for h in await store.search(query=query((1.0, 0.0), limit=2))] == [
        pid(1),
        pid(2),
    ]
    floor = await store.search(query=query((1.0, 0.0), score_threshold=0.5))
    assert [h.page_id for h in floor] == [pid(1), pid(2)]


async def test_filters_apply_before_the_limit(store: VectorStore) -> None:
    """Eligible neighbours just outside k must still be found (VectorFilters docstring)."""
    await store.upsert(
        points=[
            point(1, (1.0, 0.0), page_type="insight"),
            point(2, (0.99, 0.1), page_type="insight"),
            point(3, (0.5, 0.5), page_type="report"),
        ]
    )
    hits = await store.search(
        query=query((1.0, 0.0), limit=1, filters=VectorFilters(page_types=("report",)))
    )
    assert [h.page_id for h in hits] == [pid(3)]


async def test_page_exclusion_model_version_hash_and_language_filters(
    store: VectorStore,
) -> None:
    await store.upsert(
        points=[
            point(1, (1.0, 0.0), language="en"),
            point(2, (0.9, 0.1), language="de", content_hash=H2),
            point(3, (0.8, 0.2), language="en"),
        ]
    )

    async def run(f: VectorFilters) -> list[VectorHit]:
        return await store.search(query=query((1.0, 0.0), filters=f))

    assert [h.page_id for h in await run(VectorFilters(exclude_page_ids=(pid(1),)))] == [
        pid(2),
        pid(3),
    ]
    assert {h.page_id for h in await run(VectorFilters(page_ids=(pid(1), pid(3))))} == {
        pid(1),
        pid(3),
    }
    assert {h.page_id for h in await run(VectorFilters(content_hashes=(H2,)))} == {pid(2)}
    assert {h.page_id for h in await run(VectorFilters(languages=("en",)))} == {pid(1), pid(3)}
    assert await run(VectorFilters(embedding_model="other")) == []
    assert len(await run(VectorFilters(embedding_model=MODEL, embedding_version=VERSION))) == 3
    assert await run(VectorFilters(embedding_version="r9-zzz")) == []


async def test_delete_pages_and_delete_stale(store: VectorStore) -> None:
    await store.upsert(
        points=[
            point(1, (1.0, 0.0), version=1),
            point(1, (0.9, 0.1), version=2),
            point(2, (0.0, 1.0)),
        ]
    )
    assert await store.delete_stale(page_id=pid(1), keep_version_id=f"{pid(1)}:2") == 1
    survivors = {h.page_version_id for h in await store.search(query=query((1.0, 0.0)))}
    assert survivors == {f"{pid(1)}:2", f"{pid(2)}:1"}
    assert await store.delete_pages(page_ids=[pid(1), pid(99)]) == 1
    assert await store.delete_pages(page_ids=[pid(1)]) == 0
    assert (await store.collection_info()).points == 1


async def test_zero_and_wrong_dimension_vectors_are_skipped_not_scored(
    store: VectorStore,
) -> None:
    await store.ensure_collection(vector_size=2)
    result = await store.upsert(
        points=[point(1, (1.0, 0.0)), point(2, (0.0, 0.0)), point(3, (1.0, 0.0, 0.0))]
    )
    assert (result.upserted, result.skipped) == (1, 2)
    assert await store.search(query=query((0.0, 0.0))) == []


async def test_rebuild_from_drops_and_refills_and_accepts_empty(store: VectorStore) -> None:
    await store.upsert(points=[point(1, (1.0, 0.0)), point(2, (0.0, 1.0))])
    report = await store.rebuild_from(points=[point(3, (1.0, 0.0, 0.0))], batch_size=1)
    assert report.dropped and report.points_written == 1 and report.vector_size == 3
    assert [h.page_id for h in await store.search(query=query((1.0, 0.0, 0.0)))] == [pid(3)]
    empty = await store.rebuild_from(points=[])
    assert empty.points_written == 0
    assert (await store.collection_info()).points == 0


async def test_scan_points_lists_references_without_vectors(store: VectorStore) -> None:
    assert isinstance(store, InspectableVectorStore)
    await store.upsert(points=[point(2, (1.0, 0.0)), point(1, (0.0, 1.0))])
    refs = [r async for r in store.scan_points()]
    assert {r.page_id for r in refs} == {pid(1), pid(2)}
    assert all(r.embedding_model == MODEL and r.embedding_version == VERSION for r in refs)
    assert [r.point_id for r in refs] == sorted(r.point_id for r in refs)


async def test_collection_info_reports_the_identity_it_was_built_with(
    store: VectorStore,
) -> None:
    await store.upsert(points=[point(1, (1.0, 0.0))])
    info = await store.collection_info()
    assert (info.embedding_model, info.embedding_version) == (MODEL, VERSION)
    assert info.points == 1 and info.status in {CollectionStatus.GREEN, CollectionStatus.YELLOW}
    health = await store.health()
    assert health.reachable and health.detail["points"] == 1


async def test_an_unreachable_qdrant_is_an_answer_not_an_exception() -> None:
    dead = QdrantVectorStore(url="http://127.0.0.1:1", collection="ken_wsa_dead", timeout_s=1.0)
    try:
        health = await dead.health()
        assert health.reachable is False and health.error is not None
        assert "127.0.0.1" not in str(health.model_dump())  # no URL leaks
        with pytest.raises(VectorStoreUnavailableError) as raised:
            await dead.search(query=query((1.0, 0.0)))
        assert "127.0.0.1" not in str(raised.value)
    finally:
        await dead.aclose()
