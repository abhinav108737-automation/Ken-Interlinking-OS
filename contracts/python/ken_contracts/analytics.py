"""Analytics: provider protocols, stored vocabulary, event payload, API read models.

FROZEN CONTRACT. Phase 0 froze :class:`AnalyticsProvider` (read-only: analytics is a
measurement input and never a mutation target). Phase 7 (docs/adr/ADR-0015-analytics.md, whose
section 12 amendments are binding) adds, ADDITIVELY -- every Phase 0 name and field keeps
working:

* ``PageMetrics.pageviews`` / ``PageMetrics.position``;
* :class:`FetchStats`, :class:`AnalyticsBatch`, :class:`Dataset` and the batch protocol
  :class:`AnalyticsBatchProvider` (every Phase 7 provider implements it);
* :class:`PageUrlResolver` (injected into providers at construction);
* the STORED vocabulary -- :class:`AnalyticsSource` (``ga4 | search_console | first_party``;
  ``MetricSource.CSV`` is a transport and is never stored) and :class:`ImportVia`
  (``api | csv | collector | synthetic``);
* :class:`AnalyticsUpdatedPayload` (``analytics.updated``, event_version 1);
* :class:`LinkMeasurement` (the append-only ``link_measurements`` snapshot, ADR-0015 §12.6.7);
* :class:`AnalyticsPolicy` (``policies/analytics/analytics.v<N>.yaml``);
* the GET API read models and :class:`AnalyticsReader`.

Provider rules (binding, ADR-0015 §4 as amended):

* Phase 7 providers return DAILY rows (``range.start == range.end``); the importer rejects
  any other row as ``non_daily_row``.
* ``LinkMetrics.link_id`` is ``str(plan_id)`` (ADR-0011: the plan id is the link id);
  ``PageMetrics.page_id`` is ``str(pages.id)`` as resolved by the :class:`PageUrlResolver`.
* A provider's ``ctr`` is ignored and recomputed; ``downstream_pageviews`` stays 0 (landing
  events are deferred).
* Rows carry their ORIGIN (``ga4`` / ``search_console`` / ``first_party``), never ``csv``.

Deferred to Phase 8 (ADR-0015 §12.8): experiments, landing events, query-level metrics, the
retention purge command, JSONL import, a standalone/production collector. Experiment
attribution is always DERIVED (source-page assignment x active interval), never stamped.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from datetime import date
from enum import StrEnum
from typing import Final, Literal, Protocol, Self, runtime_checkable
from uuid import UUID

from pydantic import AwareDatetime, Field, field_validator, model_validator

from ken_contracts.agents import CriticVerdict
from ken_contracts.common import (
    AsyncClosable,
    ContractModel,
    FrozenModel,
    LinkId,
    PageId,
    reject_unstorable_text,
)
from ken_contracts.errors import ListResponse
from ken_contracts.intelligence import RelationshipType
from ken_contracts.model import ProviderHealth
from ken_contracts.recommendations import LinkPlanStatus, ReasonCode

# --------------------------------------------------------------------------- #
# Constants (ADR-0015 §5 / §12)
# --------------------------------------------------------------------------- #
ANALYTICS_ACTOR: Final = "system:analytics"
"""The only actor analytics uses: it moves ``verified -> measured`` and nothing else."""
ANALYTICS_EMITTED_BY: Final = "ken-analytics"
SUBJECT_ANALYTICS_IMPORT: Final = "analytics_import"
"""``subject_type`` of ``analytics.updated``; ``subject_id`` is ``str(import_id)``."""
MAX_RANGE_DAYS: Final = 400
"""Largest inclusive span of one import or one API query."""
MAX_UNRESOLVED_SAMPLE: Final = 100
MAX_SAMPLE_PATH_CHARS: Final = 512
MAX_TOP_LINKS: Final = 10
MAX_FILE_NAME_CHARS: Final = 255
_SHA256: Final = r"^[0-9a-f]{64}$"
_PATH_ONLY: Final = re.compile(r"^/[^?#\s]*$")


# --------------------------------------------------------------------------- #
# Phase 0 (unchanged names; PageMetrics gains two optional fields)
# --------------------------------------------------------------------------- #
class MetricSource(StrEnum):
    GA4 = "ga4"
    SEARCH_CONSOLE = "search_console"
    FIRST_PARTY = "first_party"
    CSV = "csv"
    """Kept for compatibility; never stored (CSV is a transport, rows carry their origin)."""


class DateRange(FrozenModel):
    start: date
    end: date


class LinkMetrics(ContractModel):
    """Observed performance of one deployed internal link."""

    link_id: str
    source: MetricSource
    range: DateRange
    impressions: int = Field(default=0, ge=0)
    clicks: int = Field(default=0, ge=0)
    ctr: float | None = Field(default=None, ge=0.0, le=1.0)
    downstream_pageviews: int = Field(default=0, ge=0)


class PageMetrics(ContractModel):
    """Observed performance of one page."""

    page_id: str
    source: MetricSource
    range: DateRange
    impressions: int = Field(default=0, ge=0)
    clicks: int = Field(default=0, ge=0)
    sessions: int = Field(default=0, ge=0)
    pageviews: int = Field(default=0, ge=0, description="Phase 7: GA4 screenPageViews.")
    position: float | None = Field(
        default=None,
        ge=1.0,
        description="Phase 7: GSC average position (impression-weighted) for the row's day.",
    )


class QueryMetrics(ContractModel):
    """One Search Console query for one resolved page and day."""

    page_id: str
    source: MetricSource = MetricSource.SEARCH_CONSOLE
    range: DateRange
    query: str = Field(min_length=1, max_length=500)
    impressions: int = Field(default=0, ge=0)
    clicks: int = Field(default=0, ge=0)
    position: float | None = Field(default=None, ge=1.0)

    @field_validator("query")
    @classmethod
    def _storable_query(cls, value: str) -> str:
        return reject_unstorable_text(value)


@runtime_checkable
class AnalyticsProvider(AsyncClosable, Protocol):
    """Read-only measurement input. Deliberately has no write method."""

    name: str

    async def fetch_link_metrics(
        self, *, link_ids: Sequence[LinkId], date_range: DateRange
    ) -> list[LinkMetrics]: ...

    async def fetch_page_metrics(
        self, *, page_ids: Sequence[PageId], date_range: DateRange
    ) -> list[PageMetrics]: ...

    async def health(self) -> ProviderHealth: ...


# --------------------------------------------------------------------------- #
# Phase 7: stored vocabulary
# --------------------------------------------------------------------------- #
class AnalyticsSource(StrEnum):
    """Where a STORED row came from (the stored subset of :class:`MetricSource`)."""

    GA4 = "ga4"
    SEARCH_CONSOLE = "search_console"
    FIRST_PARTY = "first_party"


LINK_SOURCES: Final = frozenset({AnalyticsSource.GA4, AnalyticsSource.FIRST_PARTY})
"""Sources with link-level rows (``link_metrics_daily``). GSC has no link-level data."""
PAGE_SOURCES: Final = frozenset({AnalyticsSource.GA4, AnalyticsSource.SEARCH_CONSOLE})
"""Sources with page-level rows (``page_metrics_daily``)."""
QUERY_SOURCES: Final = frozenset({AnalyticsSource.SEARCH_CONSOLE})
"""Search Console is the sole source of query-level rows."""


class Dataset(StrEnum):
    LINKS = "links"
    PAGES = "pages"
    QUERIES = "queries"


class ImportVia(StrEnum):
    """How rows reached the database (recorded on every ledger row)."""

    API = "api"
    """GA4 Data API / Search Console API (ASK: Google egress, off by default)."""
    CSV = "csv"
    """``ken_link_daily`` / ``ken_page_daily`` (ISO dates)."""
    COLLECTOR = "collector"
    """Aggregation of first-party ``link_events``."""
    SYNTHETIC = "synthetic"
    """The seeded demo generator (always ``synthetic = true``)."""


class LinkEventType(StrEnum):
    """``link_events.event_type`` (landing events are deferred)."""

    IMPRESSION = "internal_link_impression"
    CLICK = "internal_link_click"


class MatchMode(StrEnum):
    """How the collector identified the link (ADR-0015 §12.6.4)."""

    ATTR = "attr"
    """``a[data-ken-link-id]``."""
    HREF = "href"
    """(normalised source path, href equivalent to ``deployment_items.href``, exact anchor)."""


def _sample_path(value: str) -> str:
    reject_unstorable_text(value)
    if len(value) > MAX_SAMPLE_PATH_CHARS or not _PATH_ONLY.fullmatch(value):
        raise ValueError("an unresolved sample is a bounded path without query or fragment")
    return value


# --------------------------------------------------------------------------- #
# Phase 7: provider batch protocol
# --------------------------------------------------------------------------- #
class FetchStats(FrozenModel):
    """What a provider read and could not use. Counters, never row contents."""

    rows_read: int = Field(default=0, ge=0)
    unresolved_url: int = Field(default=0, ge=0, description="URL matched no page.")
    unresolved_link: int = Field(default=0, ge=0, description="Link id is not a known plan.")
    ambiguous_url: int = Field(default=0, ge=0, description="URL matched two pages.")
    other_row: int = Field(default=0, ge=0, description="GA4 '(other)' rows.")
    rejected_invalid: int = Field(default=0, ge=0, description="Malformed rows or lines.")
    sampled: bool = False
    unresolved_sample: tuple[str, ...] = Field(
        default=(),
        max_length=MAX_UNRESOLVED_SAMPLE,
        description="Up to 100 unresolved PATHS (no query string, no host), for the ledger.",
    )

    @field_validator("unresolved_sample")
    @classmethod
    def _paths(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        for item in value:
            _sample_path(item)
        return value


class AnalyticsBatch(FrozenModel):
    """One provider fetch. Every row is daily and carries its origin source."""

    link_rows: tuple[LinkMetrics, ...] = ()
    page_rows: tuple[PageMetrics, ...] = ()
    query_rows: tuple[QueryMetrics, ...] = ()
    stats: FetchStats = Field(default_factory=FetchStats)


@runtime_checkable
class PageUrlResolver(Protocol):
    """URL -> ``str(pages.id)``, built once per import (``InventoryUrlResolver``).

    Drops query and fragment, normalises (``normalize_url``), keys on (host, path) ignoring
    the scheme, over ``pages.canonical_url`` first and then ``known_urls.page_id``. A key that
    maps to two pages is ambiguous (``None``; the provider counts ``ambiguous_url``).
    Injected at provider construction, never passed through method signatures. Synchronous:
    it is an in-memory lookup.
    """

    def resolve(self, url: str) -> PageId | None: ...


@runtime_checkable
class AnalyticsBatchProvider(AnalyticsProvider, Protocol):
    """Every Phase 7 provider (mock, csv, ga4, gsc, stored). ``fetch_link_metrics`` /
    ``fetch_page_metrics`` are thin filters over :meth:`fetch_batch`.

    Rows outside ``date_range`` are the importer's to reject (``outside_range``); a provider
    never sends link ids or URLs to Google (only the property id and dates)."""

    async def fetch_batch(
        self, *, date_range: DateRange, datasets: frozenset[Dataset]
    ) -> AnalyticsBatch: ...


# --------------------------------------------------------------------------- #
# Phase 7: read models (API)
# --------------------------------------------------------------------------- #
class MetricTotals(FrozenModel):
    """Link-level totals. ``ctr`` is ``clicks / impressions``, ``None`` when there are no
    impressions. It may exceed 1 (a source can count clicks without impressions)."""

    impressions: int = Field(ge=0)
    clicks: int = Field(ge=0)
    ctr: float | None = Field(default=None, ge=0.0)

    @model_validator(mode="after")
    def _ctr_iff_impressions(self) -> Self:
        if (self.ctr is None) != (self.impressions == 0):
            raise ValueError("ctr is null exactly when impressions is 0")
        return self

    @classmethod
    def of(cls, *, impressions: int, clicks: int) -> MetricTotals:
        return cls(
            impressions=impressions,
            clicks=clicks,
            ctr=None if impressions == 0 else clicks / impressions,
        )


class SourceFreshness(FrozenModel):
    """The newest stored data of one (source, dataset)."""

    source: AnalyticsSource
    dataset: Dataset
    latest_date: date | None = Field(default=None, description="Newest metric_date stored.")
    last_import_id: UUID | None = None
    last_via: ImportVia | None = None
    last_imported_at: AwareDatetime | None = None
    synthetic: bool = False


class DailyTotals(FrozenModel):
    """Link-level totals over all links for one (day, source)."""

    metric_date: date
    source: AnalyticsSource
    impressions: int = Field(ge=0)
    clicks: int = Field(ge=0)


class LinkPerformanceView(FrozenModel):
    plan_id: UUID
    anchor_text: str
    source_page_id: UUID
    source_url: str
    target_page_id: UUID
    target_url: str
    plan_status: LinkPlanStatus
    deployed_at: AwareDatetime | None = None
    rolled_back_at: AwareDatetime | None = None
    totals: dict[AnalyticsSource, MetricTotals] = Field(
        description="Per link source (ga4, first_party) over the requested range."
    )
    synthetic: bool = False


class AnalyticsSummary(FrozenModel):
    range: DateRange
    sources: list[SourceFreshness]
    totals_by_source: dict[AnalyticsSource, MetricTotals]
    daily: list[DailyTotals] = Field(description="Sorted by (metric_date, source).")
    live_links: int = Field(ge=0, description="Plans whose live window is open now.")
    links_with_data: int = Field(ge=0, description="Plans with any link-level row in range.")
    top_links: list[LinkPerformanceView] = Field(max_length=MAX_TOP_LINKS)
    synthetic: bool = Field(description="Any synthetic row contributed to this view.")


class LinkAttribution(FrozenModel):
    """Plan -> recommendation run -> agent -> human -> deployment -> rollback (ADR-0015 §3),
    computed by one query; never stored."""

    plan_id: UUID
    plan_status: LinkPlanStatus
    anchor_text: str
    anchor_source: str
    relationship_type: RelationshipType | None = None
    reason_codes: tuple[ReasonCode, ...]
    supersedes: UUID | None = None
    recommendation_run_id: UUID | None = None
    engine_version: str
    policy_version: int
    ranking_policy_version: int
    analysis_version: str
    calibration_version: str | None = None
    source_page_id: UUID
    source_url: str
    source_page_version: int
    target_page_id: UUID
    target_url: str
    target_page_version: int
    agent_run_id: UUID | None = None
    agent_verdict: CriticVerdict | None = None
    agent_model: str | None = None
    agent_graph_version: str | None = None
    approved_by: str | None = Field(default=None, description="Actor of the approval.")
    approved_at: AwareDatetime | None = None
    deployment_id: UUID | None = Field(default=None, description="The deployment that applied it.")
    cms_provider: str | None = None
    deployed_at: AwareDatetime | None = Field(
        default=None, description="The link_plan_transitions row to 'deployed'."
    )
    verified_at: AwareDatetime | None = None
    rolled_back_at: AwareDatetime | None = Field(
        default=None, description="The link_plan_transitions row to 'rolled_back'."
    )


class LiveWindow(FrozenModel):
    """When the link was on the page: ``[deployed_at, rolled_back_at)`` from the
    ``link_plan_transitions`` rows to ``deployed`` and ``rolled_back`` (ADR-0015 §12.6.3).
    ``deployed_at is None``: never on a page (superseded, rejected, never applied)."""

    plan_id: UUID
    deployed_at: AwareDatetime | None = None
    rolled_back_at: AwareDatetime | None = None

    @model_validator(mode="after")
    def _ordered(self) -> Self:
        if (
            self.deployed_at is not None
            and self.rolled_back_at is not None
            and self.rolled_back_at < self.deployed_at
        ):
            raise ValueError("rolled_back_at precedes deployed_at")
        return self

    @property
    def ever_live(self) -> bool:
        return self.deployed_at is not None

    def contains(self, moment: AwareDatetime) -> bool:
        """Whether ``moment`` lies in ``[deployed_at, rolled_back_at)``."""
        if self.deployed_at is None or moment < self.deployed_at:
            return False
        return self.rolled_back_at is None or moment < self.rolled_back_at


class LinkDailyPoint(FrozenModel):
    metric_date: date
    impressions: int = Field(ge=0)
    clicks: int = Field(ge=0)


class ConfounderFlag(StrEnum):
    OVERLAPPING_CHANGES = "overlapping_changes"
    """Another plan was applied to the same page inside either window."""
    CONTENT_CHANGED = "content_changed"
    """A new ``page_versions`` row inside the windows."""
    POST_WINDOW_TRUNCATED = "post_window_truncated"
    ROLLED_BACK_IN_WINDOW = "rolled_back_in_window"
    INSUFFICIENT_DATA = "insufficient_data"
    """A window has fewer than ``windows.min_covered_days`` covered days."""
    SAMPLED_DATA = "sampled_data"
    SYNTHETIC_DATA = "synthetic_data"


class WindowStats(FrozenModel):
    """One pre or post window of one page and source. Means are per COVERED day."""

    start: date
    end: date | None = Field(default=None, description="None: the window is empty.")
    days: int = Field(ge=0, description="Calendar days in the window.")
    covered_days: int = Field(ge=0, description="Days with a stored row.")
    impressions: int = Field(ge=0)
    clicks: int = Field(ge=0)
    pageviews: int = Field(ge=0)
    sessions: int = Field(ge=0)
    ctr: float | None = Field(default=None, ge=0.0)
    avg_position: float | None = Field(
        default=None, ge=1.0, description="sum(position_sum) / sum(impressions)."
    )
    mean_per_day: float | None = Field(
        default=None, ge=0.0, description="Mean of the comparison metric per covered day."
    )

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        if self.covered_days > self.days:
            raise ValueError("covered_days exceeds days")
        if (self.end is None) != (self.days == 0):
            raise ValueError("end is null exactly when the window is empty")
        return self


class PageWindowComparison(FrozenModel):
    """Before/after of one page around one plan's ``anchor_date`` = D(deployed_at), which is
    excluded as a partial day (ADR-0015 §3). Observed change, NOT a causal effect."""

    page_id: UUID
    role: Literal["source", "target"]
    plan_id: UUID
    source: AnalyticsSource = Field(description="ga4 or search_console.")
    metric: Literal["clicks", "pageviews"] = Field(
        description="The compared metric: clicks for search_console, pageviews for ga4."
    )
    anchor_date: date
    pre: WindowStats
    post: WindowStats
    delta_per_day: float | None = Field(
        default=None, description="post mean - pre mean; only when both windows are sufficient."
    )
    ratio: float | None = Field(default=None, ge=0.0, description="post mean / pre mean.")
    site_trend_ratio: float | None = Field(
        default=None, ge=0.0, description="Same windows over all pages: context, never applied."
    )
    flags: list[ConfounderFlag]
    synthetic: bool = False


class LinkMeasurement(FrozenModel):
    """``link_measurements`` row (append-only, one per plan), written in the SAME transaction
    as ``verified -> measured`` so a measurement stays reproducible after replace-imports
    (ADR-0015 §12.6.7). Synthetic data never measures."""

    plan_id: UUID
    measured_at: AwareDatetime
    analytics_policy_version: int = Field(ge=1)
    window_start: date = Field(description="D(deployed_at).")
    window_end: date = Field(description="D(deployed_at) + measurement_window_days.")
    link_totals: dict[AnalyticsSource, MetricTotals]
    target_comparison: list[PageWindowComparison]
    source_comparison: list[PageWindowComparison]
    flags: list[ConfounderFlag]
    import_ids: list[UUID] = Field(min_length=1, description="The ledger rows measured.")

    @model_validator(mode="after")
    def _valid(self) -> Self:
        if self.window_end < self.window_start:
            raise ValueError("window_end precedes window_start")
        if len(set(self.import_ids)) != len(self.import_ids):
            raise ValueError("import_ids must be unique")
        if ConfounderFlag.SYNTHETIC_DATA in self.flags:
            raise ValueError("synthetic data never measures")
        return self


class LinkPerformanceDetail(FrozenModel):
    range: DateRange
    attribution: LinkAttribution
    live_window: LiveWindow
    series: dict[AnalyticsSource, list[LinkDailyPoint]] = Field(
        description="Empty when the plan was never deployed."
    )
    totals: dict[AnalyticsSource, MetricTotals]
    target_impact: list[PageWindowComparison]
    source_impact: list[PageWindowComparison]
    measurement: LinkMeasurement | None = None
    synthetic: bool = False


class PageDailyPoint(FrozenModel):
    metric_date: date
    impressions: int = Field(ge=0)
    clicks: int = Field(ge=0)
    avg_position: float | None = Field(default=None, ge=1.0)
    pageviews: int = Field(ge=0)
    sessions: int = Field(ge=0)


class PageImpactView(FrozenModel):
    page_id: UUID
    url: str
    window_days: int = Field(ge=7, le=90)
    series: dict[AnalyticsSource, list[PageDailyPoint]] = Field(
        description="Per page source (ga4, search_console)."
    )
    changes: list[PageWindowComparison] = Field(
        description="One per (plan deployed on or to this page, page source)."
    )
    synthetic: bool = False


class AnalyticsImportView(FrozenModel):
    """One ``analytics_imports`` ledger row (written once, on success). Counters live on the
    job run."""

    id: UUID
    job_run_id: UUID
    source: AnalyticsSource
    dataset: Dataset
    via: ImportVia
    synthetic: bool
    replace_via: bool
    date_start: date
    date_end: date
    source_timezone: str
    file_name: str | None = None
    file_sha256: str | None = Field(default=None, pattern=_SHA256)
    sampled: bool
    rows_written: int = Field(ge=0)
    analytics_policy_version: int = Field(ge=1)
    unresolved_sample: list[str] = Field(max_length=MAX_UNRESOLVED_SAMPLE)
    triggered_by: str
    created_at: AwareDatetime
    counters: dict[str, int] = Field(description="The job run's counters (ADR-0015 §5).")


class LinkSort(StrEnum):
    CLICKS = "clicks"
    IMPRESSIONS = "impressions"
    CTR = "ctr"


class SortOrder(StrEnum):
    ASC = "asc"
    DESC = "desc"


@runtime_checkable
class AnalyticsReader(Protocol):
    """The API's only route to stored analytics (READ ONLY sessions).

    ``date_range is None`` means the default: the 28 days ending at the latest stored date.
    Unknown plan: :class:`~ken_contracts.recommendations.PlanNotFoundError`; unknown page:
    :class:`~ken_contracts.inventory.InventoryNotFoundError` ``("page")``.
    """

    async def summary(self, *, date_range: DateRange | None) -> AnalyticsSummary: ...

    async def list_links(
        self,
        *,
        date_range: DateRange | None,
        limit: int,
        offset: int,
        source: AnalyticsSource | None = None,
        source_page_id: UUID | None = None,
        target_page_id: UUID | None = None,
        deployment_id: UUID | None = None,
        plan_status: LinkPlanStatus | None = None,
        sort: LinkSort = LinkSort.CLICKS,
        order: SortOrder = SortOrder.DESC,
    ) -> ListResponse[LinkPerformanceView]: ...

    async def get_link(
        self, *, plan_id: UUID, date_range: DateRange | None
    ) -> LinkPerformanceDetail: ...

    async def page_impact(self, *, page_id: UUID, window_days: int) -> PageImpactView: ...

    async def list_imports(
        self,
        *,
        limit: int,
        offset: int,
        source: AnalyticsSource | None = None,
        dataset: Dataset | None = None,
    ) -> ListResponse[AnalyticsImportView]: ...


# --------------------------------------------------------------------------- #
# Phase 7: typed event payload (event_version 1)
# --------------------------------------------------------------------------- #
class AnalyticsUpdatedPayload(FrozenModel):
    """``analytics.updated``: once per SUCCEEDED import or aggregation, in the same
    transaction as its ledger row. ``subject_type = "analytics_import"``, ``subject_id =
    str(import_id)``, ``emitted_by = "ken-analytics"``, ``correlation_id = job_run_id``.
    Failed runs emit only ``job.completed``; the collector emits no events."""

    import_id: UUID
    job_run_id: UUID
    source: AnalyticsSource
    dataset: Dataset
    via: ImportVia
    synthetic: bool
    date_start: date
    date_end: date
    rows_written: int = Field(ge=0)
    rows_unresolved: int = Field(ge=0)
    rows_rejected: int = Field(ge=0)
    links_affected: int = Field(ge=0)
    pages_affected: int = Field(ge=0)
    plans_measured: int = Field(ge=0)

    @model_validator(mode="after")
    def _valid(self) -> Self:
        if self.date_end < self.date_start:
            raise ValueError("date_end precedes date_start")
        if self.synthetic and self.plans_measured:
            raise ValueError("synthetic data never measures")
        if self.via is ImportVia.SYNTHETIC and not self.synthetic:
            raise ValueError("via 'synthetic' is always synthetic")
        return self


# --------------------------------------------------------------------------- #
# Phase 7: policy (policies/analytics/analytics.v<N>.yaml)
# --------------------------------------------------------------------------- #
class AnalyticsWindowPolicy(FrozenModel):
    comparison_days: int = Field(default=28, ge=7, le=90, description="N of pre/post windows.")
    min_covered_days: int = Field(
        default=14, ge=1, description="Both windows need this many for delta/ratio."
    )
    measurement_window_days: int = Field(
        default=28, ge=1, le=365, description="verified -> measured once D0 + this is covered."
    )

    @model_validator(mode="after")
    def _bounded(self) -> Self:
        if self.min_covered_days > self.comparison_days:
            raise ValueError("min_covered_days exceeds comparison_days")
        return self


class SourceLagPolicy(FrozenModel):
    """Newest importable day = today - lag (partial days are never stored)."""

    ga4: int = Field(default=1, ge=0, le=30)
    search_console: int = Field(default=3, ge=0, le=30)
    first_party: int = Field(default=0, ge=0, le=30)


class RateLimitPolicy(FrozenModel):
    per_ip_events_per_minute: int = Field(default=120, ge=1, le=100_000)
    global_events_per_minute: int = Field(default=6000, ge=1, le=10_000_000)
    max_tracked_ips: int = Field(
        default=10_000, ge=1, le=1_000_000, description="LRU-bounded fixed-window map."
    )


class CollectorPolicy(FrozenModel):
    max_body_bytes: int = Field(
        default=4096, ge=256, le=4096, description="Counted on the streamed bytes."
    )
    max_events_per_request: int = Field(default=50, ge=1, le=50)
    max_path_chars: int = Field(default=512, ge=1, le=512)
    max_href_chars: int = Field(default=2048, ge=1, le=2048)
    max_anchor_chars: int = Field(default=120, ge=1, le=120)
    max_href_links_per_page: int = Field(default=50, ge=1, le=50)
    live_index_refresh_seconds: float = Field(default=30.0, gt=0, le=3600)
    rejection_log_interval_seconds: float = Field(default=60.0, gt=0, le=3600)
    beacon_flush_seconds: float = Field(default=5.0, gt=0, le=60)
    beacon_cache_max_age_seconds: int = Field(default=300, ge=0, le=86_400)
    rate_limit: RateLimitPolicy = Field(default_factory=RateLimitPolicy)


class AggregationPolicy(FrozenModel):
    trailing_days: int = Field(
        default=3, ge=1, le=31, description="Default re-aggregation window (late events)."
    )


class ImportPolicy(FrozenModel):
    max_rows: int = Field(default=1_000_000, ge=1, le=10_000_000)
    max_unresolved_sample: int = Field(default=100, ge=0, le=MAX_UNRESOLVED_SAMPLE)
    max_queries_per_page_day: int = Field(default=50, ge=1, le=500)


class RetentionPolicy(FrozenModel):
    """RECORDED for later: Phase 7 enforces none of these (the purge command is deferred)."""

    link_events_days: int = Field(default=90, ge=1)
    daily_aggregates_months: int = Field(default=25, ge=1)


class AnalyticsPolicy(FrozenModel):
    version: int = Field(ge=1)
    windows: AnalyticsWindowPolicy
    source_lag_days: SourceLagPolicy
    aggregation: AggregationPolicy
    collector: CollectorPolicy
    bot_ua_patterns: tuple[str, ...] = Field(
        min_length=1,
        max_length=100,
        description="Case-insensitive regular expressions; a match drops the request.",
    )
    imports: ImportPolicy
    retention: RetentionPolicy

    @field_validator("bot_ua_patterns")
    @classmethod
    def _patterns_compile(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        for pattern in value:
            if not pattern or len(pattern) > 200:
                raise ValueError("a bot pattern is 1-200 characters")
            try:
                re.compile(pattern, re.IGNORECASE)
            except re.error as exc:
                raise ValueError("a bot pattern is not a valid regular expression") from exc
        return value

    def lag_days(self, source: AnalyticsSource) -> int:
        return int(getattr(self.source_lag_days, AnalyticsSource(source).value))


__all__ = [
    "ANALYTICS_ACTOR",
    "ANALYTICS_EMITTED_BY",
    "LINK_SOURCES",
    "MAX_FILE_NAME_CHARS",
    "MAX_RANGE_DAYS",
    "MAX_TOP_LINKS",
    "MAX_UNRESOLVED_SAMPLE",
    "PAGE_SOURCES",
    "QUERY_SOURCES",
    "SUBJECT_ANALYTICS_IMPORT",
    "AggregationPolicy",
    "AnalyticsBatch",
    "AnalyticsBatchProvider",
    "AnalyticsImportView",
    "AnalyticsPolicy",
    "AnalyticsProvider",
    "AnalyticsReader",
    "AnalyticsSource",
    "AnalyticsSummary",
    "AnalyticsUpdatedPayload",
    "AnalyticsWindowPolicy",
    "CollectorPolicy",
    "ConfounderFlag",
    "DailyTotals",
    "Dataset",
    "DateRange",
    "FetchStats",
    "ImportPolicy",
    "ImportVia",
    "LinkAttribution",
    "LinkDailyPoint",
    "LinkEventType",
    "LinkMeasurement",
    "LinkMetrics",
    "LinkPerformanceDetail",
    "LinkPerformanceView",
    "LinkSort",
    "LiveWindow",
    "MatchMode",
    "MetricSource",
    "MetricTotals",
    "PageDailyPoint",
    "PageImpactView",
    "PageMetrics",
    "PageUrlResolver",
    "PageWindowComparison",
    "QueryMetrics",
    "RateLimitPolicy",
    "RetentionPolicy",
    "SortOrder",
    "SourceFreshness",
    "SourceLagPolicy",
    "WindowStats",
]
