"""Read side of the link-graph metrics: per-page signals and corpus-wide summaries.

Everything is a read over ``page_graph_metrics`` (which the ``graph_metrics`` job fills), so the
UI can show a page's neighbourhood AND the corpus clusters/orphans without recomputing anything.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy import and_, case, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ken_contracts.recommendations import GraphSignals
from ken_core.db.models.intelligence import PageGraphMetrics
from ken_core.db.models.inventory import Page
from ken_core.db.repositories import graph as repo

__all__ = ["ClusterSummary", "GraphOverview", "GraphPageRow", "SqlGraphReader"]


@dataclass(frozen=True, slots=True)
class ClusterSummary:
    cluster_id: int | None
    pages: int
    orphans: int
    nearly_orphans: int
    bridges: int
    mean_in_degree: float
    top_page_id: uuid.UUID | None
    """The page with the highest PageRank in the cluster (ties by id)."""


@dataclass(frozen=True, slots=True)
class GraphOverview:
    metrics_version: str | None
    pages: int
    orphans: int
    nearly_orphans: int
    bridges: int
    components: int
    clusters: int


@dataclass(frozen=True, slots=True)
class GraphPageRow:
    """One page's link-graph position with enough identity to render a list row."""

    signals: GraphSignals
    canonical_url: str
    title: str | None


class SqlGraphReader:
    def __init__(self, sessionmaker: async_sessionmaker[AsyncSession]) -> None:
        self._sessionmaker = sessionmaker

    async def signals(
        self, *, page_ids: Sequence[uuid.UUID], metrics_version: str | None = None
    ) -> dict[uuid.UUID, GraphSignals]:
        async with self._sessionmaker() as session:
            return await repo.fetch_signals(
                session, page_ids=list(page_ids), metrics_version=metrics_version
            )

    async def signal(self, *, page_id: uuid.UUID) -> GraphSignals | None:
        return (await self.signals(page_ids=[page_id])).get(page_id)

    async def overview(self, *, metrics_version: str | None = None) -> GraphOverview:
        async with self._sessionmaker() as session:
            version = metrics_version or await repo.latest_metrics_version(session)
            if version is None:
                return GraphOverview(None, 0, 0, 0, 0, 0, 0)
            row = (
                await session.execute(
                    select(
                        func.count(),
                        func.count().filter(PageGraphMetrics.is_orphan),
                        func.count().filter(PageGraphMetrics.is_nearly_orphan),
                        func.count().filter(PageGraphMetrics.is_bridge),
                        func.count(func.distinct(PageGraphMetrics.component_id)),
                        func.count(func.distinct(PageGraphMetrics.cluster_id)),
                    )
                    .join(
                        Page,
                        and_(
                            PageGraphMetrics.page_id == Page.id,
                            PageGraphMetrics.page_version == Page.current_version,
                        ),
                    )
                    .where(PageGraphMetrics.metrics_version == version)
                )
            ).one()
        return GraphOverview(version, *(int(x) for x in row))

    async def clusters(self, *, metrics_version: str | None = None) -> list[ClusterSummary]:
        async with self._sessionmaker() as session:
            version = metrics_version or await repo.latest_metrics_version(session)
            if version is None:
                return []
            rows = (
                await session.execute(
                    select(
                        PageGraphMetrics.cluster_id,
                        PageGraphMetrics.page_id,
                        PageGraphMetrics.in_degree,
                        PageGraphMetrics.internal_pagerank,
                        case((PageGraphMetrics.is_orphan, 1), else_=0),
                        case((PageGraphMetrics.is_nearly_orphan, 1), else_=0),
                        case((PageGraphMetrics.is_bridge, 1), else_=0),
                    )
                    .join(
                        Page,
                        and_(
                            PageGraphMetrics.page_id == Page.id,
                            PageGraphMetrics.page_version == Page.current_version,
                        ),
                    )
                    .where(PageGraphMetrics.metrics_version == version)
                    .order_by(PageGraphMetrics.page_id)
                )
            ).all()
        grouped: dict[int | None, list[tuple[uuid.UUID, int, float, int, int, int]]] = {}
        for r in rows:
            grouped.setdefault(r[0], []).append((r[1], r[2], r[3], r[4], r[5], r[6]))
        out: list[ClusterSummary] = []
        for cluster_id, members in grouped.items():
            top = min(members, key=lambda m: (-m[2], str(m[0])))
            out.append(
                ClusterSummary(
                    cluster_id=cluster_id,
                    pages=len(members),
                    orphans=sum(m[3] for m in members),
                    nearly_orphans=sum(m[4] for m in members),
                    bridges=sum(m[5] for m in members),
                    mean_in_degree=sum(m[1] for m in members) / len(members),
                    top_page_id=top[0],
                )
            )
        out.sort(
            key=lambda c: (c.cluster_id is None, c.cluster_id if c.cluster_id is not None else 0)
        )
        return out

    async def list_pages(
        self,
        *,
        limit: int,
        offset: int,
        metrics_version: str | None = None,
        cluster_id: int | None = None,
        component_id: int | None = None,
        orphans_only: bool = False,
        bridges_only: bool = False,
    ) -> tuple[list[GraphPageRow], int]:
        """A page of graph positions (most authoritative first) and the unpaged total.

        Filters AND together. Order is deterministic: PageRank desc, then canonical URL.
        """
        async with self._sessionmaker() as session:
            version = metrics_version or await repo.latest_metrics_version(session)
            if version is None:
                return [], 0
            conditions = [PageGraphMetrics.metrics_version == version]
            if cluster_id is not None:
                conditions.append(PageGraphMetrics.cluster_id == cluster_id)
            if component_id is not None:
                conditions.append(PageGraphMetrics.component_id == component_id)
            if orphans_only:
                conditions.append(PageGraphMetrics.is_orphan.is_(True))
            if bridges_only:
                conditions.append(PageGraphMetrics.is_bridge.is_(True))
            current = and_(
                PageGraphMetrics.page_id == Page.id,
                PageGraphMetrics.page_version == Page.current_version,
            )
            total = (
                await session.execute(
                    select(func.count())
                    .select_from(PageGraphMetrics)
                    .join(Page, current)
                    .where(*conditions)
                )
            ).scalar_one()
            rows = (
                await session.execute(
                    select(PageGraphMetrics, Page.canonical_url, Page.title)
                    .join(Page, current)
                    .where(*conditions)
                    .order_by(PageGraphMetrics.internal_pagerank.desc(), Page.canonical_url)
                    .limit(limit)
                    .offset(offset)
                )
            ).all()
        return (
            [
                GraphPageRow(signals=repo.signals_from_row(r[0]), canonical_url=r[1], title=r[2])
                for r in rows
            ],
            int(total),
        )
