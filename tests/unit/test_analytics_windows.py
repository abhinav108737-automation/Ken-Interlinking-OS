"""Before/after window maths (ADR-0015 §3). Pure: no database, no provider.

These are the numbers the UI will print next to "observed change, not a causal effect", so
the arithmetic, the coverage rules and every confounder flag are pinned down here.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from ken_contracts.analytics import (
    AnalyticsPolicy,
    AnalyticsSource,
    ConfounderFlag,
    PageWindowComparison,
)
from ken_services.analytics.policy import load_analytics_policy
from ken_services.analytics.windows import (
    COMPARED_METRIC,
    GSC_TIMEZONE,
    PageDailyRow,
    day_of,
    page_comparison,
    site_trend_ratio,
    source_timezone,
    window_bounds,
    window_stats,
)
from tests.conftest import make_settings

PAGE = uuid.UUID("22222222-2222-4222-8222-222222222222")
OTHER = uuid.UUID("33333333-3333-4333-8333-333333333333")
PLAN = uuid.UUID("11111111-1111-4111-8111-111111111111")
ANCHOR = date(2026, 5, 15)


POLICY_PATH = Path(__file__).resolve().parents[2] / "policies" / "analytics" / "analytics.v1.yaml"


def _policy() -> AnalyticsPolicy:
    return load_analytics_policy(POLICY_PATH)


def _row(
    page_id: uuid.UUID,
    day: date,
    *,
    clicks: int = 1,
    views: int = 10,
    impressions: int = 100,
    position_sum: float = 300.0,
    sessions: int = 5,
    synthetic: bool = False,
) -> PageDailyRow:
    return PageDailyRow(
        page_id=page_id,
        source=AnalyticsSource.SEARCH_CONSOLE,
        metric_date=day,
        impressions=impressions,
        clicks=clicks,
        position_sum=position_sum,
        pageviews=views,
        sessions=sessions,
        synthetic=synthetic,
    )


def _series(
    page_id: uuid.UUID,
    first: date,
    last: date,
    *,
    clicks: int,
    source: AnalyticsSource = AnalyticsSource.SEARCH_CONSOLE,
) -> list[PageDailyRow]:
    days = (last - first).days + 1
    return [
        PageDailyRow(
            page_id=page_id,
            source=source,
            metric_date=first + timedelta(days=offset),
            impressions=100,
            clicks=clicks,
            position_sum=300.0,
            pageviews=clicks * 2,
            sessions=clicks,
        )
        for offset in range(days)
    ]


# --------------------------------------------------------------------------- #
# Day boundaries
# --------------------------------------------------------------------------- #
def test_each_source_has_its_own_day_boundary() -> None:
    settings = make_settings(ga4_timezone="Asia/Kolkata")
    assert source_timezone(AnalyticsSource.GA4, settings) == "Asia/Kolkata"
    assert source_timezone(AnalyticsSource.SEARCH_CONSOLE, settings) == GSC_TIMEZONE
    assert source_timezone(AnalyticsSource.FIRST_PARTY, settings) == "UTC"


def test_the_same_instant_is_a_different_day_per_source() -> None:
    moment = datetime(2026, 5, 11, 19, 30, tzinfo=UTC)
    assert day_of(moment, "UTC") == date(2026, 5, 11)
    assert day_of(moment, "Asia/Kolkata") == date(2026, 5, 12)  # +05:30
    assert day_of(moment, GSC_TIMEZONE) == date(2026, 5, 11)  # -07:00
    moment = datetime(2026, 5, 12, 2, 0, tzinfo=UTC)
    assert day_of(moment, GSC_TIMEZONE) == date(2026, 5, 11)


def test_an_unknown_time_zone_falls_back_to_utc() -> None:
    assert day_of(datetime(2026, 5, 11, 12, 0, tzinfo=UTC), "Mars/Olympus") == date(2026, 5, 11)


# --------------------------------------------------------------------------- #
# Bounds
# --------------------------------------------------------------------------- #
def test_the_anchor_day_is_excluded_from_both_windows() -> None:
    bounds = window_bounds(ANCHOR, policy=_policy().windows)
    assert bounds.pre_start == ANCHOR - timedelta(days=28)
    assert bounds.pre_end == ANCHOR - timedelta(days=1)
    assert bounds.post_start == ANCHOR + timedelta(days=1)
    assert bounds.post_end == ANCHOR + timedelta(days=28)
    assert bounds.truncated is False


def test_a_rollback_truncates_the_post_window_the_day_before() -> None:
    bounds = window_bounds(
        ANCHOR, policy=_policy().windows, rolled_back_day=ANCHOR + timedelta(days=5)
    )
    assert bounds.post_end == ANCHOR + timedelta(days=4)
    assert bounds.truncated is True


def test_missing_coverage_truncates_the_post_window() -> None:
    bounds = window_bounds(
        ANCHOR, policy=_policy().windows, last_covered=ANCHOR + timedelta(days=3)
    )
    assert bounds.post_end == ANCHOR + timedelta(days=3)
    assert bounds.truncated is True


def test_a_post_window_with_no_day_at_all_is_empty() -> None:
    bounds = window_bounds(ANCHOR, policy=_policy().windows, last_covered=ANCHOR)
    assert bounds.post_end is None
    assert bounds.truncated is True


# --------------------------------------------------------------------------- #
# Statistics
# --------------------------------------------------------------------------- #
def test_means_are_per_covered_day_not_per_calendar_day() -> None:
    rows = [_row(PAGE, date(2026, 5, 1), clicks=10), _row(PAGE, date(2026, 5, 3), clicks=20)]
    stats = window_stats(start=date(2026, 5, 1), end=date(2026, 5, 4), rows=rows, metric="clicks")
    assert stats.days == 4
    assert stats.covered_days == 2
    assert stats.clicks == 30
    assert stats.mean_per_day == 15.0
    assert stats.impressions == 200
    assert stats.ctr == pytest.approx(30 / 200)
    assert stats.avg_position == pytest.approx(600.0 / 200)


def test_an_empty_window_has_no_end_and_no_mean() -> None:
    stats = window_stats(start=date(2026, 5, 1), end=None, rows=[], metric="clicks")
    assert stats.end is None
    assert stats.days == 0
    assert stats.covered_days == 0
    assert stats.mean_per_day is None
    assert stats.ctr is None
    assert stats.avg_position is None


def test_rows_outside_the_window_are_ignored() -> None:
    rows = [_row(PAGE, date(2026, 4, 1), clicks=99), _row(PAGE, date(2026, 5, 2), clicks=1)]
    stats = window_stats(start=date(2026, 5, 1), end=date(2026, 5, 3), rows=rows, metric="clicks")
    assert stats.clicks == 1


def test_the_compared_metric_is_per_source() -> None:
    assert COMPARED_METRIC[AnalyticsSource.SEARCH_CONSOLE] == "clicks"
    assert COMPARED_METRIC[AnalyticsSource.GA4] == "pageviews"


# --------------------------------------------------------------------------- #
# Comparisons and flags
# --------------------------------------------------------------------------- #
def _comparison(rows: Sequence[PageDailyRow], **kwargs: Any) -> PageWindowComparison:
    policy = _policy()
    return page_comparison(
        page_id=PAGE,
        role="target",
        plan_id=PLAN,
        source=AnalyticsSource.SEARCH_CONSOLE,
        anchor_date=ANCHOR,
        policy=policy.windows,
        page_rows=rows,
        **kwargs,
    )


def test_a_full_pre_and_post_window_yields_a_delta_and_a_ratio() -> None:
    rows = _series(PAGE, ANCHOR - timedelta(days=28), ANCHOR - timedelta(days=1), clicks=10)
    rows += _series(PAGE, ANCHOR + timedelta(days=1), ANCHOR + timedelta(days=28), clicks=15)
    comparison = _comparison(rows)
    assert comparison.pre.covered_days == 28
    assert comparison.post.covered_days == 28
    assert comparison.delta_per_day == pytest.approx(5.0)
    assert comparison.ratio == pytest.approx(1.5)
    assert ConfounderFlag.INSUFFICIENT_DATA not in comparison.flags
    assert comparison.metric == "clicks"
    assert comparison.anchor_date == ANCHOR
    assert comparison.role == "target"


def test_too_few_covered_days_means_no_delta_and_a_flag() -> None:
    rows = _series(PAGE, ANCHOR - timedelta(days=5), ANCHOR - timedelta(days=1), clicks=10)
    rows += _series(PAGE, ANCHOR + timedelta(days=1), ANCHOR + timedelta(days=28), clicks=15)
    comparison = _comparison(rows)
    assert comparison.pre.covered_days == 5 < _policy().windows.min_covered_days
    assert comparison.delta_per_day is None
    assert comparison.ratio is None
    assert ConfounderFlag.INSUFFICIENT_DATA in comparison.flags


def test_a_rollback_inside_the_window_is_flagged_and_truncates() -> None:
    rolled_back = ANCHOR + timedelta(days=10)
    rows = _series(PAGE, ANCHOR - timedelta(days=28), rolled_back, clicks=10)
    comparison = _comparison(rows, rolled_back_day=rolled_back)
    assert ConfounderFlag.ROLLED_BACK_IN_WINDOW in comparison.flags
    assert ConfounderFlag.POST_WINDOW_TRUNCATED in comparison.flags
    assert comparison.post.end == rolled_back - timedelta(days=1)


def test_synthetic_rows_flag_the_comparison() -> None:
    rows = [
        PageDailyRow(
            page_id=PAGE,
            source=AnalyticsSource.SEARCH_CONSOLE,
            metric_date=ANCHOR - timedelta(days=1),
            clicks=1,
            synthetic=True,
        )
    ]
    comparison = _comparison(rows)
    assert ConfounderFlag.SYNTHETIC_DATA in comparison.flags
    assert comparison.synthetic is True


def test_sampled_and_extra_flags_are_carried_through() -> None:
    comparison = _comparison(
        [],
        sampled=True,
        extra_flags=[ConfounderFlag.OVERLAPPING_CHANGES, ConfounderFlag.CONTENT_CHANGED],
    )
    assert ConfounderFlag.SAMPLED_DATA in comparison.flags
    assert ConfounderFlag.OVERLAPPING_CHANGES in comparison.flags
    assert ConfounderFlag.CONTENT_CHANGED in comparison.flags


def test_another_pages_rows_never_leak_into_a_comparison() -> None:
    rows = _series(OTHER, ANCHOR - timedelta(days=28), ANCHOR + timedelta(days=28), clicks=99)
    comparison = _comparison(rows)
    assert comparison.pre.clicks == 0
    assert comparison.post.clicks == 0


def test_rows_of_another_source_never_leak_into_a_comparison() -> None:
    rows = _series(
        PAGE,
        ANCHOR - timedelta(days=28),
        ANCHOR + timedelta(days=28),
        clicks=99,
        source=AnalyticsSource.GA4,
    )
    comparison = _comparison(rows)
    assert comparison.pre.clicks == 0


# --------------------------------------------------------------------------- #
# Site trend (context only)
# --------------------------------------------------------------------------- #
def test_the_site_trend_uses_only_pages_present_in_both_windows() -> None:
    bounds = window_bounds(ANCHOR, policy=_policy().windows)
    assert bounds.post_end is not None
    rows = _series(PAGE, bounds.pre_start, bounds.pre_end, clicks=10)
    rows += _series(PAGE, bounds.post_start, bounds.post_end, clicks=20)
    # A page that exists only after the change must not move the trend.
    rows += _series(OTHER, bounds.post_start, bounds.post_end, clicks=1000)
    assert site_trend_ratio(bounds=bounds, rows=rows, metric="clicks") == pytest.approx(2.0)


def test_the_site_trend_is_none_without_a_shared_page() -> None:
    bounds = window_bounds(ANCHOR, policy=_policy().windows)
    rows = _series(PAGE, bounds.pre_start, bounds.pre_end, clicks=10)
    assert site_trend_ratio(bounds=bounds, rows=rows, metric="clicks") is None
    assert site_trend_ratio(bounds=bounds, rows=[], metric="clicks") is None
