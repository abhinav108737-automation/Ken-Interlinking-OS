"""``run_aggregation``: first-party ``link_events`` -> ``link_metrics_daily`` (ADR-0015 §5).
Implements the frozen :class:`~ken_services.analytics.protocol.RunAggregation`.

An aggregation IS a replace import: ``source = first_party``, ``dataset = links``,
``via = collector``, over the trailing ``aggregation.trailing_days`` COMPLETE UTC days (late
events are why the window trails rather than covering only yesterday). It therefore inherits
every guarantee of :mod:`ken_services.analytics.importer` -- one transaction, the mix check,
the ledger row, the measure sweep and ``analytics.updated``.

**The live window is re-checked here**, exactly as the frozen protocol states: an event
counts only while ``date_trunc('minute', deployed_at) <= received_at < rolled_back_at``. The
collector already checked it at ingest; a rollback that happened afterwards must not leave
counted events behind. Events the re-check drops are LOGGED rather than counted, because the
frozen ``FetchStats`` has no field for them and ``COUNTER_NAMES`` is frozen too.

``synthetic`` is carried THROUGH from the events (the collector sets it from its own
configuration, never the client). A range holding both synthetic and real events is refused
as ``synthetic_real_mix`` rather than silently collapsed into one flag.
"""

from __future__ import annotations

from datetime import datetime
from typing import Final

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ken_contracts.analytics import (
    AnalyticsPolicy,
    AnalyticsSource,
    Dataset,
    DateRange,
    FetchStats,
    ImportVia,
    LinkMetrics,
    MetricSource,
)
from ken_core.config import Settings
from ken_core.db.repositories import analytics as repo
from ken_core.logging import get_logger
from ken_core.providers.analytics.mock import MockAnalyticsProvider
from ken_services.analytics.importer import run_import, trailing_range
from ken_services.analytics.protocol import (
    AnalyticsImportError,
    ImportErrorCode,
    ImportOutcome,
    ImportRequest,
    validate_range,
)
from ken_services.jobs.runner import utc_now

__all__ = ["AGGREGATION_TIMEZONE", "run_aggregation"]

logger = get_logger(__name__)

AGGREGATION_TIMEZONE: Final = "UTC"
"""First-party day boundaries are UTC (ADR-0015 §3), and ``link_events.event_date`` is the
UTC date of ``received_at`` by a CHECK, so the two can never disagree."""


async def run_aggregation(
    *,
    sessionmaker: async_sessionmaker[AsyncSession],
    settings: Settings,
    policy: AnalyticsPolicy,
    date_range: DateRange | None = None,
    triggered_by: str = "cli",
    now: datetime | None = None,
) -> ImportOutcome:
    """Aggregate the first-party events of ``date_range`` and import them.

    Raises:
        AnalyticsImportError: refused (an inverted or over-long range, mixed synthetic
            events, or any refusal of the import itself).
    """
    moment = now or utc_now()
    window = date_range or trailing_range(moment, days=policy.aggregation.trailing_days)
    validate_range(window)
    async with sessionmaker() as reading:
        counted = await repo.aggregate_link_events(reading, start=window.start, end=window.end)
        stored = await repo.total_link_events(reading, start=window.start, end=window.end)
    flags = {row.synthetic for row in counted}
    if len(flags) > 1:
        raise AnalyticsImportError(ImportErrorCode.SYNTHETIC_REAL_MIX)
    synthetic = flags.pop() if flags else False
    rows = [
        LinkMetrics(
            link_id=str(row.plan_id),
            source=MetricSource.FIRST_PARTY,
            range=DateRange(start=row.event_date, end=row.event_date),
            impressions=row.impressions,
            clicks=row.clicks,
        )
        for row in counted
    ]
    inside = sum(row.impressions + row.clicks for row in counted)
    if stored > inside:
        logger.info(
            "analytics_aggregation_dropped_events",
            dropped=stored - inside,
            date_start=window.start.isoformat(),
            date_end=window.end.isoformat(),
        )
    provider = MockAnalyticsProvider(link_rows=rows, stats=FetchStats(rows_read=len(rows)))
    request = ImportRequest(
        source=AnalyticsSource.FIRST_PARTY,
        dataset=Dataset.LINKS,
        via=ImportVia.COLLECTOR,
        date_range=window,
        source_timezone=AGGREGATION_TIMEZONE,
        synthetic=synthetic,
        triggered_by=triggered_by,
    )
    return await run_import(
        sessionmaker=sessionmaker,
        settings=settings,
        policy=policy,
        provider=provider,
        request=request,
        now=moment,
    )
