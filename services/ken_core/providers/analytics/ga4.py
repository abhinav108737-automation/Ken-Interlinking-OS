"""``Ga4AnalyticsProvider``: the GA4 Data API, through the guarded egress of
:class:`~ken_core.providers.analytics.base.GoogleEgress` (ADR-0015 §4, §12.3, §12.9.4).

One endpoint, ``POST {base}/v1beta/properties/{property_id}:runReport``:

* **links** -- dimensions ``date``, ``eventName``, ``customEvent:ken_link_id``; metric
  ``eventCount``; an ``inListFilter`` on the TWO event names (landing events are deferred);
* **pages** -- dimensions ``date``, ``hostName``, ``pagePath``; metrics ``screenPageViews``
  and ``sessions``.

Both page through ``limit`` / ``offset`` ordered by date then the identifying dimension.

**We never send a link id or a URL to Google.** The request carries the property id, the
dates, the dimension names and the two event names, and nothing else: there is no id filter
and no URL filter, so nothing about Ken's inventory leaves the machine.

``(other)`` rows (GA4 cardinality collapsing) are counted as ``other_row`` and dropped; a
``samplingMetadatas`` entry with a sampled read sets ``FetchStats.sampled``. GA4 reports its
day in the property's own time zone, which is why the import records
``KEN_GA4_TIMEZONE`` as the run's ``source_timezone``.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Final
from urllib.parse import quote

import httpx

from ken_contracts.analytics import (
    AnalyticsBatch,
    Dataset,
    DateRange,
    LinkEventType,
    LinkMetrics,
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
    "EVENT_NAMES",
    "GA4_HOST_SETTING",
    "GA4_LIMIT",
    "LINK_ID_DIMENSION",
    "MAX_PAGES",
    "PROVIDER_NAME",
    "Ga4AnalyticsProvider",
]

PROVIDER_NAME: Final = "ga4"
GA4_HOST_SETTING: Final = "analytics_ga4_base_url"
GA4_LIMIT: Final = 100_000
MAX_PAGES: Final = 100
"""Hard bound on the paging loop (10 M rows), so a looping API cannot hang an import."""
LINK_ID_DIMENSION: Final = "customEvent:ken_link_id"
EVENT_NAMES: Final = (LinkEventType.IMPRESSION.value, LinkEventType.CLICK.value)
_OTHER: Final = "(other)"


def _ga4_day(value: date) -> str:
    return value.isoformat()


def _parse_ga4_date(value: str) -> date | None:
    try:
        return datetime.strptime(value.strip(), "%Y%m%d").date()
    except ValueError:
        return None


def _int_of(value: Any) -> int | None:
    """A non-negative integer from a GA4 JSON value (they arrive as strings)."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return int(stripped) if stripped.isdigit() else None


class Ga4AnalyticsProvider(BaseAnalyticsProvider):
    """GA4 link-level and page-level daily rows."""

    name: str = PROVIDER_NAME

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
            base_url=settings.analytics_ga4_base_url,
            host_setting=GA4_HOST_SETTING,
            transport=transport,
        )
        self._property_id = settings.ga4_property_id

    @property
    def source_timezone(self) -> str:
        return self._settings.ga4_timezone

    def _path(self) -> str:
        if not self._property_id:
            raise AnalyticsFetchError(FetchErrorCode.INVALID_REQUEST)
        return f"/v1beta/properties/{quote(self._property_id, safe='')}:runReport"

    # --------------------------------------------------------------- bodies
    @staticmethod
    def link_body(date_range: DateRange, *, offset: int) -> dict[str, Any]:
        return {
            "dateRanges": [
                {"startDate": _ga4_day(date_range.start), "endDate": _ga4_day(date_range.end)}
            ],
            "dimensions": [
                {"name": "date"},
                {"name": "eventName"},
                {"name": LINK_ID_DIMENSION},
            ],
            "metrics": [{"name": "eventCount"}],
            "dimensionFilter": {
                "filter": {
                    "fieldName": "eventName",
                    "inListFilter": {"values": list(EVENT_NAMES)},
                }
            },
            "orderBys": [
                {"dimension": {"dimensionName": "date"}},
                {"dimension": {"dimensionName": LINK_ID_DIMENSION}},
            ],
            "limit": GA4_LIMIT,
            "offset": offset,
            "keepEmptyRows": False,
        }

    @staticmethod
    def page_body(date_range: DateRange, *, offset: int) -> dict[str, Any]:
        return {
            "dateRanges": [
                {"startDate": _ga4_day(date_range.start), "endDate": _ga4_day(date_range.end)}
            ],
            "dimensions": [{"name": "date"}, {"name": "hostName"}, {"name": "pagePath"}],
            "metrics": [{"name": "screenPageViews"}, {"name": "sessions"}],
            "orderBys": [
                {"dimension": {"dimensionName": "date"}},
                {"dimension": {"dimensionName": "pagePath"}},
            ],
            "limit": GA4_LIMIT,
            "offset": offset,
            "keepEmptyRows": False,
        }

    # ---------------------------------------------------------------- fetch
    async def fetch_batch(
        self, *, date_range: DateRange, datasets: frozenset[Dataset]
    ) -> AnalyticsBatch:
        self._egress.gate()  # before ANY I/O, and before the token file is touched
        path = self._path()
        token = self._egress.read_token()
        stats = StatsBuilder()
        link_rows: list[LinkMetrics] = []
        page_rows: list[PageMetrics] = []
        if Dataset.LINKS in datasets:
            for payload in await self._paged(path, date_range, token=token, links=True):
                self._read_links(payload, stats, link_rows)
        if Dataset.PAGES in datasets:
            for payload in await self._paged(path, date_range, token=token, links=False):
                self._read_pages(payload, stats, page_rows)
        return AnalyticsBatch(
            link_rows=tuple(link_rows), page_rows=tuple(page_rows), stats=stats.build()
        )

    async def _paged(
        self, path: str, date_range: DateRange, *, token: str | None, links: bool
    ) -> list[dict[str, Any]]:
        payloads: list[dict[str, Any]] = []
        offset = 0
        for _ in range(MAX_PAGES):
            body = (
                self.link_body(date_range, offset=offset)
                if links
                else self.page_body(date_range, offset=offset)
            )
            payload = await self._egress.post(path, body=body, token=token)
            payloads.append(payload)
            rows = payload.get("rows")
            count = len(rows) if isinstance(rows, list) else 0
            offset += count
            row_count = _int_of(payload.get("rowCount"))
            if count < GA4_LIMIT or (row_count is not None and offset >= row_count):
                break
        return payloads

    # ----------------------------------------------------------- row types
    @staticmethod
    def _dimensions(row: Any, expected: int) -> list[str] | None:
        values = row.get("dimensionValues") if isinstance(row, dict) else None
        if not isinstance(values, list) or len(values) != expected:
            return None
        out: list[str] = []
        for item in values:
            value = item.get("value") if isinstance(item, dict) else None
            if not isinstance(value, str):
                return None
            out.append(value)
        return out

    @staticmethod
    def _metrics(row: Any, expected: int) -> list[int] | None:
        values = row.get("metricValues") if isinstance(row, dict) else None
        if not isinstance(values, list) or len(values) != expected:
            return None
        out: list[int] = []
        for item in values:
            number = _int_of(item.get("value") if isinstance(item, dict) else None)
            if number is None:
                return None
            out.append(number)
        return out

    def _note_sampling(self, payload: dict[str, Any], stats: StatsBuilder) -> None:
        metadatas = payload.get("samplingMetadatas")
        if isinstance(metadatas, list) and metadatas:
            stats.sampled = True

    def _read_links(
        self, payload: dict[str, Any], stats: StatsBuilder, out: list[LinkMetrics]
    ) -> None:
        self._note_sampling(payload, stats)
        merged: dict[tuple[date, str], dict[str, int]] = {}
        order: list[tuple[date, str]] = []
        rows = payload.get("rows")
        for row in rows if isinstance(rows, list) else []:
            stats.rows_read += 1
            dimensions = self._dimensions(row, 3)
            metrics = self._metrics(row, 1)
            if dimensions is None or metrics is None:
                stats.rejected_invalid += 1
                continue
            raw_day, event_name, link_id = dimensions
            if _OTHER in {raw_day, event_name, link_id}:
                stats.other_row += 1
                continue
            day = _parse_ga4_date(raw_day)
            if day is None or event_name not in EVENT_NAMES:
                stats.rejected_invalid += 1
                continue
            if not link_id.strip():
                stats.unresolved_link += 1
                continue
            key = (day, link_id.strip())
            if key not in merged:
                merged[key] = {"impressions": 0, "clicks": 0}
                order.append(key)
            field = "impressions" if event_name == LinkEventType.IMPRESSION.value else "clicks"
            merged[key][field] += metrics[0]
        for day, link_id in order:
            counts = merged[(day, link_id)]
            out.append(
                LinkMetrics(
                    link_id=link_id,
                    source=MetricSource.GA4,
                    range=DateRange(start=day, end=day),
                    impressions=counts["impressions"],
                    clicks=counts["clicks"],
                )
            )

    def _read_pages(
        self, payload: dict[str, Any], stats: StatsBuilder, out: list[PageMetrics]
    ) -> None:
        self._note_sampling(payload, stats)
        rows = payload.get("rows")
        for row in rows if isinstance(rows, list) else []:
            stats.rows_read += 1
            dimensions = self._dimensions(row, 3)
            metrics = self._metrics(row, 2)
            if dimensions is None or metrics is None:
                stats.rejected_invalid += 1
                continue
            raw_day, host_name, page_path = dimensions
            if _OTHER in {raw_day, host_name, page_path}:
                stats.other_row += 1
                continue
            day = _parse_ga4_date(raw_day)
            if day is None or not host_name.strip() or not page_path.startswith("/"):
                stats.rejected_invalid += 1
                continue
            page_id = stats.resolve(self._resolver, f"https://{host_name.strip()}{page_path}")
            if page_id is None:
                continue
            out.append(
                PageMetrics(
                    page_id=page_id,
                    source=MetricSource.GA4,
                    range=DateRange(start=day, end=day),
                    pageviews=metrics[0],
                    sessions=metrics[1],
                )
            )

    async def health(self) -> ProviderHealth:
        if not self._settings.analytics_google_enabled:
            return ProviderHealth(
                provider=self.name, reachable=False, error="google egress is disabled"
            )
        return ProviderHealth(provider=self.name, reachable=True)

    async def aclose(self) -> None:
        await self._egress.aclose()
