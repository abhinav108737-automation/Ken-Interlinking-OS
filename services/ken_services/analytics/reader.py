"""``SqlAnalyticsReader``: the frozen :class:`~ken_contracts.analytics.AnalyticsReader` over
PostgreSQL (ADR-0015 §6). The API's ONLY route to stored analytics.

Every method opens its own session from the factory the API hands in, which is the READ ONLY
factory (ADR-0014 §8): a reader cannot write, cannot move a plan and cannot trigger Google
egress -- imports are CLI-only precisely so no UI action can.

Shapes are computed, never stored: windows and comparisons come from
:mod:`ken_services.analytics.windows` through :func:`~ken_services.analytics.measure.build_impact`,
so a live detail view and a stored ``link_measurements`` snapshot agree by construction.

Read fan-out is BOUNDED on every route that could otherwise grow with the requested range
(§12.10.8):

* ``list_links`` filters, aggregates, ranks and pages entirely in SQL
  (:func:`~ken_core.db.repositories.analytics.rank_deployed_plans` plus
  :func:`~ken_core.db.repositories.analytics.link_totals_by_plan`). One request materialises
  at most ``limit`` ranked rows, ``limit`` plan rows and ``limit x |LINK_SOURCES|`` aggregate
  rows -- at the route's cap of ``limit = 100``, at most 400 rows -- however long the range
  and however many links are live. The ordering is the one the Python version produced:
  the per-range total of the selected sources, ``ctr`` counted as 0 at zero impressions
  (while ``MetricTotals.ctr`` itself stays null), ties broken by the plan id as text, both
  keys in the requested direction.
* ``page_impact`` builds at most :data:`MAX_PAGE_CHANGES` comparisons, newest deployment
  first, so one very busy page cannot turn a GET into an unbounded query fan-out.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import UTC, date, datetime, timedelta
from typing import Final

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ken_contracts.analytics import (
    LINK_SOURCES,
    MAX_TOP_LINKS,
    PAGE_SOURCES,
    AnalyticsImportView,
    AnalyticsPolicy,
    AnalyticsSource,
    AnalyticsSummary,
    DailyTotals,
    Dataset,
    DateRange,
    ImportVia,
    LinkDailyPoint,
    LinkMeasurement,
    LinkPerformanceDetail,
    LinkPerformanceView,
    LinkSort,
    LiveWindow,
    MetricTotals,
    PageDailyPoint,
    PageImpactView,
    SortOrder,
    SourceFreshness,
)
from ken_contracts.errors import ListResponse, PageInfo
from ken_contracts.inventory import InventoryNotFoundError
from ken_contracts.recommendations import LinkPlanStatus
from ken_core.config import Settings
from ken_core.db.models.intelligence import JobRun
from ken_core.db.models.inventory import Page
from ken_core.db.models.recommendations import LinkPlan
from ken_core.db.repositories import analytics as repo
from ken_services.analytics.attribution import link_attribution, live_windows
from ken_services.analytics.measure import build_impact
from ken_services.analytics.policy import load_configured_analytics_policy

__all__ = ["MAX_PAGE_CHANGES", "SqlAnalyticsReader"]

MAX_PAGE_CHANGES: Final = 50
"""Most comparisons one page-impact view builds (newest deployment first)."""
_DATASET_SOURCES: Final = (
    (AnalyticsSource.GA4, Dataset.LINKS),
    (AnalyticsSource.FIRST_PARTY, Dataset.LINKS),
    (AnalyticsSource.GA4, Dataset.PAGES),
    (AnalyticsSource.SEARCH_CONSOLE, Dataset.PAGES),
)
"""Every (source, dataset) Phase 7 can hold, so freshness can say "no imports yet"."""


def _totals(rows: Sequence[repo.LinkDaily]) -> dict[AnalyticsSource, MetricTotals]:
    out: dict[AnalyticsSource, MetricTotals] = {}
    for source in sorted({row.source for row in rows}, key=lambda item: item.value):
        mine = [row for row in rows if row.source is source]
        out[source] = MetricTotals.of(
            impressions=sum(row.impressions for row in mine),
            clicks=sum(row.clicks for row in mine),
        )
    return out


def _aggregated_totals(rows: Sequence[repo.LinkTotals]) -> dict[AnalyticsSource, MetricTotals]:
    """Per-source totals from SQL aggregates (one row per (plan, source))."""
    return {
        row.source: MetricTotals.of(impressions=row.impressions, clicks=row.clicks)
        for row in sorted(rows, key=lambda row: row.source.value)
    }


class SqlAnalyticsReader:
    """Stored analytics for the five GET routes."""

    def __init__(
        self, *, sessionmaker: async_sessionmaker[AsyncSession], settings: Settings
    ) -> None:
        self._sessionmaker = sessionmaker
        self._settings = settings

    @property
    def policy(self) -> AnalyticsPolicy:
        """The configured analytics policy (the loader caches by path and mtime)."""
        return load_configured_analytics_policy(self._settings)

    # ------------------------------------------------------------- helpers
    async def _latest_date(self, session: AsyncSession) -> date | None:
        candidates = [
            row.latest_date
            for row in (
                await repo.latest_link_dates(session) + await repo.latest_page_dates(session)
            )
            if row.latest_date is not None
        ]
        return max(candidates) if candidates else None

    async def _resolve_range(
        self, session: AsyncSession, date_range: DateRange | None
    ) -> DateRange:
        """``None`` means the default window: ``comparison_days`` ending at the latest day."""
        if date_range is not None:
            return date_range
        end = await self._latest_date(session) or datetime.now(UTC).date()
        return DateRange(
            start=end - timedelta(days=self.policy.windows.comparison_days - 1), end=end
        )

    async def _plan_rows(
        self, session: AsyncSession, plan_ids: Sequence[uuid.UUID]
    ) -> dict[uuid.UUID, LinkPlan]:
        if not plan_ids:
            return {}
        found = await session.scalars(select(LinkPlan).where(LinkPlan.id.in_(list(plan_ids))))
        return {row.id: row for row in found}

    @staticmethod
    def _view(
        plan: LinkPlan,
        *,
        window: LiveWindow,
        totals: dict[AnalyticsSource, MetricTotals],
        synthetic: bool,
    ) -> LinkPerformanceView:
        return LinkPerformanceView(
            plan_id=plan.id,
            anchor_text=plan.anchor_text,
            source_page_id=plan.source_page_id,
            source_url=plan.source_url,
            target_page_id=plan.target_page_id,
            target_url=plan.target_url,
            plan_status=LinkPlanStatus(plan.status),
            deployed_at=window.deployed_at,
            rolled_back_at=window.rolled_back_at,
            totals=totals,
            synthetic=synthetic,
        )

    # ------------------------------------------------------------- summary
    async def summary(self, *, date_range: DateRange | None) -> AnalyticsSummary:
        async with self._sessionmaker() as session:
            window = await self._resolve_range(session, date_range)
            link_rows = await repo.link_daily_rows(session, start=window.start, end=window.end)
            page_rows = await repo.page_daily_rows(session, start=window.start, end=window.end)
            freshness = await self._freshness(session)
            live = await repo.deployed_plans(session)
            per_plan: dict[uuid.UUID, list[repo.LinkDaily]] = {}
            for row in link_rows:
                per_plan.setdefault(row.plan_id, []).append(row)
            top_ids = sorted(
                per_plan,
                key=lambda plan_id: (
                    -sum(row.clicks for row in per_plan[plan_id]),
                    -sum(row.impressions for row in per_plan[plan_id]),
                    str(plan_id),
                ),
            )[:MAX_TOP_LINKS]
            plans = await self._plan_rows(session, top_ids)
            windows = await live_windows(session, top_ids)
            daily: dict[tuple[date, AnalyticsSource], list[int]] = {}
            for row in link_rows:
                bucket = daily.setdefault((row.metric_date, row.source), [0, 0])
                bucket[0] += row.impressions
                bucket[1] += row.clicks
            return AnalyticsSummary(
                range=window,
                sources=freshness,
                totals_by_source=_totals(link_rows),
                daily=[
                    DailyTotals(
                        metric_date=day,
                        source=source,
                        impressions=counts[0],
                        clicks=counts[1],
                    )
                    for (day, source), counts in sorted(
                        daily.items(), key=lambda item: (item[0][0], item[0][1].value)
                    )
                ],
                live_links=sum(1 for plan in live if plan.rolled_back_at is None),
                links_with_data=len(per_plan),
                top_links=[
                    self._view(
                        plans[plan_id],
                        window=windows.get(plan_id, LiveWindow(plan_id=plan_id)),
                        totals=_totals(per_plan[plan_id]),
                        synthetic=any(row.synthetic for row in per_plan[plan_id]),
                    )
                    for plan_id in top_ids
                    if plan_id in plans
                ],
                synthetic=any(row.synthetic for row in link_rows)
                or any(row.synthetic for row in page_rows),
            )

    async def _freshness(self, session: AsyncSession) -> list[SourceFreshness]:
        link_latest = {row.source: row for row in await repo.latest_link_dates(session)}
        page_latest = {row.source: row for row in await repo.latest_page_dates(session)}
        out: list[SourceFreshness] = []
        for source, dataset in _DATASET_SOURCES:
            latest = (link_latest if dataset is Dataset.LINKS else page_latest).get(source)
            ledger = await repo.newest_import(session, source=source, dataset=dataset)
            out.append(
                SourceFreshness(
                    source=source,
                    dataset=dataset,
                    latest_date=None if latest is None else latest.latest_date,
                    last_import_id=None if ledger is None else ledger.id,
                    last_via=None if ledger is None else ImportVia(ledger.via),
                    last_imported_at=None if ledger is None else ledger.created_at,
                    synthetic=bool(latest.synthetic) if latest is not None else False,
                )
            )
        return out

    # --------------------------------------------------------------- links
    async def list_links(
        self,
        *,
        date_range: DateRange | None,
        limit: int,
        offset: int,
        source: AnalyticsSource | None = None,
        source_page_id: uuid.UUID | None = None,
        target_page_id: uuid.UUID | None = None,
        deployment_id: uuid.UUID | None = None,
        plan_status: LinkPlanStatus | None = None,
        sort: LinkSort = LinkSort.CLICKS,
        order: SortOrder = SortOrder.DESC,
    ) -> ListResponse[LinkPerformanceView]:
        async with self._sessionmaker() as session:
            window = await self._resolve_range(session, date_range)
            ranked, total = await repo.rank_deployed_plans(
                session,
                start=window.start,
                end=window.end,
                limit=limit,
                offset=offset,
                sort=sort,
                order=order,
                source=source,
                source_page_id=source_page_id,
                target_page_id=target_page_id,
                deployment_id=deployment_id,
                plan_status=plan_status,
            )
            plan_ids = [row.plan_id for row in ranked]
            plans = await self._plan_rows(session, plan_ids)
            per_plan: dict[uuid.UUID, list[repo.LinkTotals]] = {}
            for aggregate in await repo.link_totals_by_plan(
                session,
                start=window.start,
                end=window.end,
                plan_ids=plan_ids,
                source=source,
            ):
                per_plan.setdefault(aggregate.plan_id, []).append(aggregate)
            items: list[LinkPerformanceView] = []
            for plan in ranked:
                record = plans.get(plan.plan_id)
                if record is None:  # pragma: no cover - the ranking joins link_plans
                    continue
                mine = per_plan.get(plan.plan_id, [])
                items.append(
                    self._view(
                        record,
                        window=LiveWindow(
                            plan_id=plan.plan_id,
                            deployed_at=plan.deployed_at,
                            rolled_back_at=plan.rolled_back_at,
                        ),
                        totals=_aggregated_totals(mine),
                        synthetic=any(aggregate.synthetic for aggregate in mine),
                    )
                )
            return ListResponse[LinkPerformanceView](
                items=items,
                page=PageInfo(total=total, limit=limit, offset=offset),
            )

    async def get_link(
        self, *, plan_id: uuid.UUID, date_range: DateRange | None
    ) -> LinkPerformanceDetail:
        async with self._sessionmaker() as session:
            window = await self._resolve_range(session, date_range)
            attribution = await link_attribution(session, plan_id=plan_id)
            windows = await live_windows(session, [plan_id])
            live = windows[plan_id]
            rows = await repo.link_daily_rows(
                session, start=window.start, end=window.end, plan_ids=[plan_id]
            )
            series: dict[AnalyticsSource, list[LinkDailyPoint]] = {}
            for source in sorted(LINK_SOURCES, key=lambda item: item.value):
                mine = [row for row in rows if row.source is source]
                if not mine:
                    continue
                series[source] = [
                    LinkDailyPoint(
                        metric_date=row.metric_date,
                        impressions=row.impressions,
                        clicks=row.clicks,
                    )
                    for row in sorted(mine, key=lambda row: row.metric_date)
                ]
            impact = await build_impact(
                session,
                settings=self._settings,
                policy=self.policy,
                plan_id=plan_id,
                source_page_id=attribution.source_page_id,
                target_page_id=attribution.target_page_id,
                window=live,
                synthetic=None,
            )
            stored = await repo.measurement_of(session, plan_id=plan_id)
            measurement = (
                None
                if stored is None
                else LinkMeasurement.model_validate(
                    {
                        "plan_id": str(stored.plan_id),
                        "measured_at": stored.measured_at,
                        "analytics_policy_version": stored.analytics_policy_version,
                        "window_start": stored.window_start,
                        "window_end": stored.window_end,
                        "link_totals": stored.link_totals,
                        "target_comparison": stored.target_comparison,
                        "source_comparison": stored.source_comparison,
                        "flags": stored.flags,
                        "import_ids": stored.import_ids,
                    }
                )
            )
            return LinkPerformanceDetail(
                range=window,
                attribution=attribution,
                live_window=live,
                series=series,
                totals=_totals(rows),
                target_impact=impact.target_impact,
                source_impact=impact.source_impact,
                measurement=measurement,
                synthetic=any(row.synthetic for row in rows),
            )

    # --------------------------------------------------------------- pages
    async def page_impact(self, *, page_id: uuid.UUID, window_days: int) -> PageImpactView:
        async with self._sessionmaker() as session:
            page = (await session.scalars(select(Page).where(Page.id == page_id))).first()
            if page is None:
                raise InventoryNotFoundError("page")
            latest = await self._latest_date(session)
            end = latest or datetime.now(UTC).date()
            start = end - timedelta(days=window_days - 1)
            rows = await repo.page_daily_rows(session, start=start, end=end, page_ids=[page_id])
            series: dict[AnalyticsSource, list[PageDailyPoint]] = {}
            for source in sorted(PAGE_SOURCES, key=lambda item: item.value):
                mine = [row for row in rows if row.source is source]
                if not mine:
                    continue
                series[source] = [
                    PageDailyPoint(
                        metric_date=row.metric_date,
                        impressions=row.impressions,
                        clicks=row.clicks,
                        avg_position=(
                            None
                            if row.impressions == 0 or row.position_sum == 0
                            else row.position_sum / row.impressions
                        ),
                        pageviews=row.pageviews,
                        sessions=row.sessions,
                    )
                    for row in sorted(mine, key=lambda row: row.metric_date)
                ]
            touching = await repo.deployed_plans(session, page_ids=[page_id])
            recent = sorted(touching, key=lambda plan: plan.deployed_at, reverse=True)[
                :MAX_PAGE_CHANGES
            ]
            changes = []
            synthetic = any(row.synthetic for row in rows)
            for plan in recent:
                impact = await build_impact(
                    session,
                    settings=self._settings,
                    policy=self.policy,
                    plan_id=plan.plan_id,
                    source_page_id=plan.source_page_id,
                    target_page_id=plan.target_page_id,
                    window=LiveWindow(
                        plan_id=plan.plan_id,
                        deployed_at=plan.deployed_at,
                        rolled_back_at=plan.rolled_back_at,
                    ),
                    synthetic=None,
                )
                for comparison in impact.source_impact + impact.target_impact:
                    if comparison.page_id == page_id:
                        changes.append(comparison)
                        synthetic = synthetic or comparison.synthetic
            return PageImpactView(
                page_id=page_id,
                url=page.canonical_url,
                window_days=window_days,
                series=series,
                changes=sorted(
                    changes,
                    key=lambda item: (item.anchor_date, str(item.plan_id), item.source.value),
                    reverse=True,
                ),
                synthetic=synthetic,
            )

    # ------------------------------------------------------------- imports
    async def list_imports(
        self,
        *,
        limit: int,
        offset: int,
        source: AnalyticsSource | None = None,
        dataset: Dataset | None = None,
    ) -> ListResponse[AnalyticsImportView]:
        async with self._sessionmaker() as session:
            rows, total = await repo.list_ledger(
                session, limit=limit, offset=offset, source=source, dataset=dataset
            )
            counters: dict[uuid.UUID, dict[str, int]] = {}
            if rows:
                found = await session.execute(
                    select(JobRun.id, JobRun.counters).where(
                        JobRun.id.in_([row.job_run_id for row in rows])
                    )
                )
                counters = {row.id: dict(row.counters or {}) for row in found}
            return ListResponse[AnalyticsImportView](
                items=[
                    AnalyticsImportView(
                        id=row.id,
                        job_run_id=row.job_run_id,
                        source=AnalyticsSource(row.source),
                        dataset=Dataset(row.dataset),
                        via=ImportVia(row.via),
                        synthetic=row.synthetic,
                        replace_via=row.replace_via,
                        date_start=row.date_start,
                        date_end=row.date_end,
                        source_timezone=row.source_timezone,
                        file_name=row.file_name,
                        file_sha256=row.file_sha256,
                        sampled=row.sampled,
                        rows_written=row.rows_written,
                        analytics_policy_version=row.analytics_policy_version,
                        unresolved_sample=list(row.unresolved_sample or []),
                        triggered_by=row.triggered_by,
                        created_at=row.created_at,
                        counters=counters.get(row.job_run_id, {}),
                    )
                    for row in rows
                ],
                page=PageInfo(total=total, limit=limit, offset=offset),
            )
