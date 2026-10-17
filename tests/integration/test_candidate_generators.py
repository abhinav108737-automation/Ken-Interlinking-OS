"""The six candidate generators against PostgreSQL (directive section 6; ADR-0017).

Each generator is exercised on its own and through ``retrieve``: every candidate records which
generators nominated it, the signals stay separate, semantic retrieval goes through
``VectorStore.search`` with stale hits dropped, and analytics can surface and block but never
alone justify.
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ken_contracts.analytics import AnalyticsSource, Dataset
from ken_contracts.recommendations import RetrievalChannel, RetrievalPolicy
from ken_core.providers.vectors.memory import MemoryVectorStore
from ken_services.graph.compute import run_graph_metrics
from ken_services.intelligence.embeddings import current_recipe_fingerprint
from ken_services.recommendations.channels import EmbeddingKey, retrieve
from ken_services.recommendations.generators import (
    AnalyticsCandidateGenerator,
    GenerationContext,
    GraphCandidateGenerator,
    OrphanRecoveryCandidateGenerator,
    SemanticCandidateGenerator,
)
from ken_services.vectors.index import PostgresVectorIndexer
from tests.conftest import make_settings
from tests.integration.analytics_support import page_csv, request_for, run_csv_import
from tests.integration.conftest import SITE
from tests.integration.graph_support import build, leave_clean, pages  # noqa: F401
from tests.integration.intelligence_support import AV, EMBED_MODEL, analyse, page, seed

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("migrated", "leave_clean")]

SM = async_sessionmaker[AsyncSession]
SETTINGS = make_settings(similarity_top_k=3, similarity_min_score=0.5, embedding_batch_size=2)
S, L, T, G, AN, OR = (
    RetrievalChannel.SEMANTIC,
    RetrievalChannel.LEXICAL,
    RetrievalChannel.TAXONOMY,
    RetrievalChannel.GRAPH,
    RetrievalChannel.ANALYTICS,
    RetrievalChannel.ORPHAN_RECOVERY,
)
KEY = EmbeddingKey(model=EMBED_MODEL, recipe_fingerprint=current_recipe_fingerprint(SETTINGS))


def ctx(
    source: uuid.UUID,
    *,
    store: MemoryVectorStore | None = None,
    industry: str | None = "automotive",
    **policy: Any,
) -> GenerationContext:
    return GenerationContext(
        source_page_id=source,
        retrieval=RetrievalPolicy(**policy),
        embedding=KEY,
        keywords=("alpha", "beta"),
        industry_id=industry,
        analysis_version=AV,
        vector_store=store,
    )


async def indexed_store(sm: SM) -> MemoryVectorStore:
    store = MemoryVectorStore(collection="ken_wsa_generators")
    await PostgresVectorIndexer(sessionmaker=sm, store=store, settings=SETTINGS).index()
    return store


async def test_semantic_generator_goes_through_the_vector_store(sessionmaker: SM) -> None:
    ids = await build(sessionmaker)
    store = await indexed_store(sessionmaker)
    async with sessionmaker() as session:
        shared = await SemanticCandidateGenerator().generate(
            session, ctx(ids["/reports/a"], store=store, semantic_k=3)
        )
        # No shared index: the same answer from an exact index built from PostgreSQL.
        fallback = await SemanticCandidateGenerator().generate(
            session, ctx(ids["/reports/a"], semantic_k=3)
        )
    assert [c.target_page_id for c in shared] == [c.target_page_id for c in fallback]
    assert len(shared) == 3 and ids["/reports/a"] not in {c.target_page_id for c in shared}
    assert shared[0].target_page_id == ids["/reports/b"]  # shares alpha/beta with a
    scores = [c.raw for c in shared]
    assert scores == sorted(scores, reverse=True)
    top = shared[0]
    assert top.signals == {"semantic_similarity": top.raw}
    assert set(top.evidence) == {"point_id", "content_hash", "page_version_id"}
    points = await PostgresVectorIndexer(
        sessionmaker=sessionmaker, store=store, settings=SETTINGS
    ).points_for(page_ids=[ids["/reports/b"]])
    assert top.evidence["point_id"] == str(points[0].point_id)


async def test_a_stale_vector_hit_is_dropped_never_used(sessionmaker: SM) -> None:
    ids = await build(sessionmaker)
    store = await indexed_store(sessionmaker)
    # Page b changes (v2 analysed and embedded); the index still holds the v1 point.
    await seed(sessionmaker, *pages()[:2], page("/reports/b", "gamma gamma beta market:ev"))
    await analyse(sessionmaker)
    async with sessionmaker() as session:
        found = await SemanticCandidateGenerator().generate(
            session, ctx(ids["/reports/a"], store=store, semantic_k=5)
        )
    assert ids["/reports/b"] not in {c.target_page_id for c in found}


async def test_graph_generator_falls_back_to_sql_then_uses_metrics(sessionmaker: SM) -> None:
    ids = await build(sessionmaker)
    source = ids["/reports/a"]
    async with sessionmaker() as session:
        before = await GraphCandidateGenerator().generate(session, ctx(source, graph_k=10))
    assert before and all(c.evidence["source"] == "sql_inbound" for c in before)
    await run_graph_metrics(sessionmaker=sessionmaker, triggered_by="test")
    async with sessionmaker() as session:
        after = await GraphCandidateGenerator().generate(session, ctx(source, graph_k=10))
    # Lowest in-degree first among automotive pages other than a: d(0), c(1), b(2).
    assert [c.target_page_id for c in after] == [
        ids["/reports/d"],
        ids["/reports/c"],
        ids["/reports/b"],
    ]
    assert [c.raw for c in after] == [1.0, 0.5, pytest.approx(1 / 3)]
    first = after[0]
    assert first.evidence == {"source": "page_graph_metrics", "metrics_version": "graph-alg1-pol1"}
    assert {"graph_in_degree", "graph_pagerank", "graph_authority", "graph_opportunity"} <= set(
        first.signals
    )


async def test_orphan_recovery_targets_orphans_near_the_source(sessionmaker: SM) -> None:
    ids = await build(sessionmaker)
    await run_graph_metrics(sessionmaker=sessionmaker, triggered_by="test")
    async with sessionmaker() as session:
        found = await OrphanRecoveryCandidateGenerator().generate(
            session, ctx(ids["/reports/a"], orphan_recovery_k=10)
        )
        inert = await OrphanRecoveryCandidateGenerator().generate(session, ctx(ids["/reports/a"]))
    assert inert == []  # orphan_recovery_k defaults to 0
    # d is an orphan (in 0); c is nearly orphaned (in 1). Importance 2 -> priority 2/3.
    assert [c.target_page_id for c in found] == [ids["/reports/d"], ids["/reports/c"]]
    priority = 2 / 3
    assert found[0].raw == pytest.approx(1.0 * (0.5 + 0.5 * priority))
    assert found[1].raw == pytest.approx(0.5 * (0.5 + 0.5 * priority))
    assert found[0].evidence["is_orphan"] is True and found[1].evidence["is_orphan"] is False
    assert found[0].signals["destination_priority"] == pytest.approx(priority)
    assert set(found[0].signals) == {
        "orphan_recovery_value",
        "destination_priority",
        "graph_in_degree",
    }


async def import_search_console(sm: SM, tmp_path: Path) -> None:
    rows = [
        f"2026-05-11,search_console,{SITE}/reports/c,1000,10,3.0,0,0",
        f"2026-05-11,search_console,{SITE}/reports/b,500,250,2.0,0,0",
        f"2026-05-11,search_console,{SITE}/reports/d,100,0,9.0,0,0",
    ]
    await run_csv_import(
        sm,
        page_csv(tmp_path, rows),
        request=request_for(
            source=AnalyticsSource.SEARCH_CONSOLE,
            dataset=Dataset.PAGES,
            start="2026-05-11",
            end="2026-05-11",
        ),
    )


async def test_analytics_generator_surfaces_demand_without_supply(
    sessionmaker: SM, tmp_path: Path
) -> None:
    ids = await build(sessionmaker)
    await import_search_console(sessionmaker, tmp_path)
    async with sessionmaker() as session:
        found = await AnalyticsCandidateGenerator().generate(
            session, ctx(ids["/reports/a"], analytics_k=10)
        )
        inert = await AnalyticsCandidateGenerator().generate(session, ctx(ids["/reports/a"]))
    assert inert == []  # analytics_k defaults to 0
    # opportunity = (impressions / max) x (1 - clicks / impressions)
    assert [c.target_page_id for c in found] == [
        ids["/reports/c"],
        ids["/reports/b"],
        ids["/reports/d"],
    ]
    assert [round(c.raw, 4) for c in found] == [0.99, 0.25, 0.1]
    assert found[0].signals == {"gsc_opportunity": pytest.approx(0.99), "ga4_engagement": 0.0}
    assert found[0].evidence["impressions"] == 1000 and found[0].evidence["synthetic"] is False


async def test_analytics_alone_cannot_justify_a_candidate_but_support_lets_it_through(
    sessionmaker: SM, tmp_path: Path
) -> None:
    ids = await build(sessionmaker)
    await import_search_console(sessionmaker, tmp_path)
    policy = {"channels_enabled": (AN,), "analytics_k": 10}
    async with sessionmaker() as session:
        # The home page has no market, hence no stored taxonomy relationship to anything:
        # analytics surfaces c, b, d but NOTHING supports them -> all blocked.
        blocked = await retrieve(
            session,
            source_page_id=ids["/"],
            retrieval=RetrievalPolicy(**policy),
            embedding=None,
            keywords=(),
            industry_id=None,
            analysis_version=AV,
        )
        # Page a shares a market with b, c, d: the stored SAME_MARKET relationship supports
        # them, so they survive -- and the attribution still says analytics found them.
        kept = await retrieve(
            session,
            source_page_id=ids["/reports/a"],
            retrieval=RetrievalPolicy(**policy),
            embedding=None,
            keywords=(),
            industry_id="automotive",
            analysis_version=AV,
        )
    assert blocked == []
    assert {c.target_page_id for c in kept} == {
        ids["/reports/b"],
        ids["/reports/c"],
        ids["/reports/d"],
    }
    for candidate in kept:
        assert candidate.channels == (AN,)
        assert candidate.attribution[0].evidence["window_days"] == 28
        assert candidate.signals["gsc_opportunity"] > 0


async def test_only_semantic_support_above_the_floor_lets_analytics_candidates_through(
    sessionmaker: SM, tmp_path: Path
) -> None:
    """Page e (batteries) has no stored taxonomy tie to the ev pages. d has exactly e's word
    counts (cosine 1.0); b and c are unrelated vectors."""
    ids = await build(sessionmaker)
    await import_search_console(sessionmaker, tmp_path)

    async def run(floor: float | None) -> list[object]:
        async with sessionmaker() as session:
            return list(
                await retrieve(
                    session,
                    source_page_id=ids["/reports/e"],
                    retrieval=RetrievalPolicy(channels_enabled=(AN,), analytics_k=10),
                    embedding=KEY,
                    keywords=(),
                    industry_id="batteries",
                    analysis_version=AV,
                    semantic_floor=floor,
                )
            )

    assert await run(None) == []  # no floor and no taxonomy tie: nothing supports anything
    supported = await run(0.99)
    assert [c.target_page_id for c in supported] == [ids["/reports/d"]]  # type: ignore[attr-defined]
    assert supported[0].channels == (AN,)  # type: ignore[attr-defined]  # still attributed to analytics


async def test_retrieve_records_every_nominating_generator_and_keeps_signals_separate(
    sessionmaker: SM,
) -> None:
    ids = await build(sessionmaker)
    await run_graph_metrics(sessionmaker=sessionmaker, triggered_by="test")
    store = await indexed_store(sessionmaker)
    policy = RetrievalPolicy(
        channels_enabled=(S, L, T, G, OR),
        semantic_k=5,
        lexical_k=5,
        graph_k=5,
        orphan_recovery_k=5,
    )
    async with sessionmaker() as session:
        kept = await retrieve(
            session,
            source_page_id=ids["/reports/a"],
            retrieval=policy,
            embedding=KEY,
            keywords=("alpha", "beta"),
            industry_id="automotive",
            analysis_version=AV,
            vector_store=store,
        )
    by_target = {c.target_page_id: c for c in kept}
    d = by_target[ids["/reports/d"]]
    assert {OR, G, T} <= set(d.channels)  # orphan, under-linked and same-market
    assert [a.channel for a in d.attribution] == list(d.channels)
    assert all(a.rank_in_channel >= 1 and 0.0 <= a.normalised <= 1.0 for a in d.attribution)
    # The separate signals sit side by side, never summed into one number.
    assert {
        "semantic_similarity",
        "topic_relevance",
        "orphan_recovery_value",
        "destination_priority",
        "graph_in_degree",
    } <= set(d.signals)
    assert d.graph_signals is not None and d.graph_signals.is_orphan
    assert d.graph_signals.metrics_version == "graph-alg1-pol1"
    b = by_target[ids["/reports/b"]]
    assert S in b.channels and "semantic_similarity" in b.signals
    assert b.channel_scores["semantic"] == b.signals["semantic_similarity"]  # raw values agree


async def test_the_crawl_entry_page_is_never_an_orphan_recovery_target(sessionmaker: SM) -> None:
    """The home page has no inbound link (so the schema marks it an orphan) but it is the
    crawl entry, not a page to recover -- even when it sits in the source's market."""
    from tests.integration.conftest import make_link, url

    await seed(
        sessionmaker,
        page("/", "home alpha market:ev", links=(make_link(0, url("/reports/a")),)),
        page("/reports/a", "alpha alpha beta market:ev"),
        page("/reports/d", "delta delta gamma market:ev"),  # a genuine orphan in the market
    )
    await analyse(sessionmaker)
    await run_graph_metrics(sessionmaker=sessionmaker, triggered_by="test")
    from tests.integration.intelligence_support import page_ids

    ids = await page_ids(sessionmaker)
    async with sessionmaker() as session:
        found = await OrphanRecoveryCandidateGenerator().generate(
            session, ctx(ids["/reports/a"], orphan_recovery_k=10)
        )
    assert [c.target_page_id for c in found] == [ids["/reports/d"]]


async def test_the_analytics_lookback_is_the_one_the_caller_passes(
    sessionmaker: SM, tmp_path: Path
) -> None:
    """The versioned analytics policy's window reaches the generator and is recorded."""
    import dataclasses

    ids = await build(sessionmaker)
    await import_search_console(sessionmaker, tmp_path)
    base = ctx(ids["/reports/a"], analytics_k=10)
    async with sessionmaker() as session:
        default = await AnalyticsCandidateGenerator().generate(session, base)
        narrow = await AnalyticsCandidateGenerator().generate(
            session, dataclasses.replace(base, analytics_window_days=7)
        )
    assert {c.evidence["window_days"] for c in default} == {28}  # the documented fallback
    assert {c.evidence["window_days"] for c in narrow} == {7}
    assert [c.target_page_id for c in narrow] == [c.target_page_id for c in default]
