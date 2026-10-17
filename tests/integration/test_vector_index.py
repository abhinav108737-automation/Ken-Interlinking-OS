"""Indexing, rebuild, drift and freshness against PostgreSQL (ADR-0017 sections 1, 4, 5).

Runs against the in-memory store always and against a real Qdrant container when reachable
(collection prefixed ``ken_wsa_``, deleted afterwards).
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ken_contracts.inventory import RunStatus
from ken_contracts.jobs import JobKind
from ken_contracts.vectors import (
    VectorFilters,
    VectorPoint,
    VectorQuery,
    VectorStore,
)
from ken_core.providers.vectors.memory import MemoryVectorStore
from ken_core.providers.vectors.qdrant import QdrantVectorStore
from ken_services.intelligence.embeddings import current_recipe_fingerprint
from ken_services.vectors.identity import VectorIdentity, page_version_id
from ken_services.vectors.index import (
    PostgresVectorIndexer,
    PostgresVectorRebuilder,
    run_index_job,
    run_rebuild_job,
)
from ken_services.vectors.retrieval import PostgresSemanticRetriever
from ken_services.vectors.verify import PostgresVectorVerifier
from tests.conftest import make_settings
from tests.integration.graph_support import leave_clean  # noqa: F401
from tests.integration.intelligence_support import (
    EMBED_MODEL,
    analyse,
    page,
    page_ids,
    seed,
)
from tests.unit.vectors.test_store_contract import QDRANT_REACHABLE, QDRANT_URL

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("migrated", "leave_clean")]

SM = async_sessionmaker[AsyncSession]
SETTINGS = make_settings(similarity_top_k=3, similarity_min_score=0.5, embedding_batch_size=2)

A = page("/reports/a", "alpha alpha alpha beta market:ev")
B = page("/reports/b", "alpha alpha beta beta market:ev")
C = page("/reports/c", "gamma gamma gamma delta market:batteries")
D = page("/reports/d", "delta delta delta gamma market:batteries")


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
            await instance._client.delete_collection(name)
        await instance.aclose()


def identity() -> VectorIdentity:
    return VectorIdentity(
        model=EMBED_MODEL, recipe_version=1, recipe_fingerprint=current_recipe_fingerprint(SETTINGS)
    )


def indexer(sm: SM, store: VectorStore) -> PostgresVectorIndexer:
    return PostgresVectorIndexer(sessionmaker=sm, store=store, settings=SETTINGS)


def verifier(sm: SM, store: VectorStore) -> PostgresVectorVerifier:
    return PostgresVectorVerifier(sessionmaker=sm, store=store, settings=SETTINGS)


async def corpus(sm: SM) -> dict[str, uuid.UUID]:
    await seed(sm, A, B, C, D)
    await analyse(sm)
    return await page_ids(sm)


def near_a() -> VectorQuery:
    # Bag-of-words over (alpha, beta, gamma, delta) with the +0.01 smoothing the fake adds.
    return VectorQuery(vector=(3.01, 1.01, 0.01, 0.01), limit=10)


async def test_index_is_idempotent_and_verify_reports_in_sync(
    sessionmaker: SM, store: VectorStore
) -> None:
    await corpus(sessionmaker)
    first = await indexer(sessionmaker, store).index()
    assert first.ok and first.scanned == 4 and first.upserted == 4 and first.vector_size == 4
    assert first.embedding_model == EMBED_MODEL
    again = await indexer(sessionmaker, store).index()
    # Nothing changed: nothing is re-sent, and the collection is byte-for-byte the same size.
    assert (again.scanned, again.upserted, again.updated) == (4, 0, 0)
    assert (await store.collection_info()).points == 4
    drift = await verifier(sessionmaker, store).verify()
    assert drift.in_sync and drift.collection_info.points == 4


async def test_force_resends_unchanged_points_without_duplicating(
    sessionmaker: SM, store: VectorStore
) -> None:
    await corpus(sessionmaker)
    await indexer(sessionmaker, store).index()
    forced = await indexer(sessionmaker, store).index(force=True)
    assert (forced.upserted, forced.updated) == (4, 4)
    assert (await store.collection_info()).points == 4


async def test_the_index_is_derived_dropping_it_and_rebuilding_returns_the_same_hits(
    sessionmaker: SM, store: VectorStore
) -> None:
    await corpus(sessionmaker)
    await indexer(sessionmaker, store).index()
    before = await store.search(query=near_a())
    await store.rebuild_from(points=[])  # lose the whole index
    assert (await store.collection_info()).points == 0
    assert not (await verifier(sessionmaker, store).verify()).in_sync  # everything missing
    report = await PostgresVectorRebuilder(
        sessionmaker=sessionmaker, store=store, settings=SETTINGS
    ).rebuild()
    assert report.points_written == 4
    after = await store.search(query=near_a())
    assert [(h.page_id, round(h.score, 5)) for h in after] == [
        (h.page_id, round(h.score, 5)) for h in before
    ]
    assert (await verifier(sessionmaker, store).verify()).in_sync


async def test_an_orphaned_point_is_reported_then_removed(
    sessionmaker: SM, store: VectorStore
) -> None:
    await corpus(sessionmaker)
    await indexer(sessionmaker, store).index()
    ghost = uuid.uuid4()
    await store.upsert(
        points=[
            VectorPoint.build(
                page_id=ghost,
                page_version_id=page_version_id(ghost, 1),
                content_hash="c" * 64,
                embedding_model=EMBED_MODEL,
                embedding_version=identity().embedding_version,
                vector=(1.0, 0.0, 0.0, 0.0),
            )
        ]
    )
    drift = await verifier(sessionmaker, store).verify()
    assert not drift.in_sync and len(drift.orphaned_points) == 1 and not drift.missing_vectors
    repaired = await indexer(sessionmaker, store).index()
    assert repaired.pages_deleted == 1
    assert (await verifier(sessionmaker, store).verify()).in_sync


async def test_a_missing_vector_is_reported_then_repaired(
    sessionmaker: SM, store: VectorStore
) -> None:
    ids = await corpus(sessionmaker)
    await indexer(sessionmaker, store).index()
    assert await store.delete_pages(page_ids=[ids["/reports/b"]]) == 1
    drift = await verifier(sessionmaker, store).verify()
    assert drift.missing_vectors == (ids["/reports/b"],) and not drift.orphaned_points
    repaired = await indexer(sessionmaker, store).index()
    assert repaired.upserted == 1
    assert (await verifier(sessionmaker, store).verify()).in_sync


async def test_a_superseded_version_is_orphaned_until_the_next_index_removes_it(
    sessionmaker: SM, store: VectorStore
) -> None:
    ids = await corpus(sessionmaker)
    await indexer(sessionmaker, store).index()
    # Page A changes: version 2 is analysed and embedded, but the index is not refreshed yet.
    await seed(sessionmaker, page("/reports/a", "beta beta beta alpha market:ev"))
    await analyse(sessionmaker)
    drift = await verifier(sessionmaker, store).verify()
    assert len(drift.orphaned_points) == 1  # the v1 point describes a superseded version
    assert drift.missing_vectors == (ids["/reports/a"],)  # v2 has no point yet
    # Retrieval re-checks PostgreSQL: the stale v1 hit is dropped by default ...
    retriever = PostgresSemanticRetriever(
        sessionmaker=sessionmaker, store=store, identity=identity()
    )
    hits = await retriever.search(query=near_a())
    assert ids["/reports/a"] not in {h.page_id for h in hits}
    # ... and, when explicitly inspected, it is returned MARKED stale (never usable for a link).
    inspected = await retriever.search(
        query=near_a().model_copy(update={"filters": VectorFilters(include_stale=True)})
    )
    a_hit = next(h for h in inspected if h.page_id == ids["/reports/a"])
    assert a_hit.stale and a_hit.page_version_id == page_version_id(ids["/reports/a"], 1)
    repaired = await indexer(sessionmaker, store).index()
    assert repaired.stale_deleted == 1 and repaired.upserted == 1
    assert (await verifier(sessionmaker, store).verify()).in_sync
    fresh = await retriever.search(query=near_a())
    assert ids["/reports/a"] in {h.page_id for h in fresh} and not any(h.stale for h in fresh)


async def test_a_gone_page_keeps_no_points(sessionmaker: SM, store: VectorStore) -> None:
    ids = await corpus(sessionmaker)
    await indexer(sessionmaker, store).index()
    async with sessionmaker() as session, session.begin():
        await session.execute(
            text("UPDATE pages SET gone_at = now() WHERE id = :id"), {"id": ids["/reports/d"]}
        )
    drift = await verifier(sessionmaker, store).verify()
    assert len(drift.orphaned_points) == 1
    report = await indexer(sessionmaker, store).index()
    assert report.pages_deleted == 1
    assert (await verifier(sessionmaker, store).verify()).in_sync
    assert ids["/reports/d"] not in {h.page_id for h in await store.search(query=near_a())}


async def test_a_changed_content_hash_is_reported_as_stale_and_overwritten(
    sessionmaker: SM, store: VectorStore
) -> None:
    ids = await corpus(sessionmaker)
    await indexer(sessionmaker, store).index()
    points = await indexer(sessionmaker, store).points_for(page_ids=[ids["/reports/c"]])
    tampered = points[0].model_copy(update={"content_hash": "d" * 64})
    await store.upsert(points=[tampered])  # same deterministic id, wrong hash
    drift = await verifier(sessionmaker, store).verify()
    assert drift.stale_points == (points[0].point_id,)
    await indexer(sessionmaker, store).index()
    assert (await verifier(sessionmaker, store).verify()).in_sync


async def test_a_collection_built_with_another_model_is_an_identity_mismatch(
    sessionmaker: SM, store: VectorStore
) -> None:
    ids = await corpus(sessionmaker)
    await indexer(sessionmaker, store).index()
    other = VectorPoint.build(
        page_id=ids["/reports/a"],
        page_version_id=page_version_id(ids["/reports/a"], 1),
        content_hash="e" * 64,
        embedding_model="some-other-model",
        embedding_version=identity().embedding_version,
        vector=(1.0, 1.0, 1.0, 1.0),
    )
    await store.upsert(points=[other])
    drift = await verifier(sessionmaker, store).verify()
    assert drift.identity_mismatch and not drift.in_sync
    # Searching through the retriever never mixes models: the other model's point is ignored.
    retriever = PostgresSemanticRetriever(
        sessionmaker=sessionmaker, store=store, identity=identity()
    )
    assert all(h.content_hash != "e" * 64 for h in await retriever.search(query=near_a()))


async def test_similar_to_page_excludes_the_page_and_respects_the_threshold(
    sessionmaker: SM, store: VectorStore
) -> None:
    ids = await corpus(sessionmaker)
    await indexer(sessionmaker, store).index()
    retriever = PostgresSemanticRetriever(
        sessionmaker=sessionmaker, store=store, identity=identity()
    )
    near = await retriever.similar_to_page(page_id=ids["/reports/a"], limit=3)
    assert ids["/reports/a"] not in {h.page_id for h in near}
    assert near[0].page_id == ids["/reports/b"]  # shares alpha/beta with A
    assert [h.score for h in near] == sorted((h.score for h in near), reverse=True)
    strict = await retriever.similar_to_page(
        page_id=ids["/reports/a"], limit=3, score_threshold=0.99
    )
    assert strict == []


async def test_no_embeddings_is_reported_not_raised(sessionmaker: SM, store: VectorStore) -> None:
    await seed(sessionmaker, A)  # ingested but never analysed or embedded
    report = await indexer(sessionmaker, store).index()
    assert report.issues == ("no_embeddings",) and report.upserted == 0 and not report.ok


async def test_index_and_rebuild_are_recorded_vector_index_jobs(
    sessionmaker: SM, store: VectorStore
) -> None:
    await corpus(sessionmaker)
    detail, report = await run_index_job(
        sessionmaker=sessionmaker, store=store, settings=SETTINGS, triggered_by="test"
    )
    assert detail.kind is JobKind.VECTOR_INDEX and detail.status is RunStatus.SUCCEEDED
    assert detail.counters["upserted"] == 4 and report is not None and report.ok
    detail2, rebuilt = await run_rebuild_job(
        sessionmaker=sessionmaker, store=store, settings=SETTINGS, triggered_by="test"
    )
    assert detail2.status is RunStatus.SUCCEEDED and rebuilt is not None
    assert rebuilt.points_written == 4
    async with sessionmaker() as session:
        count: int = (
            await session.execute(text("SELECT count(*) FROM job_runs WHERE kind = 'vector_index'"))
        ).scalar_one()
    assert count == 2
