"""Deterministic link-graph algorithms in application memory (directive section 5).

Pure functions over a :class:`LinkGraph`; no database, no clock, no randomness, no third-party
numeric library. There is no Neo4j and no learned model here. Determinism comes from ONE rule:
every iteration walks nodes in sorted-id order and every tie is broken by id, so the same graph
yields byte-identical numbers on every run (no seed is needed because nothing is random).

Definitions (recorded as ``metrics_version``; bump ``GRAPH_ALGORITHM_VERSION`` on any change):

* **Edges** are DISTINCT directed page pairs (many links between the same two pages are one
  edge); self-links and links to unknown pages are dropped. ``in_degree`` / ``out_degree`` count
  those distinct linking / linked pages.
* **PageRank**: damped power iteration, dangling mass spread uniformly; sums to 1.
* **Hub / authority**: Kleinberg's HITS by power iteration, each vector scaled so its maximum is
  1 (an unconnected graph scores 0, not NaN).
* **Crawl depth**: shortest directed click path from the entry page(s); ``None`` = unreachable.
* **Components**: WEAKLY connected; ordinal 0 is the largest (ties by smallest member id).
* **Bridge pages**: articulation points of the undirected graph -- removing the page would
  split its component.
* **Orphan**: no distinct inbound page. **Nearly orphan**: at most ``nearly_orphan_max_inbound``.
"""

from __future__ import annotations

import math
import uuid
from collections import deque
from collections.abc import Collection, Iterable, Mapping
from dataclasses import dataclass
from typing import Final

__all__ = [
    "LinkGraph",
    "NodeMetrics",
    "articulation_points",
    "build_graph",
    "cluster_ordinals",
    "compute_metrics",
    "crawl_depths",
    "hits",
    "pagerank",
    "weak_components",
]

_NodeId = uuid.UUID
_EPS: Final = 1e-15


@dataclass(frozen=True, slots=True)
class LinkGraph:
    """Nodes in sorted order and deduplicated adjacency (successors / predecessors sorted)."""

    nodes: tuple[_NodeId, ...]
    out: Mapping[_NodeId, tuple[_NodeId, ...]]
    inbound: Mapping[_NodeId, tuple[_NodeId, ...]]

    @property
    def edge_count(self) -> int:
        return sum(len(targets) for targets in self.out.values())


def build_graph(nodes: Iterable[_NodeId], edges: Iterable[tuple[_NodeId, _NodeId]]) -> LinkGraph:
    """Normalise to a simple directed graph over ``nodes`` (see module docstring)."""
    ordered = tuple(sorted(set(nodes), key=str))
    known = set(ordered)
    out: dict[_NodeId, set[_NodeId]] = {n: set() for n in ordered}
    inbound: dict[_NodeId, set[_NodeId]] = {n: set() for n in ordered}
    for source, target in edges:
        if source == target or source not in known or target not in known:
            continue
        out[source].add(target)
        inbound[target].add(source)
    return LinkGraph(
        nodes=ordered,
        out={n: tuple(sorted(out[n], key=str)) for n in ordered},
        inbound={n: tuple(sorted(inbound[n], key=str)) for n in ordered},
    )


# --------------------------------------------------------------------------- #
def pagerank(
    graph: LinkGraph, *, damping: float, tolerance: float, max_iterations: int
) -> dict[_NodeId, float]:
    n = len(graph.nodes)
    if n == 0:
        return {}
    rank = dict.fromkeys(graph.nodes, 1.0 / n)
    for _ in range(max_iterations):
        dangling = math.fsum(rank[v] for v in graph.nodes if not graph.out[v])
        base = (1.0 - damping) / n + damping * dangling / n
        nxt: dict[_NodeId, float] = {}
        for v in graph.nodes:
            share = math.fsum(rank[u] / len(graph.out[u]) for u in graph.inbound[v])
            nxt[v] = base + damping * share
        delta = math.fsum(abs(nxt[v] - rank[v]) for v in graph.nodes)
        rank = nxt
        if delta < tolerance:
            break
    total = math.fsum(rank.values())
    return {v: min(1.0, max(0.0, rank[v] / total)) for v in graph.nodes}


def hits(
    graph: LinkGraph, *, tolerance: float, max_iterations: int
) -> tuple[dict[_NodeId, float], dict[_NodeId, float]]:
    """(hub, authority), each scaled to a maximum of 1 (all zeros when there are no edges)."""
    n = len(graph.nodes)
    if n == 0:
        return {}, {}
    hub = dict.fromkeys(graph.nodes, 1.0)
    auth = dict.fromkeys(graph.nodes, 1.0)

    def normalise(vector: dict[_NodeId, float]) -> dict[_NodeId, float]:
        norm = math.sqrt(math.fsum(x * x for x in vector.values()))
        return (
            dict.fromkeys(vector, 0.0) if norm < _EPS else {k: x / norm for k, x in vector.items()}
        )

    for _ in range(max_iterations):
        new_auth = normalise({v: math.fsum(hub[u] for u in graph.inbound[v]) for v in graph.nodes})
        new_hub = normalise({v: math.fsum(new_auth[w] for w in graph.out[v]) for v in graph.nodes})
        delta = math.fsum(
            abs(new_hub[v] - hub[v]) + abs(new_auth[v] - auth[v]) for v in graph.nodes
        )
        hub, auth = new_hub, new_auth
        if delta < tolerance:
            break

    def scale(vector: dict[_NodeId, float]) -> dict[_NodeId, float]:
        top = max(vector.values())
        return (
            dict.fromkeys(vector, 0.0)
            if top < _EPS
            else {k: min(1.0, x / top) for k, x in vector.items()}
        )

    return scale(hub), scale(auth)


def crawl_depths(graph: LinkGraph, entries: Collection[_NodeId]) -> dict[_NodeId, int]:
    """Clicks from the nearest entry page along directed links; unreachable nodes are absent."""
    depth: dict[_NodeId, int] = {}
    queue: deque[_NodeId] = deque()
    for entry in sorted(set(entries) & set(graph.nodes), key=str):
        depth[entry] = 0
        queue.append(entry)
    while queue:
        node = queue.popleft()
        for nxt in graph.out[node]:
            if nxt not in depth:
                depth[nxt] = depth[node] + 1
                queue.append(nxt)
    return depth


def _undirected(graph: LinkGraph) -> dict[_NodeId, tuple[_NodeId, ...]]:
    return {
        v: tuple(sorted(set(graph.out[v]) | set(graph.inbound[v]), key=str)) for v in graph.nodes
    }


def weak_components(graph: LinkGraph) -> dict[_NodeId, int]:
    """Weakly connected component ordinal per node; 0 = largest, ties by smallest member id."""
    adjacency = _undirected(graph)
    seen: set[_NodeId] = set()
    groups: list[list[_NodeId]] = []
    for start in graph.nodes:
        if start in seen:
            continue
        seen.add(start)
        stack = [start]
        members = [start]
        while stack:
            node = stack.pop()
            for nxt in adjacency[node]:
                if nxt not in seen:
                    seen.add(nxt)
                    stack.append(nxt)
                    members.append(nxt)
        groups.append(sorted(members, key=str))
    groups.sort(key=lambda g: (-len(g), str(g[0])))
    return {node: ordinal for ordinal, members in enumerate(groups) for node in members}


def articulation_points(graph: LinkGraph) -> set[_NodeId]:
    """Cut vertices of the undirected graph (iterative Tarjan: no recursion limit)."""
    adjacency = _undirected(graph)
    index: dict[_NodeId, int] = {}
    low: dict[_NodeId, int] = {}
    cut: set[_NodeId] = set()
    counter = 0
    for root in graph.nodes:
        if root in index:
            continue
        index[root] = low[root] = counter
        counter += 1
        root_children = 0
        stack: list[tuple[_NodeId, _NodeId | None, int]] = [(root, None, 0)]
        while stack:
            node, parent, position = stack.pop()
            if position < len(adjacency[node]):
                stack.append((node, parent, position + 1))
                nxt = adjacency[node][position]
                if nxt == parent:
                    continue
                if nxt in index:
                    low[node] = min(low[node], index[nxt])
                else:
                    index[nxt] = low[nxt] = counter
                    counter += 1
                    if node == root:
                        root_children += 1
                    stack.append((nxt, node, 0))
            elif parent is not None:
                low[parent] = min(low[parent], low[node])
                if parent != root and low[node] >= index[parent]:
                    cut.add(parent)
        if root_children > 1:
            cut.add(root)
    return cut


def cluster_ordinals(keys: Mapping[_NodeId, str | None]) -> dict[_NodeId, int | None]:
    """Cluster ordinal per node from a topical key: largest cluster first, ties by key.
    A node without a key is in no cluster (``None``)."""
    sizes: dict[str, int] = {}
    for key in keys.values():
        if key is not None:
            sizes[key] = sizes.get(key, 0) + 1
    order = sorted(sizes, key=lambda k: (-sizes[k], k))
    ordinal = {key: i for i, key in enumerate(order)}
    return {node: (None if key is None else ordinal[key]) for node, key in keys.items()}


# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class NodeMetrics:
    in_degree: int
    out_degree: int
    internal_pagerank: float
    hub_score: float
    authority_score: float
    crawl_depth: int | None
    is_orphan: bool
    is_nearly_orphan: bool
    is_bridge: bool
    component_id: int
    cluster_id: int | None


def compute_metrics(
    graph: LinkGraph,
    *,
    entries: Collection[_NodeId],
    cluster_keys: Mapping[_NodeId, str | None],
    damping: float,
    pagerank_tolerance: float,
    pagerank_iterations: int,
    hits_tolerance: float,
    hits_iterations: int,
    nearly_orphan_max_inbound: int,
) -> dict[_NodeId, NodeMetrics]:
    """Every metric for every node. Pure and deterministic."""
    ranks = pagerank(
        graph, damping=damping, tolerance=pagerank_tolerance, max_iterations=pagerank_iterations
    )
    hub, auth = hits(graph, tolerance=hits_tolerance, max_iterations=hits_iterations)
    depths = crawl_depths(graph, entries)
    components = weak_components(graph)
    bridges = articulation_points(graph)
    clusters = cluster_ordinals({n: cluster_keys.get(n) for n in graph.nodes})
    out: dict[_NodeId, NodeMetrics] = {}
    for node in graph.nodes:
        inbound = len(graph.inbound[node])
        out[node] = NodeMetrics(
            in_degree=inbound,
            out_degree=len(graph.out[node]),
            internal_pagerank=ranks[node],
            hub_score=hub[node],
            authority_score=auth[node],
            crawl_depth=depths.get(node),
            is_orphan=inbound == 0,
            is_nearly_orphan=inbound <= nearly_orphan_max_inbound,
            is_bridge=node in bridges,
            component_id=components[node],
            cluster_id=clusters[node],
        )
    return out
