"""Read side of the link-graph metrics: per-page signals and corpus-wide views.

Everything is a read over ``page_graph_metrics`` (which the ``graph_metrics`` job fills), so the
control-plane UI can show a page's neighbourhood AND the corpus clusters, orphans and bridges
without recomputing anything. :class:`SqlGraphReader` implements the
:class:`~ken_contracts.graph.GraphReader` protocol over a READ ONLY sessionmaker.

The crawl entry page (``crawl_depth == 0``) is normally ``is_orphan`` because nothing links to
the home page; the views therefore report ``entry_pages`` and ``true_orphans`` separately so a
consumer never mistakes the home page for a page that needs rescuing.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence

from sqlalchemy import and_, case, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ken_contracts.errors import ListResponse, PageInfo
from ken_contracts.graph import GraphClusterSummary, GraphOverview, GraphPageRow
from ken_contracts.intelligence import IntelligenceNotFoundError
from ken_contracts.recommendations import GraphSignals
from ken_core.db.models.intelligence import PageGraphMetrics
from ken_core.db.models.inventory import Page
from ken_core.db.repositories import graph as repo

__all__ = ["SqlGraphReader"]

_CURRENT = and_(
    PageGraphMetrics.page_id == Page.id,
    PageGraphMetrics.page_version == Page.current_version,
)
_TRUE_ORPHAN = and_(
    PageGraphMetrics.is_orphan.is_(True),
    or_(PageGraphMetrics.crawl_depth.is_(None), PageGraphMetrics.crawl_depth != 0),
)


def _row(metrics: PageGraphMetrics, url: str, title: str | None) -> GraphPageRow:
    signals = repo.signals_from_row(metrics)
    return GraphPageRow(
        signals=signals,
        canonical_url=url,
        title=title,
        is_entry_page=signals.crawl_depth == 0,
    )


class SqlGraphReader:
    def __init__(self, sessionmaker: async_sessionmaker[AsyncSession]) -> None:
        self._sessionmaker = sessionmaker

    # ----------------------------------------------------------- per page
    async def signals(
        self, *, page_ids: Sequence[uuid.UUID], metrics_version: str | None = None
    ) -> dict[uuid.UUID, GraphSignals]:
        async with self._sessionmaker() as session:
            return await repo.fetch_signals(
                session, page_ids=list(page_ids), metrics_version=metrics_version
            )

    async def signal(self, *, page_id: uuid.UUID) -> GraphSignals | None:
        return (await self.signals(page_ids=[page_id])).get(page_id)

    async def get_page(self, *, page_id: uuid.UUID) -> GraphPageRow:
        async with self._sessionmaker() as session:
            version = await repo.latest_metrics_version(session)
            row = (
                None
                if version is None
                else (
                    await session.execute(
                        select(PageGraphMetrics, Page.canonical_url, Page.title)
                        .join(Page, _CURRENT)
                        .where(
                            PageGraphMetrics.metrics_version == version,
                            PageGraphMetrics.page_id == page_id,
                        )
                    )
                ).first()
            )
        if row is None:
            raise IntelligenceNotFoundError("page")
        return _row(row[0], row[1], row[2])

    # ------------------------------------------------------------- corpus
    async def get_overview(self) -> GraphOverview:
        async with self._sessionmaker() as session:
            version = await repo.latest_metrics_version(session)
            if version is None:
                return GraphOverview(
                    metrics_version=None,
                    computed_at=None,
                    pages=0,
                    entry_pages=0,
                    orphans=0,
                    true_orphans=0,
                    nearly_orphans=0,
                    bridges=0,
                    components=0,
                    clusters=0,
                )
            row = (
                await session.execute(
                    select(
                        func.count(),
                        func.count().filter(PageGraphMetrics.crawl_depth == 0),
                        func.count().filter(PageGraphMetrics.is_orphan),
                        func.count().filter(_TRUE_ORPHAN),
                        func.count().filter(PageGraphMetrics.is_nearly_orphan),
                        func.count().filter(PageGraphMetrics.is_bridge),
                        func.count(func.distinct(PageGraphMetrics.component_id)),
                        func.count(func.distinct(PageGraphMetrics.cluster_id)),
                        func.max(PageGraphMetrics.computed_at),
                    )
                    .select_from(PageGraphMetrics)
                    .join(Page, _CURRENT)
                    .where(PageGraphMetrics.metrics_version == version)
                )
            ).one()
        return GraphOverview(
            metrics_version=version,
            computed_at=row[8],
            pages=int(row[0]),
            entry_pages=int(row[1]),
            orphans=int(row[2]),
            true_orphans=int(row[3]),
            nearly_orphans=int(row[4]),
            bridges=int(row[5]),
            components=int(row[6]),
            clusters=int(row[7]),
        )

    async def list_clusters(self) -> list[GraphClusterSummary]:
        async with self._sessionmaker() as session:
            version = await repo.latest_metrics_version(session)
            if version is None:
                return []
            rows = (
                await session.execute(
                    select(
                        PageGraphMetrics.cluster_id,
                        PageGraphMetrics.page_id,
                        Page.canonical_url,
                        PageGraphMetrics.in_degree,
                        PageGraphMetrics.internal_pagerank,
                        case((PageGraphMetrics.is_orphan, 1), else_=0),
                        case((_TRUE_ORPHAN, 1), else_=0),
                        case((PageGraphMetrics.is_nearly_orphan, 1), else_=0),
                        case((PageGraphMetrics.is_bridge, 1), else_=0),
                    )
                    .join(Page, _CURRENT)
                    .where(PageGraphMetrics.metrics_version == version)
                    .order_by(PageGraphMetrics.page_id)
                )
            ).all()
        grouped: dict[int | None, list[tuple[uuid.UUID, str, int, float, int, int, int, int]]] = {}
        for r in rows:
            grouped.setdefault(r[0], []).append((r[1], r[2], r[3], r[4], r[5], r[6], r[7], r[8]))
        out: list[GraphClusterSummary] = []
        for cluster_id, members in grouped.items():
            top = min(members, key=lambda m: (-m[3], m[1]))
            out.append(
                GraphClusterSummary(
                    cluster_id=cluster_id,
                    pages=len(members),
                    orphans=sum(m[4] for m in members),
                    true_orphans=sum(m[5] for m in members),
                    nearly_orphans=sum(m[6] for m in members),
                    bridges=sum(m[7] for m in members),
                    mean_in_degree=sum(m[2] for m in members) / len(members),
                    top_page_id=top[0],
                    top_page_url=top[1],
                )
            )
        # Largest cluster first (its ordinal), the topic-less group last.
        out.sort(
            key=lambda c: (c.cluster_id is None, c.cluster_id if c.cluster_id is not None else 0)
        )
        return out

    async def list_pages(
        self,
        *,
        limit: int,
        offset: int,
        cluster_id: int | None = None,
        component_id: int | None = None,
        orphans_only: bool = False,
        bridges_only: bool = False,
    ) -> ListResponse[GraphPageRow]:
        """A page of graph positions, most authoritative first (PageRank desc, then URL).

        Filters AND together. ``orphans_only`` is the literal ``is_orphan`` flag (so it
        INCLUDES an entry page nothing links to); consumers separate the two with
        ``is_entry_page``.
        """
        async with self._sessionmaker() as session:
            version = await repo.latest_metrics_version(session)
            if version is None:
                return ListResponse[GraphPageRow](
                    items=[], page=PageInfo(total=0, limit=limit, offset=offset)
                )
            conditions = [PageGraphMetrics.metrics_version == version]
            if cluster_id is not None:
                conditions.append(PageGraphMetrics.cluster_id == cluster_id)
            if component_id is not None:
                conditions.append(PageGraphMetrics.component_id == component_id)
            if orphans_only:
                conditions.append(PageGraphMetrics.is_orphan.is_(True))
            if bridges_only:
                conditions.append(PageGraphMetrics.is_bridge.is_(True))
            total = (
                await session.execute(
                    select(func.count())
                    .select_from(PageGraphMetrics)
                    .join(Page, _CURRENT)
                    .where(*conditions)
                )
            ).scalar_one()
            rows = (
                await session.execute(
                    select(PageGraphMetrics, Page.canonical_url, Page.title)
                    .join(Page, _CURRENT)
                    .where(*conditions)
                    .order_by(PageGraphMetrics.internal_pagerank.desc(), Page.canonical_url)
                    .limit(limit)
                    .offset(offset)
                )
            ).all()
        return ListResponse[GraphPageRow](
            items=[_row(r[0], r[1], r[2]) for r in rows],
            page=PageInfo(total=int(total), limit=limit, offset=offset),
        )
