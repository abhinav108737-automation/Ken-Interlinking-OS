"""The golden attribution scenario (ADR-0015 §9, fixture ``evals/analytics/attribution``).

One scripted history covers every case Phase 7 has to get right: applied, rolled back
mid-window, per-plan REMOVE, superseded, never deployed, two plans overlapping on one source,
a content change inside the windows, and a plan whose verification stayed inconclusive.

Everything is built with the REAL services -- the transition service writes the history, the
real importer writes the metrics, and the measure sweep runs inside the import transaction --
and then the attribution, the live windows, the confounder flags and the ``measured``
transitions are asserted EXACTLY against the fixture.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from ken_contracts.analytics import (
    AnalyticsSource,
    ConfounderFlag,
    Dataset,
    DateRange,
    ImportVia,
    LinkMeasurement,
    LinkMetrics,
    MetricSource,
    PageMetrics,
)
from ken_contracts.deployment import DeploymentStatus
from ken_contracts.recommendations import LinkPlanStatus
from ken_core.db.repositories import analytics as repo
from ken_services.analytics.attribution import link_attribution, live_windows
from ken_services.analytics.protocol import ImportOutcome
from tests.integration.analytics_support import (
    POLICY,
    a_user,
    add_page_version,
    analytics_events,
    approve_record,
    make_live,
    move,
    new_plan,
    plan_status,
    record_agent_review,
    record_deployment,
    request_for,
    rows_of,
    run_mock_import,
    seed_pages,
    supersede,
)
from tests.integration.recommendation_support import SITE, SM

pytestmark = pytest.mark.integration

FIXTURE = (
    Path(__file__).resolve().parents[2]
    / "evals"
    / "analytics"
    / "attribution"
    / "golden_scenario.json"
)
SCENARIO: dict[str, Any] = json.loads(FIXTURE.read_text(encoding="utf-8"))
TIMELINE = SCENARIO["timeline"]
PLANS = SCENARIO["plans"]
PATHS = ("/s1", "/s2", "/t1", "/t2", "/t3", "/t4", "/t5", "/t6")
ANCHOR = "electric vehicles"


def _moment(key: str) -> datetime:
    return datetime.fromisoformat(TIMELINE[key])


def _day(key: str) -> date:
    return date.fromisoformat(TIMELINE[key])


async def _world(sessionmaker: SM) -> tuple[dict[str, uuid.UUID], dict[str, uuid.UUID]]:
    """Build the fixture's history. Returns ``(page ids, plan ids by case name)``."""
    pages = await seed_pages(sessionmaker, *PATHS)
    user_id = await a_user(sessionmaker)
    plans: dict[str, uuid.UUID] = {}
    for name, spec in PLANS.items():
        plans[name] = await new_plan(
            sessionmaker,
            source=pages[spec["source"]],
            target=pages[spec["target"]],
            source_url=SITE + spec["source"],
            target_url=SITE + spec["target"],
            anchor=ANCHOR,
            # A distinct context per plan: open plans of one source may not share an anchor
            # span (uq_link_plans_open_span), and every case starts out open.
            block_text=f"Case {name}: batteries power electric vehicles.",
        )

    # applied: the full chain, still live.
    await record_agent_review(
        sessionmaker,
        plans["applied"],
        source_page_id=pages["/s1"],
        source_url=SITE + "/s1",
        at=_moment("plan_created") + timedelta(days=1),
    )
    await approve_record(
        sessionmaker,
        plans["applied"],
        user_id=user_id,
        at=_moment("deployed_at") - timedelta(days=2),
    )
    await make_live(
        sessionmaker,
        plans["applied"],
        deployed_at=_moment("deployed_at"),
        verified_at=_moment("verified_at"),
    )
    await record_deployment(
        sessionmaker,
        source_page_id=pages["/s1"],
        plan_ids=[plans["applied"]],
        user_id=user_id,
        applied_at=_moment("deployed_at"),
        verified_at=_moment("verified_at"),
    )

    # rolled_back_mid_window: a whole-deployment rollback.
    await record_agent_review(
        sessionmaker,
        plans["rolled_back_mid_window"],
        source_page_id=pages["/s1"],
        source_url=SITE + "/s1",
        at=_moment("plan_created") + timedelta(days=1),
    )
    await approve_record(
        sessionmaker,
        plans["rolled_back_mid_window"],
        user_id=user_id,
        at=_moment("deployed_at") - timedelta(days=2),
    )
    await make_live(
        sessionmaker,
        plans["rolled_back_mid_window"],
        deployed_at=_moment("deployed_at"),
        verified_at=_moment("verified_at"),
        rolled_back_at=_moment("rolled_back_at"),
    )
    await record_deployment(
        sessionmaker,
        source_page_id=pages["/s1"],
        plan_ids=[plans["rolled_back_mid_window"]],
        user_id=user_id,
        applied_at=_moment("deployed_at"),
        verified_at=_moment("verified_at"),
    )

    # per_plan_remove: the deployment row stays 'verified'; only THIS plan came off the page.
    await make_live(
        sessionmaker,
        plans["per_plan_remove"],
        deployed_at=_moment("deployed_at"),
        verified_at=_moment("verified_at"),
        rolled_back_at=_moment("rolled_back_at"),
    )
    await record_deployment(
        sessionmaker,
        source_page_id=pages["/s2"],
        plan_ids=[plans["per_plan_remove"]],
        user_id=user_id,
        applied_at=_moment("deployed_at"),
        verified_at=_moment("verified_at"),
    )

    # superseded / never_deployed: never on a page.
    await supersede(sessionmaker, plans["superseded"], at=_moment("deployed_at"))
    await move(
        sessionmaker,
        plans["never_deployed"],
        to=LinkPlanStatus.AWAITING_APPROVAL,
        at=_moment("deployed_at"),
    )

    # overlapping_on_same_source: a second live plan on /s1, inside the first's windows.
    await make_live(
        sessionmaker,
        plans["overlapping_on_same_source"],
        deployed_at=_moment("overlapping_deployed_at"),
        verified_at=_moment("overlapping_deployed_at") + timedelta(days=1),
    )
    await record_deployment(
        sessionmaker,
        source_page_id=pages["/s1"],
        plan_ids=[plans["overlapping_on_same_source"]],
        user_id=user_id,
        applied_at=_moment("overlapping_deployed_at"),
        verified_at=_moment("overlapping_deployed_at") + timedelta(days=1),
    )

    # cms_noop_already_applied: the CMS had the link already, so the ONLY item stage is
    # 'already_applied' -- and the plan still became 'deployed', so it is attributed and
    # measurable exactly like any other (amendment 1a).
    await make_live(
        sessionmaker,
        plans["cms_noop_already_applied"],
        deployed_at=_moment("deployed_at"),
        verified_at=_moment("verified_at"),
    )
    await record_deployment(
        sessionmaker,
        source_page_id=pages["/s1"],
        plan_ids=[plans["cms_noop_already_applied"]],
        user_id=user_id,
        applied_at=_moment("deployed_at"),
        verified_at=_moment("verified_at"),
        stage="already_applied",
    )

    # deployed_then_failed: live for the collector's index (the link may be on the page),
    # never verified, so it can never measure (amendment 1e).
    await make_live(sessionmaker, plans["deployed_then_failed"], deployed_at=_moment("deployed_at"))
    await move(
        sessionmaker,
        plans["deployed_then_failed"],
        to=LinkPlanStatus.FAILED,
        at=_moment("deployed_at") + timedelta(hours=1),
    )
    await record_deployment(
        sessionmaker,
        source_page_id=pages["/s2"],
        plan_ids=[plans["deployed_then_failed"]],
        user_id=user_id,
        applied_at=_moment("deployed_at"),
        status=DeploymentStatus.VERIFICATION_FAILED,
    )

    # inconclusive_verification: applied, never verified (a CMS no-op records already_applied).
    await make_live(
        sessionmaker, plans["inconclusive_verification"], deployed_at=_moment("deployed_at")
    )
    await record_deployment(
        sessionmaker,
        source_page_id=pages["/s2"],
        plan_ids=[plans["inconclusive_verification"]],
        user_id=user_id,
        applied_at=_moment("deployed_at"),
        stage="already_applied",
    )

    # The target of 'applied' changed inside the windows.
    await add_page_version(
        sessionmaker, "/t1", body="a rewritten body", fetched_at=_moment("content_changed_at")
    )
    return pages, plans


def _days(first: date, last: date) -> list[date]:
    return [first + timedelta(days=offset) for offset in range((last - first).days + 1)]


async def _import_page_metrics(sessionmaker: SM, pages: dict[str, uuid.UUID]) -> None:
    """Flat, non-synthetic GA4 and Search Console series over every page."""
    for source, timezone, via in (
        (AnalyticsSource.GA4, "Asia/Kolkata", ImportVia.API),
        (AnalyticsSource.SEARCH_CONSOLE, "America/Los_Angeles", ImportVia.API),
    ):
        rows = [
            PageMetrics(
                page_id=str(page_id),
                source=MetricSource(source.value),
                range=DateRange(start=day, end=day),
                impressions=100,
                clicks=5,
                sessions=20,
                pageviews=40,
                position=4.0,
            )
            for page_id in sorted(set(pages.values()), key=str)
            for day in _days(_day("page_data_first"), _day("page_data_last"))
        ]
        await run_mock_import(
            sessionmaker,
            request=request_for(
                source=source,
                dataset=Dataset.PAGES,
                start=TIMELINE["page_data_first"],
                end=TIMELINE["page_data_last"],
                via=via,
                source_timezone=timezone,
            ),
            page_rows=rows,
            now=_moment("now"),
        )


async def _import_link_metrics(
    sessionmaker: SM, plans: dict[str, uuid.UUID], *, synthetic: bool
) -> ImportOutcome:
    """First-party daily rows for every plan, long enough to close a measurement window."""
    last = _day("page_data_last") + timedelta(days=4)
    rows = [
        LinkMetrics(
            link_id=str(plan_id),
            source=MetricSource.FIRST_PARTY,
            range=DateRange(start=day, end=day),
            impressions=50,
            clicks=3,
        )
        for plan_id in sorted(plans.values(), key=str)
        for day in _days(_day("page_data_first"), last)
    ]
    return await run_mock_import(
        sessionmaker,
        request=request_for(
            source=AnalyticsSource.FIRST_PARTY,
            dataset=Dataset.LINKS,
            start=TIMELINE["page_data_first"],
            end=last.isoformat(),
            via=ImportVia.SYNTHETIC if synthetic else ImportVia.COLLECTOR,
            synthetic=synthetic,
        ),
        link_rows=rows,
        now=_moment("now"),
    )


# --------------------------------------------------------------------------- #
# The golden assertions
# --------------------------------------------------------------------------- #
async def test_the_golden_attribution_chain_and_live_windows(
    migrated: None,
    sessionmaker: SM,
) -> None:
    pages, plans = await _world(sessionmaker)
    async with sessionmaker() as session:
        windows = await live_windows(session, list(plans.values()))
        for name, spec in PLANS.items():
            plan_id = plans[name]
            attribution = await link_attribution(session, plan_id=plan_id)
            window = windows[plan_id]
            where = f"{name}: {spec['case']}"
            assert attribution.source_page_id == pages[spec["source"]], where
            assert attribution.target_page_id == pages[spec["target"]], where
            assert attribution.source_url == SITE + spec["source"], where
            assert attribution.source_page_version == 1, where
            assert attribution.engine_version, where
            assert attribution.analysis_version, where
            assert attribution.policy_version >= 1, where
            assert attribution.ranking_policy_version >= 1, where
            assert attribution.anchor_source == "target_topic", where

            expected_deployed = spec["deployed_at"]
            actual_deployed = None if window.deployed_at is None else window.deployed_at.isoformat()
            assert actual_deployed == expected_deployed, where
            assert attribution.deployed_at == window.deployed_at, where
            expected_rolled_back = spec["rolled_back_at"]
            actual_rolled_back = (
                None if window.rolled_back_at is None else window.rolled_back_at.isoformat()
            )
            assert actual_rolled_back == expected_rolled_back, where
            assert attribution.rolled_back_at == window.rolled_back_at, where
            assert window.ever_live is spec["ever_live"], where
            actual_verified = (
                None if attribution.verified_at is None else attribution.verified_at.isoformat()
            )
            assert actual_verified == spec["verified_at"], where

            assert (attribution.agent_run_id is not None) is spec["has_agent"], where
            assert (
                None if attribution.agent_verdict is None else attribution.agent_verdict.value
            ) == spec["agent_verdict"], where
            assert attribution.agent_graph_version == spec["agent_graph_version"], where
            assert (attribution.approved_by is not None) is spec["approved"], where
            assert (attribution.deployment_id is not None) is spec["has_deployment"], where
            assert attribution.cms_provider == spec["cms_provider"], where


async def test_the_scripted_statuses_match_the_fixture_before_any_measurement(
    migrated: None, sessionmaker: SM
) -> None:
    _pages, plans = await _world(sessionmaker)
    expected = {
        name: (
            LinkPlanStatus.VERIFIED.value
            if spec["plan_status"] == LinkPlanStatus.MEASURED.value
            else spec["plan_status"]
        )
        for name, spec in PLANS.items()
    }
    found = await rows_of(sessionmaker, "SELECT id, status FROM link_plans")
    by_id = {row.id: row.status for row in found}
    assert {name: by_id[plan_id] for name, plan_id in plans.items()} == expected


async def test_the_measure_sweep_measures_exactly_the_due_plans(
    migrated: None,
    sessionmaker: SM,
) -> None:
    pages, plans = await _world(sessionmaker)
    await _import_page_metrics(sessionmaker, pages)
    outcome = await _import_link_metrics(sessionmaker, plans, synthetic=False)

    measured = {name for name, spec in PLANS.items() if spec["measures"]}
    found = await rows_of(sessionmaker, "SELECT id, status FROM link_plans")
    by_id = {row.id: row.status for row in found}
    assert {name for name, plan_id in plans.items() if by_id[plan_id] == "measured"} == measured
    assert outcome.counters["plans_measured"] == len(measured)
    assert (await analytics_events(sessionmaker))[-1].payload["plans_measured"] == len(measured)

    # Only system:analytics moves a plan into 'measured', and only from 'verified'.
    transitions = await rows_of(
        sessionmaker,
        "SELECT plan_id, from_status, actor FROM link_plan_transitions "
        "WHERE to_status = 'measured'",
    )
    assert len(transitions) == len(measured)
    assert {row.from_status for row in transitions} == {"verified"}
    assert {row.actor for row in transitions} == {"system:analytics"}

    for name in measured:
        spec = PLANS[name]["measurement"]
        async with sessionmaker() as session:
            stored = await repo.measurement_of(session, plan_id=plans[name])
        assert stored is not None, name
        assert stored.window_start.isoformat() == spec["window_start"], name
        assert stored.window_end.isoformat() == spec["window_end"], name
        assert sorted(stored.link_totals) == spec["link_sources"], name
        assert stored.import_ids, name
        flags = set(stored.flags)
        assert set(spec["required_flags"]) <= flags, (name, sorted(flags))
        assert not set(spec["forbidden_flags"]) & flags, (name, sorted(flags))
        # The snapshot validates against the frozen contract, comparisons included.
        measurement = LinkMeasurement.model_validate(
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
        assert measurement.analytics_policy_version == POLICY.version
        roles = {comparison.role for comparison in measurement.source_comparison} | {
            comparison.role for comparison in measurement.target_comparison
        }
        assert roles == {"source", "target"}
        assert {comparison.source for comparison in measurement.target_comparison} == {
            AnalyticsSource.GA4,
            AnalyticsSource.SEARCH_CONSOLE,
        }
        assert ConfounderFlag.SYNTHETIC_DATA not in measurement.flags


async def test_a_measurement_is_written_once_and_cannot_be_rewritten(
    migrated: None, sessionmaker: SM
) -> None:
    pages, plans = await _world(sessionmaker)
    await _import_page_metrics(sessionmaker, pages)
    await _import_link_metrics(sessionmaker, plans, synthetic=False)
    before = await rows_of(sessionmaker, "SELECT plan_id, measured_at FROM link_measurements")
    # A second identical import must not measure again (the plans are no longer 'verified').
    outcome = await _import_link_metrics(sessionmaker, plans, synthetic=False)
    assert outcome.counters["plans_measured"] == 0
    after = await rows_of(sessionmaker, "SELECT plan_id, measured_at FROM link_measurements")
    assert {(row.plan_id, row.measured_at) for row in after} == {
        (row.plan_id, row.measured_at) for row in before
    }
    with pytest.raises(Exception, match=r"append-only|not allowed|immutable"):
        await rows_of(sessionmaker, "UPDATE link_measurements SET window_end = window_start")


async def test_synthetic_data_never_measures(migrated: None, sessionmaker: SM) -> None:
    pages, plans = await _world(sessionmaker)
    await _import_page_metrics(sessionmaker, pages)
    # Replace the real page rows with synthetic ones first would be a mix; this scenario is
    # a synthetic-only link import on top of (equally synthetic) absence of real link data.
    await rows_of(sessionmaker, "SELECT 1")
    outcome = await _import_link_metrics(sessionmaker, plans, synthetic=True)
    assert outcome.counters["plans_measured"] == 0
    assert await rows_of(sessionmaker, "SELECT plan_id FROM link_measurements") == []
    found = await rows_of(sessionmaker, "SELECT id, status FROM link_plans")
    by_id = {row.id: row.status for row in found}
    assert "measured" not in set(by_id.values())
    assert by_id[plans["applied"]] == "verified"
    event = (await analytics_events(sessionmaker))[-1].payload
    assert event["synthetic"] is True
    assert event["plans_measured"] == 0


async def test_a_source_locked_by_a_deployment_is_skipped_not_waited_on(
    migrated: None, sessionmaker: SM
) -> None:
    """The sweep takes ``pg_try_advisory_xact_lock(deploy_lock_key(source))`` (§12.4).

    The lock is held on a DEDICATED connection that is never returned to the pool, exactly
    as ``run_job`` holds its kind lock: advisory locks are per connection, so a pooled
    connection handed back to the importer would make the try-lock succeed and prove nothing.
    """
    from sqlalchemy import text

    from ken_services.deployment.service import deploy_lock_key

    pages, plans = await _world(sessionmaker)
    await _import_page_metrics(sessionmaker, pages)
    key = deploy_lock_key(pages["/s1"])
    engine = sessionmaker.kw["bind"]
    holder = await engine.connect()
    try:
        assert await holder.scalar(text("SELECT pg_try_advisory_lock(:key)"), {"key": key})
        await holder.commit()
        outcome = await _import_link_metrics(sessionmaker, plans, synthetic=False)
    finally:
        await holder.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": key})
        await holder.commit()
        await holder.close()
    assert outcome.counters["plans_skipped_busy"] == 1
    assert outcome.counters["plans_measured"] == 0
    found = await rows_of(sessionmaker, "SELECT id, status FROM link_plans")
    by_id = {row.id: row.status for row in found}
    assert by_id[plans["applied"]] == "verified"
    assert await rows_of(sessionmaker, "SELECT plan_id FROM link_measurements") == []


async def test_a_source_rolling_back_is_skipped(migrated: None, sessionmaker: SM) -> None:
    pages, plans = await _world(sessionmaker)
    user_id = await a_user(sessionmaker, "ollie")
    await record_deployment(
        sessionmaker,
        source_page_id=pages["/s1"],
        plan_ids=[],
        user_id=user_id,
        applied_at=_moment("deployed_at"),
        verified_at=None,
        status_rolling_back=True,
    )
    await _import_page_metrics(sessionmaker, pages)
    outcome = await _import_link_metrics(sessionmaker, plans, synthetic=False)
    assert outcome.counters["plans_skipped_busy"] == 1
    found = await rows_of(sessionmaker, "SELECT id, status FROM link_plans")
    by_id = {row.id: row.status for row in found}
    assert by_id[plans["applied"]] == "verified"
    assert by_id[plans["overlapping_on_same_source"]] == "verified"


async def test_the_fixture_covers_every_required_case() -> None:
    required = {
        "applied",
        "rolled_back_mid_window",
        "per_plan_remove",
        "superseded",
        "never_deployed",
        "overlapping_on_same_source",
        "inconclusive_verification",
        "cms_noop_already_applied",
        "deployed_then_failed",
    }
    assert required <= set(PLANS)
    assert datetime.fromisoformat(TIMELINE["now"]).tzinfo is UTC


# --------------------------------------------------------------------------- #
# The two review amendments, pinned (Phase 7 QA finding 5)
# --------------------------------------------------------------------------- #
async def test_a_cms_no_op_is_attributed_and_measurable(migrated: None, sessionmaker: SM) -> None:
    """Amendment 1a: the live window comes from ``link_plan_transitions`` ONLY, so a plan
    whose only ``deployment_items`` stage is ``already_applied`` measures like any other."""
    pages, plans = await _world(sessionmaker)
    plan_id = plans["cms_noop_already_applied"]
    stages = await rows_of(
        sessionmaker, "SELECT stage FROM deployment_items WHERE plan_id = :p", p=plan_id
    )
    assert [row.stage for row in stages] == PLANS["cms_noop_already_applied"]["item_stages"]

    async with sessionmaker() as session:
        attribution = await link_attribution(session, plan_id=plan_id)
        window = (await live_windows(session, [plan_id]))[plan_id]
    assert attribution.deployment_id is not None
    assert attribution.cms_provider == "mock"
    assert window.deployed_at == _moment("deployed_at")
    assert window.ever_live is True

    await _import_page_metrics(sessionmaker, pages)
    await _import_link_metrics(sessionmaker, plans, synthetic=False)
    assert await plan_status(sessionmaker, plan_id) is LinkPlanStatus.MEASURED
    async with sessionmaker() as session:
        stored = await repo.measurement_of(session, plan_id=plan_id)
    assert stored is not None
    assert (
        stored.window_start.isoformat()
        == (PLANS["cms_noop_already_applied"]["measurement"]["window_start"])
    )


async def test_a_failed_plan_is_live_for_the_collector_but_never_measures(
    migrated: None, sessionmaker: SM
) -> None:
    """Amendment 1e: ``LINK_ON_PAGE`` includes ``failed`` on purpose (the link may still be
    on the page), while measuring is only ever from ``verified``."""
    from ken_services.analytics.collector.index import SqlLiveLinkIndex
    from ken_services.analytics.protocol import normalized_path

    pages, plans = await _world(sessionmaker)
    plan_id = plans["deployed_then_failed"]
    assert await plan_status(sessionmaker, plan_id) is LinkPlanStatus.FAILED

    index = SqlLiveLinkIndex(sessionmaker)
    await index.refresh()
    path = normalized_path(SITE + PLANS["deployed_then_failed"]["source"])
    assert path is not None
    resolution = index.resolve_attr(plan_id=plan_id, path=path)
    assert resolution.plan_id == plan_id, "a failed plan's events are still attributable"
    assert resolution.rejection is None
    # Its window is open: only a 'rolled_back' transition closes one.
    async with sessionmaker() as session:
        window = (await live_windows(session, [plan_id]))[plan_id]
    assert window.deployed_at == _moment("deployed_at")
    assert window.rolled_back_at is None

    # Measuring, though, is only from 'verified': link data covering the whole window
    # changes nothing for it, now or after a second sweep.
    await _import_page_metrics(sessionmaker, pages)
    for _ in range(2):
        await _import_link_metrics(sessionmaker, plans, synthetic=False)
        assert await plan_status(sessionmaker, plan_id) is LinkPlanStatus.FAILED
        async with sessionmaker() as session:
            assert await repo.measurement_of(session, plan_id=plan_id) is None


# --------------------------------------------------------------------------- #
# The rolling_back check is read under the source lock (Phase 7 QA finding 4)
# --------------------------------------------------------------------------- #
async def test_a_rollback_that_commits_during_the_sweep_is_still_skipped(
    migrated: None, sessionmaker: SM, monkeypatch: pytest.MonkeyPatch
) -> None:
    """§12.4: the ``rolling_back`` check must be read INSIDE the per-source loop, after the
    try-lock succeeded. Read once before the loop it is time-of-check-to-time-of-use: a
    rollback that commits its intent while the sweep is running would be missed and one of
    its plans measured anyway.

    The rollback is committed from another connection in the window between the source lock
    and the check, which is exactly the race the old code lost.
    """
    from ken_services.analytics import measure as measure_module

    pages, plans = await _world(sessionmaker)
    user_id = await a_user(sessionmaker, "ollie")
    await _import_page_metrics(sessionmaker, pages)

    real_lock = measure_module._try_source_lock
    rolled: list[uuid.UUID] = []

    async def lock_then_start_a_rollback(session: Any, source_page_id: uuid.UUID) -> bool:
        acquired = await real_lock(session, source_page_id)
        if acquired and source_page_id == pages["/s1"] and not rolled:
            rolled.append(source_page_id)
            await record_deployment(
                sessionmaker,
                source_page_id=pages["/s1"],
                plan_ids=[],
                user_id=user_id,
                applied_at=_moment("deployed_at"),
                status_rolling_back=True,
            )
        return acquired

    monkeypatch.setattr(measure_module, "_try_source_lock", lock_then_start_a_rollback)
    outcome = await _import_link_metrics(sessionmaker, plans, synthetic=False)

    assert rolled == [pages["/s1"]], "the sweep must have reached /s1 and taken its lock"
    assert outcome.counters["plans_skipped_busy"] == 1
    assert outcome.counters["plans_measured"] == 0
    for name in ("applied", "overlapping_on_same_source", "cms_noop_already_applied"):
        assert await plan_status(sessionmaker, plans[name]) is LinkPlanStatus.VERIFIED, name
    assert await rows_of(sessionmaker, "SELECT plan_id FROM link_measurements") == []
