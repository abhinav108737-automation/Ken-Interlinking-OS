"""The measure sweep: ``verified -> measured`` plus the ``link_measurements`` snapshot
(ADR-0015 §3, §12.6.7, §12.9.1; the lock order of §12.4 is binding).

This is the ONLY plan mutation analytics performs, and it runs INSIDE the caller's
transaction -- the import's own (§12.9.1), which is what makes
``analytics.updated.plans_measured`` exact.

A plan moves when, for at least one LINK source, NON-synthetic data already covers
``D0 + windows.measurement_window_days``, where ``D0`` is ``D(deployed_at)`` in that source's
own time zone. The snapshot is written in the same transaction, because replace-imports
delete the rows a measurement was based on and Phase 8/9 must still be able to reproduce it.

**Synthetic data never measures.** Every read here filters ``synthetic = false``, so the
local demo never shows a ``measured`` plan (accepted in §12.6.7) and a measurement's flags
can never carry ``synthetic_data``.

**Lock order (§12.4), per source page, sorted by id:**

1. ``pg_try_advisory_xact_lock(deploy_lock_key(source))`` -- BUSY means a deployment or
   rollback holds that source, so the whole source is skipped (never waited on: the sweep
   runs inside an import transaction and must not block the deployer);
2. a source with a ``rolling_back`` deployment is skipped too -- its links are mid-flight.
   That check is made INSIDE the loop, after the try-lock succeeded: read once before the
   loop it would be time-of-check-to-time-of-use, because a rollback committing its intent
   after the read would still find its plans measurable;
3. ``lock_source_plans`` (the per-source plan-writer lock);
4. the plan row lock, taken by :func:`~ken_services.recommendations.transitions.transition`
   with ``expected_from = verified`` and the actor
   :data:`~ken_contracts.analytics.ANALYTICS_ACTOR`.

A plan whose verification stayed inconclusive is still ``deployed``, never ``verified``, so
it never measures (amendment 1e) -- asserted by the attribution eval.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import UTC, date, datetime, time, timedelta
from typing import Final

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from ken_contracts.analytics import (
    ANALYTICS_ACTOR,
    LINK_SOURCES,
    PAGE_SOURCES,
    AnalyticsPolicy,
    AnalyticsSource,
    ConfounderFlag,
    Dataset,
    LinkMeasurement,
    LiveWindow,
    MetricTotals,
    PageWindowComparison,
)
from ken_contracts.recommendations import LinkPlanStatus
from ken_core.config import Settings
from ken_core.db.repositories import analytics as repo
from ken_core.logging import get_logger
from ken_services.analytics.attribution import live_windows
from ken_services.analytics.protocol import MeasureOutcome
from ken_services.analytics.windows import (
    PageDailyRow,
    day_of,
    page_comparison,
    source_timezone,
)
from ken_services.deployment.service import deploy_lock_key
from ken_services.recommendations.transitions import transition
from ken_services.recommendations.validity import lock_source_plans

__all__ = ["MEASUREMENT_WINDOW_TIMEZONE", "PlanImpact", "build_impact", "measure_sweep"]

logger = get_logger(__name__)

MEASUREMENT_WINDOW_TIMEZONE: Final = "UTC"
"""``link_measurements`` records ONE ``window_start`` / ``window_end`` pair, so that pair is
kept in UTC. Each comparison inside the snapshot carries its own source-local
``anchor_date``, which is where the time-zone rule of §3 actually bites."""


class PlanImpact:
    """The before/after view of one plan: both pages, every page source, and what it used."""

    __slots__ = ("flags", "import_ids", "sampled", "source_impact", "target_impact")

    def __init__(
        self,
        *,
        source_impact: list[PageWindowComparison],
        target_impact: list[PageWindowComparison],
        import_ids: set[uuid.UUID],
        flags: set[ConfounderFlag],
        sampled: bool,
    ) -> None:
        self.source_impact = source_impact
        self.target_impact = target_impact
        self.import_ids = import_ids
        self.flags = flags
        self.sampled = sampled


def _as_window_rows(rows: Sequence[repo.PageDaily]) -> list[PageDailyRow]:
    return [
        PageDailyRow(
            page_id=row.page_id,
            source=row.source,
            metric_date=row.metric_date,
            impressions=row.impressions,
            clicks=row.clicks,
            position_sum=row.position_sum,
            pageviews=row.pageviews,
            sessions=row.sessions,
            synthetic=row.synthetic,
        )
        for row in rows
    ]


def _midnight(day: date) -> datetime:
    return datetime.combine(day, time.min, tzinfo=UTC)


async def build_impact(
    session: AsyncSession,
    *,
    settings: Settings,
    policy: AnalyticsPolicy,
    plan_id: uuid.UUID,
    source_page_id: uuid.UUID,
    target_page_id: uuid.UUID,
    window: LiveWindow,
    synthetic: bool | None = False,
) -> PlanImpact:
    """Both pages' observed change around one plan's ``D0``, per page source.

    Shared by the sweep and the API reader, so a stored measurement and a live detail view
    are computed by exactly the same rules. ``synthetic=False`` (the sweep) measures real
    data only; ``None`` (the reader) shows whatever is stored and flags it.
    """
    moment = window.deployed_at
    if moment is None:
        return PlanImpact(
            source_impact=[], target_impact=[], import_ids=set(), flags=set(), sampled=False
        )
    span = policy.windows.comparison_days
    anchors = {source: day_of(moment, source_timezone(source, settings)) for source in PAGE_SOURCES}
    first = min(anchors.values()) - timedelta(days=span)
    last = max(anchors.values()) + timedelta(days=span)
    page_rows = await repo.page_daily_rows(
        session,
        start=first,
        end=last,
        page_ids=[source_page_id, target_page_id],
        synthetic=synthetic,
    )
    site_rows = await repo.page_daily_rows(session, start=first, end=last, synthetic=synthetic)
    windowed = _as_window_rows(page_rows)
    site = _as_window_rows(site_rows)
    neighbours = await repo.deployed_plans(session, page_ids=[source_page_id, target_page_id])
    content_changed = await repo.content_change_dates(
        session,
        page_ids=[source_page_id, target_page_id],
        start=_midnight(first),
        end=_midnight(last + timedelta(days=1)),
    )
    sampled = any(row.sampled for row in page_rows)
    impacts: dict[str, list[PageWindowComparison]] = {"source": [], "target": []}
    for role, page_id in (("source", source_page_id), ("target", target_page_id)):
        for source in sorted(PAGE_SOURCES, key=lambda item: item.value):
            anchor = anchors[source]
            timezone = source_timezone(source, settings)
            extra: set[ConfounderFlag] = set()
            if page_id in content_changed:
                extra.add(ConfounderFlag.CONTENT_CHANGED)
            pre_start = anchor - timedelta(days=span)
            post_limit = anchor + timedelta(days=span)
            for other in neighbours:
                if other.plan_id == plan_id:
                    continue
                if page_id not in {other.source_page_id, other.target_page_id}:
                    continue
                if pre_start <= day_of(other.deployed_at, timezone) <= post_limit:
                    extra.add(ConfounderFlag.OVERLAPPING_CHANGES)
                    break
            covered = [row.metric_date for row in site_rows if row.source is source]
            comparison = page_comparison(
                page_id=page_id,
                role="source" if role == "source" else "target",
                plan_id=plan_id,
                source=source,
                anchor_date=anchor,
                policy=policy.windows,
                page_rows=windowed,
                site_rows=site,
                rolled_back_day=(
                    None
                    if window.rolled_back_at is None
                    else day_of(window.rolled_back_at, timezone)
                ),
                last_covered=max(covered) if covered else None,
                sampled=any(
                    row.sampled
                    for row in page_rows
                    if row.source is source and row.page_id == page_id
                ),
                extra_flags=extra,
            )
            impacts[role].append(comparison)
    flags: set[ConfounderFlag] = set()
    for comparisons in impacts.values():
        for comparison in comparisons:
            flags.update(comparison.flags)
    return PlanImpact(
        source_impact=impacts["source"],
        target_impact=impacts["target"],
        import_ids={row.import_id for row in page_rows},
        flags=flags,
        sampled=sampled,
    )


async def _try_source_lock(session: AsyncSession, source_page_id: uuid.UUID) -> bool:
    acquired = await session.scalar(
        text("SELECT pg_try_advisory_xact_lock(:key)"), {"key": deploy_lock_key(source_page_id)}
    )
    return bool(acquired)


def _covering_source(
    *,
    deployed_at: datetime,
    settings: Settings,
    coverage: dict[AnalyticsSource, date | None],
    window_days: int,
) -> AnalyticsSource | None:
    """The first link source whose NON-synthetic data already covers ``D0 + window_days``."""
    for source in sorted(LINK_SOURCES, key=lambda item: item.value):
        covered = coverage.get(source)
        if covered is None:
            continue
        anchor = day_of(deployed_at, source_timezone(source, settings))
        if covered >= anchor + timedelta(days=window_days):
            return source
    return None


async def _snapshot(
    session: AsyncSession,
    *,
    settings: Settings,
    policy: AnalyticsPolicy,
    plan: repo.VerifiedPlan,
    window: LiveWindow,
    covering: AnalyticsSource,
    now: datetime,
) -> LinkMeasurement | None:
    """The ``link_measurements`` row for one plan, or ``None`` if it must not be written."""
    moment = window.deployed_at
    if moment is None:  # pragma: no cover - callers filter never-live plans
        return None
    window_start = day_of(moment, MEASUREMENT_WINDOW_TIMEZONE)
    window_end = window_start + timedelta(days=policy.windows.measurement_window_days)
    link_rows = await repo.link_daily_rows(
        session,
        start=window_start,
        end=window_end,
        plan_ids=[plan.plan_id],
        synthetic=False,
    )
    totals: dict[AnalyticsSource, MetricTotals] = {}
    for source in sorted(LINK_SOURCES, key=lambda item: item.value):
        mine = [row for row in link_rows if row.source is source]
        if not mine:
            continue
        totals[source] = MetricTotals.of(
            impressions=sum(row.impressions for row in mine),
            clicks=sum(row.clicks for row in mine),
        )
    impact = await build_impact(
        session,
        settings=settings,
        policy=policy,
        plan_id=plan.plan_id,
        source_page_id=plan.source_page_id,
        target_page_id=plan.target_page_id,
        window=window,
        synthetic=False,
    )
    flags = set(impact.flags)
    if any(row.sampled for row in link_rows) or impact.sampled:
        flags.add(ConfounderFlag.SAMPLED_DATA)
    if ConfounderFlag.SYNTHETIC_DATA in flags:  # pragma: no cover - reads filter synthetic
        logger.warning("analytics_measure_refused_synthetic", plan_id=str(plan.plan_id))
        return None
    import_ids = {row.import_id for row in link_rows} | impact.import_ids
    if not import_ids:
        covering_import = await repo.newest_import(session, source=covering, dataset=Dataset.LINKS)
        if covering_import is None:  # pragma: no cover - coverage implies a ledger row
            return None
        import_ids = {covering_import.id}
    return LinkMeasurement(
        plan_id=plan.plan_id,
        measured_at=now,
        analytics_policy_version=policy.version,
        window_start=window_start,
        window_end=window_end,
        link_totals=totals,
        target_comparison=impact.target_impact,
        source_comparison=impact.source_impact,
        flags=sorted(flags),
        import_ids=sorted(import_ids),
    )


async def measure_sweep(
    session: AsyncSession,
    *,
    settings: Settings,
    policy: AnalyticsPolicy,
    now: datetime,
) -> MeasureOutcome:
    """Move every due ``verified`` plan to ``measured`` in the CALLER's transaction."""
    candidates = await repo.verified_plan_rows(session)
    if not candidates:
        return MeasureOutcome()
    windows = await live_windows(session, [plan.plan_id for plan in candidates])
    coverage = {
        source: await repo.max_covered_link_date(session, source=source, synthetic=False)
        for source in LINK_SOURCES
    }
    window_days = policy.windows.measurement_window_days
    due: dict[uuid.UUID, list[tuple[repo.VerifiedPlan, LiveWindow, AnalyticsSource]]] = {}
    for plan in candidates:
        window = windows.get(plan.plan_id)
        if window is None or window.deployed_at is None or window.rolled_back_at is not None:
            continue
        covering = _covering_source(
            deployed_at=window.deployed_at,
            settings=settings,
            coverage=coverage,
            window_days=window_days,
        )
        if covering is None:
            continue
        due.setdefault(plan.source_page_id, []).append((plan, window, covering))
    if not due:
        return MeasureOutcome()
    measured: list[uuid.UUID] = []
    skipped = 0
    for source_page_id in sorted(due, key=str):
        if not await _try_source_lock(session, source_page_id):
            skipped += 1
            logger.info("analytics_measure_source_busy", source_page_id=str(source_page_id))
            continue
        # Read the rollback intent only once the source lock is HELD: read before the loop it
        # would be a TOCTOU window, and a rollback that commits its first transaction after
        # the read could still have one of its plans measured (§12.4).
        if await repo.source_is_rolling_back(session, source_page_id):
            skipped += 1
            logger.info("analytics_measure_source_rolling_back", source_page_id=str(source_page_id))
            continue
        await lock_source_plans(session, source_page_id=source_page_id)
        ordered = sorted(due[source_page_id], key=lambda item: str(item[0].plan_id))
        for plan, window, covering in ordered:
            snapshot = await _snapshot(
                session,
                settings=settings,
                policy=policy,
                plan=plan,
                window=window,
                covering=covering,
                now=now,
            )
            if snapshot is None:
                continue
            await transition(
                session,
                plan_id=plan.plan_id,
                to_status=LinkPlanStatus.MEASURED,
                actor=ANALYTICS_ACTOR,
                expected_from=LinkPlanStatus.VERIFIED,
                clock=lambda: now,
            )
            payload = snapshot.model_dump(mode="json")
            await repo.insert_measurement(
                session,
                plan_id=snapshot.plan_id,
                measured_at=snapshot.measured_at,
                analytics_policy_version=snapshot.analytics_policy_version,
                window_start=snapshot.window_start,
                window_end=snapshot.window_end,
                link_totals=payload["link_totals"],
                target_comparison=payload["target_comparison"],
                source_comparison=payload["source_comparison"],
                flags=payload["flags"],
                import_ids=payload["import_ids"],
            )
            measured.append(plan.plan_id)
            logger.info(
                "analytics_plan_measured",
                plan_id=str(plan.plan_id),
                window_start=snapshot.window_start.isoformat(),
                window_end=snapshot.window_end.isoformat(),
            )
    return MeasureOutcome(measured=tuple(measured), skipped_busy_sources=skipped)
