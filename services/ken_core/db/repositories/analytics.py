"""Analytics repository: SQL primitives for migration 0008 (ADR-0015 §2 as amended by §12).

The caller owns the transaction: nothing here commits. Three rules shape this module:

* ``link_events``, ``link_metrics_daily`` and ``page_metrics_daily`` reject every UPDATE and
  allow DELETE only in a transaction that ran :func:`enable_writer` -- so a replace import
  is DELETE + INSERT, never an UPSERT;
* ``analytics_imports`` and ``link_measurements`` are append-only, so their rows are
  inserted complete (``rows_written`` and the unresolved sample are known before the insert,
  and the ledger row goes in BEFORE the metric rows that reference it by foreign key);
* a plan's live window comes from ``link_plan_transitions`` only (amendment 3), never from a
  ``deployment_items`` stage.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Final

from sqlalchemy import Float, Text, case, cast, delete, func, literal, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from ken_contracts.analytics import AnalyticsSource, Dataset, LinkSort, SortOrder
from ken_contracts.deployment import DeploymentStatus
from ken_contracts.recommendations import LinkPlanStatus
from ken_core.db.models.analytics import (
    ANALYTICS_WRITER_SETTING,
    AnalyticsImport,
    LinkMeasurementRow,
    LinkMetricsDaily,
    PageMetricsDaily,
    PageQueryMetricsDaily,
)
from ken_core.db.models.deployment import Deployment, DeploymentItem, ItemStage
from ken_core.db.models.inventory import KnownUrl, Page, PageVersion
from ken_core.db.models.recommendations import LinkPlan, LinkPlanTransition

__all__ = [
    "DEPLOYED",
    "ROLLED_BACK",
    "DailyEventCount",
    "DeployedPlan",
    "LatestDate",
    "LinkDaily",
    "LinkRow",
    "LinkTotals",
    "PageDaily",
    "PageRow",
    "QueryRow",
    "TransitionWindow",
    "VerifiedPlan",
    "aggregate_link_events",
    "content_change_dates",
    "deployed_plans",
    "deployment_item_hrefs",
    "enable_writer",
    "existing_link_origins",
    "existing_page_origins",
    "existing_query_origins",
    "has_non_synthetic_rows",
    "insert_import",
    "insert_link_rows",
    "insert_measurement",
    "insert_page_rows",
    "insert_query_rows",
    "latest_link_dates",
    "latest_page_dates",
    "ledger_row",
    "link_daily_rows",
    "link_totals_by_plan",
    "list_ledger",
    "live_transition_moments",
    "max_covered_link_date",
    "measurement_of",
    "newest_import",
    "page_daily_rows",
    "page_exists",
    "page_ids_present",
    "plan_ids_present",
    "rank_deployed_plans",
    "replace_delete_links",
    "replace_delete_pages",
    "replace_delete_queries",
    "source_is_rolling_back",
    "total_link_events",
    "url_index_rows",
    "verified_plan_rows",
]

DEPLOYED: Final = LinkPlanStatus.DEPLOYED.value
ROLLED_BACK: Final = LinkPlanStatus.ROLLED_BACK.value
_APPLIED_STAGES: Final = (ItemStage.APPLIED, ItemStage.ALREADY_APPLIED)


@dataclass(frozen=True, slots=True)
class LinkRow:
    """One prepared ``link_metrics_daily`` row (natural key + counts)."""

    source: AnalyticsSource
    metric_date: date
    plan_id: uuid.UUID
    impressions: int
    clicks: int

    @property
    def key(self) -> tuple[str, date, uuid.UUID]:
        return (self.source.value, self.metric_date, self.plan_id)


@dataclass(frozen=True, slots=True)
class PageRow:
    """One prepared ``page_metrics_daily`` row (natural key + counts)."""

    source: AnalyticsSource
    metric_date: date
    page_id: uuid.UUID
    impressions: int
    clicks: int
    position_sum: float
    pageviews: int
    sessions: int

    @property
    def key(self) -> tuple[str, date, uuid.UUID]:
        return (self.source.value, self.metric_date, self.page_id)


@dataclass(frozen=True, slots=True)
class QueryRow:
    metric_date: date
    page_id: uuid.UUID
    query_hash: str
    query: str
    impressions: int
    clicks: int
    position_sum: float

    @property
    def key(self) -> tuple[date, uuid.UUID, str]:
        return (self.metric_date, self.page_id, self.query_hash)


@dataclass(frozen=True, slots=True)
class TransitionWindow:
    """The two ``link_plan_transitions`` moments that ARE a plan's live window."""

    plan_id: uuid.UUID
    deployed_at: datetime | None
    rolled_back_at: datetime | None


@dataclass(frozen=True, slots=True)
class VerifiedPlan:
    plan_id: uuid.UUID
    source_page_id: uuid.UUID
    target_page_id: uuid.UUID


@dataclass(frozen=True, slots=True)
class LatestDate:
    source: AnalyticsSource
    latest_date: date | None
    synthetic: bool


@dataclass(frozen=True, slots=True)
class DailyEventCount:
    plan_id: uuid.UUID
    event_date: date
    synthetic: bool
    impressions: int
    clicks: int


# --------------------------------------------------------------------------- #
# The writer gate
# --------------------------------------------------------------------------- #
async def enable_writer(session: AsyncSession) -> None:
    """``set_config('ken.analytics_writer', 'on', true)``: transaction-local, so it cannot
    leak to the next transaction on a pooled connection (0008's guard function)."""
    await session.execute(
        text("SELECT set_config(:name, 'on', true)"), {"name": ANALYTICS_WRITER_SETTING}
    )


# --------------------------------------------------------------------------- #
# URL -> page index (built once per import)
# --------------------------------------------------------------------------- #
async def url_index_rows(
    session: AsyncSession,
) -> tuple[list[tuple[str, uuid.UUID]], list[tuple[str, uuid.UUID]]]:
    """``(pages.canonical_url, id)`` then ``(known_urls.url, page_id)``: the canonical URLs
    win, the known URLs fill the rest (ADR-0015 §4 "URL -> page")."""
    canonical = await session.execute(
        select(Page.canonical_url, Page.id).order_by(Page.canonical_url)
    )
    known = await session.execute(
        select(KnownUrl.url, KnownUrl.page_id)
        .where(KnownUrl.page_id.is_not(None))
        .order_by(KnownUrl.url)
    )
    return (
        [(row[0], row[1]) for row in canonical],
        [(row[0], row[1]) for row in known if row[1] is not None],
    )


# --------------------------------------------------------------------------- #
# Plans, live windows, deployments
# --------------------------------------------------------------------------- #
async def plan_ids_present(session: AsyncSession, plan_ids: Iterable[uuid.UUID]) -> set[uuid.UUID]:
    wanted = list(dict.fromkeys(plan_ids))
    if not wanted:
        return set()
    found = await session.execute(select(LinkPlan.id).where(LinkPlan.id.in_(wanted)))
    return set(found.scalars())


async def page_ids_present(session: AsyncSession, page_ids: Iterable[uuid.UUID]) -> set[uuid.UUID]:
    wanted = list(dict.fromkeys(page_ids))
    if not wanted:
        return set()
    found = await session.execute(select(Page.id).where(Page.id.in_(wanted)))
    return set(found.scalars())


async def page_exists(session: AsyncSession, *, page_id: uuid.UUID) -> bool:
    return (
        await session.scalar(select(func.count()).select_from(Page).where(Page.id == page_id))
    ) == 1


async def live_transition_moments(
    session: AsyncSession, plan_ids: Iterable[uuid.UUID] | None = None
) -> list[TransitionWindow]:
    """Per plan: the FIRST move to ``deployed`` and the first to ``rolled_back``.

    These two rows ARE the live window (amendment 3). ``plan_ids = None`` means every plan
    that was ever deployed.
    """
    statement = (
        select(
            LinkPlanTransition.plan_id,
            func.min(LinkPlanTransition.occurred_at)
            .filter(LinkPlanTransition.to_status == DEPLOYED)
            .label("deployed_at"),
            func.min(LinkPlanTransition.occurred_at)
            .filter(LinkPlanTransition.to_status == ROLLED_BACK)
            .label("rolled_back_at"),
        )
        .where(LinkPlanTransition.to_status.in_([DEPLOYED, ROLLED_BACK]))
        .group_by(LinkPlanTransition.plan_id)
    )
    if plan_ids is not None:
        wanted = list(dict.fromkeys(plan_ids))
        if not wanted:
            return []
        statement = statement.where(LinkPlanTransition.plan_id.in_(wanted))
    found = await session.execute(statement)
    return [
        TransitionWindow(
            plan_id=row.plan_id,
            deployed_at=row.deployed_at,
            rolled_back_at=row.rolled_back_at,
        )
        for row in found
    ]


async def source_is_rolling_back(session: AsyncSession, source_page_id: uuid.UUID) -> bool:
    """Whether ONE source page has a ``rolling_back`` deployment right now.

    The measure sweep asks per source, after it holds that source's deploy lock: a single
    set read before the loop would be time-of-check-to-time-of-use (§12.4).
    """
    found = await session.scalar(
        select(Deployment.id)
        .where(
            Deployment.source_page_id == source_page_id,
            Deployment.status == DeploymentStatus.ROLLING_BACK.value,
        )
        .limit(1)
    )
    return found is not None


async def deployment_item_hrefs(
    session: AsyncSession, plan_ids: Iterable[uuid.UUID]
) -> dict[uuid.UUID, str]:
    """The href actually WRITTEN per plan (stage ``applied`` / ``already_applied``), newest
    first (amendment 1b / 4: never compare against ``plan.target_url``)."""
    wanted = list(dict.fromkeys(plan_ids))
    if not wanted:
        return {}
    found = await session.execute(
        select(DeploymentItem.plan_id, DeploymentItem.href)
        .where(
            DeploymentItem.plan_id.in_(wanted),
            DeploymentItem.stage.in_(_APPLIED_STAGES),
        )
        .order_by(DeploymentItem.id)
    )
    return {row.plan_id: row.href for row in found}


async def verified_plan_rows(session: AsyncSession) -> list[VerifiedPlan]:
    """Every ``verified`` plan with no measurement yet, ordered by (source page, plan id)."""
    measured = select(LinkMeasurementRow.plan_id)
    found = await session.execute(
        select(LinkPlan.id, LinkPlan.source_page_id, LinkPlan.target_page_id)
        .where(
            LinkPlan.status == LinkPlanStatus.VERIFIED.value,
            LinkPlan.id.not_in(measured),
        )
        .order_by(LinkPlan.source_page_id, LinkPlan.id)
    )
    return [
        VerifiedPlan(
            plan_id=row.id,
            source_page_id=row.source_page_id,
            target_page_id=row.target_page_id,
        )
        for row in found
    ]


# --------------------------------------------------------------------------- #
# Replace semantics
# --------------------------------------------------------------------------- #
async def existing_link_origins(
    session: AsyncSession, *, source: AnalyticsSource, start: date, end: date
) -> set[tuple[bool, str]]:
    """The distinct ``(synthetic, via)`` of the rows an import would replace."""
    found = await session.execute(
        select(LinkMetricsDaily.synthetic, AnalyticsImport.via)
        .join(AnalyticsImport, AnalyticsImport.id == LinkMetricsDaily.import_id)
        .where(
            LinkMetricsDaily.source == source.value,
            LinkMetricsDaily.metric_date >= start,
            LinkMetricsDaily.metric_date <= end,
        )
        .distinct()
    )
    return {(bool(row[0]), str(row[1])) for row in found}


async def existing_page_origins(
    session: AsyncSession, *, source: AnalyticsSource, start: date, end: date
) -> set[tuple[bool, str]]:
    found = await session.execute(
        select(PageMetricsDaily.synthetic, AnalyticsImport.via)
        .join(AnalyticsImport, AnalyticsImport.id == PageMetricsDaily.import_id)
        .where(
            PageMetricsDaily.source == source.value,
            PageMetricsDaily.metric_date >= start,
            PageMetricsDaily.metric_date <= end,
        )
        .distinct()
    )
    return {(bool(row[0]), str(row[1])) for row in found}


async def existing_query_origins(
    session: AsyncSession, *, start: date, end: date
) -> set[tuple[bool, str]]:
    found = await session.execute(
        select(PageQueryMetricsDaily.synthetic, AnalyticsImport.via)
        .join(AnalyticsImport, AnalyticsImport.id == PageQueryMetricsDaily.import_id)
        .where(
            PageQueryMetricsDaily.metric_date >= start,
            PageQueryMetricsDaily.metric_date <= end,
        )
        .distinct()
    )
    return {(bool(row[0]), str(row[1])) for row in found}


async def replace_delete_links(
    session: AsyncSession, *, source: AnalyticsSource, start: date, end: date
) -> None:
    """DELETE the (source, links, [start, end]) rows. Needs :func:`enable_writer` first."""
    await session.execute(
        delete(LinkMetricsDaily).where(
            LinkMetricsDaily.source == source.value,
            LinkMetricsDaily.metric_date >= start,
            LinkMetricsDaily.metric_date <= end,
        )
    )


async def replace_delete_pages(
    session: AsyncSession, *, source: AnalyticsSource, start: date, end: date
) -> None:
    await session.execute(
        delete(PageMetricsDaily).where(
            PageMetricsDaily.source == source.value,
            PageMetricsDaily.metric_date >= start,
            PageMetricsDaily.metric_date <= end,
        )
    )


async def replace_delete_queries(session: AsyncSession, *, start: date, end: date) -> None:
    await session.execute(
        delete(PageQueryMetricsDaily).where(
            PageQueryMetricsDaily.metric_date >= start,
            PageQueryMetricsDaily.metric_date <= end,
        )
    )


async def insert_import(session: AsyncSession, **values: Any) -> AnalyticsImport:
    """Insert the ledger row (once, complete, immutable) and flush it: the metric rows
    reference it by foreign key, so it must exist before they are inserted."""
    row = AnalyticsImport(**values)
    session.add(row)
    await session.flush()
    return row


async def insert_link_rows(
    session: AsyncSession,
    rows: Sequence[LinkRow],
    *,
    import_id: uuid.UUID,
    synthetic: bool,
) -> int:
    """Insert prepared link rows IN NATURAL-KEY ORDER (so a re-import writes identically)."""
    if not rows:
        return 0
    session.add_all(
        [
            LinkMetricsDaily(
                source=row.source.value,
                metric_date=row.metric_date,
                plan_id=row.plan_id,
                impressions=row.impressions,
                clicks=row.clicks,
                import_id=import_id,
                synthetic=synthetic,
            )
            for row in sorted(rows, key=lambda item: item.key)
        ]
    )
    await session.flush()
    return len(rows)


async def insert_page_rows(
    session: AsyncSession,
    rows: Sequence[PageRow],
    *,
    import_id: uuid.UUID,
    synthetic: bool,
) -> int:
    if not rows:
        return 0
    session.add_all(
        [
            PageMetricsDaily(
                source=row.source.value,
                metric_date=row.metric_date,
                page_id=row.page_id,
                impressions=row.impressions,
                clicks=row.clicks,
                position_sum=row.position_sum,
                pageviews=row.pageviews,
                sessions=row.sessions,
                import_id=import_id,
                synthetic=synthetic,
            )
            for row in sorted(rows, key=lambda item: item.key)
        ]
    )
    await session.flush()
    return len(rows)


async def insert_query_rows(
    session: AsyncSession,
    rows: Sequence[QueryRow],
    *,
    import_id: uuid.UUID,
    synthetic: bool,
) -> int:
    if not rows:
        return 0
    session.add_all(
        [
            PageQueryMetricsDaily(
                metric_date=row.metric_date,
                page_id=row.page_id,
                query_hash=row.query_hash,
                query=row.query,
                impressions=row.impressions,
                clicks=row.clicks,
                position_sum=row.position_sum,
                import_id=import_id,
                synthetic=synthetic,
            )
            for row in sorted(rows, key=lambda item: item.key)
        ]
    )
    await session.flush()
    return len(rows)


# --------------------------------------------------------------------------- #
# First-party aggregation
# --------------------------------------------------------------------------- #
_AGGREGATE_SQL: Final = """
WITH win AS (
    SELECT plan_id,
           min(occurred_at) FILTER (WHERE to_status = 'deployed')     AS deployed_at,
           min(occurred_at) FILTER (WHERE to_status = 'rolled_back')  AS rolled_back_at
      FROM link_plan_transitions
     WHERE to_status IN ('deployed', 'rolled_back')
     GROUP BY plan_id
)
SELECT e.plan_id,
       e.event_date,
       e.synthetic,
       count(*) FILTER (WHERE e.event_type = 'internal_link_impression') AS impressions,
       count(*) FILTER (WHERE e.event_type = 'internal_link_click')      AS clicks
  FROM link_events e
  JOIN win w ON w.plan_id = e.plan_id
 WHERE e.event_date >= :start
   AND e.event_date <= :end
   AND w.deployed_at IS NOT NULL
   AND e.received_at >= date_trunc('minute', w.deployed_at)
   AND (w.rolled_back_at IS NULL OR e.received_at < w.rolled_back_at)
 GROUP BY e.plan_id, e.event_date, e.synthetic
 ORDER BY e.event_date, e.plan_id, e.synthetic
"""


async def aggregate_link_events(
    session: AsyncSession, *, start: date, end: date
) -> list[DailyEventCount]:
    """Daily first-party counts, with the live window RE-CHECKED at aggregation time
    (``date_trunc('minute', deployed_at) <= received_at < rolled_back_at``)."""
    found = await session.execute(text(_AGGREGATE_SQL), {"start": start, "end": end})
    return [
        DailyEventCount(
            plan_id=row.plan_id,
            event_date=row.event_date,
            synthetic=bool(row.synthetic),
            impressions=int(row.impressions),
            clicks=int(row.clicks),
        )
        for row in found
    ]


async def total_link_events(session: AsyncSession, *, start: date, end: date) -> int:
    """Every stored event in the range, window or not (so the importer can count drops)."""
    return int(
        await session.scalar(
            text(
                "SELECT count(*) FROM link_events WHERE event_date >= :start AND event_date <= :end"
            ),
            {"start": start, "end": end},
        )
        or 0
    )


# --------------------------------------------------------------------------- #
# Measurement
# --------------------------------------------------------------------------- #
async def insert_measurement(session: AsyncSession, **values: Any) -> LinkMeasurementRow:
    row = LinkMeasurementRow(**values)
    session.add(row)
    await session.flush()
    return row


async def measurement_of(session: AsyncSession, *, plan_id: uuid.UUID) -> LinkMeasurementRow | None:
    return (
        await session.scalars(
            select(LinkMeasurementRow).where(LinkMeasurementRow.plan_id == plan_id)
        )
    ).first()


async def max_covered_link_date(
    session: AsyncSession, *, source: AnalyticsSource, synthetic: bool = False
) -> date | None:
    """The newest ``link_metrics_daily`` day of one source and synthetic flag."""
    value: date | None = await session.scalar(
        select(func.max(LinkMetricsDaily.metric_date)).where(
            LinkMetricsDaily.source == source.value,
            LinkMetricsDaily.synthetic.is_(synthetic),
        )
    )
    return value


async def has_non_synthetic_rows(session: AsyncSession) -> bool:
    """Whether ANY real daily row is stored: the demo seeder refuses to mix with it."""
    for model in (LinkMetricsDaily, PageMetricsDaily):
        found = await session.scalar(
            select(func.count()).select_from(model).where(model.synthetic.is_(False)).limit(1)
        )
        if found:
            return True
    return False


async def content_change_dates(
    session: AsyncSession, *, page_ids: Iterable[uuid.UUID], start: datetime, end: datetime
) -> set[uuid.UUID]:
    """Pages whose content was OBSERVED to change inside the window (``content_changed``).

    The moment compared is ``page_versions.fetched_at`` -- when the new content was seen --
    not ``created_at``, which is when the row happened to be written. A backfill or a
    re-ingestion run must not look like an editorial change on the day it ran.
    """
    wanted = list(dict.fromkeys(page_ids))
    if not wanted:
        return set()
    found = await session.execute(
        select(PageVersion.page_id)
        .where(
            PageVersion.page_id.in_(wanted),
            PageVersion.version > 1,
            PageVersion.fetched_at >= start,
            PageVersion.fetched_at <= end,
        )
        .distinct()
    )
    return set(found.scalars())


# --------------------------------------------------------------------------- #
# Ledger reads
# --------------------------------------------------------------------------- #
async def ledger_row(session: AsyncSession, *, import_id: uuid.UUID) -> AnalyticsImport | None:
    return (
        await session.scalars(select(AnalyticsImport).where(AnalyticsImport.id == import_id))
    ).first()


async def list_ledger(
    session: AsyncSession,
    *,
    limit: int,
    offset: int,
    source: AnalyticsSource | None = None,
    dataset: Dataset | None = None,
) -> tuple[list[AnalyticsImport], int]:
    conditions = []
    if source is not None:
        conditions.append(AnalyticsImport.source == source.value)
    if dataset is not None:
        conditions.append(AnalyticsImport.dataset == dataset.value)
    total = int(
        await session.scalar(select(func.count()).select_from(AnalyticsImport).where(*conditions))
        or 0
    )
    found = await session.scalars(
        select(AnalyticsImport)
        .where(*conditions)
        .order_by(AnalyticsImport.created_at.desc(), AnalyticsImport.id.desc())
        .limit(limit)
        .offset(offset)
    )
    return list(found), total


async def latest_link_dates(session: AsyncSession) -> list[LatestDate]:
    """Per link source: the newest stored day, and whether any of it is synthetic."""
    found = await session.execute(
        select(
            LinkMetricsDaily.source,
            func.max(LinkMetricsDaily.metric_date).label("latest_date"),
            func.bool_or(LinkMetricsDaily.synthetic).label("synthetic"),
        ).group_by(LinkMetricsDaily.source)
    )
    return [
        LatestDate(
            source=AnalyticsSource(row.source),
            latest_date=row.latest_date,
            synthetic=bool(row.synthetic),
        )
        for row in found
    ]


async def latest_page_dates(session: AsyncSession) -> list[LatestDate]:
    found = await session.execute(
        select(
            PageMetricsDaily.source,
            func.max(PageMetricsDaily.metric_date).label("latest_date"),
            func.bool_or(PageMetricsDaily.synthetic).label("synthetic"),
        ).group_by(PageMetricsDaily.source)
    )
    return [
        LatestDate(
            source=AnalyticsSource(row.source),
            latest_date=row.latest_date,
            synthetic=bool(row.synthetic),
        )
        for row in found
    ]


async def newest_import(
    session: AsyncSession, *, source: AnalyticsSource, dataset: Dataset
) -> AnalyticsImport | None:
    return (
        await session.scalars(
            select(AnalyticsImport)
            .where(
                AnalyticsImport.source == source.value,
                AnalyticsImport.dataset == dataset.value,
            )
            .order_by(AnalyticsImport.created_at.desc(), AnalyticsImport.id.desc())
            .limit(1)
        )
    ).first()


# --------------------------------------------------------------------------- #
# Daily reads (the reader, the window maths and the measure sweep share these)
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class LinkDaily:
    """One ``link_metrics_daily`` row plus its import's ``sampled`` flag."""

    source: AnalyticsSource
    metric_date: date
    plan_id: uuid.UUID
    impressions: int
    clicks: int
    import_id: uuid.UUID
    synthetic: bool
    sampled: bool


@dataclass(frozen=True, slots=True)
class PageDaily:
    """One ``page_metrics_daily`` row plus its import's ``sampled`` flag."""

    source: AnalyticsSource
    metric_date: date
    page_id: uuid.UUID
    impressions: int
    clicks: int
    position_sum: float
    pageviews: int
    sessions: int
    import_id: uuid.UUID
    synthetic: bool
    sampled: bool


@dataclass(frozen=True, slots=True)
class DeployedPlan:
    """A plan that was ever on a page, with the moment it went live."""

    plan_id: uuid.UUID
    source_page_id: uuid.UUID
    target_page_id: uuid.UUID
    deployed_at: datetime
    rolled_back_at: datetime | None


async def link_daily_rows(
    session: AsyncSession,
    *,
    start: date,
    end: date,
    plan_ids: Iterable[uuid.UUID] | None = None,
    source: AnalyticsSource | None = None,
    synthetic: bool | None = None,
) -> list[LinkDaily]:
    conditions: list[Any] = [
        LinkMetricsDaily.metric_date >= start,
        LinkMetricsDaily.metric_date <= end,
    ]
    if plan_ids is not None:
        wanted = list(dict.fromkeys(plan_ids))
        if not wanted:
            return []
        conditions.append(LinkMetricsDaily.plan_id.in_(wanted))
    if source is not None:
        conditions.append(LinkMetricsDaily.source == source.value)
    if synthetic is not None:
        conditions.append(LinkMetricsDaily.synthetic.is_(synthetic))
    found = await session.execute(
        select(
            LinkMetricsDaily.source,
            LinkMetricsDaily.metric_date,
            LinkMetricsDaily.plan_id,
            LinkMetricsDaily.impressions,
            LinkMetricsDaily.clicks,
            LinkMetricsDaily.import_id,
            LinkMetricsDaily.synthetic,
            AnalyticsImport.sampled,
        )
        .join(AnalyticsImport, AnalyticsImport.id == LinkMetricsDaily.import_id)
        .where(*conditions)
        .order_by(LinkMetricsDaily.metric_date, LinkMetricsDaily.source, LinkMetricsDaily.plan_id)
    )
    return [
        LinkDaily(
            source=AnalyticsSource(row.source),
            metric_date=row.metric_date,
            plan_id=row.plan_id,
            impressions=int(row.impressions),
            clicks=int(row.clicks),
            import_id=row.import_id,
            synthetic=bool(row.synthetic),
            sampled=bool(row.sampled),
        )
        for row in found
    ]


async def page_daily_rows(
    session: AsyncSession,
    *,
    start: date,
    end: date,
    page_ids: Iterable[uuid.UUID] | None = None,
    source: AnalyticsSource | None = None,
    synthetic: bool | None = None,
) -> list[PageDaily]:
    conditions: list[Any] = [
        PageMetricsDaily.metric_date >= start,
        PageMetricsDaily.metric_date <= end,
    ]
    if page_ids is not None:
        wanted = list(dict.fromkeys(page_ids))
        if not wanted:
            return []
        conditions.append(PageMetricsDaily.page_id.in_(wanted))
    if source is not None:
        conditions.append(PageMetricsDaily.source == source.value)
    if synthetic is not None:
        conditions.append(PageMetricsDaily.synthetic.is_(synthetic))
    found = await session.execute(
        select(
            PageMetricsDaily.source,
            PageMetricsDaily.metric_date,
            PageMetricsDaily.page_id,
            PageMetricsDaily.impressions,
            PageMetricsDaily.clicks,
            PageMetricsDaily.position_sum,
            PageMetricsDaily.pageviews,
            PageMetricsDaily.sessions,
            PageMetricsDaily.import_id,
            PageMetricsDaily.synthetic,
            AnalyticsImport.sampled,
        )
        .join(AnalyticsImport, AnalyticsImport.id == PageMetricsDaily.import_id)
        .where(*conditions)
        .order_by(PageMetricsDaily.metric_date, PageMetricsDaily.source, PageMetricsDaily.page_id)
    )
    return [
        PageDaily(
            source=AnalyticsSource(row.source),
            metric_date=row.metric_date,
            page_id=row.page_id,
            impressions=int(row.impressions),
            clicks=int(row.clicks),
            position_sum=float(row.position_sum),
            pageviews=int(row.pageviews),
            sessions=int(row.sessions),
            import_id=row.import_id,
            synthetic=bool(row.synthetic),
            sampled=bool(row.sampled),
        )
        for row in found
    ]


def _window_subquery() -> Any:
    """The ``link_plan_transitions`` aggregate that IS every plan's live window."""
    return (
        select(
            LinkPlanTransition.plan_id.label("plan_id"),
            func.min(LinkPlanTransition.occurred_at)
            .filter(LinkPlanTransition.to_status == DEPLOYED)
            .label("deployed_at"),
            func.min(LinkPlanTransition.occurred_at)
            .filter(LinkPlanTransition.to_status == ROLLED_BACK)
            .label("rolled_back_at"),
        )
        .where(LinkPlanTransition.to_status.in_([DEPLOYED, ROLLED_BACK]))
        .group_by(LinkPlanTransition.plan_id)
        .subquery()
    )


async def deployed_plans(
    session: AsyncSession, *, page_ids: Iterable[uuid.UUID] | None = None
) -> list[DeployedPlan]:
    """Every plan that was ever on a page (optionally only those touching ``page_ids``).

    The window comes from ``link_plan_transitions`` (amendment 3), which is what makes
    ``overlapping_changes`` and the page-impact screen agree with attribution.
    """
    pages = [] if page_ids is None else list(dict.fromkeys(page_ids))
    if page_ids is not None and not pages:
        return []
    window = _window_subquery()
    statement = (
        select(
            LinkPlan.id,
            LinkPlan.source_page_id,
            LinkPlan.target_page_id,
            window.c.deployed_at,
            window.c.rolled_back_at,
        )
        .join(window, window.c.plan_id == LinkPlan.id)
        .where(window.c.deployed_at.is_not(None))
        .order_by(window.c.deployed_at, LinkPlan.id)
    )
    if pages:
        statement = statement.where(
            or_(LinkPlan.source_page_id.in_(pages), LinkPlan.target_page_id.in_(pages))
        )
    found = await session.execute(statement)
    return [
        DeployedPlan(
            plan_id=row.id,
            source_page_id=row.source_page_id,
            target_page_id=row.target_page_id,
            deployed_at=row.deployed_at,
            rolled_back_at=row.rolled_back_at,
        )
        for row in found
    ]


@dataclass(frozen=True, slots=True)
class LinkTotals:
    """One (plan, source) aggregate over a date range (what ``MetricTotals`` is built from)."""

    plan_id: uuid.UUID
    source: AnalyticsSource
    impressions: int
    clicks: int
    synthetic: bool


async def link_totals_by_plan(
    session: AsyncSession,
    *,
    start: date,
    end: date,
    plan_ids: Iterable[uuid.UUID],
    source: AnalyticsSource | None = None,
) -> list[LinkTotals]:
    """Per (plan, source) sums over ``[start, end]``, aggregated in SQL.

    At most ``len(plan_ids) x |LINK_SOURCES|`` rows leave the database however long the range
    is: this is what keeps the link list's memory constant in the span (§12.10.8).
    """
    wanted = list(dict.fromkeys(plan_ids))
    if not wanted:
        return []
    conditions: list[Any] = [
        LinkMetricsDaily.metric_date >= start,
        LinkMetricsDaily.metric_date <= end,
        LinkMetricsDaily.plan_id.in_(wanted),
    ]
    if source is not None:
        conditions.append(LinkMetricsDaily.source == source.value)
    found = await session.execute(
        select(
            LinkMetricsDaily.plan_id,
            LinkMetricsDaily.source,
            func.sum(LinkMetricsDaily.impressions).label("impressions"),
            func.sum(LinkMetricsDaily.clicks).label("clicks"),
            func.bool_or(LinkMetricsDaily.synthetic).label("synthetic"),
        )
        .where(*conditions)
        .group_by(LinkMetricsDaily.plan_id, LinkMetricsDaily.source)
        .order_by(LinkMetricsDaily.plan_id, LinkMetricsDaily.source)
    )
    return [
        LinkTotals(
            plan_id=row.plan_id,
            source=AnalyticsSource(row.source),
            impressions=int(row.impressions or 0),
            clicks=int(row.clicks or 0),
            synthetic=bool(row.synthetic),
        )
        for row in found
    ]


def _directed(expression: Any, descending: bool) -> Any:
    """``expression`` as an ORDER BY term in the requested direction."""
    return expression.desc() if descending else expression.asc()


def _ranking_expression(sort: LinkSort, impressions: Any, clicks: Any) -> Any:
    """The sort key, computed in SQL exactly as the reader used to compute it in Python."""
    match sort:
        case LinkSort.IMPRESSIONS:
            return cast(impressions, Float)
        case LinkSort.CTR:
            # Zero impressions sorts as 0.0 (``MetricTotals.ctr`` itself stays NULL there).
            return case(
                (impressions == 0, literal(0.0)),
                else_=cast(clicks, Float) / cast(impressions, Float),
            )
        case LinkSort.CLICKS:
            return cast(clicks, Float)


async def rank_deployed_plans(
    session: AsyncSession,
    *,
    start: date,
    end: date,
    limit: int,
    offset: int,
    sort: LinkSort = LinkSort.CLICKS,
    order: SortOrder = SortOrder.DESC,
    source: AnalyticsSource | None = None,
    source_page_id: uuid.UUID | None = None,
    target_page_id: uuid.UUID | None = None,
    deployment_id: uuid.UUID | None = None,
    plan_status: LinkPlanStatus | None = None,
) -> tuple[list[DeployedPlan], int]:
    """One PAGE of the plans that were ever on a page, ranked by their totals over the range.

    Filtering, aggregation, ranking and paging all happen in SQL, so a wide range can never
    turn one GET into an ``O(live links x days)`` read: at most ``limit`` rows come back,
    plus one count. The order is ``(ranking, plan id as text)`` in ``order``'s direction,
    with ``COLLATE "C"`` so the tie-break is byte ordering -- the same order the reader
    produced in Python, independent of the database's collation.

    Returns ``(the page, the total number of matching plans)``.
    """
    window = _window_subquery()
    metric_conditions: list[Any] = [
        LinkMetricsDaily.metric_date >= start,
        LinkMetricsDaily.metric_date <= end,
    ]
    if source is not None:
        metric_conditions.append(LinkMetricsDaily.source == source.value)
    totals = (
        select(
            LinkMetricsDaily.plan_id.label("plan_id"),
            func.sum(LinkMetricsDaily.impressions).label("impressions"),
            func.sum(LinkMetricsDaily.clicks).label("clicks"),
        )
        .where(*metric_conditions)
        .group_by(LinkMetricsDaily.plan_id)
        .subquery()
    )
    conditions: list[Any] = [window.c.deployed_at.is_not(None)]
    if source_page_id is not None:
        conditions.append(LinkPlan.source_page_id == source_page_id)
    if target_page_id is not None:
        conditions.append(LinkPlan.target_page_id == target_page_id)
    if plan_status is not None:
        conditions.append(LinkPlan.status == plan_status.value)
    if deployment_id is not None:
        conditions.append(
            LinkPlan.id.in_(
                select(DeploymentItem.plan_id).where(DeploymentItem.deployment_id == deployment_id)
            )
        )
    total = await session.scalar(
        select(func.count())
        .select_from(LinkPlan)
        .join(window, window.c.plan_id == LinkPlan.id)
        .where(*conditions)
    )
    impressions = func.coalesce(totals.c.impressions, 0)
    clicks = func.coalesce(totals.c.clicks, 0)
    ranking = _ranking_expression(sort, impressions, clicks)
    tiebreak = cast(LinkPlan.id, Text).collate("C")
    # Equality, not identity: a ``SortOrder`` is a ``StrEnum``, so an (untyped) caller
    # handing in "desc" must not silently get ascending order.
    descending = order == SortOrder.DESC
    found = await session.execute(
        select(
            LinkPlan.id,
            LinkPlan.source_page_id,
            LinkPlan.target_page_id,
            window.c.deployed_at,
            window.c.rolled_back_at,
        )
        .join(window, window.c.plan_id == LinkPlan.id)
        .outerjoin(totals, totals.c.plan_id == LinkPlan.id)
        .where(*conditions)
        .order_by(_directed(ranking, descending), _directed(tiebreak, descending))
        .limit(limit)
        .offset(offset)
    )
    return (
        [
            DeployedPlan(
                plan_id=row.id,
                source_page_id=row.source_page_id,
                target_page_id=row.target_page_id,
                deployed_at=row.deployed_at,
                rolled_back_at=row.rolled_back_at,
            )
            for row in found
        ],
        int(total or 0),
    )
