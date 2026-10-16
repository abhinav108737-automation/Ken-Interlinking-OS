"""First-party aggregation and the seeded demo data (ADR-0015 §5, §8 as amended).

Aggregation is a replace import like any other, so what matters here is what it COUNTS: the
live window is re-checked at aggregation time (``date_trunc('minute', deployed_at) <=
received_at < rolled_back_at``), and the ``synthetic`` flag the collector stamped is carried
through to ``link_metrics_daily`` rather than guessed.

The demo seed must be repeatable byte for byte, must never run outside local/ci, and must
never mix with real rows.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy import text

from ken_contracts.analytics import (
    AnalyticsSource,
    Dataset,
    DateRange,
    ImportVia,
    LinkMetrics,
    MetricSource,
)
from ken_services.analytics.aggregate import run_aggregation
from ken_services.analytics.importer import trailing_range
from ken_services.analytics.protocol import (
    AnalyticsImportError,
    ImportErrorCode,
    ImportOutcome,
    dedupe_key,
)
from ken_services.analytics.synthetic import DEFAULT_SEED, SyntheticPlan, generate, run_demo_seed
from tests.conftest import make_settings
from tests.integration.analytics_support import (
    POLICY,
    SETTINGS,
    link_metric_rows,
    make_live,
    new_plan,
    page_metric_rows,
    request_for,
    rows_of,
    run_mock_import,
    seed_pages,
)
from tests.integration.recommendation_support import SITE, SM

pytestmark = pytest.mark.integration

SOURCE = "/s1"
TARGET = "/t1"
DEPLOYED_AT = datetime(2026, 3, 1, 12, 0, tzinfo=UTC)
NOW = datetime(2026, 3, 6, 9, 0, tzinfo=UTC)


async def _live_plan(
    sessionmaker: SM, **kwargs: datetime | None
) -> tuple[dict[str, uuid.UUID], uuid.UUID]:
    pages = await seed_pages(sessionmaker, SOURCE, TARGET)
    plan_id = await new_plan(
        sessionmaker,
        source=pages[SOURCE],
        target=pages[TARGET],
        source_url=SITE + SOURCE,
        target_url=SITE + TARGET,
    )
    await make_live(sessionmaker, plan_id, deployed_at=DEPLOYED_AT, **kwargs)
    return pages, plan_id


async def _event(
    sessionmaker: SM,
    plan_id: uuid.UUID,
    *,
    received_at: datetime,
    kind: str = "imp",
    synthetic: bool = True,
    page_view: str | None = None,
) -> None:
    """One stored ``link_events`` row, exactly as the collector writes it."""
    minute = received_at.replace(second=0, microsecond=0)
    pv = page_view or uuid.uuid4().hex
    async with sessionmaker() as session, session.begin():
        await session.execute(
            text(
                "INSERT INTO link_events (event_type, plan_id, match_mode, synthetic, "
                "event_date, received_at, dedupe_key) VALUES (:type, :plan, 'attr', "
                ":synthetic, :day, :at, :key) ON CONFLICT (dedupe_key) DO NOTHING"
            ),
            {
                "type": ("internal_link_impression" if kind == "imp" else "internal_link_click"),
                "plan": plan_id,
                "synthetic": synthetic,
                "day": minute.astimezone(UTC).date(),
                "at": minute,
                "key": dedupe_key(pv, "imp" if kind == "imp" else "clk", plan_id),
            },
        )


async def _aggregate(
    sessionmaker: SM, *, start: str, end: str, now: datetime = NOW
) -> ImportOutcome:
    return await run_aggregation(
        sessionmaker=sessionmaker,
        settings=SETTINGS,
        policy=POLICY,
        date_range=DateRange(start=date.fromisoformat(start), end=date.fromisoformat(end)),
        triggered_by="test",
        now=now,
    )


# --------------------------------------------------------------------------- #
# Aggregation
# --------------------------------------------------------------------------- #
async def test_events_become_daily_rows_with_their_synthetic_flag(
    migrated: None,
    sessionmaker: SM,
) -> None:
    _pages, plan_id = await _live_plan(sessionmaker)
    for hour in (13, 14, 15):
        await _event(sessionmaker, plan_id, received_at=DEPLOYED_AT.replace(hour=hour))
    await _event(sessionmaker, plan_id, received_at=DEPLOYED_AT.replace(hour=16), kind="clk")
    await _event(sessionmaker, plan_id, received_at=DEPLOYED_AT + timedelta(days=1), kind="imp")
    outcome = await _aggregate(sessionmaker, start="2026-03-01", end="2026-03-02")
    assert await link_metric_rows(sessionmaker) == [
        ("first_party", date(2026, 3, 1), plan_id, 3, 1, True),
        ("first_party", date(2026, 3, 2), plan_id, 1, 0, True),
    ]
    ledger = await rows_of(
        sessionmaker, "SELECT via, synthetic, source_timezone FROM analytics_imports"
    )
    assert ledger[0].via == ImportVia.COLLECTOR.value
    assert ledger[0].synthetic is True
    assert ledger[0].source_timezone == "UTC"
    assert outcome.counters["rows_written"] == 2
    assert outcome.plans_measured == 0


async def test_an_event_outside_the_live_window_is_not_counted(
    migrated: None,
    sessionmaker: SM,
) -> None:
    _pages, plan_id = await _live_plan(sessionmaker, rolled_back_at=DEPLOYED_AT + timedelta(days=1))
    # Before the link went live, while it was live, and after it was rolled back.
    await _event(sessionmaker, plan_id, received_at=DEPLOYED_AT - timedelta(hours=2))
    await _event(sessionmaker, plan_id, received_at=DEPLOYED_AT + timedelta(hours=2))
    await _event(sessionmaker, plan_id, received_at=DEPLOYED_AT + timedelta(days=1, hours=2))
    assert len(await rows_of(sessionmaker, "SELECT id FROM link_events")) == 3
    await _aggregate(sessionmaker, start="2026-02-28", end="2026-03-03")
    assert await link_metric_rows(sessionmaker) == [
        ("first_party", date(2026, 3, 1), plan_id, 1, 0, True)
    ]


async def test_an_event_in_the_same_minute_as_the_deployment_counts(
    migrated: None,
    sessionmaker: SM,
) -> None:
    """``received_at`` is minute-truncated, so the boundary is the deployment's minute."""
    _pages, plan_id = await _live_plan(sessionmaker)
    await _event(sessionmaker, plan_id, received_at=DEPLOYED_AT + timedelta(seconds=30))
    await _aggregate(sessionmaker, start="2026-03-01", end="2026-03-01")
    assert len(await link_metric_rows(sessionmaker)) == 1


async def test_mixed_synthetic_and_real_events_are_refused(
    migrated: None,
    sessionmaker: SM,
) -> None:
    _pages, plan_id = await _live_plan(sessionmaker)
    await _event(sessionmaker, plan_id, received_at=DEPLOYED_AT.replace(hour=13))
    await _event(sessionmaker, plan_id, received_at=DEPLOYED_AT.replace(hour=14), synthetic=False)
    with pytest.raises(AnalyticsImportError) as refused:
        await _aggregate(sessionmaker, start="2026-03-01", end="2026-03-01")
    assert refused.value.code is ImportErrorCode.SYNTHETIC_REAL_MIX
    assert await link_metric_rows(sessionmaker) == []


async def test_a_duplicate_page_view_is_already_deduplicated_at_ingest(
    migrated: None, sessionmaker: SM
) -> None:
    _pages, plan_id = await _live_plan(sessionmaker)
    for _ in range(3):
        await _event(
            sessionmaker, plan_id, received_at=DEPLOYED_AT.replace(hour=13), page_view="a" * 32
        )
    assert len(await rows_of(sessionmaker, "SELECT id FROM link_events")) == 1
    await _aggregate(sessionmaker, start="2026-03-01", end="2026-03-01")
    assert (await link_metric_rows(sessionmaker))[0][3] == 1


async def test_re_aggregating_the_same_window_is_idempotent(
    migrated: None,
    sessionmaker: SM,
) -> None:
    _pages, plan_id = await _live_plan(sessionmaker)
    await _event(sessionmaker, plan_id, received_at=DEPLOYED_AT.replace(hour=13))
    await _aggregate(sessionmaker, start="2026-03-01", end="2026-03-01")
    first = await link_metric_rows(sessionmaker)
    await _aggregate(sessionmaker, start="2026-03-01", end="2026-03-01")
    assert await link_metric_rows(sessionmaker) == first


async def test_the_default_window_is_the_trailing_complete_utc_days(
    migrated: None,
    sessionmaker: SM,
) -> None:
    _pages, plan_id = await _live_plan(sessionmaker)
    await _event(sessionmaker, plan_id, received_at=DEPLOYED_AT.replace(hour=13))
    outcome = await run_aggregation(
        sessionmaker=sessionmaker,
        settings=SETTINGS,
        policy=POLICY,
        triggered_by="test",
        now=datetime(2026, 3, 4, 9, 0, tzinfo=UTC),
    )
    expected = trailing_range(
        datetime(2026, 3, 4, 9, 0, tzinfo=UTC), days=POLICY.aggregation.trailing_days
    )
    assert expected.end == date(2026, 3, 3)
    assert expected.start == date(2026, 3, 1)
    ledger = await rows_of(
        sessionmaker,
        "SELECT date_start, date_end FROM analytics_imports WHERE id = :i",
        i=outcome.import_id,
    )
    assert (ledger[0].date_start, ledger[0].date_end) == (expected.start, expected.end)


async def test_aggregating_an_empty_window_still_writes_a_ledger_row(
    migrated: None, sessionmaker: SM
) -> None:
    await _live_plan(sessionmaker)
    outcome = await _aggregate(sessionmaker, start="2026-03-01", end="2026-03-01")
    assert outcome.counters["rows_written"] == 0
    assert len(await rows_of(sessionmaker, "SELECT id FROM analytics_imports")) == 1
    assert await link_metric_rows(sessionmaker) == []


# --------------------------------------------------------------------------- #
# The seeded demo data
# --------------------------------------------------------------------------- #
def _plan_spec() -> SyntheticPlan:
    return SyntheticPlan(
        plan_id=uuid.UUID("11111111-1111-4111-8111-111111111111"),
        source_page_id=uuid.UUID("22222222-2222-4222-8222-222222222222"),
        target_page_id=uuid.UUID("33333333-3333-4333-8333-333333333333"),
        deployed_at=DEPLOYED_AT,
        rolled_back_at=None,
    )


def test_the_generator_is_byte_repeatable_for_a_seed() -> None:
    first = generate([_plan_spec()], settings=SETTINGS, policy=POLICY, seed="7", now=NOW)
    second = generate([_plan_spec()], settings=SETTINGS, policy=POLICY, seed="7", now=NOW)
    assert first == second
    other = generate([_plan_spec()], settings=SETTINGS, policy=POLICY, seed="8", now=NOW)
    assert other != first
    assert first.link_rows, "a live plan must produce link rows"
    assert first.ga4_page_rows and first.gsc_page_rows


def test_the_generator_never_depends_on_the_process_hash_seed() -> None:
    """The seed is hashed with sha512, so an explicit value is enough to reproduce a run."""
    import json
    import subprocess
    import sys

    script = (
        "import json;"
        "from datetime import UTC, datetime;"
        "from tests.integration.test_analytics_aggregate_seed import _plan_spec;"
        "from tests.integration.analytics_support import POLICY, SETTINGS;"
        "from ken_services.analytics.synthetic import generate;"
        "data = generate([_plan_spec()], settings=SETTINGS, policy=POLICY, seed='7',"
        " now=datetime(2026, 3, 6, 9, 0, tzinfo=UTC));"
        "print(json.dumps([r.model_dump(mode='json') for r in data.link_rows]))"
    )
    runs = [
        subprocess.run(  # noqa: S603 -- a fixed command, no shell, no untrusted input
            [sys.executable, "-c", script],
            capture_output=True,
            text=True,
            check=True,
            env={"PYTHONHASHSEED": seed, "PATH": "/usr/bin:/bin"},
        ).stdout
        for seed in ("0", "12345")
    ]
    assert json.loads(runs[0]) == json.loads(runs[1])
    assert json.loads(runs[0]), "the generator produced nothing"


def test_the_generator_stops_at_a_rollback_and_at_the_source_lag() -> None:
    rolled_back = DEPLOYED_AT + timedelta(days=2)
    data = generate(
        [
            SyntheticPlan(
                plan_id=_plan_spec().plan_id,
                source_page_id=_plan_spec().source_page_id,
                target_page_id=_plan_spec().target_page_id,
                deployed_at=DEPLOYED_AT,
                rolled_back_at=rolled_back,
            )
        ],
        settings=SETTINGS,
        policy=POLICY,
        seed=DEFAULT_SEED,
        now=NOW,
    )
    assert max(row.range.end for row in data.link_rows) == rolled_back.date()
    # GSC lags three days, so its newest generated day is older than GA4's.
    assert max(row.range.end for row in data.gsc_page_rows) < max(
        row.range.end for row in data.ga4_page_rows
    )


async def test_the_demo_seed_imports_synthetic_rows_for_every_live_plan(
    migrated: None, sessionmaker: SM
) -> None:
    _pages, plan_id = await _live_plan(sessionmaker)
    outcomes = await run_demo_seed(
        sessionmaker=sessionmaker, settings=SETTINGS, policy=POLICY, seed="7", now=NOW
    )
    assert len(outcomes) == 3
    links = await link_metric_rows(sessionmaker)
    assert links, "the demo seed wrote no link rows"
    assert all(row[5] is True for row in links)
    assert {row[0] for row in links} == {AnalyticsSource.FIRST_PARTY.value}
    pages = await page_metric_rows(sessionmaker)
    assert {row[0] for row in pages} == {
        AnalyticsSource.GA4.value,
        AnalyticsSource.SEARCH_CONSOLE.value,
    }
    assert all(row[8] is True for row in pages)
    ledger = await rows_of(sessionmaker, "SELECT via, synthetic FROM analytics_imports")
    assert {(row.via, row.synthetic) for row in ledger} == {(ImportVia.SYNTHETIC.value, True)}
    # Synthetic data never measures.
    assert await rows_of(sessionmaker, "SELECT plan_id FROM link_measurements") == []
    found = await rows_of(sessionmaker, "SELECT status FROM link_plans WHERE id = :p", p=plan_id)
    assert found[0].status == "deployed"


async def test_the_demo_seed_is_repeatable_against_the_database(
    migrated: None,
    sessionmaker: SM,
) -> None:
    await _live_plan(sessionmaker)
    await run_demo_seed(
        sessionmaker=sessionmaker, settings=SETTINGS, policy=POLICY, seed="7", now=NOW
    )
    first = await link_metric_rows(sessionmaker), await page_metric_rows(sessionmaker)
    await run_demo_seed(
        sessionmaker=sessionmaker, settings=SETTINGS, policy=POLICY, seed="7", now=NOW
    )
    assert (await link_metric_rows(sessionmaker), await page_metric_rows(sessionmaker)) == first


async def test_the_demo_seed_refuses_to_mix_with_real_rows(
    migrated: None,
    sessionmaker: SM,
) -> None:
    _pages, plan_id = await _live_plan(sessionmaker)
    real = await run_mock_import(
        sessionmaker,
        request=request_for(
            source=AnalyticsSource.FIRST_PARTY,
            dataset=Dataset.LINKS,
            start="2026-03-01",
            end="2026-03-01",
            via=ImportVia.COLLECTOR,
        ),
        link_rows=[
            LinkMetrics(
                link_id=str(plan_id),
                source=MetricSource.FIRST_PARTY,
                range=DateRange(start=date(2026, 3, 1), end=date(2026, 3, 1)),
                impressions=10,
                clicks=1,
            )
        ],
        now=NOW,
    )
    assert real.counters["rows_written"] == 1
    with pytest.raises(AnalyticsImportError) as refused:
        await run_demo_seed(
            sessionmaker=sessionmaker, settings=SETTINGS, policy=POLICY, seed="7", now=NOW
        )
    assert refused.value.code is ImportErrorCode.SYNTHETIC_REAL_MIX


async def test_the_demo_seed_refuses_staging_and_production(
    migrated: None,
    sessionmaker: SM,
) -> None:
    for environment in ("staging", "production"):
        settings = make_settings(
            environment=environment,
            analytics_policy_path=SETTINGS.analytics_policy_path,
            database_url="postgresql+asyncpg://ken_app:n0t-the-dev-password@db.internal:5432/ken",
        )
        with pytest.raises(AnalyticsImportError) as refused:
            await run_demo_seed(
                sessionmaker=sessionmaker, settings=settings, policy=POLICY, seed="7", now=NOW
            )
        assert refused.value.code is ImportErrorCode.ENVIRONMENT_REFUSED
    assert await rows_of(sessionmaker, "SELECT id FROM analytics_imports") == []


async def test_the_demo_seed_does_nothing_without_a_live_plan(
    migrated: None,
    sessionmaker: SM,
) -> None:
    await seed_pages(sessionmaker, SOURCE, TARGET)
    assert (
        await run_demo_seed(
            sessionmaker=sessionmaker, settings=SETTINGS, policy=POLICY, seed="7", now=NOW
        )
        == []
    )
    assert await rows_of(sessionmaker, "SELECT id FROM analytics_imports") == []


# --------------------------------------------------------------------------- #
# The stored provider (the Phase 8/9 read path over the same rows)
# --------------------------------------------------------------------------- #
async def test_the_stored_provider_reads_back_what_an_import_wrote(
    migrated: None, sessionmaker: SM
) -> None:
    """``first_party`` in the factory is a READER: Phase 8/9 consumers see daily rows through
    the same frozen batch protocol, without knowing which transport wrote them."""
    from ken_contracts.analytics import AnalyticsBatchProvider
    from ken_core.providers.analytics.stored import StoredAnalyticsProvider

    _pages, plan_id = await _live_plan(sessionmaker)
    await _event(sessionmaker, plan_id, received_at=DEPLOYED_AT.replace(hour=13))
    await _event(sessionmaker, plan_id, received_at=DEPLOYED_AT.replace(hour=14), kind="clk")
    await _aggregate(sessionmaker, start="2026-03-01", end="2026-03-01")

    provider = StoredAnalyticsProvider(sessionmaker=sessionmaker)
    assert isinstance(provider, AnalyticsBatchProvider)
    try:
        batch = await provider.fetch_batch(
            date_range=DateRange(start=date(2026, 3, 1), end=date(2026, 3, 1)),
            datasets=frozenset({Dataset.LINKS, Dataset.PAGES}),
        )
        assert [row.link_id for row in batch.link_rows] == [str(plan_id)]
        assert batch.link_rows[0].impressions == 1
        assert batch.link_rows[0].clicks == 1
        assert batch.link_rows[0].source is MetricSource.FIRST_PARTY
        assert batch.link_rows[0].range.start == batch.link_rows[0].range.end
        # first_party is not a page source, so a pages fetch is empty rather than wrong.
        assert batch.page_rows == ()
        filtered = await provider.fetch_link_metrics(
            link_ids=[str(uuid.uuid4())],
            date_range=DateRange(start=date(2026, 3, 1), end=date(2026, 3, 1)),
        )
        assert filtered == []
    finally:
        await provider.aclose()
