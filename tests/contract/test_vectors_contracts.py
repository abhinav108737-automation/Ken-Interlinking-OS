"""Contract tests for the frozen vector-store shapes (ADR-0017 section 3).

A freeze without tests is a convention. Four workstreams implement against these shapes, so
what is asserted here is exactly what they may rely on.
"""

from __future__ import annotations

import inspect
import math
import uuid

import pytest
from pydantic import ValidationError

from ken_contracts.common import AsyncClosable
from ken_contracts.vectors import (
    POINT_ID_NAMESPACE,
    CollectionInfo,
    CollectionStatus,
    RebuildReport,
    UpsertResult,
    VectorDistance,
    VectorFilters,
    VectorHit,
    VectorPoint,
    VectorQuery,
    VectorStore,
    VectorStoreError,
    VectorStoreNotConfiguredError,
    VectorStoreUnavailableError,
    cosine_similarity,
    point_id,
)
from ken_core.providers.vectors.memory import MemoryVectorStore

HASH = "a" * 64
OTHER_HASH = "b" * 64


# --------------------------------------------------------------------------- #
# point_id: the determinism the whole idempotency story rests on
# --------------------------------------------------------------------------- #
def test_point_id_is_a_uuid5_over_the_four_identity_fields() -> None:
    expected = uuid.uuid5(POINT_ID_NAMESPACE, "pv1:c0:nomic-embed-text:r1-abc")
    assert (
        point_id(
            page_version_id="pv1",
            chunk_id="c0",
            embedding_model="nomic-embed-text",
            embedding_version="r1-abc",
        )
        == expected
    )
    assert POINT_ID_NAMESPACE == uuid.NAMESPACE_URL


def test_point_id_is_stable_and_distinguishes_every_identity_field() -> None:
    base: dict[str, str | None] = {
        "page_version_id": "pv1",
        "chunk_id": None,
        "embedding_model": "m",
        "embedding_version": "v",
    }

    def _id(**overrides: str | None) -> uuid.UUID:
        merged = {**base, **overrides}
        return point_id(
            page_version_id=str(merged["page_version_id"]),
            chunk_id=merged["chunk_id"],
            embedding_model=str(merged["embedding_model"]),
            embedding_version=str(merged["embedding_version"]),
        )

    assert _id() == _id()  # re-indexing overwrites, never duplicates
    for field, value in (
        ("page_version_id", "pv2"),
        ("chunk_id", "c0"),
        ("embedding_model", "m2"),
        ("embedding_version", "v2"),
    ):
        assert _id(**{field: value}) != _id(), field


def test_a_missing_chunk_id_is_a_dash_and_cannot_collide_with_a_real_one() -> None:
    """``None`` renders as ``-``, so a whole-page point and a chunk called "-" would
    collide -- which is why a chunk id is a generated key and never free text."""
    assert point_id(
        page_version_id="pv1", chunk_id=None, embedding_model="m", embedding_version="v"
    ) == point_id(page_version_id="pv1", chunk_id="-", embedding_model="m", embedding_version="v")


# --------------------------------------------------------------------------- #
# cosine_similarity: "no answer" is not "unrelated"
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("left", "right", "expected"),
    [
        ([1.0, 0.0], [1.0, 0.0], 1.0),
        ([1.0, 0.0], [0.0, 1.0], 0.0),
        ([1.0, 0.0], [-1.0, 0.0], -1.0),
        ([3.0, 4.0], [6.0, 8.0], 1.0),  # magnitude is irrelevant
    ],
)
def test_cosine_similarity_values(left: list[float], right: list[float], expected: float) -> None:
    result = cosine_similarity(left, right)
    assert result is not None
    assert math.isclose(result, expected, abs_tol=1e-9)


@pytest.mark.parametrize(
    ("left", "right"),
    [
        ([0.0, 0.0], [1.0, 0.0]),  # a zero vector has no direction
        ([1.0, 0.0], [0.0, 0.0]),
        ([1.0, 0.0, 0.0], [1.0, 0.0]),  # two models are never comparable
        ([], []),
        ([float("nan"), 1.0], [1.0, 1.0]),
        ([float("inf"), 1.0], [1.0, 1.0]),
    ],
)
def test_cosine_similarity_returns_none_rather_than_zero(
    left: list[float], right: list[float]
) -> None:
    """``None`` means "no answer". Returning 0.0 would read as "unrelated", which is a
    different and much worse claim: it would rank a broken vector as a legitimate miss."""
    assert cosine_similarity(left, right) is None


def test_cosine_similarity_is_clamped_to_the_valid_range() -> None:
    value = cosine_similarity([1e-20, 0.0], [1e-20, 0.0])
    assert value is not None
    assert -1.0 <= value <= 1.0


# --------------------------------------------------------------------------- #
# Model shapes
# --------------------------------------------------------------------------- #
def _point(**overrides: object) -> VectorPoint:
    kwargs: dict[str, object] = {
        "page_id": uuid.uuid4(),
        "page_version_id": "pv1",
        "content_hash": HASH,
        "embedding_model": "m",
        "embedding_version": "v",
        "vector": (1.0, 0.0),
    }
    kwargs.update(overrides)
    return VectorPoint.build(**kwargs)  # type: ignore[arg-type]


def test_vector_point_build_computes_its_own_id() -> None:
    point = _point()
    assert point.point_id == point_id(
        page_version_id="pv1", chunk_id=None, embedding_model="m", embedding_version="v"
    )


def test_a_point_with_a_hand_written_id_is_refused() -> None:
    """The id is not a field a caller chooses: a wrong one would make upserts duplicate."""
    with pytest.raises(ValidationError):
        VectorPoint(
            point_id=uuid.uuid4(),
            page_id=uuid.uuid4(),
            page_version_id="pv1",
            content_hash=HASH,
            embedding_model="m",
            embedding_version="v",
            vector=(1.0, 0.0),
        )


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
def test_a_non_finite_component_is_refused_on_both_a_point_and_a_query(bad: float) -> None:
    with pytest.raises(ValidationError):
        _point(vector=(bad, 0.0))
    with pytest.raises(ValidationError):
        VectorQuery(vector=(bad, 0.0), limit=5)


def test_content_hash_must_be_a_sha256() -> None:
    with pytest.raises(ValidationError):
        _point(content_hash="not-a-hash")


def test_filters_exclude_stale_by_default() -> None:
    assert VectorFilters().include_stale is False


def test_a_hit_defaults_to_fresh_and_bounds_its_score() -> None:
    hit = VectorHit(page_id=uuid.uuid4(), page_version_id="pv1", score=0.5, content_hash=HASH)
    assert hit.stale is False
    with pytest.raises(ValidationError):
        VectorHit(page_id=uuid.uuid4(), page_version_id="pv1", score=1.5, content_hash=HASH)


def test_cosine_is_the_only_distance_and_every_model_forbids_extra_keys() -> None:
    assert [d.value for d in VectorDistance] == ["cosine"]
    for model in (VectorPoint, VectorQuery, VectorFilters, VectorHit, CollectionInfo):
        assert model.model_config["extra"] == "forbid"
        assert model.model_config.get("frozen") is True


def test_the_result_models_exist_with_their_documented_fields() -> None:
    assert set(UpsertResult.model_fields) == {"upserted", "updated", "skipped"}
    assert set(RebuildReport.model_fields) == {
        "collection",
        "dropped",
        "points_written",
        "batches",
        "vector_size",
        "duration_ms",
        "completed_at",
    }
    assert set(CollectionInfo.model_fields) == {
        "name",
        "vector_size",
        "distance",
        "points",
        "status",
        "embedding_model",
        "embedding_version",
    }


# --------------------------------------------------------------------------- #
# The protocol
# --------------------------------------------------------------------------- #
def test_the_protocol_is_async_keyword_only_and_closable() -> None:
    methods = (
        "ensure_collection",
        "upsert",
        "search",
        "delete_pages",
        "delete_stale",
        "collection_info",
        "health",
        "rebuild_from",
    )
    for name in methods:
        fn = getattr(VectorStore, name)
        assert inspect.iscoroutinefunction(fn), name
        parameters = list(inspect.signature(fn).parameters.values())[1:]
        assert all(p.kind is inspect.Parameter.KEYWORD_ONLY for p in parameters), name
    assert issubclass(VectorStore, AsyncClosable)


def test_the_errors_are_typed_and_share_a_base() -> None:
    assert issubclass(VectorStoreUnavailableError, VectorStoreError)
    assert issubclass(VectorStoreNotConfiguredError, VectorStoreError)


def test_the_memory_store_satisfies_the_protocol_structurally() -> None:
    assert isinstance(MemoryVectorStore(), VectorStore)


# --------------------------------------------------------------------------- #
# The default store's behaviour IS part of the freeze: CI runs on it
# --------------------------------------------------------------------------- #
async def test_the_memory_store_is_idempotent_and_deterministic() -> None:
    store = MemoryVectorStore(collection="ken_pages")
    page_a, page_b = uuid.uuid4(), uuid.uuid4()
    points = [
        _point(page_id=page_a, page_version_id="a:1", vector=(1.0, 0.0)),
        _point(page_id=page_b, page_version_id="b:1", vector=(0.0, 1.0)),
    ]
    first = await store.upsert(points=points)
    assert (first.upserted, first.updated) == (2, 0)
    again = await store.upsert(points=points)
    assert (again.upserted, again.updated) == (2, 2)  # replaced, never added
    info = await store.collection_info()
    assert info.points == 2
    assert info.status is CollectionStatus.GREEN

    hits = await store.search(query=VectorQuery(vector=(1.0, 0.0), limit=2))
    assert [h.page_id for h in hits] == [page_a, page_b]
    assert hits[0].score == pytest.approx(1.0)
    assert all(h.stale is False for h in hits)  # the store never claims to know freshness


async def test_the_memory_store_applies_filters_before_the_limit() -> None:
    store = MemoryVectorStore()
    keep, drop = uuid.uuid4(), uuid.uuid4()
    await store.upsert(
        points=[
            _point(page_id=drop, page_version_id="d:1", vector=(1.0, 0.0)),
            _point(page_id=keep, page_version_id="k:1", vector=(0.9, 0.1)),
        ]
    )
    hits = await store.search(
        query=VectorQuery(
            vector=(1.0, 0.0), limit=1, filters=VectorFilters(exclude_page_ids=(drop,))
        )
    )
    # Filtering AFTER a top-1 would have returned nothing at all.
    assert [h.page_id for h in hits] == [keep]


async def test_the_memory_store_rebuilds_from_postgresql_supplied_points() -> None:
    """ADR-0017 section 1: the index is derived, so a drop-and-refill must restore it."""
    store = MemoryVectorStore()
    points = [_point(page_version_id=f"p{i}:1", vector=(1.0, float(i))) for i in range(5)]
    await store.upsert(points=points)
    before = await store.search(query=VectorQuery(vector=(1.0, 1.0), limit=5))

    report = await store.rebuild_from(points=points, batch_size=2)
    assert report.dropped is True
    assert report.points_written == 5
    assert report.batches == 3
    assert (await store.search(query=VectorQuery(vector=(1.0, 1.0), limit=5))) == before


async def test_deleting_stale_points_keeps_only_the_named_version() -> None:
    store = MemoryVectorStore()
    page = uuid.uuid4()
    await store.upsert(
        points=[
            _point(page_id=page, page_version_id="p:1", content_hash=HASH),
            _point(page_id=page, page_version_id="p:2", content_hash=OTHER_HASH),
        ]
    )
    assert await store.delete_stale(page_id=page, keep_version_id="p:2") == 1
    info = await store.collection_info()
    assert info.points == 1


async def test_a_dimension_mismatch_is_skipped_not_mixed_in() -> None:
    store = MemoryVectorStore()
    await store.ensure_collection(vector_size=2)
    result = await store.upsert(points=[_point(vector=(1.0, 0.0, 0.0))])
    assert (result.upserted, result.skipped) == (0, 1)


async def test_recreating_a_collection_with_another_size_is_refused() -> None:
    """Never silently recreate: an operator must rebuild knowingly (ADR-0017 s3)."""
    store = MemoryVectorStore()
    await store.ensure_collection(vector_size=4)
    with pytest.raises(VectorStoreError):
        await store.ensure_collection(vector_size=8)


async def test_health_never_raises_and_closing_is_idempotent() -> None:
    store = MemoryVectorStore()
    health = await store.health()
    assert health.reachable is True
    await store.aclose()
    await store.aclose()
