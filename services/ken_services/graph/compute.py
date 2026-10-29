"""``make graph-metrics``: compute the link-graph metrics and persist them to
``page_graph_metrics`` (directive section 5), as a recorded ``graph_metrics`` job run.

Read -> compute in application memory -> replace, in one transaction per run:

1. load live pages, current internal edges and topical cluster keys (repositories/graph.py);
2. :func:`~ken_services.graph.algorithms.compute_metrics` under the versioned graph policy;
3. replace every row of this ``metrics_version`` and commit.

Deterministic: the same corpus gives the same rows. No Neo4j, no learned model.
"""

from __future__ import annotations

import re
import uuid
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Final
from urllib.parse import urlsplit

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ken_contracts.inventory import RunStatus
from ken_contracts.jobs import JobKind, JobRunDetail
from ken_core.db.repositories import graph as repo
from ken_services.graph.algorithms import NodeMetrics, build_graph, compute_metrics
from ken_services.graph.policy import GraphPolicy, load_graph_policy
from ken_services.jobs.runner import JobContext, run_job, utc_now

__all__ = [
    "GRAPH_COUNTERS",
    "GraphComputation",
    "cluster_sizes",
    "compute_graph",
    "run_graph_metrics",
]

GRAPH_COUNTERS: Final = (
    "pages",
    "edges",
    "entry_pages",
    "orphans",
    "nearly_orphans",
    "bridges",
    "components",
    "clusters",
    "unreachable",
    "rows_written",
)


@dataclass(frozen=True, slots=True)
class GraphComputation:
    """The pure result of one computation (no database)."""

    metrics_version: str
    rows: tuple[repo.NewGraphMetrics, ...]
    nodes: Mapping[uuid.UUID, NodeMetrics]
    entry_pages: int
    edges: int

    @property
    def counters(self) -> dict[str, int]:
        values = list(self.nodes.values())
        return {
            "pages": len(values),
            "edges": self.edges,
            "entry_pages": self.entry_pages,
            "orphans": sum(m.is_orphan for m in values),
            "nearly_orphans": sum(m.is_nearly_orphan for m in values),
            "bridges": sum(m.is_bridge for m in values),
            "components": len({m.component_id for m in values}),
            "clusters": len({m.cluster_id for m in values if m.cluster_id is not None}),
            "unreachable": sum(m.crawl_depth is None for m in values),
        }


def compute_graph(
    pages: Sequence[repo.LivePage],
    edges: Sequence[tuple[uuid.UUID, uuid.UUID]],
    cluster_keys: Mapping[uuid.UUID, str | None],
    *,
    policy: GraphPolicy,
) -> GraphComputation:
    """Compute every metric for ``pages`` (pure; used by the job and by tests)."""
    graph = build_graph((p.id for p in pages), edges)
    patterns = [re.compile(pattern) for pattern in policy.entry_path_patterns]
    entries = {
        p.id
        for p in pages
        if any(pattern.search(urlsplit(p.url).path or "/") for pattern in patterns)
    }
    metrics = compute_metrics(
        graph,
        entries=entries,
        cluster_keys=cluster_keys,
        damping=policy.pagerank.damping,
        pagerank_tolerance=policy.pagerank.tolerance,
        pagerank_iterations=policy.pagerank.max_iterations,
        hits_tolerance=policy.hits.tolerance,
        hits_iterations=policy.hits.max_iterations,
        nearly_orphan_max_inbound=policy.nearly_orphan_max_inbound,
    )
    version_of = {p.id: p.version for p in pages}
    rows = tuple(
        repo.NewGraphMetrics(
            page_id=node,
            page_version=version_of[node],
            in_degree=m.in_degree,
            out_degree=m.out_degree,
            internal_pagerank=m.internal_pagerank,
            hub_score=m.hub_score,
            authority_score=m.authority_score,
            crawl_depth=m.crawl_depth,
            is_orphan=m.is_orphan,
            is_nearly_orphan=m.is_nearly_orphan,
            is_bridge=m.is_bridge,
            component_id=m.component_id,
            cluster_id=m.cluster_id,
        )
        for node, m in metrics.items()
    )
    return GraphComputation(
        metrics_version=policy.metrics_version,
        rows=rows,
        nodes=dict(metrics),
        entry_pages=len(entries),
        edges=graph.edge_count,
    )


async def run_graph_metrics(
    *,
    sessionmaker: async_sessionmaker[AsyncSession],
    triggered_by: str,
    policy: GraphPolicy | None = None,
    clock: Callable[[], datetime] = utc_now,
) -> tuple[JobRunDetail, GraphComputation | None]:
    """One recorded ``graph_metrics`` run. Raises ``JobAlreadyRunningError`` if one is active."""
    active = policy if policy is not None else load_graph_policy()
    holder: list[GraphComputation] = []

    async def body(job: JobContext) -> RunStatus:
        async with job.transaction() as tx:
            pages = await repo.load_live_pages(tx.session)
            edges = await repo.load_edges(tx.session)
            keys = await repo.cluster_keys(tx.session)
            result = compute_graph(pages, edges, keys, policy=active)
            written = await repo.replace_metrics(
                tx.session,
                metrics_version=result.metrics_version,
                rows=result.rows,
                run_id=job.run_id,
                computed_at=job.clock(),
            )
            counters = result.counters
            counters["rows_written"] = written
            for name, value in counters.items():
                tx.increment(name, value)
        holder.append(result)
        return RunStatus.SUCCEEDED

    detail = await run_job(
        kind=JobKind.GRAPH_METRICS,
        sessionmaker=sessionmaker,
        triggered_by=triggered_by,
        body=body,
        config_snapshot={
            "metrics_version": active.metrics_version,
            "policy": {"version": active.version, "status": active.status},
            "pagerank": active.pagerank.model_dump(mode="json"),
            "nearly_orphan_max_inbound": active.nearly_orphan_max_inbound,
        },
        counter_names=GRAPH_COUNTERS,
        clock=clock,
    )
    return detail, (holder[0] if holder else None)


def cluster_sizes(result: GraphComputation) -> dict[int, int]:
    """Cluster ordinal -> page count (for the CLI summary)."""
    counts = Counter(m.cluster_id for m in result.nodes.values() if m.cluster_id is not None)
    return dict(sorted(counts.items()))
