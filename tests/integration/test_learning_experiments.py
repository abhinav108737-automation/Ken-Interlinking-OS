"""Experiments (workstream D; ADR-0016 15.5, 16.6, 17.2): the guard, the lifecycle, attribution.

The properties that matter, in order:

* a ``holdout`` CANNOT be activated by default: the lifecycle refuses it
  (``HOLDOUT_NOT_ENABLED``) while ``experiments_allow_holdout`` is unset, a raw
  ``UPDATE ... status = 'active'`` fails in the database without the activation setting, and a
  fraction above 0.20 is refused by the service AND by the database CHECK;
* ``locked()`` is true iff the TARGET page is a ``control`` page of an ACTIVE ``holdout`` --
  whatever the source -- and a ``policy_ab`` (or a draft or ended holdout) locks nobody;
* arms are deterministic and MATERIALISED, and a holdout withholds at most its fraction of
  pages;
* attribution is derived (assignment x interval x the plan's live window), and
  ``link_events`` keeps exactly its eight columns;
* the injected guard really blocks a pair at the recommendation pipeline and in the review
  service;
* the comparison reports counts and flags only: no significance, no causal lift.
"""

from __future__ import annotations

import uuid
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from ken_contracts.analytics import (
    AnalyticsSource,
    ConfounderFlag,
    Dataset,
    DateRange,
    ImportVia,
    MetricSource,
    PageMetrics,
)
from ken_contracts.learning import (
    MAX_HOLDOUT_TREATMENT_FRACTION,
    ExperimentArm,
    ExperimentKind,
    ExperimentRefusal,
    ExperimentRefusedError,
    PromotionRefusal,
    PromotionRefusedError,
)
from ken_services.learning.experiments import (
    ExperimentLifecycleService,
    SqlExperimentGuard,
    attribute,
    compare_arms,
)
from ken_services.learning.protocol import experiment_arm
from tests.integration.analytics_support import (
    make_live,
    new_plan,
    request_for,
    run_mock_import,
    seed_pages,
)
from tests.integration.learning_lifecycle_support import (
    SM,
    learning_policy,
    operator,
    reviewer,
    seed_proposal,
    settings,
)
from tests.integration.recommendation_support import SITE, recommend, seed_corpus
from tests.integration.review_support import review_service

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("migrated")]

SYSTEM = "system:learning"
T0 = datetime(2026, 6, 1, 9, 0, tzinfo=UTC)
T_END = datetime(2026, 6, 20, 9, 0, tzinfo=UTC)
PATHS = [f"/p{n:02d}" for n in range(24)]


def _service(
    sm: SM, *, allow_holdout: bool = False, now: datetime = T0, min_pages: int = 1
) -> ExperimentLifecycleService:
    policy = learning_policy()
    policy = policy.model_copy(
        update={
            "experiments": policy.experiments.model_copy(update={"min_pages_per_arm": min_pages})
        }
    )
    return ExperimentLifecycleService(
        sessionmaker=sm,
        settings=settings(experiments_allow_holdout=allow_holdout),
        policy=policy,
        clock=lambda: now,
    )


async def _holdout(
    sm: SM, key: str = "hold-out-one", *, fraction: float = 0.2, allow: bool = True
) -> tuple[ExperimentLifecycleService, dict[str, uuid.UUID]]:
    pages = await seed_pages(sm, *PATHS)
    service = _service(sm, allow_holdout=allow)
    admin = await operator(sm)
    await service.create(
        principal=admin,
        key=key,
        description="withhold inbound links",
        kind=ExperimentKind.HOLDOUT,
        treatment_fraction=fraction,
    )
    await service.assign(key=key, actor=SYSTEM)
    return service, pages


async def _arms(sm: SM, key: str) -> dict[uuid.UUID, ExperimentArm]:
    async with sm() as session:
        rows = (
            await session.execute(
                text(
                    "SELECT a.page_id, a.arm FROM experiment_assignments a "
                    "JOIN experiments e ON e.id = a.experiment_id WHERE e.key = :k"
                ),
                {"k": key},
            )
        ).all()
    return {row.page_id: ExperimentArm(row.arm) for row in rows}


async def _status(sm: SM, key: str) -> str:
    async with sm() as session:
        return str(
            await session.scalar(text("SELECT status FROM experiments WHERE key = :k"), {"k": key})
        )


# --------------------------------------------------------------------------- #
# A holdout cannot be activated by default
# --------------------------------------------------------------------------- #
async def test_a_holdout_cannot_be_activated_while_the_setting_is_unset(sessionmaker: SM) -> None:
    assert settings().experiments_allow_holdout is False
    _service_default, _pages = await _holdout(sessionmaker, allow=False)
    admin = await operator(sessionmaker, "otto2")
    service = _service(sessionmaker, allow_holdout=False)

    with pytest.raises(ExperimentRefusedError) as raised:
        await service.activate(principal=admin, key="hold-out-one")

    assert raised.value.refusal is ExperimentRefusal.HOLDOUT_NOT_ENABLED
    assert await _status(sessionmaker, "hold-out-one") == "draft"


async def test_a_raw_update_cannot_start_a_holdout_even_when_the_setting_is_on(
    sessionmaker: SM,
) -> None:
    """The database guard requires the transaction-local activator setting, which only the
    lifecycle service establishes."""
    await _holdout(sessionmaker)
    async with sessionmaker() as session, session.begin():
        with pytest.raises(DBAPIError):
            async with session.begin_nested():
                await session.execute(
                    text(
                        "UPDATE experiments SET status = 'active', started_at = now() "
                        "WHERE key = 'hold-out-one'"
                    )
                )
    assert await _status(sessionmaker, "hold-out-one") == "draft"


async def test_with_the_setting_on_the_service_activates_a_holdout(sessionmaker: SM) -> None:
    await _holdout(sessionmaker)
    admin = await operator(sessionmaker, "otto2")
    await _service(sessionmaker, allow_holdout=True).activate(principal=admin, key="hold-out-one")
    assert await _status(sessionmaker, "hold-out-one") == "active"
    # The activator setting is transaction-local: it did not leak to the next transaction, so a
    # raw activation of ANOTHER draft holdout on the same pool still fails.
    await _service(sessionmaker, allow_holdout=True).create(
        principal=admin,
        key="hold-out-two",
        description="second",
        kind=ExperimentKind.HOLDOUT,
        treatment_fraction=0.1,
    )
    async with sessionmaker() as session, session.begin():
        with pytest.raises(DBAPIError):
            async with session.begin_nested():
                await session.execute(
                    text(
                        "UPDATE experiments SET status = 'active', started_at = now() "
                        "WHERE key = 'hold-out-two'"
                    )
                )
    assert await _status(sessionmaker, "hold-out-two") == "draft"


async def test_a_holdout_fraction_above_the_cap_is_refused_twice(sessionmaker: SM) -> None:
    admin = await operator(sessionmaker)
    with pytest.raises(ExperimentRefusedError) as raised:
        await _service(sessionmaker).create(
            principal=admin,
            key="too-big",
            description="x",
            kind=ExperimentKind.HOLDOUT,
            treatment_fraction=MAX_HOLDOUT_TREATMENT_FRACTION + 0.01,
        )
    assert raised.value.refusal is ExperimentRefusal.FRACTION_TOO_LARGE
    async with sessionmaker() as session, session.begin():
        with pytest.raises(DBAPIError):
            async with session.begin_nested():
                await session.execute(
                    text(
                        "INSERT INTO experiments (id, key, description, unit, kind, "
                        "treatment_fraction, created_by) VALUES (gen_random_uuid(), 'raw-big', "
                        "'x', 'target_page', 'holdout', 0.3, 'user:otto')"
                    )
                )


async def test_the_holdout_unit_is_the_target_page_and_policy_ab_is_the_source(
    sessionmaker: SM,
) -> None:
    await _holdout(sessionmaker)
    seeded = await seed_proposal(sessionmaker)
    await _service(sessionmaker).create(
        principal=await operator(sessionmaker, "otto2"),
        key="ab-one",
        description="candidate ranking",
        kind=ExperimentKind.POLICY_AB,
        candidate_id=seeded.candidate_id,
    )
    async with sessionmaker() as session:
        units: dict[Any, Any] = dict(
            (await session.execute(text("SELECT key, unit FROM experiments"))).all()
        )
    assert units == {"hold-out-one": "target_page", "ab-one": "source_page"}


async def test_only_an_operator_may_create_or_activate(sessionmaker: SM) -> None:
    await _holdout(sessionmaker)
    who = await reviewer(sessionmaker)
    with pytest.raises(PromotionRefusedError) as raised:
        await _service(sessionmaker, allow_holdout=True).activate(principal=who, key="hold-out-one")
    assert raised.value.refusal is PromotionRefusal.PERMISSION_DENIED
    assert await _status(sessionmaker, "hold-out-one") == "draft"


# --------------------------------------------------------------------------- #
# Assignment
# --------------------------------------------------------------------------- #
async def test_assignment_is_deterministic_materialised_and_idempotent(sessionmaker: SM) -> None:
    service = _service(sessionmaker)
    pages = await seed_pages(sessionmaker, *PATHS)
    seeded = await seed_proposal(sessionmaker)
    await service.create(
        principal=await operator(sessionmaker),
        key="ab-two",
        description="candidate ranking",
        kind=ExperimentKind.POLICY_AB,
        candidate_id=seeded.candidate_id,
        treatment_fraction=0.5,
    )

    counts = await service.assign(key="ab-two", actor=SYSTEM)
    arms = await _arms(sessionmaker, "ab-two")

    assert set(arms) == set(pages.values())
    for page_id, arm in arms.items():
        assert arm is experiment_arm(
            kind=ExperimentKind.POLICY_AB, key="ab-two", page_id=page_id, fraction=0.5
        )
    assert sum(counts.values()) == len(PATHS)
    assert counts[ExperimentArm.CONTROL] > 0 < counts[ExperimentArm.TREATMENT]
    again = await service.assign(key="ab-two", actor=SYSTEM)
    assert again == counts
    assert await _arms(sessionmaker, "ab-two") == arms
    async with sessionmaker() as session:
        assert (
            await session.scalar(text("SELECT DISTINCT assigned_by FROM experiment_assignments"))
            == SYSTEM
        )


async def test_an_assignment_needs_a_system_actor_and_a_draft(sessionmaker: SM) -> None:
    service = _service(sessionmaker)
    await _holdout(sessionmaker)
    with pytest.raises(ValueError, match="system actor"):
        await service.assign(key="hold-out-one", actor="agent:critic@1")
    admin = await operator(sessionmaker, "otto3")
    await _service(sessionmaker, allow_holdout=True).activate(principal=admin, key="hold-out-one")
    with pytest.raises(ExperimentRefusedError) as raised:
        await service.assign(key="hold-out-one", actor=SYSTEM)
    assert raised.value.refusal is ExperimentRefusal.NOT_A_DRAFT


async def test_activation_needs_assignments_and_enough_pages_per_arm(sessionmaker: SM) -> None:
    admin = await operator(sessionmaker)
    seeded = await seed_proposal(sessionmaker)
    await seed_pages(sessionmaker, *PATHS)
    service = _service(sessionmaker, min_pages=1)
    await service.create(
        principal=admin,
        key="empty-ab",
        description="x",
        kind=ExperimentKind.POLICY_AB,
        candidate_id=seeded.candidate_id,
    )
    with pytest.raises(ExperimentRefusedError) as none:
        await service.activate(principal=admin, key="empty-ab")
    assert none.value.refusal is ExperimentRefusal.NO_ASSIGNMENTS

    await service.assign(key="empty-ab", actor=SYSTEM)
    with pytest.raises(ExperimentRefusedError) as thin:
        await _service(sessionmaker, min_pages=20).activate(principal=admin, key="empty-ab")
    assert thin.value.refusal is ExperimentRefusal.NO_ASSIGNMENTS
    await service.activate(principal=admin, key="empty-ab")
    assert await _status(sessionmaker, "empty-ab") == "active"


async def test_a_page_belongs_to_one_live_experiment(sessionmaker: SM) -> None:
    await _holdout(sessionmaker)
    seeded = await seed_proposal(sessionmaker)
    service = _service(sessionmaker)
    await service.create(
        principal=await operator(sessionmaker, "otto2"),
        key="ab-three",
        description="x",
        kind=ExperimentKind.POLICY_AB,
        candidate_id=seeded.candidate_id,
    )
    with pytest.raises(ExperimentRefusedError) as raised:
        await service.assign(key="ab-three", actor=SYSTEM)
    assert raised.value.refusal is ExperimentRefusal.PAGE_ALREADY_IN_A_LIVE_EXPERIMENT


def test_a_holdout_withholds_about_its_fraction_of_pages_never_most_of_them() -> None:
    """The cap bounds the WITHHELD (control) arm. Hashing the fraction as the TREATMENT share
    would have locked 90 % of pages at fraction 0.10."""
    pages = [uuid.UUID(int=n * 7919 + 13) for n in range(5000)]
    held = Counter(
        experiment_arm(kind=ExperimentKind.HOLDOUT, key="hold-out-one", page_id=p, fraction=0.10)
        for p in pages
    )
    assert 0.08 <= held[ExperimentArm.CONTROL] / len(pages) <= 0.12
    ab = Counter(
        experiment_arm(kind=ExperimentKind.POLICY_AB, key="ab-x", page_id=p, fraction=0.10)
        for p in pages
    )
    assert 0.08 <= ab[ExperimentArm.TREATMENT] / len(pages) <= 0.12


# --------------------------------------------------------------------------- #
# The guard
# --------------------------------------------------------------------------- #
async def _guard_world(sm: SM) -> tuple[SqlExperimentGuard, uuid.UUID, uuid.UUID, uuid.UUID]:
    service, pages = await _holdout(sm)
    arms = await _arms(sm, "hold-out-one")
    control = next(p for p, a in arms.items() if a is ExperimentArm.CONTROL)
    treatment = next(p for p, a in arms.items() if a is ExperimentArm.TREATMENT)
    outsider = uuid.uuid4()
    del service, pages
    return SqlExperimentGuard(sm, ttl_seconds=0.0), control, treatment, outsider


async def test_a_draft_holdout_locks_nobody(sessionmaker: SM) -> None:
    guard, control, treatment, outsider = await _guard_world(sessionmaker)
    for page in (control, treatment, outsider):
        assert await guard.locked(source_page_id=uuid.uuid4(), target_page_id=page) is False


async def test_an_active_holdout_locks_exactly_the_control_targets(sessionmaker: SM) -> None:
    guard, control, treatment, outsider = await _guard_world(sessionmaker)
    admin = await operator(sessionmaker, "otto2")
    await _service(sessionmaker, allow_holdout=True).activate(principal=admin, key="hold-out-one")

    some_source = uuid.uuid4()
    assert await guard.locked(source_page_id=some_source, target_page_id=control) is True
    assert await guard.locked(source_page_id=some_source, target_page_id=treatment) is False
    assert await guard.locked(source_page_id=some_source, target_page_id=outsider) is False
    # The arm is the TARGET's: a control page may still be a SOURCE of links.
    assert await guard.locked(source_page_id=control, target_page_id=treatment) is False
    # Whatever the source, the control target is locked (no contamination from unassigned pages).
    for _ in range(3):
        assert await guard.locked(source_page_id=uuid.uuid4(), target_page_id=control) is True


async def test_an_ended_holdout_locks_nobody(sessionmaker: SM) -> None:
    guard, control, _treatment, _outsider = await _guard_world(sessionmaker)
    admin = await operator(sessionmaker, "otto2")
    service = _service(sessionmaker, allow_holdout=True)
    await service.activate(principal=admin, key="hold-out-one")
    assert await guard.locked(source_page_id=uuid.uuid4(), target_page_id=control) is True
    await service.end(principal=admin, key="hold-out-one")
    assert await guard.locked(source_page_id=uuid.uuid4(), target_page_id=control) is False


async def test_a_policy_ab_locks_nobody(sessionmaker: SM) -> None:
    pages = await seed_pages(sessionmaker, *PATHS)
    seeded = await seed_proposal(sessionmaker)
    admin = await operator(sessionmaker)
    service = _service(sessionmaker)
    await service.create(
        principal=admin,
        key="ab-four",
        description="x",
        kind=ExperimentKind.POLICY_AB,
        candidate_id=seeded.candidate_id,
    )
    await service.assign(key="ab-four", actor=SYSTEM)
    await service.activate(principal=admin, key="ab-four")  # needs no setting and no GUC
    guard = SqlExperimentGuard(sessionmaker, ttl_seconds=0.0)
    arms = await _arms(sessionmaker, "ab-four")
    assert ExperimentArm.CONTROL in arms.values()
    for page in pages.values():
        assert await guard.locked(source_page_id=page, target_page_id=page) is False


class _Broken:
    def __call__(self) -> Any:
        raise OSError("database unreachable")


async def test_a_guard_that_never_loaded_fails_closed_and_one_that_did_keeps_its_map(
    sessionmaker: SM,
) -> None:
    never = SqlExperimentGuard(_Broken())  # type: ignore[arg-type]
    assert await never.locked(source_page_id=uuid.uuid4(), target_page_id=uuid.uuid4()) is True

    guard, control, _treatment, _outsider = await _guard_world(sessionmaker)
    await _service(sessionmaker, allow_holdout=True).activate(
        principal=await operator(sessionmaker, "otto2"), key="hold-out-one"
    )
    await guard.refresh()
    guard._sessionmaker = _Broken()  # type: ignore[assignment]
    await guard.refresh()  # fails, never raises
    assert await guard.locked(source_page_id=uuid.uuid4(), target_page_id=control) is True


# --------------------------------------------------------------------------- #
# The injection: the guard blocks the pair at both sites
# --------------------------------------------------------------------------- #
async def test_the_review_service_defaults_to_the_real_guard(sessionmaker: SM) -> None:
    service = review_service(sessionmaker)
    assert isinstance(service._guard, SqlExperimentGuard)


async def test_the_real_guard_blocks_a_pair_in_the_recommendation_pipeline(
    sessionmaker: SM, tmp_path: Path
) -> None:
    await seed_corpus(sessionmaker, tmp_path)
    admin = await operator(sessionmaker)
    # Choose the key BEFORE creating anything: a holdout withholds only ``fraction`` of the pages,
    # so many keys yield no control page on a small corpus, and a holdout with no control page
    # cannot be activated. Computing it up front leaves no unlucky draft holding pages.
    async with sessionmaker() as session:
        corpus = [row[0] for row in (await session.execute(text("SELECT id FROM pages"))).all()]
    key = next(
        (
            f"hold-out-{attempt:02d}"
            for attempt in range(200)
            if any(
                experiment_arm(
                    kind=ExperimentKind.HOLDOUT,
                    key=f"hold-out-{attempt:02d}",
                    page_id=page_id,
                    fraction=0.2,
                )
                is ExperimentArm.CONTROL
                for page_id in corpus
            )
        ),
        None,
    )
    assert key is not None, "no key produced a control page in 200 tries"
    service = _service(sessionmaker, allow_holdout=True)
    await service.create(
        principal=admin,
        key=key,
        description="x",
        kind=ExperimentKind.HOLDOUT,
        treatment_fraction=0.2,
    )
    await service.assign(key=key, actor=SYSTEM)
    arms = await _arms(sessionmaker, key)
    await service.activate(principal=admin, key=key)
    controls = {page for page, arm in arms.items() if arm is ExperimentArm.CONTROL}

    run = await recommend(sessionmaker, experiment_guard=SqlExperimentGuard(sessionmaker))

    assert run.counters["rejected_experiment_locked"] > 0, run.counters
    async with sessionmaker() as session:
        targets = {
            row[0]
            for row in (await session.execute(text("SELECT target_page_id FROM link_plans"))).all()
        }
    assert targets, "the run proposed nothing at all"
    assert not targets & controls, "a plan points at a withheld control page"


# --------------------------------------------------------------------------- #
# Attribution and the observed comparison
# --------------------------------------------------------------------------- #
async def test_attribution_is_assignment_times_interval_times_live_window(
    sessionmaker: SM,
) -> None:
    _, pages = await _holdout(sessionmaker)
    arms = await _arms(sessionmaker, "hold-out-one")
    control = [p for p, a in arms.items() if a is ExperimentArm.CONTROL]
    treatment = [p for p, a in arms.items() if a is ExperimentArm.TREATMENT]
    source_id = treatment[0]
    by_id = {v: k for k, v in pages.items()}

    async def plan(
        target: uuid.UUID, *, deployed: datetime, rolled_back: datetime | None
    ) -> uuid.UUID:
        plan_id = await new_plan(
            sessionmaker,
            source=source_id,
            target=target,
            source_url=SITE + by_id[source_id],
            target_url=SITE + by_id[target],
            block_text=f"Batteries about term{target.hex[:6]} today.",
            anchor=f"term{target.hex[:6]}",
        )
        await make_live(sessionmaker, plan_id, deployed_at=deployed, rolled_back_at=rolled_back)
        return plan_id

    inside = await plan(treatment[1], deployed=T0 + timedelta(days=2), rolled_back=None)
    before = await plan(
        treatment[2],
        deployed=T0 - timedelta(days=40),
        rolled_back=T0 - timedelta(days=10),
    )
    after = await plan(treatment[3], deployed=T_END + timedelta(days=3), rolled_back=None)
    spanning = await plan(treatment[4], deployed=T0 - timedelta(days=5), rolled_back=None)
    contaminated = await plan(control[0], deployed=T0 + timedelta(days=1), rolled_back=None)

    admin = await operator(sessionmaker, "otto2")
    await _service(sessionmaker, allow_holdout=True, now=T0).activate(
        principal=admin, key="hold-out-one"
    )
    await _service(sessionmaker, now=T_END).end(principal=admin, key="hold-out-one")

    async with sessionmaker() as session:
        experiment_id = await session.scalar(
            text("SELECT id FROM experiments WHERE key = 'hold-out-one'")
        )
        attributed = await attribute(session, experiment_id=experiment_id)

    assert set(attributed[ExperimentArm.TREATMENT]) == {inside, spanning}
    assert attributed[ExperimentArm.CONTROL] == (contaminated,)
    assert before not in attributed[ExperimentArm.TREATMENT]
    assert after not in attributed[ExperimentArm.TREATMENT]

    async with sessionmaker() as session:
        comparison = await compare_arms(session, experiment_id=experiment_id, min_pages_per_arm=1)
    # A plan that reached a control page IS the contamination the flags exist to show.
    assert ConfounderFlag.OVERLAPPING_CHANGES in comparison.confounder_flags
    assert comparison.control.attributable_plans == 1
    assert comparison.treatment.attributable_plans == 2


async def test_a_link_event_gains_no_experiment_column(sessionmaker: SM) -> None:
    async with sessionmaker() as session:
        found = await session.execute(
            text(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = 'link_events'"
            )
        )
        rows: list[Any] = list(found.scalars().all())
    columns = [str(each) for each in rows]
    assert len(columns) == 8
    assert not any("experiment" in each for each in columns)


async def test_the_comparison_reports_observed_differences_with_flags_and_no_inference(
    sessionmaker: SM,
) -> None:
    _, pages = await _holdout(sessionmaker)
    arms = await _arms(sessionmaker, "hold-out-one")
    admin = await operator(sessionmaker, "otto2")
    await _service(sessionmaker, allow_holdout=True, now=T0).activate(
        principal=admin, key="hold-out-one"
    )
    days = [T0.date() + timedelta(days=n) for n in range(5)]
    rows = [
        PageMetrics(
            page_id=str(page),
            source=MetricSource.GA4,
            range=DateRange(start=day, end=day),
            pageviews=10 if arm is ExperimentArm.CONTROL else 30,
            sessions=5,
        )
        for page, arm in arms.items()
        for day in days
    ]
    await run_mock_import(
        sessionmaker,
        request=request_for(
            source=AnalyticsSource.GA4,
            dataset=Dataset.PAGES,
            start=days[0].isoformat(),
            end=days[-1].isoformat(),
            via=ImportVia.API,
            source_timezone="Asia/Kolkata",
        ),
        page_rows=rows,
        now=T0 + timedelta(days=6),
    )
    async with sessionmaker() as session:
        experiment_id = await session.scalar(
            text("SELECT id FROM experiments WHERE key = 'hold-out-one'")
        )
        comparison = await compare_arms(
            session,
            experiment_id=experiment_id,
            min_pages_per_arm=1,
            now=T0 + timedelta(days=6),
        )

    assert comparison.control.pages + comparison.treatment.pages == len(pages)
    assert comparison.control.pageviews == 10 * 5 * comparison.control.pages
    assert comparison.treatment.pageviews == 30 * 5 * comparison.treatment.pages
    assert comparison.observed_pageviews_per_page_difference == pytest.approx(100.0)
    assert ConfounderFlag.POST_WINDOW_TRUNCATED in comparison.confounder_flags
    assert "not a causal effect" in comparison.note
    forbidden = {"p", "pvalue", "significance", "significant", "lift", "causal", "revenue", "ci"}
    names = set(comparison.__dataclass_fields__) | set(comparison.control.__dataclass_fields__)
    tokens = {token for name in names for token in name.split("_")}
    assert not tokens & forbidden
