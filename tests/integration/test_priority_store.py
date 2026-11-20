"""Priority designations through the database, and a shadow of a REAL engine run (2A-1).

The end-to-end tests run the real pipeline (import -> analysis -> recommendation) on the
integration corpus, then shadow that run. The load-bearing one designates EVERY page in the
corpus at the strongest tier and asserts that the report still contains only QUALIFIED
candidates: no pair refused by a gate, an eligibility rule, a floor or the density guard
appears, however strongly its target is designated. A second asserts the live tables are
byte-identical before and after a shadow run -- "shadow-only" checked, not promised.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.exc import IntegrityError

from ken_contracts.intelligence import LinkEligibility, analysis_version
from ken_contracts.inventory import RunStatus
from ken_core.priority import MIN_TIER, PriorityType
from ken_services.intelligence.taxonomy import load_taxonomy
from ken_services.recommendations.pipeline import load_policies
from ken_services.recommendations.priority import designated_value
from ken_services.recommendations.priority_shadow import CAPACITY_REJECTIONS
from ken_services.recommendations.priority_store import (
    DesignationNotFoundError,
    RunNotFoundError,
    add_designation,
    build_shadow_report,
    latest_recommendation_run,
    list_designations,
    load_designations,
    retire_designation,
    violated_constraint,
)
from tests.integration.recommendation_support import (
    SETTINGS,
    SM,
    page_ids,
    recommend,
    rows,
    seed_corpus,
)

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("migrated")]

POLICY, _RANKING = load_policies(SETTINGS)

NOW = datetime(2026, 11, 19, 12, 0, tzinfo=UTC)
ACTOR = "abhinav.kumar"
LIVE_TABLES = ("link_plans", "link_candidates", "link_plan_transitions", "page_modules",
               "page_module_items", "opportunities")  # fmt: skip


async def _add(sm: SM, page_id: uuid.UUID, **overrides: Any) -> uuid.UUID:
    values: dict[str, Any] = {
        "page_id": page_id,
        "priority_type": PriorityType.HIGH_PRIORITY_REPORT,
        "tier": 2,
        "reason": "owner priority for the pilot cluster",
        "owner": ACTOR,
        "effective_from": NOW - timedelta(days=1),
        "effective_to": None,
        "actor": ACTOR,
        "now": NOW,
    } | overrides
    async with sm() as session:
        designation = await add_designation(session, **values)
        await session.commit()
    return designation.id


async def _any_page(sm: SM, tmp_path: Path) -> uuid.UUID:
    await seed_corpus(sm, tmp_path)
    return next(iter((await page_ids(sm)).values()))


# --------------------------------------------------------------------------- #
# Management
# --------------------------------------------------------------------------- #
async def test_add_list_and_retire_round_trip(sessionmaker: SM, tmp_path: Path) -> None:
    page = await _any_page(sessionmaker, tmp_path)
    designation_id = await _add(sessionmaker, page)

    async with sessionmaker() as session:
        active = await list_designations(session, page_id=page)
    assert [d.id for d in active] == [designation_id]
    assert active[0].created_by == ACTOR

    later = NOW + timedelta(hours=1)
    async with sessionmaker() as session:
        retired = await retire_designation(
            session, designation_id=designation_id, actor="reviewer.two", now=later
        )
        await session.commit()
    assert retired.retired_at == later, "retirement records the instant, not a flag"
    assert not retired.is_effective_at(later)
    assert retired.is_effective_at(NOW), "it was in force before that instant"
    assert retired.updated_by == "reviewer.two"
    assert retired.created_by == ACTOR, "retiring must not rewrite who created it"

    async with sessionmaker() as session:
        assert await list_designations(session, page_id=page) == []
        history = await list_designations(session, page_id=page, include_retired=True)
    assert [d.id for d in history] == [designation_id]


async def test_retiring_is_idempotent(sessionmaker: SM, tmp_path: Path) -> None:
    page = await _any_page(sessionmaker, tmp_path)
    designation_id = await _add(sessionmaker, page)
    for actor in ("first.retirer", "second.retirer"):
        async with sessionmaker() as session:
            await retire_designation(session, designation_id=designation_id, actor=actor, now=NOW)
            await session.commit()
    async with sessionmaker() as session:
        (only,) = await list_designations(session, page_id=page, include_retired=True)
    assert only.updated_by == "first.retirer", "a no-op retire must not overwrite the audit"
    assert only.retired_at == NOW, "nor move the instant it stopped applying"


async def test_retiring_an_unknown_designation_is_refused(sessionmaker: SM) -> None:
    async with sessionmaker() as session:
        with pytest.raises(DesignationNotFoundError):
            await retire_designation(session, designation_id=uuid.uuid4(), actor=ACTOR, now=NOW)


async def test_a_change_without_an_actor_is_refused(sessionmaker: SM, tmp_path: Path) -> None:
    page = await _any_page(sessionmaker, tmp_path)
    with pytest.raises(ValueError, match="actor"):
        await _add(sessionmaker, page, actor="  ")


async def test_load_designations_returns_retired_and_future_rows_for_the_resolver(
    sessionmaker: SM, tmp_path: Path
) -> None:
    """The store does not pre-filter: a report for a past instant must stay possible."""
    page = await _any_page(sessionmaker, tmp_path)
    retired = await _add(sessionmaker, page, priority_type=PriorityType.TRENDING_MARKET)
    async with sessionmaker() as session:
        await retire_designation(session, designation_id=retired, actor=ACTOR, now=NOW)
        await session.commit()
    future = await _add(sessionmaker, page, effective_from=NOW + timedelta(days=30))

    async with sessionmaker() as session:
        loaded = await load_designations(session, page_ids=[page])
    assert {d.id for d in loaded[page]} == {retired, future}


# --------------------------------------------------------------------------- #
# Shadow of a real engine run
# --------------------------------------------------------------------------- #
async def _real_run(sm: SM, tmp_path: Path) -> uuid.UUID:
    await seed_corpus(sm, tmp_path)
    detail = await recommend(sm)
    assert detail.status is RunStatus.SUCCEEDED, detail.counters
    return detail.id


async def _shadow(sm: SM, run_id: uuid.UUID, at: datetime = NOW) -> Any:
    taxonomy = load_taxonomy(SETTINGS.taxonomy_path)
    async with sm() as session:
        return await build_shadow_report(
            session,
            run_id=run_id,
            taxonomy=taxonomy,
            fallback_analysis_version=analysis_version(taxonomy.version),
            at=at,
        )


async def test_designating_every_page_still_admits_only_qualified_candidates(
    sessionmaker: SM, tmp_path: Path
) -> None:
    """THE LOAD-BEARING INTEGRATION TEST: maximal priority everywhere admits nothing new."""
    run_id = await _real_run(sessionmaker, tmp_path)
    candidates = await rows(
        sessionmaker,
        "SELECT source_page_id, target_page_id, rejection FROM link_candidates WHERE run_id = :r",
        r=run_id,
    )
    qualified = {
        (c.source_page_id, c.target_page_id)
        for c in candidates
        if c.rejection is None or c.rejection in {r.value for r in CAPACITY_REJECTIONS}
    }
    refused = {
        (c.source_page_id, c.target_page_id): c.rejection
        for c in candidates
        if (c.source_page_id, c.target_page_id) not in qualified
    }
    # Positive controls: the corpus must exercise BOTH sides, or this test proves nothing.
    assert qualified, "the real run produced no qualified candidate"
    assert refused, "the real run refused nothing -- no gate was exercised"

    for page in (await page_ids(sessionmaker)).values():
        await _add(sessionmaker, page, tier=MIN_TIER)

    _, report = await _shadow(sessionmaker, run_id)
    reported = {(r.source_page_id, r.target_page_id) for r in report.rows}

    assert reported == qualified, "the report must be exactly the qualified candidates"
    leaked = {pair: refused[pair] for pair in reported if pair in refused}
    assert not leaked, f"refused pairs reached the shadow: {leaked}"


async def test_a_shadow_run_changes_no_live_table(sessionmaker: SM, tmp_path: Path) -> None:
    run_id = await _real_run(sessionmaker, tmp_path)
    for page in (await page_ids(sessionmaker)).values():
        await _add(sessionmaker, page, tier=MIN_TIER)

    async def snapshot() -> dict[str, list[Any]]:
        out: dict[str, list[Any]] = {}
        for table in LIVE_TABLES:
            out[table] = sorted(
                str(row)
                for row in await rows(sessionmaker, f"SELECT * FROM {table}")  # noqa: S608
            )
        return out

    before = await snapshot()
    await _shadow(sessionmaker, run_id)
    after = await snapshot()

    assert before == after, "a shadow report must not change any live table"
    assert any(before.values()), "the snapshot must contain rows, or it proves nothing"


async def test_the_shadow_of_a_real_run_is_deterministic(sessionmaker: SM, tmp_path: Path) -> None:
    run_id = await _real_run(sessionmaker, tmp_path)
    for page in list((await page_ids(sessionmaker)).values())[::2]:
        await _add(sessionmaker, page, tier=2)

    _, first = await _shadow(sessionmaker, run_id)
    _, second = await _shadow(sessionmaker, run_id)

    assert first.rows == second.rows
    assert first.rows, "an empty report is trivially deterministic"


async def test_the_shadow_records_which_run_and_versions_it_shadowed(
    sessionmaker: SM, tmp_path: Path
) -> None:
    run_id = await _real_run(sessionmaker, tmp_path)
    context, _ = await _shadow(sessionmaker, run_id)
    snapshot = (
        await rows(sessionmaker, "SELECT config_snapshot FROM job_runs WHERE id = :r", r=run_id)
    )[0].config_snapshot

    assert context.run_id == run_id
    assert context.analysis_version == snapshot["analysis_version"]
    assert context.policy_version == snapshot["policy"]["version"]
    assert context.ranking_policy_version == snapshot["ranking_policy"]["version"]


async def test_latest_run_is_found_and_a_non_recommendation_run_is_refused(
    sessionmaker: SM, tmp_path: Path
) -> None:
    run_id = await _real_run(sessionmaker, tmp_path)
    async with sessionmaker() as session:
        assert await latest_recommendation_run(session) == run_id

    analysis_run = (
        await rows(sessionmaker, "SELECT id FROM job_runs WHERE kind = 'analysis' LIMIT 1")
    )[0].id
    with pytest.raises(RunNotFoundError):
        await _shadow(sessionmaker, analysis_run)


async def test_an_expired_designation_moves_nothing_in_a_real_shadow(
    sessionmaker: SM, tmp_path: Path
) -> None:
    run_id = await _real_run(sessionmaker, tmp_path)
    for page in (await page_ids(sessionmaker)).values():
        await _add(
            sessionmaker,
            page,
            tier=MIN_TIER,
            effective_from=NOW - timedelta(days=10),
            effective_to=NOW - timedelta(days=1),
        )

    _, report = await _shadow(sessionmaker, run_id)

    assert report.rows
    assert report.designated == ()
    assert report.moved == ()
    assert report.selection_changes == ()


async def test_priority_would_change_the_plan_set_of_a_real_capacity_limited_run(
    sessionmaker: SM, tmp_path: Path
) -> None:
    """The decision-relevant case on REAL engine output, not only on unit fixtures.

    With ``max_plans_per_source=1`` the real engine cuts qualified candidates by capacity.
    Designating only those cut candidates' targets must make the shadow swap at least one into
    the plan set -- while the number of plans per source, and every live row, stay unchanged.
    """
    await seed_corpus(sessionmaker, tmp_path)
    policy = POLICY.model_copy(
        update={"selection": POLICY.selection.model_copy(update={"max_plans_per_source": 1})}
    )
    detail = await recommend(sessionmaker, policy=policy)
    assert detail.status is RunStatus.SUCCEEDED, detail.counters

    cut = await rows(
        sessionmaker,
        "SELECT DISTINCT target_page_id FROM link_candidates "
        "WHERE run_id = :r AND rejection = 'per_source_limit'",
        r=detail.id,
    )
    assert cut, "positive control: the capacity cap must actually cut a qualified candidate"
    for row in cut:
        await _add(sessionmaker, row.target_page_id, tier=MIN_TIER)

    _, report = await _shadow(sessionmaker, detail.id)

    assert report.selection_changes, "a designated, capacity-cut candidate should be swapped in"
    per_source_live: dict[uuid.UUID, int] = {}
    per_source_shadow: dict[uuid.UUID, int] = {}
    for r in report.rows:
        per_source_live[r.source_page_id] = per_source_live.get(r.source_page_id, 0) + int(
            r.live_selected
        )
        per_source_shadow[r.source_page_id] = per_source_shadow.get(r.source_page_id, 0) + int(
            r.shadow_selected
        )
    assert per_source_live == per_source_shadow, "shadow keeps each source's plan count"
    assert all(v <= 1 for v in per_source_shadow.values())


# --------------------------------------------------------------------------- #
# DEFECT 1: a retired designation must still reconstruct its own past
# --------------------------------------------------------------------------- #
async def test_a_designation_retired_later_still_resolves_for_a_date_it_was_in_force(
    sessionmaker: SM, tmp_path: Path
) -> None:
    """Create, retire LATER, then ask on both sides of the retirement instant.

    This is the defect the boolean ``active`` model could not express: it answered "is it live
    now", so a designation retired today reported as absent for every past date, and a shadow
    report reconstructed for that date was wrong. Asserted through the real database and the
    real resolver, not against the row's own fields.
    """
    page = await _any_page(sessionmaker, tmp_path)
    began = NOW - timedelta(days=10)
    retired_on = NOW - timedelta(days=2)
    designation_id = await _add(sessionmaker, page, tier=1, effective_from=began)

    async with sessionmaker() as session:
        await retire_designation(
            session, designation_id=designation_id, actor=ACTOR, now=retired_on
        )
        await session.commit()

    async def value_at(moment: datetime) -> Any:
        async with sessionmaker() as session:
            loaded = await load_designations(session, page_ids=[page])
        return designated_value(
            loaded[page],
            page_id=page,
            at=moment,
            eligibility=LinkEligibility(
                as_source=True, as_target=True, source_reasons=[], target_reasons=[]
            ),
        )

    while_in_force = await value_at(began + timedelta(days=1))
    at_retirement = await value_at(retired_on)
    after = await value_at(NOW)

    assert while_in_force.applies, "retired today, but it WAS in force nine days ago"
    assert while_in_force.tier == 1
    assert while_in_force.types == (PriorityType.HIGH_PRIORITY_REPORT,)
    assert not at_retirement.applies, "half-open: the retirement instant is already outside"
    assert not after.applies

    # And the stored row carries the instant, not a flag.
    async with sessionmaker() as session:
        (row,) = await list_designations(session, page_id=page, include_retired=True)
    assert row.retired_at == retired_on
    assert row.is_retired


async def test_a_scheduled_future_retirement_is_in_force_until_it_arrives(
    sessionmaker: SM, tmp_path: Path
) -> None:
    page = await _any_page(sessionmaker, tmp_path)
    handover = NOW + timedelta(days=7)
    designation_id = await _add(sessionmaker, page, tier=1)
    async with sessionmaker() as session:
        scheduled = await retire_designation(
            session, designation_id=designation_id, actor=ACTOR, now=NOW, at=handover
        )
        await session.commit()

    assert scheduled.is_retired, "the retirement is recorded"
    assert scheduled.is_effective_at(NOW), "...and it is still in force today"
    assert not scheduled.is_effective_at(handover)

    # It is hidden from the default listing precisely because retirement is recorded.
    async with sessionmaker() as session:
        assert await list_designations(session, page_id=page) == []
        assert len(await list_designations(session, page_id=page, include_retired=True)) == 1


# --------------------------------------------------------------------------- #
# DEFECT 2: scheduled, non-overlapping replacement
# --------------------------------------------------------------------------- #
async def test_a_successor_can_be_scheduled_before_the_incumbent_stops_applying(
    sessionmaker: SM, tmp_path: Path
) -> None:
    """The whole point of the fix: schedule the handover, then add the successor, with the
    incumbent still in force and never edited."""
    page = await _any_page(sessionmaker, tmp_path)
    handover = NOW + timedelta(days=30)
    incumbent_id = await _add(sessionmaker, page, tier=3)

    async with sessionmaker() as session:
        await retire_designation(
            session, designation_id=incumbent_id, actor=ACTOR, now=NOW, at=handover
        )
        await session.commit()
    successor_id = await _add(sessionmaker, page, tier=1, effective_from=handover)

    async with sessionmaker() as session:
        loaded = (await load_designations(session, page_ids=[page]))[page]
    by_id = {d.id: d for d in loaded}
    assert set(by_id) == {incumbent_id, successor_id}, "both rows survive: nothing is edited away"

    # Exactly one is in force on each side of the handover, and never both.
    for moment, expected in ((NOW, incumbent_id), (handover, successor_id)):
        in_force = [d.id for d in loaded if d.is_effective_at(moment)]
        assert in_force == [expected], f"at {moment}: {in_force}"


async def test_an_overlapping_successor_is_refused(sessionmaker: SM, tmp_path: Path) -> None:
    page = await _any_page(sessionmaker, tmp_path)
    await _add(sessionmaker, page, tier=3)
    with pytest.raises(IntegrityError) as info:
        await _add(sessionmaker, page, tier=1, effective_from=NOW + timedelta(days=1))
    assert violated_constraint(info.value.orig or info.value) == "page_priorities_no_overlap"


async def test_an_expired_designation_does_not_block_its_successor(
    sessionmaker: SM, tmp_path: Path
) -> None:
    """The old partial unique index blocked this: the expired row was still ``active``."""
    page = await _any_page(sessionmaker, tmp_path)
    await _add(
        sessionmaker,
        page,
        tier=3,
        effective_from=NOW - timedelta(days=30),
        effective_to=NOW - timedelta(days=10),
    )
    successor = await _add(sessionmaker, page, tier=1, effective_from=NOW)

    async with sessionmaker() as session:
        loaded = (await load_designations(session, page_ids=[page]))[page]
    assert [d.id for d in loaded if d.is_effective_at(NOW)] == [successor]
    assert len(loaded) == 2


async def test_adjacent_windows_need_no_retirement_at_all(sessionmaker: SM, tmp_path: Path) -> None:
    page = await _any_page(sessionmaker, tmp_path)
    boundary = NOW + timedelta(days=30)
    first = await _add(sessionmaker, page, tier=3, effective_from=NOW, effective_to=boundary)
    second = await _add(sessionmaker, page, tier=1, effective_from=boundary)

    async with sessionmaker() as session:
        loaded = (await load_designations(session, page_ids=[page]))[page]
    assert [d.id for d in loaded if d.is_effective_at(NOW)] == [first]
    assert [d.id for d in loaded if d.is_effective_at(boundary)] == [second]
    assert all(not d.is_retired for d in loaded), "no retirement was needed"


# --------------------------------------------------------------------------- #
# Timezone-naive input is refused at the service boundary
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("field", ["effective_from", "effective_to", "now"])
async def test_a_naive_instant_is_refused_by_the_service(
    sessionmaker: SM, tmp_path: Path, field: str
) -> None:
    page = await _any_page(sessionmaker, tmp_path)
    naive = datetime(2026, 11, 19, 12, 0)
    with pytest.raises(ValueError, match=field):
        await _add(sessionmaker, page, **{field: naive})


@pytest.mark.parametrize("field", ["now", "at"])
async def test_a_naive_retirement_instant_is_refused(
    sessionmaker: SM, tmp_path: Path, field: str
) -> None:
    page = await _any_page(sessionmaker, tmp_path)
    designation_id = await _add(sessionmaker, page)
    naive = datetime(2026, 11, 19, 12, 0)
    kwargs: dict[str, Any] = {"now": NOW, "at": None, field: naive}
    async with sessionmaker() as session:
        with pytest.raises(ValueError, match=field):
            await retire_designation(session, designation_id=designation_id, actor=ACTOR, **kwargs)
