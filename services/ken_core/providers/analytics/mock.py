"""``MockAnalyticsProvider``: rows handed in by the caller (fixtures, tests, and the vehicle
for the seeded demo data of ``ken_services.analytics.synthetic``).

It filters by DATASET only. Rows outside the requested range are deliberately returned
unchanged, because rejecting them (``outside_range``) is the importer's job and must stay
testable through the real code path.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Final

from ken_contracts.analytics import (
    AnalyticsBatch,
    Dataset,
    DateRange,
    FetchStats,
    LinkMetrics,
    PageMetrics,
)
from ken_core.providers.analytics.base import (
    AnalyticsFetchError,
    BaseAnalyticsProvider,
    FetchErrorCode,
)

__all__ = ["PROVIDER_NAME", "MockAnalyticsProvider"]

PROVIDER_NAME: Final = "mock"


class MockAnalyticsProvider(BaseAnalyticsProvider):
    """A fixed batch. ``error`` makes every fetch refuse with that code (fault injection)."""

    name: str = PROVIDER_NAME

    def __init__(
        self,
        *,
        link_rows: Iterable[LinkMetrics] = (),
        page_rows: Iterable[PageMetrics] = (),
        stats: FetchStats | None = None,
        error: FetchErrorCode | None = None,
    ) -> None:
        self._link_rows = tuple(link_rows)
        self._page_rows = tuple(page_rows)
        self._stats = stats
        self._error = error
        self.calls: list[tuple[DateRange, frozenset[Dataset]]] = []

    async def fetch_batch(
        self, *, date_range: DateRange, datasets: frozenset[Dataset]
    ) -> AnalyticsBatch:
        self.calls.append((date_range, datasets))
        if self._error is not None:
            raise AnalyticsFetchError(self._error)
        links = self._link_rows if Dataset.LINKS in datasets else ()
        pages = self._page_rows if Dataset.PAGES in datasets else ()
        stats = self._stats
        if stats is None:
            stats = FetchStats(rows_read=len(links) + len(pages))
        return AnalyticsBatch(link_rows=links, page_rows=pages, stats=stats)
