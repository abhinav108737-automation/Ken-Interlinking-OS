"""Plans stranded in the pre-ADR-0018 ``awaiting_approval`` status (hardening pass).

Claims: a shadow pass can evaluate them and moves nothing; a publishing pass refuses them;
``legacy-reconcile`` is a dry run by default, moves only defective plans along edges
``ALLOWED_TRANSITIONS`` already has, keeps the original status in the history, is idempotent,
is atomic per plan, and never publishes or approves anything.
"""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest

from ken_contracts.autonomy import AutonomyMode, BlockCode
from ken_contracts.recommendations import LinkPlanStatus
from ken_services.autonomy.engine import ShadowOnlyStatusError
from ken_services.autonomy.legacy import LEGACY_ACTOR, LegacyClass, reconcile
from ken_services.recommendations.transitions import transition
from tests.integration.autonomy_support import (
    Autonomy,
    autonomy,
    decisions,
    protect,
    record_critic_review,
    transitions,
)
from tests.integration.deployment_support import count
from tests.integration.recommendation_support import CHARGING, EV, SM, rows

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("migrated")]
S = LinkPlanStatus


@pytest.fixture
async def a(sessionmaker: SM, tmp_path: Path) -> Autonomy:
    return await autonomy(sessionmaker, tmp_path)


async def _legacy(sessionmaker: SM, a: Autonomy, *, target: str = CHARGING) -> uuid.UUID:
    """Put a plan where the pre-ADR-0018 agent hand-off left it: reviewed, accepted, handed off,
    then ``proposed -> critiqued -> policy_checked -> awaiting_approval``."""
    plan = a.world.plans[(EV, target)]
    await record_critic_review(
        sessionmaker,
        plan.id,
        source_page_id=plan.source_page_id,
        source_url=plan.source_url,
        verdict="accept",
        handed_off=True,
    )
    async with sessionmaker() as session, session.begin():
        for to, frm, actor in (
            (S.CRITIQUED, S.PROPOSED, "agent:critic@graph-v1"),
            (S.POLICY_CHECKED, S.CRITIQUED, "system:agent-policy-gate"),
            (S.AWAITING_APPROVAL, S.POLICY_CHECKED, "system:agent-policy-gate"),
        ):
            await transition(session, plan_id=plan.id, to_status=to, actor=actor, expected_from=frm)
    plan_id: uuid.UUID = plan.id
    return plan_id


async def test_a_shadow_pass_evaluates_a_legacy_plan_and_moves_nothing(
    sessionmaker: SM, a: Autonomy
) -> None:
    plan_id = await _legacy(sessionmaker, a)
    engine = a.engine()
    assert plan_id not in await engine.decidable_plans()
    assert plan_id in await engine.decidable_plans(include_shadow_only=True)
    before = {t: await count(sessionmaker, t) for t in ("deployments", "publish_attempts")}
    history = await transitions(sessionmaker, plan_id)

    evaluation = await engine.evaluate_and_record(plan_id=plan_id, mode=AutonomyMode.SHADOW)

    assert evaluation.would_have_decision is not None
    assert len(await decisions(sessionmaker, plan_id)) == 1
    assert await transitions(sessionmaker, plan_id) == history, "status and history untouched"
    assert {t: await count(sessionmaker, t) for t in before} == before


async def test_a_publishing_pass_refuses_a_legacy_plan(sessionmaker: SM, a: Autonomy) -> None:
    plan_id = await _legacy(sessionmaker, a)
    guarded = a.with_mode("guarded")
    with pytest.raises(ShadowOnlyStatusError):
        await guarded.engine().evaluate_and_record(plan_id=plan_id)
    assert plan_id not in await guarded.engine().decidable_plans(include_shadow_only=False)
    assert await decisions(sessionmaker, plan_id) == []
    assert (await transitions(sessionmaker, plan_id))[-1] == "awaiting_approval"


async def test_reconcile_is_a_dry_run_by_default_and_leaves_an_eligible_plan(
    sessionmaker: SM, a: Autonomy
) -> None:
    plan_id = await _legacy(sessionmaker, a)
    findings = await reconcile(sessionmaker=sessionmaker, engine=a.engine())
    assert [(f.plan_id, f.legacy_class, f.moved_to) for f in findings] == [
        (plan_id, LegacyClass.ELIGIBLE, None)
    ]
    applied = await reconcile(sessionmaker=sessionmaker, engine=a.engine(), apply=True)
    assert applied[0].legacy_class is LegacyClass.ELIGIBLE and applied[0].moved_to is None
    assert (await transitions(sessionmaker, plan_id))[-1] == "awaiting_approval"


async def test_a_protected_page_plan_is_blocked_with_its_history_kept_and_it_is_idempotent(
    sessionmaker: SM, a: Autonomy
) -> None:
    plan_id = await _legacy(sessionmaker, a)
    await protect(sessionmaker, a.world.plans[(EV, CHARGING)].target_page_id)
    dry = await reconcile(sessionmaker=sessionmaker, engine=a.engine())
    assert dry[0].legacy_class is LegacyClass.PROTECTED
    assert (await transitions(sessionmaker, plan_id))[-1] == "awaiting_approval", "dry run"

    first = await reconcile(sessionmaker=sessionmaker, engine=a.engine(), apply=True)
    assert first[0].moved_to == "blocked"
    history = await transitions(sessionmaker, plan_id)
    assert history == ["proposed", "critiqued", "policy_checked", "awaiting_approval", "blocked"]
    last = (
        await rows(
            sessionmaker,
            "SELECT from_status, to_status, actor, note FROM link_plan_transitions "
            "WHERE plan_id = :p ORDER BY id DESC LIMIT 1",
            p=plan_id,
        )
    )[0]
    assert (last.from_status, last.to_status, last.actor) == (
        "awaiting_approval",
        "blocked",
        LEGACY_ACTOR,
    ), "the original status is preserved as from_status"
    assert BlockCode.PROTECTED_PAGE.value in last.note

    again = await reconcile(sessionmaker=sessionmaker, engine=a.engine(), apply=True)
    assert again == [], "nothing left in awaiting_approval: idempotent"
    assert await transitions(sessionmaker, plan_id) == history
    for table in ("deployments", "publish_attempts", "deployment_items"):
        assert await count(sessionmaker, table) == 0, "nothing was published"


async def test_a_stale_plan_is_expired(
    sessionmaker: SM, a: Autonomy, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan_id = await _legacy(sessionmaker, a)
    engine = a.engine()

    async def stale(*, plan_id: uuid.UUID) -> tuple[BlockCode, ...]:
        return (BlockCode.STALE_SOURCE_VERSION,)

    monkeypatch.setattr(engine, "deterministic_blocks", stale)
    result = await reconcile(sessionmaker=sessionmaker, engine=engine, apply=True)
    assert result[0].legacy_class is LegacyClass.STALE and result[0].moved_to == "expired"
    assert (await transitions(sessionmaker, plan_id))[-1] == "expired"


async def test_a_failure_while_moving_a_plan_rolls_that_plan_back(
    sessionmaker: SM, a: Autonomy, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan_id = await _legacy(sessionmaker, a)
    await protect(sessionmaker, a.world.plans[(EV, CHARGING)].target_page_id)
    history = await transitions(sessionmaker, plan_id)

    from ken_services.autonomy import legacy

    real = legacy.transition  # type: ignore[attr-defined]

    async def boom(session: object, **kw: object) -> object:
        await real(session, **kw)  # type: ignore[arg-type]  # the row is written, then it fails
        raise RuntimeError("simulated failure after the transition row")

    monkeypatch.setattr(legacy, "transition", boom)
    with pytest.raises(RuntimeError):
        await reconcile(sessionmaker=sessionmaker, engine=a.engine(), apply=True)

    assert await transitions(sessionmaker, plan_id) == history, "the whole move rolled back"
    monkeypatch.undo()
    retry = await reconcile(sessionmaker=sessionmaker, engine=a.engine(), apply=True)
    assert retry[0].moved_to == "blocked", "and a retry succeeds"


async def test_a_legacy_plan_with_no_accepting_review_is_never_called_eligible(
    sessionmaker: SM, a: Autonomy
) -> None:
    plan = a.world.plans[(EV, CHARGING)]
    await record_critic_review(
        sessionmaker,
        plan.id,
        source_page_id=plan.source_page_id,
        source_url=plan.source_url,
        verdict="uncertain",
        handed_off=False,
    )
    async with sessionmaker() as session, session.begin():
        await transition(
            session,
            plan_id=plan.id,
            to_status=S.AWAITING_APPROVAL,
            actor="system:agent-policy-gate",
            expected_from=S.PROPOSED,
        )
    result = await reconcile(sessionmaker=sessionmaker, engine=a.engine(), apply=True)
    assert result[0].legacy_class is LegacyClass.CRITIC_NOT_ACCEPT and result[0].moved_to is None
