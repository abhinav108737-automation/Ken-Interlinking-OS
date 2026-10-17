"""``make graph-metrics`` against PostgreSQL: the metrics computed on a hand-checkable graph,
persisted to ``page_graph_metrics``, replaced (never duplicated) on re-run."""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ken_contracts.intelligence import IntelligenceNotFoundError
from ken_contracts.inventory import RunStatus
from ken_contracts.jobs import JobKind
from ken_services.graph.compute import run_graph_metrics
from ken_services.graph.policy import load_graph_policy
from ken_services.graph.reader import SqlGraphReader
from tests.integration.graph_support import build, leave_clean  # noqa: F401

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("migrated", "leave_clean")]

SM = async_sessionmaker[AsyncSession]


async def _rows(sm: SM) -> dict[str, dict[str, Any]]:
    async with sm() as session:
        result = await session.execute(
            text(
                "SELECT replace(p.canonical_url, 'https://site.test', '') AS path, m.* "
                "FROM page_graph_metrics m JOIN pages p ON p.id = m.page_id"
            )
        )
        return {row["path"]: dict(row) for row in result.mappings()}


async def test_the_metrics_match_the_hand_computed_graph(sessionmaker: SM) -> None:
    await build(sessionmaker)
    detail, result = await run_graph_metrics(sessionmaker=sessionmaker, triggered_by="test")
    assert detail.kind is JobKind.GRAPH_METRICS and detail.status is RunStatus.SUCCEEDED
    assert result is not None and result.metrics_version == load_graph_policy().metrics_version
    rows = await _rows(sessionmaker)
    assert set(rows) == {"/", "/reports/a", "/reports/b", "/reports/c", "/reports/d", "/reports/e"}

    degrees = {path: (r["in_degree"], r["out_degree"]) for path, r in rows.items()}
    assert degrees == {
        "/": (0, 2),
        "/reports/a": (2, 1),
        "/reports/b": (2, 1),
        "/reports/c": (1, 0),
        "/reports/d": (0, 1),
        "/reports/e": (0, 0),
    }
    assert {p for p, r in rows.items() if r["is_orphan"]} == {"/", "/reports/d", "/reports/e"}
    assert {p for p, r in rows.items() if r["is_nearly_orphan"]} == {
        "/",
        "/reports/c",
        "/reports/d",
        "/reports/e",
    }
    # Crawl depth: clicks from the home page; d and e are unreachable from it.
    assert {p: r["crawl_depth"] for p, r in rows.items()} == {
        "/": 0,
        "/reports/a": 1,
        "/reports/b": 1,
        "/reports/c": 2,
        "/reports/d": None,
        "/reports/e": None,
    }
    # Removing a would cut d off; removing b would cut c off.
    assert {p for p, r in rows.items() if r["is_bridge"]} == {"/reports/a", "/reports/b"}
    # One big weakly-connected component (/ a b c d) and the isolated e.
    components = {p: r["component_id"] for p, r in rows.items()}
    assert (
        len({components[p] for p in ("/", "/reports/a", "/reports/b", "/reports/c", "/reports/d")})
        == 1
    )
    assert components["/"] == 0 and components["/reports/e"] == 1
    # Topical clusters: the four ev pages together, e alone, the home page in none.
    clusters = {p: r["cluster_id"] for p, r in rows.items()}
    assert clusters["/reports/a"] == clusters["/reports/b"] == clusters["/reports/c"] == 0
    assert clusters["/reports/d"] == 0 and clusters["/reports/e"] == 1 and clusters["/"] is None
    # PageRank sums to 1; the page with the most inbound weight beats the orphans.
    ranks = {p: float(r["internal_pagerank"]) for p, r in rows.items()}
    assert sum(ranks.values()) == pytest.approx(1.0, abs=1e-6)
    assert ranks["/reports/b"] > ranks["/reports/d"] and ranks["/reports/c"] > ranks["/"]
    assert all(0.0 <= float(r["hub_score"]) <= 1.0 for r in rows.values())
    assert float(rows["/reports/b"]["authority_score"]) > float(
        rows["/reports/d"]["authority_score"]
    )
    assert float(rows["/"]["hub_score"]) == pytest.approx(1.0)  # links to the most authorities
    assert detail.counters["rows_written"] == 6 and detail.counters["orphans"] == 3


async def test_rerunning_replaces_rows_and_never_duplicates_them(sessionmaker: SM) -> None:
    await build(sessionmaker)
    await run_graph_metrics(sessionmaker=sessionmaker, triggered_by="test")
    first = await _rows(sessionmaker)
    detail, _ = await run_graph_metrics(sessionmaker=sessionmaker, triggered_by="test")
    second = await _rows(sessionmaker)
    assert detail.status is RunStatus.SUCCEEDED and len(second) == 6

    def values(rows: dict[str, dict[str, Any]]) -> dict[str, tuple[object, ...]]:
        keep = (
            "in_degree",
            "out_degree",
            "internal_pagerank",
            "hub_score",
            "authority_score",
            "crawl_depth",
            "is_orphan",
            "is_nearly_orphan",
            "is_bridge",
            "component_id",
            "cluster_id",
            "metrics_version",
        )
        return {p: tuple(r[k] for k in keep) for p, r in rows.items()}

    assert values(first) == values(second)  # deterministic: identical numbers


async def test_a_different_metrics_version_coexists_and_is_never_edited(sessionmaker: SM) -> None:
    await build(sessionmaker)
    await run_graph_metrics(sessionmaker=sessionmaker, triggered_by="test")
    policy = load_graph_policy().model_copy(update={"version": 2})
    await run_graph_metrics(sessionmaker=sessionmaker, triggered_by="test", policy=policy)
    async with sessionmaker() as session:
        versions = (
            await session.execute(
                text(
                    "SELECT metrics_version, count(*) FROM page_graph_metrics GROUP BY 1 ORDER BY 1"
                )
            )
        ).all()
    assert versions == [("graph-alg1-pol1", 6), ("graph-alg1-pol2", 6)]


async def test_a_new_page_version_and_a_removed_link_change_the_metrics(sessionmaker: SM) -> None:
    from tests.integration.graph_support import pages
    from tests.integration.intelligence_support import analyse, page, seed

    await build(sessionmaker)
    await run_graph_metrics(sessionmaker=sessionmaker, triggered_by="test")
    # d stops linking to a: a loses an inbound page; the corpus is otherwise unchanged.
    await seed(sessionmaker, *pages()[:4], page("/reports/d", "delta delta delta gamma market:ev"))
    await analyse(sessionmaker)
    await run_graph_metrics(sessionmaker=sessionmaker, triggered_by="test")
    rows = await _rows(sessionmaker)
    assert rows["/reports/a"]["in_degree"] == 1 and len(rows) == 6
    assert rows["/reports/d"]["out_degree"] == 0


async def test_the_reader_serves_signals_overview_and_clusters(sessionmaker: SM) -> None:
    ids = await build(sessionmaker)
    await run_graph_metrics(sessionmaker=sessionmaker, triggered_by="test")
    reader = SqlGraphReader(sessionmaker)
    signal = await reader.signal(page_id=ids["/reports/b"])
    assert signal is not None
    assert (signal.in_degree, signal.out_degree, signal.is_bridge) == (2, 1, True)
    assert signal.metrics_version == "graph-alg1-pol1" and signal.crawl_depth == 1
    assert await reader.signals(page_ids=[]) == {}

    overview = await reader.get_overview()
    assert overview.metrics_version == "graph-alg1-pol1" and overview.computed_at is not None
    # Three orphans (/, d, e) -- but "/" is the crawl ENTRY, so only d and e are TRUE orphans.
    assert (overview.pages, overview.entry_pages) == (6, 1)
    assert (overview.orphans, overview.true_orphans) == (3, 2)
    assert (overview.nearly_orphans, overview.bridges) == (4, 2)
    assert (overview.components, overview.clusters) == (2, 2)

    by_id = {c.cluster_id: c for c in await reader.list_clusters()}
    assert by_id[0].pages == 4 and by_id[0].orphans == 1 and by_id[0].true_orphans == 1
    assert by_id[1].pages == 1 and by_id[1].true_orphans == 1
    # The topic-less group is the home page: an orphan, but not a TRUE orphan.
    assert by_id[None].pages == 1 and by_id[None].orphans == 1 and by_id[None].true_orphans == 0
    rows = await _rows(sessionmaker)
    ev = ("/reports/a", "/reports/b", "/reports/c", "/reports/d")
    best = max(ev, key=lambda path: (float(rows[path]["internal_pagerank"]), path))
    assert by_id[0].top_page_id == ids[best]  # highest PageRank in the cluster
    assert by_id[0].top_page_url == f"https://site.test{best}"
    order = [c.cluster_id for c in await reader.list_clusters()]
    assert order == [0, 1, None]  # largest first, the topic-less group last


async def test_the_entry_page_is_an_orphan_but_is_flagged_as_the_entry(sessionmaker: SM) -> None:
    ids = await build(sessionmaker)
    await run_graph_metrics(sessionmaker=sessionmaker, triggered_by="test")
    reader = SqlGraphReader(sessionmaker)
    home = await reader.get_page(page_id=ids["/"])
    lonely = await reader.get_page(page_id=ids["/reports/d"])
    # Faithful to the schema CHECK: both have no inbound link ...
    assert home.signals.is_orphan and lonely.signals.is_orphan
    # ... and crawl_depth tells the entry from a true orphan.
    assert (home.signals.crawl_depth, home.is_entry_page) == (0, True)
    assert (lonely.signals.crawl_depth, lonely.is_entry_page) == (None, False)


async def test_an_unknown_page_and_an_uncomputed_graph_are_a_404_not_a_guess(
    sessionmaker: SM,
) -> None:
    reader = SqlGraphReader(sessionmaker)
    # Nothing computed yet: empty, honest views.
    overview = await reader.get_overview()
    assert overview.metrics_version is None and overview.pages == 0
    assert await reader.list_clusters() == []
    empty = await reader.list_pages(limit=10, offset=0)
    assert empty.items == [] and empty.page.total == 0
    ids = await build(sessionmaker)
    with pytest.raises(IntelligenceNotFoundError):
        await reader.get_page(page_id=ids["/"])  # a real page, but metrics not computed yet
    await run_graph_metrics(sessionmaker=sessionmaker, triggered_by="test")
    with pytest.raises(IntelligenceNotFoundError):
        await reader.get_page(page_id=uuid.uuid4())


async def test_an_empty_corpus_is_a_clean_empty_run(sessionmaker: SM) -> None:
    detail, result = await run_graph_metrics(sessionmaker=sessionmaker, triggered_by="test")
    assert detail.status is RunStatus.SUCCEEDED and result is not None
    assert detail.counters["pages"] == 0 and detail.counters["rows_written"] == 0
    assert (await SqlGraphReader(sessionmaker).get_overview()).pages == 0


async def test_list_pages_filters_orders_and_paginates(sessionmaker: SM) -> None:
    ids = await build(sessionmaker)
    await run_graph_metrics(sessionmaker=sessionmaker, triggered_by="test")
    reader = SqlGraphReader(sessionmaker)

    everything = await reader.list_pages(limit=50, offset=0)
    assert everything.page.total == 6 and len(everything.items) == 6
    ranks = [row.signals.internal_pagerank for row in everything.items]
    assert ranks == sorted(ranks, reverse=True)  # most authoritative first

    orphans = await reader.list_pages(limit=50, offset=0, orphans_only=True)
    assert orphans.page.total == 3
    assert {row.canonical_url.removeprefix("https://site.test") for row in orphans.items} == {
        "/",
        "/reports/d",
        "/reports/e",
    }
    assert {
        row.canonical_url.removeprefix("https://site.test")
        for row in orphans.items
        if row.is_entry_page
    } == {"/"}
    bridges = await reader.list_pages(limit=50, offset=0, bridges_only=True)
    assert {row.signals.page_id for row in bridges.items} == {ids["/reports/a"], ids["/reports/b"]}
    cluster = await reader.list_pages(limit=50, offset=0, cluster_id=0)
    assert cluster.page.total == 4 and all(r.signals.cluster_id == 0 for r in cluster.items)
    component = await reader.list_pages(limit=50, offset=0, component_id=1)
    assert [r.signals.page_id for r in component.items] == [ids["/reports/e"]]

    first = await reader.list_pages(limit=2, offset=0)
    second = await reader.list_pages(limit=2, offset=2)
    assert [r.signals.page_id for r in first.items + second.items] == [
        r.signals.page_id for r in everything.items[:4]
    ]  # pages tile the list without gaps or repeats
    assert second.page.total == 6 and second.page.offset == 2
    nothing = await reader.list_pages(limit=5, offset=0, cluster_id=99)
    assert nothing.items == [] and nothing.page.total == 0
