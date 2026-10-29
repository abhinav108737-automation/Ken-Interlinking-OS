"""Link-graph algorithms against graphs whose answers can be worked out by hand."""

from __future__ import annotations

import random
import uuid

import pytest

from ken_services.graph.algorithms import (
    LinkGraph,
    articulation_points,
    build_graph,
    cluster_ordinals,
    compute_metrics,
    crawl_depths,
    hits,
    pagerank,
    weak_components,
)


def n(i: int) -> uuid.UUID:
    return uuid.UUID(int=i)


def graph(nodes: list[int], edges: list[tuple[int, int]]) -> LinkGraph:
    return build_graph([n(i) for i in nodes], [(n(a), n(b)) for a, b in edges])


def rank(g: LinkGraph) -> dict[uuid.UUID, float]:
    return pagerank(g, damping=0.85, tolerance=1e-12, max_iterations=500)


def test_build_graph_dedupes_and_drops_self_links_and_unknown_pages() -> None:
    g = graph([1, 2], [(1, 2), (1, 2), (1, 1), (2, 99)])
    assert g.out[n(1)] == (n(2),) and g.out[n(2)] == ()
    assert g.inbound[n(2)] == (n(1),)
    assert g.edge_count == 1


def test_pagerank_sums_to_one_and_a_cycle_is_uniform() -> None:
    cycle = graph([1, 2, 3], [(1, 2), (2, 3), (3, 1)])
    ranks = rank(cycle)
    assert sum(ranks.values()) == pytest.approx(1.0)
    assert all(r == pytest.approx(1 / 3, abs=1e-9) for r in ranks.values())


def test_pagerank_rewards_the_page_everyone_links_to() -> None:
    star = graph([1, 2, 3, 4], [(2, 1), (3, 1), (4, 1)])
    ranks = rank(star)
    assert ranks[n(1)] > max(ranks[n(2)], ranks[n(3)], ranks[n(4)])
    assert ranks[n(2)] == pytest.approx(ranks[n(3)])
    assert sum(ranks.values()) == pytest.approx(1.0)


def test_pagerank_handles_one_node_and_empty() -> None:
    assert rank(graph([1], [])) == {n(1): 1.0}
    assert rank(graph([], [])) == {}


def test_hits_separates_hubs_from_authorities() -> None:
    g = graph([1, 2, 3], [(1, 2), (1, 3)])
    hub, auth = hits(g, tolerance=1e-12, max_iterations=200)
    assert hub[n(1)] == pytest.approx(1.0) and hub[n(2)] == 0.0
    assert auth[n(2)] == pytest.approx(1.0) and auth[n(3)] == pytest.approx(1.0)
    assert auth[n(1)] == 0.0


def test_hits_of_an_edgeless_graph_is_zero_not_nan() -> None:
    hub, auth = hits(graph([1, 2], []), tolerance=1e-12, max_iterations=50)
    assert set(hub.values()) == {0.0} and set(auth.values()) == {0.0}


def test_crawl_depth_is_clicks_from_the_entry_and_unreachable_is_absent() -> None:
    g = graph([1, 2, 3, 4], [(1, 2), (2, 3)])
    depth = crawl_depths(g, {n(1)})
    assert depth == {n(1): 0, n(2): 1, n(3): 2}  # 4 is unreachable
    assert crawl_depths(g, set()) == {}


def test_weak_components_largest_first() -> None:
    g = graph([1, 2, 3, 4, 5], [(1, 2), (3, 2), (4, 5)])
    comp = weak_components(g)
    assert comp[n(1)] == comp[n(2)] == comp[n(3)] == 0
    assert comp[n(4)] == comp[n(5)] == 1


def test_articulation_points() -> None:
    path = graph([1, 2, 3], [(1, 2), (2, 3)])
    assert articulation_points(path) == {n(2)}
    cycle = graph([1, 2, 3], [(1, 2), (2, 3), (3, 1)])
    assert articulation_points(cycle) == set()
    bowtie = graph([1, 2, 3, 4, 5], [(1, 2), (2, 3), (3, 1), (3, 4), (4, 5), (5, 3)])
    assert articulation_points(bowtie) == {n(3)}
    # A root with two children is a cut vertex; a root with one child is not.
    assert articulation_points(graph([1, 2, 3], [(1, 2), (1, 3)])) == {n(1)}
    assert articulation_points(graph([1, 2], [(1, 2)])) == set()


def test_clusters_largest_first_with_a_stable_key_tiebreak() -> None:
    keys = {n(1): "b", n(2): "b", n(3): "a", n(4): "c", n(5): None}
    out = cluster_ordinals(keys)
    assert out[n(1)] == out[n(2)] == 0
    assert (out[n(3)], out[n(4)]) == (1, 2)  # equal sizes: key order
    assert out[n(5)] is None


def _all(g: LinkGraph, **kw: object):  # type: ignore[no-untyped-def]
    return compute_metrics(
        g,
        entries=kw.get("entries", set()),  # type: ignore[arg-type]
        cluster_keys=kw.get("keys", {}),  # type: ignore[arg-type]
        damping=0.85,
        pagerank_tolerance=1e-12,
        pagerank_iterations=500,
        hits_tolerance=1e-12,
        hits_iterations=200,
        nearly_orphan_max_inbound=1,
    )


def test_orphan_nearly_orphan_and_the_schema_invariants() -> None:
    # 1 -> 2 -> 3, 1 -> 3, 4 isolated
    g = graph([1, 2, 3, 4], [(1, 2), (2, 3), (1, 3)])
    m = _all(g, entries={n(1)})
    assert (m[n(1)].is_orphan, m[n(4)].is_orphan) == (True, True)
    assert not m[n(2)].is_orphan and m[n(2)].is_nearly_orphan  # one inbound
    assert not m[n(3)].is_nearly_orphan  # two inbound
    assert (m[n(3)].in_degree, m[n(3)].out_degree) == (2, 0)
    assert m[n(4)].crawl_depth is None and m[n(3)].crawl_depth == 1
    for item in m.values():  # the DB CHECKs, asserted on the pure result
        assert item.is_orphan == (item.in_degree == 0)
        assert not item.is_orphan or item.is_nearly_orphan
        assert 0.0 <= item.internal_pagerank <= 1.0
        assert 0.0 <= item.hub_score <= 1.0 and 0.0 <= item.authority_score <= 1.0


def test_the_result_does_not_depend_on_input_order() -> None:
    nodes = list(range(1, 9))
    edges = [(1, 2), (2, 3), (3, 1), (3, 4), (4, 5), (6, 5), (7, 1), (7, 8)]
    base = _all(graph(nodes, edges), entries={n(1)}, keys={n(i): "k" for i in nodes})
    rng = random.Random(7)  # noqa: S311 - seeded shuffle, not security
    for _ in range(5):
        shuffled_nodes, shuffled_edges = nodes[:], edges[:]
        rng.shuffle(shuffled_nodes)
        rng.shuffle(shuffled_edges)
        again = _all(
            graph(shuffled_nodes, shuffled_edges), entries={n(1)}, keys={n(i): "k" for i in nodes}
        )
        assert again == base
