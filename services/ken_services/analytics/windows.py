"""Before/after windows and their statistics (ADR-0015 §3 as amended, §12.9.6). PURE: every
function here takes rows that were already read and returns contract models.

**Day boundaries are source-local.** ``D(x)`` converts an instant to the source's own calendar
day with ``zoneinfo``: GA4 uses ``KEN_GA4_TIMEZONE`` (the property's zone), Search Console
America/Los_Angeles, first-party UTC. Mixing them would shift a window by a day and quietly
change what "the day the link went live" means.

**The windows.** With ``N = windows.comparison_days`` and ``D0 = D(deployed_at)``, which is
EXCLUDED as a partial day::

    pre  = [D0 - N, D0 - 1]
    post = [D0 + 1, min(D0 + N, D(rolled_back_at) - 1, last covered date)]

**The statistics.** Covered days, totals, per-covered-day means, CTR
(``clicks / impressions``), average position (``sum(position_sum) / sum(impressions)``), and
-- only when BOTH windows have at least ``windows.min_covered_days`` covered days -- the
delta and ratio of the means. The compared metric is clicks for Search Console and pageviews
for GA4 (§12.9.6).

``site_trend_ratio`` is the same two windows over every page with data in both, as CONTEXT:
it is never applied as an adjustment, and the UI says "observed change, not a causal effect".
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Final, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ken_contracts.analytics import (
    AnalyticsSource,
    AnalyticsWindowPolicy,
    ConfounderFlag,
    PageWindowComparison,
    WindowStats,
)
from ken_core.config import Settings

__all__ = [
    "COMPARED_METRIC",
    "GSC_TIMEZONE",
    "PageDailyRow",
    "WindowBounds",
    "day_of",
    "page_comparison",
    "site_trend_ratio",
    "source_timezone",
    "window_bounds",
    "window_stats",
]

GSC_TIMEZONE: Final = "America/Los_Angeles"
"""Search Console's day boundary (ADR-0015 §3)."""
COMPARED_METRIC: Final[Mapping[AnalyticsSource, Literal["clicks", "pageviews"]]] = {
    AnalyticsSource.SEARCH_CONSOLE: "clicks",
    AnalyticsSource.GA4: "pageviews",
}
"""Which metric a before/after comparison compares, per page source (§12.9.6)."""


def source_timezone(source: AnalyticsSource, settings: Settings) -> str:
    """The IANA zone whose midnight separates ``source``'s days."""
    match AnalyticsSource(source):
        case AnalyticsSource.GA4:
            return settings.ga4_timezone
        case AnalyticsSource.SEARCH_CONSOLE:
            return GSC_TIMEZONE
        case AnalyticsSource.FIRST_PARTY:
            return "UTC"


def day_of(moment: datetime, timezone: str) -> date:
    """``D(x)``: the calendar day of ``moment`` in ``timezone`` (UTC if it is unknown)."""
    try:
        zone = ZoneInfo(timezone)
    except (ZoneInfoNotFoundError, ValueError):
        zone = ZoneInfo("UTC")
    return moment.astimezone(zone).date()


@dataclass(frozen=True, slots=True)
class PageDailyRow:
    """One stored ``page_metrics_daily`` row, as the window maths needs it."""

    page_id: uuid.UUID
    source: AnalyticsSource
    metric_date: date
    impressions: int = 0
    clicks: int = 0
    position_sum: float = 0.0
    pageviews: int = 0
    sessions: int = 0
    synthetic: bool = False

    def metric(self, name: Literal["clicks", "pageviews"]) -> int:
        return self.clicks if name == "clicks" else self.pageviews


@dataclass(frozen=True, slots=True)
class WindowBounds:
    pre_start: date
    pre_end: date
    post_start: date
    post_end: date | None
    """``None``: the post window has no day yet (nothing after D0 is covered)."""
    truncated: bool
    """The post window ends before ``D0 + N`` (rollback or missing coverage)."""


def window_bounds(
    anchor: date,
    *,
    policy: AnalyticsWindowPolicy,
    rolled_back_day: date | None = None,
    last_covered: date | None = None,
) -> WindowBounds:
    """The pre and post windows around ``anchor`` = ``D0`` (which is itself excluded)."""
    span = policy.comparison_days
    pre_start = anchor - timedelta(days=span)
    pre_end = anchor - timedelta(days=1)
    post_start = anchor + timedelta(days=1)
    full_end = anchor + timedelta(days=span)
    end = full_end
    if rolled_back_day is not None:
        end = min(end, rolled_back_day - timedelta(days=1))
    if last_covered is not None:
        end = min(end, last_covered)
    if end < post_start:
        return WindowBounds(
            pre_start=pre_start,
            pre_end=pre_end,
            post_start=post_start,
            post_end=None,
            truncated=True,
        )
    return WindowBounds(
        pre_start=pre_start,
        pre_end=pre_end,
        post_start=post_start,
        post_end=end,
        truncated=end < full_end,
    )


def window_stats(
    *,
    start: date,
    end: date | None,
    rows: Iterable[PageDailyRow],
    metric: Literal["clicks", "pageviews"],
) -> WindowStats:
    """Totals and per-COVERED-day means of the rows inside ``[start, end]``."""
    if end is None:
        return WindowStats(start=start, end=None, days=0, covered_days=0, impressions=0, clicks=0,
                           pageviews=0, sessions=0)  # fmt: skip
    inside = [row for row in rows if start <= row.metric_date <= end]
    days = (end - start).days + 1
    covered = len({row.metric_date for row in inside})
    impressions = sum(row.impressions for row in inside)
    clicks = sum(row.clicks for row in inside)
    pageviews = sum(row.pageviews for row in inside)
    sessions = sum(row.sessions for row in inside)
    position_sum = sum(row.position_sum for row in inside)
    total = clicks if metric == "clicks" else pageviews
    return WindowStats(
        start=start,
        end=end,
        days=days,
        covered_days=covered,
        impressions=impressions,
        clicks=clicks,
        pageviews=pageviews,
        sessions=sessions,
        ctr=None if impressions == 0 else clicks / impressions,
        avg_position=None if impressions == 0 or position_sum == 0 else position_sum / impressions,
        mean_per_day=None if covered == 0 else total / covered,
    )


def site_trend_ratio(
    *,
    bounds: WindowBounds,
    rows: Iterable[PageDailyRow],
    metric: Literal["clicks", "pageviews"],
) -> float | None:
    """The same windows over EVERY page with data in both, as context only.

    Both sides are means per (page, covered day) observation, so a page with more covered
    days weighs more but a page present in only one window cannot tilt the ratio at all.
    """
    if bounds.post_end is None:
        return None
    pre: dict[uuid.UUID, list[PageDailyRow]] = {}
    post: dict[uuid.UUID, list[PageDailyRow]] = {}
    for row in rows:
        if bounds.pre_start <= row.metric_date <= bounds.pre_end:
            pre.setdefault(row.page_id, []).append(row)
        elif bounds.post_start <= row.metric_date <= bounds.post_end:
            post.setdefault(row.page_id, []).append(row)
    shared = set(pre) & set(post)
    if not shared:
        return None

    def mean(buckets: Mapping[uuid.UUID, list[PageDailyRow]]) -> float | None:
        observations = 0
        total = 0
        for page_id in shared:
            page_rows = buckets[page_id]
            observations += len({row.metric_date for row in page_rows})
            total += sum(row.metric(metric) for row in page_rows)
        return None if observations == 0 else total / observations

    pre_mean, post_mean = mean(pre), mean(post)
    if pre_mean is None or post_mean is None or pre_mean <= 0:
        return None
    return post_mean / pre_mean


def page_comparison(
    *,
    page_id: uuid.UUID,
    role: Literal["source", "target"],
    plan_id: uuid.UUID,
    source: AnalyticsSource,
    anchor_date: date,
    policy: AnalyticsWindowPolicy,
    page_rows: Sequence[PageDailyRow],
    site_rows: Sequence[PageDailyRow] = (),
    rolled_back_day: date | None = None,
    last_covered: date | None = None,
    sampled: bool = False,
    extra_flags: Iterable[ConfounderFlag] = (),
) -> PageWindowComparison:
    """One page's observed change around one plan's ``D0`` (never a causal claim)."""
    metric = COMPARED_METRIC[AnalyticsSource(source)]
    bounds = window_bounds(
        anchor_date, policy=policy, rolled_back_day=rolled_back_day, last_covered=last_covered
    )
    mine = [row for row in page_rows if row.page_id == page_id and row.source is source]
    pre = window_stats(start=bounds.pre_start, end=bounds.pre_end, rows=mine, metric=metric)
    post = window_stats(start=bounds.post_start, end=bounds.post_end, rows=mine, metric=metric)
    pre_mean, post_mean = pre.mean_per_day, post.mean_per_day
    sufficient = (
        pre.covered_days >= policy.min_covered_days
        and post.covered_days >= policy.min_covered_days
        and pre_mean is not None
        and post_mean is not None
    )
    delta: float | None = None
    ratio: float | None = None
    if sufficient and pre_mean is not None and post_mean is not None:
        delta = post_mean - pre_mean
        if pre_mean > 0:
            ratio = post_mean / pre_mean
    flags = set(extra_flags)
    if not sufficient:
        flags.add(ConfounderFlag.INSUFFICIENT_DATA)
    if bounds.truncated:
        flags.add(ConfounderFlag.POST_WINDOW_TRUNCATED)
    if rolled_back_day is not None and bounds.pre_start <= rolled_back_day <= (
        anchor_date + timedelta(days=policy.comparison_days)
    ):
        flags.add(ConfounderFlag.ROLLED_BACK_IN_WINDOW)
    if sampled:
        flags.add(ConfounderFlag.SAMPLED_DATA)
    if any(row.synthetic for row in mine):
        flags.add(ConfounderFlag.SYNTHETIC_DATA)
    return PageWindowComparison(
        page_id=page_id,
        role=role,
        plan_id=plan_id,
        source=AnalyticsSource(source),
        metric=metric,
        anchor_date=anchor_date,
        pre=pre,
        post=post,
        delta_per_day=delta,
        ratio=ratio,
        site_trend_ratio=site_trend_ratio(bounds=bounds, rows=site_rows, metric=metric),
        flags=sorted(flags),
        synthetic=any(row.synthetic for row in mine),
    )
