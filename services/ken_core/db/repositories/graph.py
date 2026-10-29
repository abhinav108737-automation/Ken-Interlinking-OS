"""Link-graph repository: reads the current internal link graph, writes ``page_graph_metrics``.

The caller owns the transaction: nothing here commits. ``page_graph_metrics`` is RECOMPUTABLE BY
REPLACE (migration 0010): a run deletes the rows of ITS ``metrics_version`` and inserts the new
ones in the same transaction, and never edits a row of another version, so two recipes coexist
and a published number stays reproducible.

Graph definition (shared with the recommendation engine's inbound counts so the two cannot
disagree): nodes are pages that are not gone; an edge is an INTERNAL body link of a node's
CURRENT version whose resolved URL maps through ``known_urls`` to another live page, excluding
URLs last seen as not-found or gone.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Final

from sqlalchemy import and_, delete, func, or_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from ken_contracts.inventory import LinkType, UrlOutcome
from ken_contracts.recommendations import GraphSignals
from ken_core.db.models.intelligence import PageAnalysis as PageAnalysisRow
from ken_core.db.models.intelligence import PageGraphMetrics
from ken_core.db.models.inventory import ExistingLink, KnownUrl, Page

__all__ = [
    "LivePage",
    "NewGraphMetrics",
    "cluster_keys",
    "fetch_signals",
    "latest_metrics_version",
    "load_edges",
    "load_live_pages",
    "replace_metrics",
    "signals_from_row",
]

_INTERNAL: Final = LinkType.INTERNAL.value
_BROKEN: Final = (UrlOutcome.NOT_FOUND.value, UrlOutcome.GONE.value)


@dataclass(frozen=True, slots=True)
class LivePage:
    id: uuid.UUID
    version: int
    url: str


@dataclass(frozen=True, slots=True)
class NewGraphMetrics:
    page_id: uuid.UUID
    page_version: int
    in_degree: int
    out_degree: int
    internal_pagerank: float
    hub_score: float
    authority_score: float
    crawl_depth: int | None
    is_orphan: bool
    is_nearly_orphan: bool
    is_bridge: bool
    component_id: int | None
    cluster_id: int | None


async def load_live_pages(session: AsyncSession) -> list[LivePage]:
    """Every page that is not gone, with its current version, in id order."""
    rows = (
        await session.execute(
            select(Page.id, Page.current_version, Page.canonical_url)
            .where(Page.gone_at.is_(None))
            .order_by(Page.id)
        )
    ).all()
    return [LivePage(id=r[0], version=r[1], url=r[2]) for r in rows]


async def load_edges(session: AsyncSession) -> list[tuple[uuid.UUID, uuid.UUID]]:
    """Distinct (source, target) pairs of current internal links between live pages."""
    source = aliased(Page)
    target = aliased(Page)
    known = aliased(KnownUrl)
    rows = (
        await session.execute(
            select(ExistingLink.page_id, known.page_id)
            .join(
                source,
                and_(
                    source.id == ExistingLink.page_id,
                    source.current_version == ExistingLink.page_version,
                ),
            )
            .join(known, known.url == ExistingLink.resolved_url)
            .join(target, target.id == known.page_id)
            .where(
                ExistingLink.link_type == _INTERNAL,
                source.gone_at.is_(None),
                target.gone_at.is_(None),
                known.page_id.is_not(None),
                known.page_id != ExistingLink.page_id,
                or_(known.outcome.is_(None), known.outcome.not_in(_BROKEN)),
            )
            .distinct()
            .order_by(ExistingLink.page_id, known.page_id)
        )
    ).all()
    return [(r[0], r[1]) for r in rows if r[1] is not None]


async def cluster_keys(session: AsyncSession) -> dict[uuid.UUID, str | None]:
    """Topical cluster key per live page: its most specific market, else its industry.

    Read from the newest analysis of the page's CURRENT version; a page with none has no key.
    """
    newest = (
        select(
            PageAnalysisRow.page_id,
            PageAnalysisRow.market_id,
            PageAnalysisRow.industry_id,
            func.row_number()
            .over(
                partition_by=PageAnalysisRow.page_id,
                order_by=(
                    PageAnalysisRow.analysis_version.desc(),
                    PageAnalysisRow.created_at.desc(),
                ),
            )
            .label("rn"),
        )
        .join(
            Page,
            and_(
                PageAnalysisRow.page_id == Page.id,
                PageAnalysisRow.page_version == Page.current_version,
            ),
        )
        .where(Page.gone_at.is_(None))
        .subquery()
    )
    rows = (
        await session.execute(
            select(newest.c.page_id, newest.c.market_id, newest.c.industry_id).where(
                newest.c.rn == 1
            )
        )
    ).all()
    out: dict[uuid.UUID, str | None] = {}
    for row in rows:
        page_id: uuid.UUID = row[0]
        market: str | None = row[1]
        industry: str | None = row[2]
        out[page_id] = (
            f"market:{market}" if market else (f"industry:{industry}" if industry else None)
        )
    return out


async def replace_metrics(
    session: AsyncSession,
    *,
    metrics_version: str,
    rows: Sequence[NewGraphMetrics],
    run_id: uuid.UUID | None,
    computed_at: datetime,
    chunk_size: int = 500,
) -> int:
    """Replace every row of ``metrics_version`` (and only that version) with ``rows``."""
    await session.execute(
        delete(PageGraphMetrics).where(PageGraphMetrics.metrics_version == metrics_version)
    )
    written = 0
    for start in range(0, len(rows), chunk_size):
        chunk = rows[start : start + chunk_size]
        await session.execute(
            insert(PageGraphMetrics).values(
                [
                    {
                        "page_id": r.page_id,
                        "page_version": r.page_version,
                        "in_degree": r.in_degree,
                        "out_degree": r.out_degree,
                        "internal_pagerank": r.internal_pagerank,
                        "hub_score": r.hub_score,
                        "authority_score": r.authority_score,
                        "crawl_depth": r.crawl_depth,
                        "is_orphan": r.is_orphan,
                        "is_nearly_orphan": r.is_nearly_orphan,
                        "is_bridge": r.is_bridge,
                        "component_id": r.component_id,
                        "cluster_id": r.cluster_id,
                        "metrics_version": metrics_version,
                        "run_id": run_id,
                        "computed_at": computed_at,
                    }
                    for r in chunk
                ]
            )
        )
        written += len(chunk)
    return written


async def latest_metrics_version(session: AsyncSession) -> str | None:
    """The ``metrics_version`` of the most recently computed rows, or ``None``."""
    return (
        await session.execute(
            select(PageGraphMetrics.metrics_version)
            .order_by(PageGraphMetrics.computed_at.desc(), PageGraphMetrics.id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


def signals_from_row(row: PageGraphMetrics) -> GraphSignals:
    """Re-validate a stored row through the contract on the way out."""
    return GraphSignals(
        page_id=row.page_id,
        page_version=row.page_version,
        in_degree=row.in_degree,
        out_degree=row.out_degree,
        internal_pagerank=min(1.0, max(0.0, row.internal_pagerank)),
        hub_score=min(1.0, max(0.0, row.hub_score)),
        authority_score=min(1.0, max(0.0, row.authority_score)),
        crawl_depth=row.crawl_depth,
        is_orphan=row.is_orphan,
        is_nearly_orphan=row.is_nearly_orphan,
        is_bridge=row.is_bridge,
        component_id=row.component_id,
        cluster_id=row.cluster_id,
        metrics_version=row.metrics_version,
        computed_at=row.computed_at,
    )


async def fetch_signals(
    session: AsyncSession,
    *,
    page_ids: Sequence[uuid.UUID] | None = None,
    metrics_version: str | None = None,
) -> dict[uuid.UUID, GraphSignals]:
    """Graph signals of each page's CURRENT version for one ``metrics_version``
    (default: the most recent). Pages without a row are absent."""
    version = metrics_version or await latest_metrics_version(session)
    if version is None:
        return {}
    statement = (
        select(PageGraphMetrics)
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
    if page_ids is not None:
        statement = statement.where(PageGraphMetrics.page_id.in_(list(page_ids)))
    rows = (await session.scalars(statement)).all()
    return {row.page_id: signals_from_row(row) for row in rows}
