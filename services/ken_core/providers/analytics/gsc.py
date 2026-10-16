"""``SearchConsoleAnalyticsProvider``: Search Console Search Analytics, through the guarded
egress (ADR-0015 §4, §12.3, §12.9.4).

``POST {base}/webmasters/v3/sites/{urlencoded site}/searchAnalytics/query`` with
``{startDate, endDate, dimensions: ["date", "page"], type: "web", dataState: "final",
rowLimit, startRow}``. ``dataState: "final"`` is what keeps partial days out, together with
the three-day source lag the importer enforces.

Search Console has NO link-level data (``LINK_SOURCES`` excludes it), so a links dataset
returns nothing; the importer refuses the combination outright.

Response rows are ``{"keys": [date, page], "clicks", "impressions", "ctr", "position"}``.
``ctr`` is ignored and recomputed; ``position`` is the row's average position and the
importer stores ``position x impressions`` as ``position_sum``. Day boundaries are
America/Los_Angeles, which the import records as its ``source_timezone``.

The query is the property and the dates: the request body never carries a Ken URL. Only the
RESPONSE names pages, which is why resolution happens locally through the injected resolver.
"""

from __future__ import annotations

import math
from datetime import date, datetime
from typing import Any, Final
from urllib.parse import quote

import httpx

from ken_contracts.analytics import (
    AnalyticsBatch,
    Dataset,
    DateRange,
    MetricSource,
    PageMetrics,
    PageUrlResolver,
)
from ken_contracts.model import ProviderHealth
from ken_core.config import Settings
from ken_core.providers.analytics.base import (
    AnalyticsFetchError,
    BaseAnalyticsProvider,
    FetchErrorCode,
    GoogleEgress,
    StatsBuilder,
)

__all__ = [
    "GSC_HOST_SETTING",
    "GSC_ROW_LIMIT",
    "GSC_TIMEZONE",
    "MAX_PAGES",
    "PROVIDER_NAME",
    "SearchConsoleAnalyticsProvider",
]

PROVIDER_NAME: Final = "gsc"
GSC_HOST_SETTING: Final = "analytics_gsc_base_url"
GSC_ROW_LIMIT: Final = 25_000
GSC_TIMEZONE: Final = "America/Los_Angeles"
"""Search Console's own day boundary (ADR-0015 §3)."""
MAX_PAGES: Final = 400
"""Hard bound on the paging loop (10 M rows)."""


def _parse_day(value: Any) -> date | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.strptime(value.strip(), "%Y-%m-%d").date()
    except ValueError:
        return None


def _count_of(value: Any) -> int | None:
    """Search Console reports counts as doubles; a count is a non-negative whole number."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    number = float(value)
    if not math.isfinite(number) or number < 0:
        return None
    return round(number)


def _position_of(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    number = float(value)
    if not math.isfinite(number) or number < 1.0:
        return None
    return number


class SearchConsoleAnalyticsProvider(BaseAnalyticsProvider):
    """Daily (date, page) rows for the configured Search Console property."""

    name: str = PROVIDER_NAME
    source_timezone: str = GSC_TIMEZONE

    def __init__(
        self,
        settings: Settings,
        *,
        resolver: PageUrlResolver,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._settings = settings
        self._resolver = resolver
        self._egress = GoogleEgress(
            settings,
            base_url=settings.analytics_gsc_base_url,
            host_setting=GSC_HOST_SETTING,
            transport=transport,
        )
        self._site = settings.gsc_site_url

    def _path(self) -> str:
        if not self._site:
            raise AnalyticsFetchError(FetchErrorCode.INVALID_REQUEST)
        return f"/webmasters/v3/sites/{quote(self._site, safe='')}/searchAnalytics/query"

    @staticmethod
    def query_body(date_range: DateRange, *, start_row: int) -> dict[str, Any]:
        return {
            "startDate": date_range.start.isoformat(),
            "endDate": date_range.end.isoformat(),
            "dimensions": ["date", "page"],
            "type": "web",
            "dataState": "final",
            "rowLimit": GSC_ROW_LIMIT,
            "startRow": start_row,
        }

    async def fetch_batch(
        self, *, date_range: DateRange, datasets: frozenset[Dataset]
    ) -> AnalyticsBatch:
        self._egress.gate()  # before ANY I/O, and before the token file is touched
        if Dataset.PAGES not in datasets:
            # Search Console has no link-level data: nothing to fetch, nothing sent.
            return AnalyticsBatch()
        path = self._path()
        token = self._egress.read_token()
        stats = StatsBuilder()
        page_rows: list[PageMetrics] = []
        start_row = 0
        for _ in range(MAX_PAGES):
            payload = await self._egress.post(
                path, body=self.query_body(date_range, start_row=start_row), token=token
            )
            rows = payload.get("rows")
            batch = rows if isinstance(rows, list) else []
            for row in batch:
                stats.rows_read += 1
                parsed = self._row(row, stats)
                if parsed is not None:
                    page_rows.append(parsed)
            start_row += len(batch)
            if len(batch) < GSC_ROW_LIMIT:
                break
        return AnalyticsBatch(page_rows=tuple(page_rows), stats=stats.build())

    def _row(self, row: Any, stats: StatsBuilder) -> PageMetrics | None:
        keys = row.get("keys") if isinstance(row, dict) else None
        if not isinstance(keys, list) or len(keys) != 2:
            stats.rejected_invalid += 1
            return None
        day = _parse_day(keys[0])
        url = keys[1]
        clicks = _count_of(row.get("clicks"))
        impressions = _count_of(row.get("impressions"))
        position = _position_of(row.get("position"))
        if day is None or not isinstance(url, str) or clicks is None or impressions is None:
            stats.rejected_invalid += 1
            return None
        page_id = stats.resolve(self._resolver, url)
        if page_id is None:
            return None
        return PageMetrics(
            page_id=page_id,
            source=MetricSource.SEARCH_CONSOLE,
            range=DateRange(start=day, end=day),
            impressions=impressions,
            clicks=clicks,
            position=position,
        )

    async def health(self) -> ProviderHealth:
        if not self._settings.analytics_google_enabled:
            return ProviderHealth(
                provider=self.name, reachable=False, error="google egress is disabled"
            )
        return ProviderHealth(provider=self.name, reachable=True)

    async def aclose(self) -> None:
        await self._egress.aclose()
