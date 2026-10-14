"""SqlIntelligenceReader against the ``*_test`` database (data written by the analysis job)."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ken_contracts.intelligence import (
    EmbeddingUnavailableError,
    IneligibilityReason,
    IntelligenceNotFoundError,
    IntelligenceReader,
    LinkEligibility,
    ProtectionSource,
    RelationshipType,
    TaxonomyPolicy,
)
from ken_contracts.inventory import CanonicalStatus, IssueCode, PublicationState
from ken_contracts.model import ModelTimeoutError
from ken_services.intelligence.embeddings import current_recipe_fingerprint
from ken_services.intelligence.reader import NO_EMBEDDING_MODEL, SqlIntelligenceReader
from tests.conftest import make_settings
from tests.integration.conftest import make_issue, url
from tests.integration.intelligence_support import (
    AV,
    EMBED_MODEL,
    TAXONOMY,
    FakeEmbedder,
    analyse,
    page,
    page_ids,
    scalar,
    seed,
)

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("migrated")]

SM = async_sessionmaker[AsyncSession]
SETTINGS = make_settings(similarity_top_k=3, similarity_min_score=0.5, embedding_batch_size=2)

A = page("/reports/a", "alpha alpha alpha beta market:ev geo:india", metadata={"lock": "yes"})
B = page("/reports/b", "alpha alpha beta beta market:ev geo:india")
C = page("/reports/c", "gamma gamma gamma delta market:batteries")


class RecordingEligibility:
    """Records the live state the reader passes; derives reasons from it like the contract."""

    def __init__(self) -> None:
        self.states: list[dict[str, Any]] = []

    def __call__(self, fields: Mapping[str, Any], taxonomy: TaxonomyPolicy) -> LinkEligibility:
        state = dict(fields)
        self.states.append(state)
        reasons: list[IneligibilityReason] = []
        if state["gone"]:
            reasons.append(IneligibilityReason.GONE)
        if state["protected"]:
            reasons.append(IneligibilityReason.PROTECTED)
        if "noindex" in state["robots_directives"]:
            reasons.append(IneligibilityReason.NOINDEX)
        if not state["has_analysis"]:
            reasons.append(IneligibilityReason.NOT_ANALYZED)
        elif not state["analysis_fresh"]:
            reasons.append(IneligibilityReason.STALE_ANALYSIS)
        return LinkEligibility(
            as_source=not reasons,
            as_target=not reasons,
            source_reasons=reasons,
            target_reasons=reasons,
        )


def reader(
    sessionmaker: SM,
    provider: Any = None,
    eligibility: RecordingEligibility | None = None,
    settings: Any = None,
) -> SqlIntelligenceReader:
    return SqlIntelligenceReader(
        sessionmaker=sessionmaker,
        model_provider=provider if provider is not None else FakeEmbedder(),
        settings=settings or SETTINGS,
        taxonomy=TAXONOMY,
        eligibility=eligibility if eligibility is not None else RecordingEligibility(),
    )


async def _seeded(sessionmaker: SM, *pages: Any) -> dict[str, Any]:
    await seed(sessionmaker, *(pages or (A, B, C)))
    await analyse(sessionmaker, settings=SETTINGS)
    return await page_ids(sessionmaker)


# --------------------------------------------------------------------------- #
# get_page_intelligence
# --------------------------------------------------------------------------- #
async def test_reader_satisfies_the_protocol(sessionmaker: SM) -> None:
    assert isinstance(reader(sessionmaker), IntelligenceReader)


async def test_page_intelligence_full_view(sessionmaker: SM) -> None:
    ids = await _seeded(sessionmaker)
    eligibility = RecordingEligibility()
    view = await reader(sessionmaker, eligibility=eligibility).get_page_intelligence(
        page_id=ids["/reports/a"]
    )
    assert (view.canonical_url, view.current_version) == (url("/reports/a"), 1)
    assert view.analysis is not None and view.analysis_fresh
    assert (view.analysis.analysis_version, view.analysis.market_id) == (AV, "ev")
    assert view.embedding is not None and view.embedding_fresh
    assert (view.embedding.model, view.embedding.dimensions, view.embedding.page_version) == (
        EMBED_MODEL,
        4,
        1,
    )
    assert [(p.source, p.rule_id, p.active) for p in view.protections] == [
        (ProtectionSource.POLICY, "locked", True)
    ]
    # same_geography is a coarse type: proposed by the fake toolkit, never stored.
    assert view.relationship_counts == {"same_market": 1, "semantic_similar": 1}
    assert view.embedding.recipe_fingerprint == current_recipe_fingerprint(SETTINGS)
    [state] = eligibility.states
    assert state == {
        "publication_state": PublicationState.PUBLISHED,
        "gone": False,
        "protected": True,
        "robots_directives": (),
        "canonical_status": CanonicalStatus.SELF,
        "http_status": 200,
        "word_count": 6,
        "page_type": "report",
        "has_analysis": True,
        "analysis_fresh": True,
    }
    assert view.eligibility.source_reasons == [IneligibilityReason.PROTECTED]


async def test_the_shared_eligibility_function_blocks_a_protected_page(sessionmaker: SM) -> None:
    ids = await _seeded(sessionmaker)
    real = SqlIntelligenceReader(
        sessionmaker=sessionmaker,
        model_provider=FakeEmbedder(),
        settings=SETTINGS,
        taxonomy=TAXONOMY,
    )  # eligibility=None: ken_services.intelligence.eligibility.compute_eligibility
    view = await real.get_page_intelligence(page_id=ids["/reports/a"])
    assert not view.eligibility.as_source and not view.eligibility.as_target
    assert IneligibilityReason.PROTECTED in view.eligibility.source_reasons
    assert IneligibilityReason.PROTECTED in view.eligibility.target_reasons


async def test_unknown_page_is_not_found(sessionmaker: SM) -> None:
    import uuid

    missing = uuid.uuid4()
    r = reader(sessionmaker)
    for call in (
        r.get_page_intelligence(page_id=missing),
        r.list_similar(page_id=missing, limit=5),
        r.list_relationships(page_id=missing, limit=5, offset=0),
    ):
        with pytest.raises(IntelligenceNotFoundError) as info:
            await call
        assert info.value.resource == "page"


async def test_a_new_version_makes_analysis_embedding_and_edges_stale(sessionmaker: SM) -> None:
    ids = await _seeded(sessionmaker)
    # A changes (version 2) and is NOT re-analysed yet.
    await seed(
        sessionmaker,
        page("/reports/a", "alpha alpha market:ev geo:india", metadata={"lock": "yes"}),
    )
    eligibility = RecordingEligibility()
    r = reader(sessionmaker, eligibility=eligibility)
    view = await r.get_page_intelligence(page_id=ids["/reports/a"])
    assert view.current_version == 2
    assert view.analysis is None and not view.analysis_fresh
    assert view.embedding is None and not view.embedding_fresh
    assert view.relationship_counts == {}
    assert (eligibility.states[0]["has_analysis"], eligibility.states[0]["analysis_fresh"]) == (
        True,
        False,
    )
    assert view.eligibility.source_reasons == [
        IneligibilityReason.PROTECTED,
        IneligibilityReason.STALE_ANALYSIS,
    ]
    # B -> A edges point at A's old version: stale, not served.
    edges = await r.list_relationships(page_id=ids["/reports/b"], limit=50, offset=0)
    assert edges.items == [] and edges.page.total == 0
    similar = await r.list_similar(page_id=ids["/reports/b"], limit=10)
    assert ids["/reports/a"] not in {s.page_id for s in similar.items}


async def test_protections_are_listed_newest_first_with_history(sessionmaker: SM) -> None:
    ids = await _seeded(sessionmaker)
    await seed(sessionmaker, page("/reports/a", "alpha market:ev"))  # unlocked
    await analyse(sessionmaker, settings=SETTINGS)
    await seed(sessionmaker, page("/reports/a", "alpha beta market:ev", metadata={"lock": "yes"}))
    await analyse(sessionmaker, settings=SETTINGS)
    view = await reader(sessionmaker).get_page_intelligence(page_id=ids["/reports/a"])
    assert [(p.rule_id, p.active) for p in view.protections] == [
        ("locked", True),
        ("locked", False),
    ]
    assert view.protections[0].id > view.protections[1].id


# --------------------------------------------------------------------------- #
# Similarity and search
# --------------------------------------------------------------------------- #
async def test_similar_pages_are_ordered_and_exclude_self_and_gone(sessionmaker: SM) -> None:
    d = page("/reports/d", "alpha beta beta beta market:ev")
    ids = await _seeded(sessionmaker, A, B, C, d)
    r = reader(sessionmaker)
    similar = await r.list_similar(page_id=ids["/reports/a"], limit=10)
    order = [s.page_id for s in similar.items]
    assert order == [ids["/reports/b"], ids["/reports/d"], ids["/reports/c"]]
    scores = [s.score for s in similar.items]
    assert scores == sorted(scores, reverse=True) and all(-1 <= s <= 1 for s in scores)
    assert similar.items[0].market_id == "ev" and similar.items[0].page_type == "report"
    assert similar.page.total == 3
    assert [
        s.page_id for s in (await r.list_similar(page_id=ids["/reports/a"], limit=1)).items
    ] == [ids["/reports/b"]]
    # D disappears from the site (410): it is no longer offered.
    await seed(sessionmaker, make_issue(IssueCode.HTTP_GONE, url("/reports/d"), http_status=410))
    after = await r.list_similar(page_id=ids["/reports/a"], limit=10)
    assert ids["/reports/d"] not in {s.page_id for s in after.items}


async def test_similar_is_empty_without_a_fresh_embedding(sessionmaker: SM) -> None:
    await seed(sessionmaker, A, B)
    await analyse(sessionmaker, settings=SETTINGS, embeddings=False)
    ids = await page_ids(sessionmaker)
    result = await reader(sessionmaker).list_similar(page_id=ids["/reports/a"], limit=5)
    assert result.items == [] and result.page.total == 0


async def test_a_different_embedding_identity_is_never_compared(sessionmaker: SM) -> None:
    ids = await _seeded(sessionmaker)
    other = FakeEmbedder()
    other.capability_model = other.result_model = "another-model"
    r = reader(sessionmaker, other)
    assert (await r.list_similar(page_id=ids["/reports/a"], limit=5)).items == []
    view = await r.get_page_intelligence(page_id=ids["/reports/a"])
    assert view.embedding is not None and view.embedding.model == EMBED_MODEL
    assert not view.embedding_fresh
    assert (await r.semantic_search(query="alpha alpha", limit=5)).items == []


async def test_semantic_search_embeds_the_query_with_the_query_prefix(sessionmaker: SM) -> None:
    ids = await _seeded(sessionmaker)
    provider = FakeEmbedder()
    result = await reader(sessionmaker, provider).semantic_search(query="gamma gamma", limit=2)
    assert result.items[0].page_id == ids["/reports/c"]
    assert len(result.items) == 2
    assert provider.embed_calls == [[SETTINGS.embedding_query_prefix + "gamma gamma"]]


@pytest.mark.parametrize("failure", ["embed", "capabilities", "model", "missing"])
async def test_semantic_search_raises_embedding_unavailable(sessionmaker: SM, failure: str) -> None:
    await _seeded(sessionmaker)
    provider = FakeEmbedder()
    if failure == "embed":
        provider.fail_with = ModelTimeoutError("timeout talking to http://secret-host:1")
    elif failure == "capabilities":
        provider.capabilities_error = ModelTimeoutError("down")
    elif failure == "model":
        provider.result_model = "silently-swapped-model"
    else:
        provider.capability_model = None
    with pytest.raises(EmbeddingUnavailableError) as info:
        await reader(sessionmaker, provider).semantic_search(query="alpha", limit=5)
    assert "secret-host" not in str(info.value)


async def test_unreachable_provider_means_nothing_is_fresh(sessionmaker: SM) -> None:
    ids = await _seeded(sessionmaker)
    provider = FakeEmbedder()
    provider.capabilities_error = ModelTimeoutError("down")
    r = reader(sessionmaker, provider)
    assert (await r.list_similar(page_id=ids["/reports/a"], limit=5)).items == []
    coverage = await r.get_coverage()
    assert (coverage.embedding_model, coverage.embedded_fresh) == (NO_EMBEDDING_MODEL, 0)
    view = await r.get_page_intelligence(page_id=ids["/reports/a"])
    assert view.embedding is not None and not view.embedding_fresh


# --------------------------------------------------------------------------- #
# Relationships
# --------------------------------------------------------------------------- #
async def test_relationships_are_strongest_first_filterable_and_paged(sessionmaker: SM) -> None:
    ids = await _seeded(sessionmaker)
    r = reader(sessionmaker)
    edges = await r.list_relationships(page_id=ids["/reports/a"], limit=50, offset=0)
    assert [(e.relationship_type, e.target_page_id) for e in edges.items][:1] == [
        (RelationshipType.SAME_MARKET, ids["/reports/b"])
    ]
    weights = [e.weight for e in edges.items]
    assert weights == sorted(weights, reverse=True)
    assert edges.page.total == 2
    first = edges.items[0]
    assert (first.target_canonical_url, first.target_title) == (url("/reports/b"), "Title")
    assert (first.source_page_version, first.target_page_version) == (1, 1)
    assert first.analysis_version == AV
    only = await r.list_relationships(
        page_id=ids["/reports/a"],
        limit=50,
        offset=0,
        relationship_type=RelationshipType.SEMANTIC_SIMILAR,
    )
    assert [e.relationship_type for e in only.items] == [RelationshipType.SEMANTIC_SIMILAR]
    # A derived-only coarse type is never stored, so filtering by it finds nothing.
    coarse = await r.list_relationships(
        page_id=ids["/reports/a"],
        limit=50,
        offset=0,
        relationship_type=RelationshipType.SAME_GEOGRAPHY,
    )
    assert coarse.items == [] and coarse.page.total == 0
    page_two = await r.list_relationships(page_id=ids["/reports/a"], limit=1, offset=1)
    assert len(page_two.items) == 1 and page_two.page.total == 2
    assert page_two.items[0] == edges.items[1]
    with pytest.raises(ValueError):
        await r.list_relationships(page_id=ids["/reports/a"], limit=0, offset=0)


async def test_a_legacy_coarse_edge_is_never_served(sessionmaker: SM) -> None:
    """A coarse row left by an older build is ignored by counts, lists and coverage."""
    ids = await _seeded(sessionmaker)
    r = reader(sessionmaker)
    before = await r.get_coverage()
    async with sessionmaker() as session, session.begin():
        from sqlalchemy import text

        await session.execute(
            text(
                "INSERT INTO page_relationships (source_page_id, source_page_version, "
                "target_page_id, target_page_version, relationship_type, weight, "
                "analysis_version, computed_at) "
                "VALUES (:a, 1, :c, 1, 'industry_adjacency', 0.3, :av, now())"
            ),
            {"a": ids["/reports/a"], "c": ids["/reports/c"], "av": AV},
        )
    assert (
        await scalar(
            sessionmaker,
            "SELECT count(*) FROM page_relationships "
            "WHERE relationship_type = 'industry_adjacency'",
        )
        == 1
    )
    view = await r.get_page_intelligence(page_id=ids["/reports/a"])
    assert "industry_adjacency" not in view.relationship_counts
    listed = await r.list_relationships(page_id=ids["/reports/a"], limit=50, offset=0)
    assert ids["/reports/c"] not in {e.target_page_id for e in listed.items}
    assert (await r.get_coverage()).relationships == before.relationships


# --------------------------------------------------------------------------- #
# Recipe fingerprint (QA M1)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "change", [{"embedding_document_prefix": "passage: "}, {"embedding_max_chars": 5000}]
)
async def test_another_recipe_fingerprint_is_stale_and_never_compared(
    sessionmaker: SM, change: dict[str, Any]
) -> None:
    ids = await _seeded(sessionmaker)
    other = make_settings(
        similarity_top_k=3, similarity_min_score=0.5, embedding_batch_size=2, **change
    )
    r = reader(sessionmaker, settings=other)
    view = await r.get_page_intelligence(page_id=ids["/reports/a"])
    assert view.embedding is not None and not view.embedding_fresh
    assert view.embedding.recipe_fingerprint == current_recipe_fingerprint(SETTINGS)
    assert (await r.list_similar(page_id=ids["/reports/a"], limit=5)).items == []
    assert (await r.semantic_search(query="alpha alpha", limit=5)).items == []
    assert (await r.get_coverage()).embedded_fresh == 0

    # Re-embedding under the new recipe: only the new vectors are compared.
    await analyse(sessionmaker, settings=other)
    view = await r.get_page_intelligence(page_id=ids["/reports/a"])
    assert view.embedding_fresh
    assert view.embedding is not None
    assert view.embedding.recipe_fingerprint == current_recipe_fingerprint(other)
    similar = await r.list_similar(page_id=ids["/reports/a"], limit=10)
    assert [s.page_id for s in similar.items] == [ids["/reports/b"], ids["/reports/c"]]
    found = await r.semantic_search(query="alpha alpha", limit=10)
    assert len(found.items) == 3  # one hit per page: the old recipe's rows never appear
    assert (await r.get_coverage()).embedded_fresh == 3
    # The original settings still see their own (older) rows, not the new ones.
    original = await reader(sessionmaker).get_page_intelligence(page_id=ids["/reports/a"])
    assert original.embedding_fresh
    assert original.embedding is not None
    assert original.embedding.recipe_fingerprint == current_recipe_fingerprint(SETTINGS)


# --------------------------------------------------------------------------- #
# Taxonomy and coverage
# --------------------------------------------------------------------------- #
async def test_taxonomy_counts_pages_over_fresh_analyses(sessionmaker: SM) -> None:
    await _seeded(sessionmaker)
    view = await reader(sessionmaker).get_taxonomy()
    assert (view.version, view.analysis_version, view.source) == (7, AV, "fixture")
    markets = {m.id: (m.depth, m.page_count, m.kind) for m in view.markets}
    assert markets == {
        "automotive": (0, 0, "market"),
        "ev": (1, 2, "market"),
        "batteries": (0, 1, "market"),
    }
    geos = {g.id: (g.depth, g.page_count, g.kind) for g in view.geographies}
    assert geos == {
        "global": (0, 0, "global"),
        "asia": (1, 0, "region"),
        "india": (2, 2, "country"),
    }
    assert {t.id: t.page_count for t in view.page_types} == {"report": 3, "other": 0}


async def test_topic_pages_by_market_or_geography(sessionmaker: SM) -> None:
    ids = await _seeded(sessionmaker)
    r = reader(sessionmaker)
    ev = await r.list_topic_pages(node_id="ev", limit=50, offset=0)
    assert [p.page_id for p in ev.items] == [ids["/reports/a"], ids["/reports/b"]]
    assert all(p.score == 1.0 and p.market_id == "ev" for p in ev.items)
    india = await r.list_topic_pages(node_id="india", limit=1, offset=1)
    assert [p.page_id for p in india.items] == [ids["/reports/b"]]
    assert india.page.total == 2
    assert (await r.list_topic_pages(node_id="automotive", limit=5, offset=0)).items == []
    with pytest.raises(IntelligenceNotFoundError) as info:
        await r.list_topic_pages(node_id="unknown", limit=5, offset=0)
    assert info.value.resource == "taxonomy_node"


async def test_coverage(sessionmaker: SM) -> None:
    ids = await _seeded(sessionmaker)
    # A changes (stale analysis + embedding); E arrives and is never analysed.
    await seed(
        sessionmaker,
        page("/reports/a", "alpha market:ev", metadata={"lock": "yes"}),
        page("/reports/e", "delta"),
    )
    coverage = await reader(sessionmaker).get_coverage()
    assert coverage.total_pages == 4
    assert coverage.analysis_version == AV
    assert (coverage.analyzed_fresh, coverage.analyzed_stale, coverage.never_analyzed) == (2, 1, 1)
    assert (coverage.embedding_model, coverage.embedded_fresh) == (EMBED_MODEL, 2)
    assert coverage.embedded_stale_or_missing == 2
    assert coverage.protected == 1
    # Fake eligibility: B and C are clean; A is protected + stale; E not analysed.
    assert (coverage.eligible_sources, coverage.eligible_targets) == (2, 2)
    # Only edges whose endpoints are both current: B <-> C have none; A's are stale.
    assert coverage.relationships == 0
    assert coverage.by_page_type == {"report": 2}
    assert coverage.by_industry == {"automotive": 1, "batteries": 1}
    assert ids["/reports/a"] in ids.values()
