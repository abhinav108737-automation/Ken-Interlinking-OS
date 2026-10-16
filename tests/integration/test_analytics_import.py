"""The replace-import, end to end against PostgreSQL (ADR-0015 §2, §9 "Import").

What is asserted here is the contract the learning phases rest on: an import is
authoritative for its (source, dataset, range), a re-import is byte-identical, every dropped
row is counted rather than guessed at, and nothing can accumulate metrics for a link that
was never on a page.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from ken_contracts.analytics import (
    AnalyticsSource,
    Dataset,
    DateRange,
    FetchStats,
    ImportVia,
    LinkMetrics,
    MetricSource,
    PageMetrics,
)
from ken_services.analytics.protocol import (
    AnalyticsImportError,
    ImportErrorCode,
    ImportRequest,
)
from tests.integration.analytics_support import (
    POLICY,
    SETTINGS,
    analytics_events,
    job_runs,
    link_csv,
    link_metric_rows,
    make_live,
    new_plan,
    page_csv,
    page_metric_rows,
    raw_csv,
    request_for,
    rows_of,
    run_csv_import,
    run_mock_import,
    seed_pages,
    supersede,
)
from tests.integration.recommendation_support import SITE, SM

pytestmark = pytest.mark.integration

SOURCE = "/reports/source"
TARGET = "/reports/target"
OTHER = "/reports/other"
DEPLOYED_AT = datetime(2026, 5, 10, 12, 0, tzinfo=UTC)


async def _world(sessionmaker: SM) -> tuple[dict[str, uuid.UUID], uuid.UUID]:
    """One live plan on one source page, deployed 2026-05-10 and never rolled back."""
    pages = await seed_pages(sessionmaker, SOURCE, TARGET, OTHER)
    plan_id = await new_plan(
        sessionmaker,
        source=pages[SOURCE],
        target=pages[TARGET],
        source_url=SITE + SOURCE,
        target_url=SITE + TARGET,
    )
    await make_live(sessionmaker, plan_id, deployed_at=DEPLOYED_AT)
    return pages, plan_id


def _link_row(plan_id: uuid.UUID, day: str, impressions: int, clicks: int) -> str:
    return f"{day},first_party,{plan_id},{impressions},{clicks}"


async def test_a_csv_import_writes_rows_a_ledger_row_and_one_event(
    migrated: None, sessionmaker: SM, tmp_path: Path
) -> None:
    _pages, plan_id = await _world(sessionmaker)
    path = link_csv(
        tmp_path,
        [_link_row(plan_id, "2026-05-11", 100, 4), _link_row(plan_id, "2026-05-12", 120, 6)],
    )
    outcome = await run_csv_import(
        sessionmaker,
        path,
        request=request_for(
            source=AnalyticsSource.FIRST_PARTY,
            dataset=Dataset.LINKS,
            start="2026-05-11",
            end="2026-05-12",
        ),
    )
    assert await link_metric_rows(sessionmaker) == [
        ("first_party", datetime(2026, 5, 11).date(), plan_id, 100, 4, False),
        ("first_party", datetime(2026, 5, 12).date(), plan_id, 120, 6, False),
    ]
    assert outcome.counters["rows_written"] == 2
    assert outcome.counters["rows_read"] == 2

    ledger = await rows_of(sessionmaker, "SELECT * FROM analytics_imports")
    assert len(ledger) == 1
    assert ledger[0].id == outcome.import_id
    assert ledger[0].via == ImportVia.CSV.value
    assert ledger[0].rows_written == 2
    assert ledger[0].file_name == path.name
    assert len(ledger[0].file_sha256) == 64
    assert ledger[0].analytics_policy_version == POLICY.version
    assert ledger[0].synthetic is False

    events = await analytics_events(sessionmaker)
    assert len(events) == 1
    payload = events[0].payload
    assert payload["import_id"] == str(outcome.import_id)
    assert payload["rows_written"] == 2
    assert payload["links_affected"] == 1
    assert payload["plans_measured"] == 0
    assert events[0].subject_type == "analytics_import"
    assert events[0].emitted_by == "ken-analytics"
    assert events[0].correlation_id == outcome.job_run_id


async def test_importing_the_same_file_twice_gives_identical_rows(
    migrated: None, sessionmaker: SM, tmp_path: Path
) -> None:
    _pages, plan_id = await _world(sessionmaker)
    path = link_csv(
        tmp_path,
        [_link_row(plan_id, "2026-05-11", 100, 4), _link_row(plan_id, "2026-05-12", 120, 6)],
    )
    request = request_for(
        source=AnalyticsSource.FIRST_PARTY,
        dataset=Dataset.LINKS,
        start="2026-05-11",
        end="2026-05-12",
    )
    await run_csv_import(sessionmaker, path, request=request)
    first = await link_metric_rows(sessionmaker)
    second_outcome = await run_csv_import(sessionmaker, path, request=request)
    assert await link_metric_rows(sessionmaker) == first
    # The ROWS are identical; the ledger keeps both runs (it is append-only).
    assert len(await rows_of(sessionmaker, "SELECT id FROM analytics_imports")) == 2
    assert second_outcome.counters["rows_written"] == 2


async def test_an_overlapping_range_replaces_exactly_those_days(
    migrated: None, sessionmaker: SM, tmp_path: Path
) -> None:
    _pages, plan_id = await _world(sessionmaker)
    await run_csv_import(
        sessionmaker,
        link_csv(
            tmp_path,
            [
                _link_row(plan_id, "2026-05-11", 10, 1),
                _link_row(plan_id, "2026-05-12", 20, 2),
                _link_row(plan_id, "2026-05-13", 30, 3),
            ],
            name="first.csv",
        ),
        request=request_for(
            source=AnalyticsSource.FIRST_PARTY,
            dataset=Dataset.LINKS,
            start="2026-05-11",
            end="2026-05-13",
        ),
    )
    # A second import covering 12-14 is authoritative for those three days only.
    await run_csv_import(
        sessionmaker,
        link_csv(
            tmp_path,
            [_link_row(plan_id, "2026-05-12", 99, 9), _link_row(plan_id, "2026-05-14", 44, 4)],
            name="second.csv",
        ),
        request=request_for(
            source=AnalyticsSource.FIRST_PARTY,
            dataset=Dataset.LINKS,
            start="2026-05-12",
            end="2026-05-14",
        ),
    )
    assert await link_metric_rows(sessionmaker) == [
        ("first_party", datetime(2026, 5, 11).date(), plan_id, 10, 1, False),
        ("first_party", datetime(2026, 5, 12).date(), plan_id, 99, 9, False),
        ("first_party", datetime(2026, 5, 14).date(), plan_id, 44, 4, False),
    ]


async def test_duplicate_keys_within_one_import_are_summed(
    migrated: None, sessionmaker: SM, tmp_path: Path
) -> None:
    _pages, plan_id = await _world(sessionmaker)
    outcome = await run_csv_import(
        sessionmaker,
        link_csv(
            tmp_path,
            [
                _link_row(plan_id, "2026-05-11", 10, 1),
                _link_row(plan_id, "2026-05-11", 5, 2),
                _link_row(plan_id, "2026-05-11", 1, 0),
            ],
        ),
        request=request_for(
            source=AnalyticsSource.FIRST_PARTY,
            dataset=Dataset.LINKS,
            start="2026-05-11",
            end="2026-05-11",
        ),
    )
    assert await link_metric_rows(sessionmaker) == [
        ("first_party", datetime(2026, 5, 11).date(), plan_id, 16, 3, False)
    ]
    assert outcome.counters["merged_rows"] == 2
    assert outcome.counters["rows_written"] == 1


async def test_synthetic_rows_never_mix_with_real_ones(
    migrated: None, sessionmaker: SM, tmp_path: Path
) -> None:
    _pages, plan_id = await _world(sessionmaker)
    path = link_csv(tmp_path, [_link_row(plan_id, "2026-05-11", 10, 1)])
    await run_csv_import(
        sessionmaker,
        path,
        request=request_for(
            source=AnalyticsSource.FIRST_PARTY,
            dataset=Dataset.LINKS,
            start="2026-05-11",
            end="2026-05-11",
        ),
    )
    with pytest.raises(AnalyticsImportError) as refused:
        await run_csv_import(
            sessionmaker,
            path,
            request=request_for(
                source=AnalyticsSource.FIRST_PARTY,
                dataset=Dataset.LINKS,
                start="2026-05-11",
                end="2026-05-11",
                synthetic=True,
                replace_via=True,
            ),
        )
    assert refused.value.code is ImportErrorCode.SYNTHETIC_REAL_MIX
    assert len(await link_metric_rows(sessionmaker)) == 1


async def test_replacing_rows_of_another_transport_needs_replace_via(
    migrated: None, sessionmaker: SM, tmp_path: Path
) -> None:
    _pages, plan_id = await _world(sessionmaker)
    path = link_csv(tmp_path, [_link_row(plan_id, "2026-05-11", 10, 1)])
    await run_csv_import(
        sessionmaker,
        path,
        request=request_for(
            source=AnalyticsSource.FIRST_PARTY,
            dataset=Dataset.LINKS,
            start="2026-05-11",
            end="2026-05-11",
        ),
    )
    collector_rows = [
        LinkMetrics(
            link_id=str(plan_id),
            source=MetricSource.FIRST_PARTY,
            range=DateRange(start=datetime(2026, 5, 11).date(), end=datetime(2026, 5, 11).date()),
            impressions=77,
            clicks=7,
        )
    ]
    with pytest.raises(AnalyticsImportError) as refused:
        await run_mock_import(
            sessionmaker,
            request=request_for(
                source=AnalyticsSource.FIRST_PARTY,
                dataset=Dataset.LINKS,
                start="2026-05-11",
                end="2026-05-11",
                via=ImportVia.COLLECTOR,
            ),
            link_rows=collector_rows,
        )
    assert refused.value.code is ImportErrorCode.VIA_MISMATCH
    assert (await link_metric_rows(sessionmaker))[0][3] == 10

    outcome = await run_mock_import(
        sessionmaker,
        request=request_for(
            source=AnalyticsSource.FIRST_PARTY,
            dataset=Dataset.LINKS,
            start="2026-05-11",
            end="2026-05-11",
            via=ImportVia.COLLECTOR,
            replace_via=True,
        ),
        link_rows=collector_rows,
    )
    assert (await link_metric_rows(sessionmaker))[0][3] == 77
    ledger = await rows_of(
        sessionmaker, "SELECT replace_via FROM analytics_imports WHERE id = :i", i=outcome.import_id
    )
    assert ledger[0].replace_via is True


@pytest.mark.parametrize(
    ("source", "timezone", "end", "lag"),
    [
        (AnalyticsSource.GA4, "Asia/Kolkata", "2026-06-01", 1),
        (AnalyticsSource.SEARCH_CONSOLE, "America/Los_Angeles", "2026-05-31", 3),
    ],
)
async def test_an_end_date_past_the_source_lag_is_refused(
    migrated: None,
    sessionmaker: SM,
    source: AnalyticsSource,
    timezone: str,
    end: str,
    lag: int,
) -> None:
    await seed_pages(sessionmaker, SOURCE)
    with pytest.raises(AnalyticsImportError) as refused:
        await run_mock_import(
            sessionmaker,
            request=request_for(
                source=source,
                dataset=Dataset.PAGES,
                start=end,
                end=end,
                via=ImportVia.API,
                source_timezone=timezone,
            ),
        )
    assert refused.value.code is ImportErrorCode.END_PAST_LAG
    assert POLICY.lag_days(source) == lag
    assert await page_metric_rows(sessionmaker) == []


async def test_non_daily_and_out_of_range_rows_are_rejected(
    migrated: None,
    sessionmaker: SM,
) -> None:
    _pages, plan_id = await _world(sessionmaker)
    day = datetime(2026, 5, 11).date()
    outcome = await run_mock_import(
        sessionmaker,
        request=request_for(
            source=AnalyticsSource.FIRST_PARTY,
            dataset=Dataset.LINKS,
            start="2026-05-11",
            end="2026-05-11",
            via=ImportVia.COLLECTOR,
        ),
        link_rows=[
            # One GOOD row, so this import is a partial failure rather than a total one: an
            # import that reads rows and writes none is now refused outright (see
            # ``test_an_import_whose_every_row_is_rejected_is_refused``), and the row rules
            # under test here are about classifying the bad rows beside a good one.
            LinkMetrics(
                link_id=str(plan_id),
                source=MetricSource.FIRST_PARTY,
                range=DateRange(start=day, end=day),
                impressions=7,
                clicks=2,
            ),
            LinkMetrics(
                link_id=str(plan_id),
                source=MetricSource.FIRST_PARTY,
                range=DateRange(start=day, end=day + timedelta(days=1)),
                impressions=5,
                clicks=1,
            ),
            LinkMetrics(
                link_id=str(plan_id),
                source=MetricSource.FIRST_PARTY,
                range=DateRange(start=day + timedelta(days=4), end=day + timedelta(days=4)),
                impressions=5,
                clicks=1,
            ),
            LinkMetrics(
                link_id=str(plan_id),
                source=MetricSource.GA4,
                range=DateRange(start=day, end=day),
                impressions=5,
                clicks=1,
            ),
            LinkMetrics(
                link_id=str(uuid.uuid4()),
                source=MetricSource.FIRST_PARTY,
                range=DateRange(start=day, end=day),
                impressions=5,
                clicks=1,
            ),
        ],
    )
    assert outcome.counters["non_daily_row"] == 1
    assert outcome.counters["outside_range"] == 1
    assert outcome.counters["rejected_invalid"] == 1
    assert outcome.counters["unresolved_link"] == 1
    assert outcome.counters["rows_written"] == 1
    assert await link_metric_rows(sessionmaker) == [
        ("first_party", day, plan_id, 7, 2, False),
    ]
    events = await analytics_events(sessionmaker)
    assert events[0].payload["rows_rejected"] == 3
    assert events[0].payload["rows_unresolved"] == 1


async def test_rows_outside_the_live_window_are_dropped(migrated: None, sessionmaker: SM) -> None:
    pages = await seed_pages(sessionmaker, SOURCE, TARGET)
    live = await new_plan(
        sessionmaker,
        source=pages[SOURCE],
        target=pages[TARGET],
        source_url=SITE + SOURCE,
        target_url=SITE + TARGET,
    )
    await make_live(
        sessionmaker,
        live,
        deployed_at=DEPLOYED_AT,
        rolled_back_at=datetime(2026, 5, 12, 8, 0, tzinfo=UTC),
    )
    never = await new_plan(
        sessionmaker,
        source=pages[SOURCE],
        target=pages[TARGET],
        anchor="Batteries",
        source_url=SITE + SOURCE,
        target_url=SITE + TARGET,
    )
    await supersede(sessionmaker, never, at=datetime(2026, 5, 9, 8, 0, tzinfo=UTC))

    def row(plan_id: uuid.UUID, day: int) -> LinkMetrics:
        return LinkMetrics(
            link_id=str(plan_id),
            source=MetricSource.FIRST_PARTY,
            range=DateRange(start=datetime(2026, 5, day).date(), end=datetime(2026, 5, day).date()),
            impressions=10,
            clicks=1,
        )

    outcome = await run_mock_import(
        sessionmaker,
        request=request_for(
            source=AnalyticsSource.FIRST_PARTY,
            dataset=Dataset.LINKS,
            start="2026-05-09",
            end="2026-05-14",
            via=ImportVia.COLLECTOR,
        ),
        link_rows=[row(live, 9), row(live, 10), row(live, 12), row(live, 13), row(never, 11)],
    )
    # 10 (the day it went live) and 12 (the day it was rolled back) are inside, inclusive.
    assert [stored[1].isoformat() for stored in await link_metric_rows(sessionmaker)] == [
        "2026-05-10",
        "2026-05-12",
    ]
    assert outcome.counters["outside_live_window"] == 3


async def test_page_urls_resolve_strip_queries_and_count_misses(
    migrated: None, sessionmaker: SM, tmp_path: Path
) -> None:
    pages = await seed_pages(sessionmaker, SOURCE, TARGET)
    rows = [
        f"2026-05-11,search_console,{SITE}{SOURCE}?utm_source=news#frag,100,5,3.5,0,0",
        f"2026-05-11,search_console,{SITE}{TARGET},200,10,2.0,0,0",
        "2026-05-11,search_console,https://site.test/nope,9,1,4.0,0,0",
        "2026-05-11,search_console,https://elsewhere.test/x,9,1,4.0,0,0",
    ]
    outcome = await run_csv_import(
        sessionmaker,
        page_csv(tmp_path, rows),
        request=request_for(
            source=AnalyticsSource.SEARCH_CONSOLE,
            dataset=Dataset.PAGES,
            start="2026-05-11",
            end="2026-05-11",
            source_timezone="America/Los_Angeles",
        ),
    )
    stored = await page_metric_rows(sessionmaker)
    assert {(row[2], row[3], row[5]) for row in stored} == {
        (pages[SOURCE], 100, 350.0),
        (pages[TARGET], 200, 400.0),
    }
    assert outcome.counters["unresolved_url"] == 2
    ledger = await rows_of(sessionmaker, "SELECT unresolved_sample FROM analytics_imports")
    # The sample holds bounded PATHS only: no host, no query string.
    assert sorted(ledger[0].unresolved_sample) == ["/nope", "/x"]


async def test_the_ga4_property_time_zone_decides_the_day(migrated: None, sessionmaker: SM) -> None:
    """Asia/Kolkata is +05:30, so 2026-05-11T19:00Z is already 2026-05-12 for the property."""
    pages = await seed_pages(sessionmaker, SOURCE, TARGET)
    plan_id = await new_plan(
        sessionmaker,
        source=pages[SOURCE],
        target=pages[TARGET],
        source_url=SITE + SOURCE,
        target_url=SITE + TARGET,
    )
    await make_live(sessionmaker, plan_id, deployed_at=datetime(2026, 5, 11, 19, 0, tzinfo=UTC))
    rows = [
        LinkMetrics(
            link_id=str(plan_id),
            source=MetricSource.GA4,
            range=DateRange(start=datetime(2026, 5, day).date(), end=datetime(2026, 5, day).date()),
            impressions=10,
            clicks=1,
        )
        for day in (11, 12)
    ]
    outcome = await run_mock_import(
        sessionmaker,
        request=request_for(
            source=AnalyticsSource.GA4,
            dataset=Dataset.LINKS,
            start="2026-05-11",
            end="2026-05-12",
            via=ImportVia.API,
            source_timezone="Asia/Kolkata",
        ),
        link_rows=rows,
    )
    assert [stored[1].isoformat() for stored in await link_metric_rows(sessionmaker)] == [
        "2026-05-12"
    ]
    assert outcome.counters["outside_live_window"] == 1


async def test_a_refused_import_writes_nothing_but_a_failed_job_run(
    migrated: None, sessionmaker: SM, tmp_path: Path
) -> None:
    await seed_pages(sessionmaker, SOURCE)
    broken = raw_csv(tmp_path, "date,source,link_id,impressions\n2026-05-11,ga4,x,1\n")
    with pytest.raises(AnalyticsImportError) as refused:
        await run_csv_import(
            sessionmaker,
            broken,
            request=request_for(
                source=AnalyticsSource.FIRST_PARTY,
                dataset=Dataset.LINKS,
                start="2026-05-11",
                end="2026-05-11",
            ),
        )
    assert refused.value.code is ImportErrorCode.FILE_INVALID
    assert await rows_of(sessionmaker, "SELECT id FROM analytics_imports") == []
    assert await analytics_events(sessionmaker) == []
    runs = await job_runs(sessionmaker)
    assert [run.status for run in runs] == ["failed"]
    assert runs[0].failure_reason == "file_invalid"
    assert runs[0].config_snapshot["mode"] == "import"


async def test_the_ledger_row_cannot_be_changed_or_deleted(
    migrated: None, sessionmaker: SM, tmp_path: Path
) -> None:
    _pages, plan_id = await _world(sessionmaker)
    await run_csv_import(
        sessionmaker,
        link_csv(tmp_path, [_link_row(plan_id, "2026-05-11", 10, 1)]),
        request=request_for(
            source=AnalyticsSource.FIRST_PARTY,
            dataset=Dataset.LINKS,
            start="2026-05-11",
            end="2026-05-11",
        ),
    )
    for statement in (
        "UPDATE analytics_imports SET rows_written = 0",
        "DELETE FROM analytics_imports",
    ):
        with pytest.raises(Exception, match=r"append-only|not allowed|immutable"):
            await rows_of(sessionmaker, statement)


async def test_a_sampled_api_import_records_sampling(migrated: None, sessionmaker: SM) -> None:
    pages = await seed_pages(sessionmaker, SOURCE)
    outcome = await run_mock_import(
        sessionmaker,
        request=request_for(
            source=AnalyticsSource.GA4,
            dataset=Dataset.PAGES,
            start="2026-05-11",
            end="2026-05-11",
            via=ImportVia.API,
            source_timezone="Asia/Kolkata",
        ),
        page_rows=[
            PageMetrics(
                page_id=str(pages[SOURCE]),
                source=MetricSource.GA4,
                range=DateRange(
                    start=datetime(2026, 5, 11).date(), end=datetime(2026, 5, 11).date()
                ),
                pageviews=42,
                sessions=30,
            )
        ],
        stats=FetchStats(rows_read=1, sampled=True),
    )
    ledger = await rows_of(
        sessionmaker,
        "SELECT sampled, source_timezone FROM analytics_imports WHERE id = :i",
        i=outcome.import_id,
    )
    assert ledger[0].sampled is True
    assert ledger[0].source_timezone == "Asia/Kolkata"


async def test_an_import_of_the_wrong_dataset_for_its_source_is_refused(
    migrated: None, sessionmaker: SM
) -> None:
    await seed_pages(sessionmaker, SOURCE)
    with pytest.raises(AnalyticsImportError) as refused:
        await run_mock_import(
            sessionmaker,
            request=request_for(
                source=AnalyticsSource.SEARCH_CONSOLE,
                dataset=Dataset.LINKS,
                start="2026-05-11",
                end="2026-05-11",
                via=ImportVia.API,
                source_timezone="America/Los_Angeles",
            ),
        )
    assert refused.value.code is ImportErrorCode.INVALID_REQUEST


async def test_synthetic_data_is_refused_outside_local_and_ci(
    migrated: None,
    sessionmaker: SM,
) -> None:
    from tests.conftest import make_settings

    await seed_pages(sessionmaker, SOURCE)
    staging = make_settings(
        environment="staging",
        analytics_policy_path=SETTINGS.analytics_policy_path,
        database_url="postgresql+asyncpg://ken_app:n0t-the-dev-password@db.internal:5432/ken",
    )
    with pytest.raises(AnalyticsImportError) as refused:
        await run_mock_import(
            sessionmaker,
            request=request_for(
                source=AnalyticsSource.FIRST_PARTY,
                dataset=Dataset.LINKS,
                start="2026-05-11",
                end="2026-05-11",
                via=ImportVia.SYNTHETIC,
                synthetic=True,
            ),
            settings=staging,
        )
    assert refused.value.code is ImportErrorCode.ENVIRONMENT_REFUSED


async def test_the_job_snapshot_holds_no_secret(
    migrated: None,
    sessionmaker: SM,
    tmp_path: Path,
) -> None:
    _pages, plan_id = await _world(sessionmaker)
    await run_csv_import(
        sessionmaker,
        link_csv(tmp_path, [_link_row(plan_id, "2026-05-11", 10, 1)]),
        request=request_for(
            source=AnalyticsSource.FIRST_PARTY,
            dataset=Dataset.LINKS,
            start="2026-05-11",
            end="2026-05-11",
        ),
    )
    snapshot = (await job_runs(sessionmaker))[0].config_snapshot
    rendered = str(snapshot)
    assert "postgresql" not in rendered
    assert str(tmp_path) not in rendered
    assert "token" not in rendered
    assert snapshot["source"] == "first_party"
    assert snapshot["mode"] == "import"


async def test_a_file_identity_without_the_csv_transport_is_refused(
    migrated: None,
    sessionmaker: SM,
) -> None:
    """The ledger CHECK ties ``file_sha256`` to ``via = 'csv'``; the refusal is typed."""
    await seed_pages(sessionmaker, SOURCE)
    with pytest.raises(AnalyticsImportError) as refused:
        await run_mock_import(
            sessionmaker,
            request=ImportRequest(
                source=AnalyticsSource.GA4,
                dataset=Dataset.PAGES,
                via=ImportVia.API,
                date_range=DateRange(
                    start=datetime(2026, 5, 11).date(), end=datetime(2026, 5, 11).date()
                ),
                source_timezone="Asia/Kolkata",
                file_name="rows.csv",
                file_sha256="0" * 64,
                triggered_by="test",
            ),
        )
    assert refused.value.code is ImportErrorCode.INVALID_REQUEST
    assert await rows_of(sessionmaker, "SELECT id FROM analytics_imports") == []


# --------------------------------------------------------------------------- #
# An import that would write nothing (Phase 7 QA finding 1)
# --------------------------------------------------------------------------- #
async def test_an_import_whose_every_row_is_rejected_is_refused_and_keeps_the_stored_rows(
    migrated: None, sessionmaker: SM, tmp_path: Path
) -> None:
    """A replace import DELETEs before it inserts, so one whose every row is dropped would
    wipe the range and store nothing. It is refused instead, and the stored rows survive."""
    _pages, plan_id = await _world(sessionmaker)
    request = request_for(
        source=AnalyticsSource.FIRST_PARTY,
        dataset=Dataset.LINKS,
        start="2026-05-11",
        end="2026-05-12",
    )
    await run_csv_import(
        sessionmaker,
        link_csv(
            tmp_path,
            [_link_row(plan_id, "2026-05-11", 10, 1), _link_row(plan_id, "2026-05-12", 20, 2)],
            name="good.csv",
        ),
        request=request,
    )
    stored = await link_metric_rows(sessionmaker)
    assert len(stored) == 2

    # The QA reproduction: one row, with the wrong ORIGIN for this run (rejected_invalid).
    wrong_origin = link_csv(tmp_path, [f"2026-05-11,ga4,{plan_id},5,1"], name="wrong-origin.csv")
    with pytest.raises(AnalyticsImportError) as refused:
        await run_csv_import(sessionmaker, wrong_origin, request=request)
    assert refused.value.code is ImportErrorCode.FILE_INVALID

    # Nothing was deleted, nothing was written, and only the failed run records the attempt.
    assert await link_metric_rows(sessionmaker) == stored
    assert len(await rows_of(sessionmaker, "SELECT id FROM analytics_imports")) == 1
    assert len(await analytics_events(sessionmaker)) == 1
    runs = await job_runs(sessionmaker)
    assert sorted(run.status for run in runs) == ["failed", "succeeded"]
    failed = next(run for run in runs if run.status == "failed")
    assert failed.failure_reason == "file_invalid"


async def test_a_genuinely_empty_source_still_clears_the_range(
    migrated: None, sessionmaker: SM, tmp_path: Path
) -> None:
    """``rows_read == 0`` is the deliberate way to clear a range: it must keep working."""
    _pages, plan_id = await _world(sessionmaker)
    request = request_for(
        source=AnalyticsSource.FIRST_PARTY,
        dataset=Dataset.LINKS,
        start="2026-05-11",
        end="2026-05-12",
    )
    await run_csv_import(
        sessionmaker,
        link_csv(
            tmp_path,
            [_link_row(plan_id, "2026-05-11", 10, 1), _link_row(plan_id, "2026-05-12", 20, 2)],
            name="good.csv",
        ),
        request=request,
    )
    outcome = await run_csv_import(
        sessionmaker, link_csv(tmp_path, [], name="empty.csv"), request=request
    )
    assert outcome.counters["rows_read"] == 0
    assert outcome.counters["rows_written"] == 0
    assert await link_metric_rows(sessionmaker) == []
    ledger = await rows_of(sessionmaker, "SELECT id, rows_written FROM analytics_imports")
    assert len(ledger) == 2
    assert [row.payload["rows_written"] for row in await analytics_events(sessionmaker)] == [2, 0]


async def test_a_partially_valid_import_still_replaces_the_range(
    migrated: None, sessionmaker: SM, tmp_path: Path
) -> None:
    _pages, plan_id = await _world(sessionmaker)
    request = request_for(
        source=AnalyticsSource.FIRST_PARTY,
        dataset=Dataset.LINKS,
        start="2026-05-11",
        end="2026-05-12",
    )
    await run_csv_import(
        sessionmaker,
        link_csv(
            tmp_path,
            [_link_row(plan_id, "2026-05-11", 10, 1), _link_row(plan_id, "2026-05-12", 20, 2)],
            name="good.csv",
        ),
        request=request,
    )
    # One good row and one unresolvable one: the range is still replaced by the good row.
    outcome = await run_csv_import(
        sessionmaker,
        link_csv(
            tmp_path,
            [
                _link_row(plan_id, "2026-05-11", 77, 7),
                _link_row(uuid.uuid4(), "2026-05-12", 1, 1),
            ],
            name="partial.csv",
        ),
        request=request,
    )
    assert outcome.counters["rows_read"] == 2
    assert outcome.counters["rows_written"] == 1
    assert outcome.counters["unresolved_link"] == 1
    assert await link_metric_rows(sessionmaker) == [
        ("first_party", datetime(2026, 5, 11).date(), plan_id, 77, 7, False),
    ]
