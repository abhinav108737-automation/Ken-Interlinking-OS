"""``StoredAnalyticsProvider`` (factory name ``first_party``): the frozen read protocol over
rows ALREADY stored by an import (ADR-0015 §4).

It is a reader, not an ingest path: Phase 8 and 9 consumers (outcome learning, confidence
calibration) ask for the same :class:`~ken_contracts.analytics.AnalyticsBatch` as any other
provider, without knowing whether the numbers came from the collector, GA4 or a CSV. Feeding
it back into the importer would be a no-op replace of a range by itself, so the CLI does not
offer it as an import source.

Rows are read on a session from the factory the caller passes (the READ ONLY factory in the
API). Every row is daily by construction, and ``source`` is the row's stored origin.
"""

from __future__ import annotations

from typing import Final

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from ken_contracts.analytics import (
    LINK_SOURCES,
    PAGE_SOURCES,
    AnalyticsBatch,
    AnalyticsSource,
    Dataset,
    DateRange,
    FetchStats,
    LinkMetrics,
    MetricSource,
    PageMetrics,
)
from ken_core.config import Settings
from ken_core.db.engine import create_engine
from ken_core.db.models.analytics import LinkMetricsDaily, PageMetricsDaily
from ken_core.db.session import create_readonly_sessionmaker
from ken_core.providers.analytics.base import BaseAnalyticsProvider

__all__ = ["PROVIDER_NAME", "StoredAnalyticsProvider"]

PROVIDER_NAME: Final = "first_party"


class StoredAnalyticsProvider(BaseAnalyticsProvider):
    """Stored daily rows of one :class:`~ken_contracts.analytics.AnalyticsSource`."""

    name: str = PROVIDER_NAME

    def __init__(
        self,
        *,
        sessionmaker: async_sessionmaker[AsyncSession],
        source: AnalyticsSource = AnalyticsSource.FIRST_PARTY,
        engine: AsyncEngine | None = None,
    ) -> None:
        self._sessionmaker = sessionmaker
        self._source = AnalyticsSource(source)
        self._own_engine = engine
        """Set only when this provider created the engine: then it disposes of it."""

    @classmethod
    def from_settings(
        cls, settings: Settings, *, source: AnalyticsSource = AnalyticsSource.FIRST_PARTY
    ) -> StoredAnalyticsProvider:
        """Own READ ONLY factory over a new engine (no I/O until the first fetch).

        ``aclose()`` disposes of it, exactly as the CMS factory's simulated provider owns its
        own connection pool.
        """
        engine = create_engine(settings)
        return cls(sessionmaker=create_readonly_sessionmaker(engine), source=source, engine=engine)

    async def aclose(self) -> None:
        engine, self._own_engine = self._own_engine, None
        if engine is not None:
            await engine.dispose()

    async def fetch_batch(
        self, *, date_range: DateRange, datasets: frozenset[Dataset]
    ) -> AnalyticsBatch:
        link_rows: list[LinkMetrics] = []
        page_rows: list[PageMetrics] = []
        async with self._sessionmaker() as session:
            if Dataset.LINKS in datasets and self._source in LINK_SOURCES:
                found = await session.execute(
                    select(LinkMetricsDaily)
                    .where(
                        LinkMetricsDaily.source == self._source.value,
                        LinkMetricsDaily.metric_date >= date_range.start,
                        LinkMetricsDaily.metric_date <= date_range.end,
                    )
                    .order_by(LinkMetricsDaily.metric_date, LinkMetricsDaily.plan_id)
                )
                link_rows = [
                    LinkMetrics(
                        link_id=str(row.plan_id),
                        source=MetricSource(row.source),
                        range=DateRange(start=row.metric_date, end=row.metric_date),
                        impressions=row.impressions,
                        clicks=row.clicks,
                    )
                    for row in found.scalars()
                ]
            if Dataset.PAGES in datasets and self._source in PAGE_SOURCES:
                found_pages = await session.execute(
                    select(PageMetricsDaily)
                    .where(
                        PageMetricsDaily.source == self._source.value,
                        PageMetricsDaily.metric_date >= date_range.start,
                        PageMetricsDaily.metric_date <= date_range.end,
                    )
                    .order_by(PageMetricsDaily.metric_date, PageMetricsDaily.page_id)
                )
                page_rows = [
                    PageMetrics(
                        page_id=str(row.page_id),
                        source=MetricSource(row.source),
                        range=DateRange(start=row.metric_date, end=row.metric_date),
                        impressions=row.impressions,
                        clicks=row.clicks,
                        sessions=row.sessions,
                        pageviews=row.pageviews,
                        position=(
                            None
                            if row.impressions == 0 or row.position_sum == 0
                            else row.position_sum / row.impressions
                        ),
                    )
                    for row in found_pages.scalars()
                ]
        return AnalyticsBatch(
            link_rows=tuple(link_rows),
            page_rows=tuple(page_rows),
            stats=FetchStats(rows_read=len(link_rows) + len(page_rows)),
        )
