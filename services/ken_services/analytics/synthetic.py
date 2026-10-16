"""Seeded demo analytics: ``make analytics-demo-seed`` (ADR-0015 §8 as amended).

It walks every plan that was EVER applied and every day of its live window, invents plausible
traffic, and feeds it through the ``mock`` provider into the NORMAL importer with
``via = synthetic, synthetic = true``. Nothing here writes a row itself, so the demo data
obeys every rule real data obeys: the replace semantics, the live-window check, the ledger,
the event -- and the measure sweep, which skips it, so the demo never shows a ``measured``
plan (accepted in §12.6.7).

**Determinism.** Each day's numbers come from ``random.Random`` seeded with the first eight
bytes of ``sha512(f"{seed}:{plan_id}:{date}")``. A STRING seed hashed with sha512 is used
precisely so nothing depends on ``PYTHONHASHSEED``: the same seed gives byte-identical rows
on any machine, in any process. Per-page base traffic comes from ``sha256(page_id)`` so a
page's series is continuous across the windows of several plans.

**Refusals.**

* ``staging`` / ``production`` -> ``environment_refused``. Synthetic analytics in a real
  environment would corrupt the measurement record the learning phases depend on.
* Any real (non-synthetic) daily row present -> ``synthetic_real_mix``, before anything runs.

Three imports are produced, in this order: first-party links, GA4 pages, Search Console
pages. Each one is its own ``run_job(ANALYTICS_IMPORT)``, so each has its own ledger row.
"""

from __future__ import annotations

import hashlib
import random
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Final

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ken_contracts.analytics import (
    MAX_RANGE_DAYS,
    AnalyticsPolicy,
    AnalyticsSource,
    Dataset,
    DateRange,
    FetchStats,
    ImportVia,
    LinkMetrics,
    MetricSource,
    PageMetrics,
)
from ken_core.config import Settings
from ken_core.db.repositories import analytics as repo
from ken_core.logging import get_logger
from ken_core.providers.analytics.mock import MockAnalyticsProvider
from ken_services.analytics.importer import run_import
from ken_services.analytics.protocol import (
    AnalyticsImportError,
    ImportErrorCode,
    ImportOutcome,
    ImportRequest,
)
from ken_services.analytics.windows import day_of, source_timezone
from ken_services.jobs.runner import utc_now

__all__ = ["DEFAULT_SEED", "SyntheticPlan", "generate", "run_demo_seed"]

logger = get_logger(__name__)

DEFAULT_SEED: Final = "7"
_MIN_BASE: Final = 40
_BASE_SPREAD: Final = 460
_CTR_LOW: Final = 0.005
_CTR_HIGH: Final = 0.04
_LINK_SHARE: Final = (0.05, 0.20)
"""A link's impressions as a share of its source page's daily traffic."""
_GSC_IMPRESSION_FACTOR: Final = (3.0, 6.0)
_POSITION_RANGE: Final = (3.0, 20.0)
_SESSION_SHARE: Final = 0.8
_DRIFT: Final = (0.85, 1.15)


def _rng(seed: str, *parts: str) -> random.Random:
    """A generator seeded from sha512 of a STRING key: never ``hash()``, never PYTHONHASHSEED."""
    digest = hashlib.sha512(":".join((seed, *parts)).encode("utf-8")).digest()
    return random.Random(int.from_bytes(digest[:8], "big"))  # noqa: S311 -- demo data, not crypto


def _page_base(page_id: uuid.UUID) -> int:
    digest = hashlib.sha256(str(page_id).encode("ascii")).digest()
    return _MIN_BASE + int.from_bytes(digest[:2], "big") % _BASE_SPREAD


@dataclass(frozen=True, slots=True)
class SyntheticPlan:
    """One plan's live window, as the generator needs it."""

    plan_id: uuid.UUID
    source_page_id: uuid.UUID
    target_page_id: uuid.UUID
    deployed_at: datetime
    rolled_back_at: datetime | None


@dataclass(frozen=True, slots=True)
class SyntheticData:
    link_rows: tuple[LinkMetrics, ...]
    ga4_page_rows: tuple[PageMetrics, ...]
    gsc_page_rows: tuple[PageMetrics, ...]
    link_range: DateRange | None
    ga4_range: DateRange | None
    gsc_range: DateRange | None


def _days(first: date, last: date) -> list[date]:
    if last < first:
        return []
    return [first + timedelta(days=offset) for offset in range((last - first).days + 1)]


def _bounded(rows: Sequence[date], *, newest: date) -> DateRange | None:
    if not rows:
        return None
    end = min(max(rows), newest)
    start = max(min(rows), end - timedelta(days=MAX_RANGE_DAYS - 1))
    if end < start:
        return None
    return DateRange(start=start, end=end)


def generate(
    plans: Sequence[SyntheticPlan],
    *,
    settings: Settings,
    policy: AnalyticsPolicy,
    seed: str,
    now: datetime,
) -> SyntheticData:
    """Every row the demo seed will import. PURE and byte-repeatable for a given seed."""
    newest = {
        source: day_of(now, source_timezone(source, settings))
        - timedelta(days=policy.lag_days(source))
        for source in AnalyticsSource
    }
    span = policy.windows.comparison_days
    link_rows: list[LinkMetrics] = []
    ga4_rows: list[PageMetrics] = []
    gsc_rows: list[PageMetrics] = []
    link_days: list[date] = []
    ga4_days: list[date] = []
    gsc_days: list[date] = []
    page_seen: set[tuple[uuid.UUID, AnalyticsSource, date]] = set()
    for plan in sorted(plans, key=lambda item: str(item.plan_id)):
        # ---------------------------------------------------- link series
        timezone = source_timezone(AnalyticsSource.FIRST_PARTY, settings)
        start = day_of(plan.deployed_at, timezone)
        last = newest[AnalyticsSource.FIRST_PARTY]
        if plan.rolled_back_at is not None:
            last = min(last, day_of(plan.rolled_back_at, timezone))
        base = _page_base(plan.source_page_id)
        for day in _days(start, last):
            generator = _rng(seed, str(plan.plan_id), day.isoformat())
            impressions = max(1, int(base * generator.uniform(*_LINK_SHARE)))
            clicks = round(impressions * generator.uniform(_CTR_LOW, _CTR_HIGH))
            link_rows.append(
                LinkMetrics(
                    link_id=str(plan.plan_id),
                    source=MetricSource.FIRST_PARTY,
                    range=DateRange(start=day, end=day),
                    impressions=impressions,
                    clicks=min(clicks, impressions),
                )
            )
            link_days.append(day)
        # ---------------------------------------------------- page series
        for page_id in sorted({plan.source_page_id, plan.target_page_id}, key=str):
            page_base = _page_base(page_id)
            for source, bucket, days_bucket in (
                (AnalyticsSource.GA4, ga4_rows, ga4_days),
                (AnalyticsSource.SEARCH_CONSOLE, gsc_rows, gsc_days),
            ):
                anchor = day_of(plan.deployed_at, source_timezone(source, settings))
                first = anchor - timedelta(days=span)
                final = min(anchor + timedelta(days=span), newest[source])
                for day in _days(first, final):
                    key = (page_id, source, day)
                    if key in page_seen:
                        continue  # one row per (page, source, day), whichever plan asked
                    page_seen.add(key)
                    generator = _rng(seed, str(page_id), source.value, day.isoformat())
                    drift = generator.uniform(*_DRIFT)
                    if source is AnalyticsSource.GA4:
                        pageviews = max(1, int(page_base * drift))
                        bucket.append(
                            PageMetrics(
                                page_id=str(page_id),
                                source=MetricSource.GA4,
                                range=DateRange(start=day, end=day),
                                pageviews=pageviews,
                                sessions=max(1, int(pageviews * _SESSION_SHARE)),
                            )
                        )
                    else:
                        impressions = max(
                            1, int(page_base * drift * generator.uniform(*_GSC_IMPRESSION_FACTOR))
                        )
                        clicks = round(impressions * generator.uniform(_CTR_LOW, _CTR_HIGH))
                        bucket.append(
                            PageMetrics(
                                page_id=str(page_id),
                                source=MetricSource.SEARCH_CONSOLE,
                                range=DateRange(start=day, end=day),
                                impressions=impressions,
                                clicks=min(clicks, impressions),
                                position=round(generator.uniform(*_POSITION_RANGE), 2),
                            )
                        )
                    days_bucket.append(day)
    return SyntheticData(
        link_rows=tuple(link_rows),
        ga4_page_rows=tuple(ga4_rows),
        gsc_page_rows=tuple(gsc_rows),
        link_range=_bounded(link_days, newest=newest[AnalyticsSource.FIRST_PARTY]),
        ga4_range=_bounded(ga4_days, newest=newest[AnalyticsSource.GA4]),
        gsc_range=_bounded(gsc_days, newest=newest[AnalyticsSource.SEARCH_CONSOLE]),
    )


async def _plans(session: AsyncSession) -> list[SyntheticPlan]:
    return [
        SyntheticPlan(
            plan_id=plan.plan_id,
            source_page_id=plan.source_page_id,
            target_page_id=plan.target_page_id,
            deployed_at=plan.deployed_at,
            rolled_back_at=plan.rolled_back_at,
        )
        for plan in await repo.deployed_plans(session)
    ]


async def run_demo_seed(
    *,
    sessionmaker: async_sessionmaker[AsyncSession],
    settings: Settings,
    policy: AnalyticsPolicy,
    seed: str = DEFAULT_SEED,
    now: datetime | None = None,
) -> list[ImportOutcome]:
    """Generate and import the demo data. Returns one outcome per import performed.

    Raises:
        AnalyticsImportError: ``environment_refused`` outside local/ci, or
            ``synthetic_real_mix`` when any real row is already stored.
    """
    if settings.environment not in {"local", "ci"}:
        raise AnalyticsImportError(ImportErrorCode.ENVIRONMENT_REFUSED)
    moment = now or utc_now()
    async with sessionmaker() as reading:
        if await repo.has_non_synthetic_rows(reading):
            raise AnalyticsImportError(ImportErrorCode.SYNTHETIC_REAL_MIX)
        plans = await _plans(reading)
    if not plans:
        logger.info("analytics_demo_seed_no_live_plans")
        return []
    data = generate(plans, settings=settings, policy=policy, seed=seed, now=moment)
    outcomes: list[ImportOutcome] = []
    for source, dataset, rows, window, timezone in (
        (
            AnalyticsSource.FIRST_PARTY,
            Dataset.LINKS,
            data.link_rows,
            data.link_range,
            source_timezone(AnalyticsSource.FIRST_PARTY, settings),
        ),
        (
            AnalyticsSource.GA4,
            Dataset.PAGES,
            data.ga4_page_rows,
            data.ga4_range,
            source_timezone(AnalyticsSource.GA4, settings),
        ),
        (
            AnalyticsSource.SEARCH_CONSOLE,
            Dataset.PAGES,
            data.gsc_page_rows,
            data.gsc_range,
            source_timezone(AnalyticsSource.SEARCH_CONSOLE, settings),
        ),
    ):
        if window is None or not rows:
            continue
        link_rows = rows if dataset is Dataset.LINKS else ()
        page_rows = rows if dataset is Dataset.PAGES else ()
        provider = MockAnalyticsProvider(
            link_rows=[row for row in link_rows if isinstance(row, LinkMetrics)],
            page_rows=[row for row in page_rows if isinstance(row, PageMetrics)],
            stats=FetchStats(rows_read=len(rows)),
        )
        outcomes.append(
            await run_import(
                sessionmaker=sessionmaker,
                settings=settings,
                policy=policy,
                provider=provider,
                request=ImportRequest(
                    source=source,
                    dataset=dataset,
                    via=ImportVia.SYNTHETIC,
                    date_range=window,
                    source_timezone=timezone,
                    synthetic=True,
                    triggered_by="cli",
                ),
                now=moment,
            )
        )
    logger.info("analytics_demo_seed_done", imports=len(outcomes), plans=len(plans))
    return outcomes
