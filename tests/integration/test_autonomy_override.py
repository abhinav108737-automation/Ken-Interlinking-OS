"""The operator override and the autonomy reader (ADR-0018 s5, s6; directive s20/s22).

The exception path. The claims under test:

* a publish override is REFUSED while a deterministic block still fires -- an override exists
  to act on a plan the engine declined, not to waive a safety rule;
* once the blocking FACT is fixed, the override publishes, and the whole VALIDATE -> apply ->
  verify path still runs;
* a block override always works: a human may always say no;
* every override records who and why, and the reason is mandatory for a ``user:`` actor;
* a replay of the same override returns the stored result instead of acting twice;
* a role without ``deploy`` cannot override;
* the reader reports the mode honestly, including ``publishing_enabled = False`` in shadow.
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import cast

import pytest

from ken_contracts.auth import Role
from ken_contracts.autonomy import (
    AutonomyDecision,
    AutonomyMode,
    BlockCode,
    OperatorOverrideRequest,
    OverrideAction,
)
from ken_contracts.recommendations import LinkPlanStatus
from ken_services.autonomy.override import (
    OVERRIDE_BLOCKED_BY_CODE,
    OVERRIDE_STATUS_MOVED,
    DatabaseOverrideService,
    OverridePermissionError,
)
from ken_services.autonomy.publish import PublishOutcome
from ken_services.autonomy.reader import SqlAutonomyReader
from tests.integration.autonomy_support import (
    Autonomy,
    autonomy,
    decisions,
    protect,
    transitions,
)
from tests.integration.deployment_support import count, plan_status
from tests.integration.recommendation_support import CHARGING, EV, LI, SM
from tests.integration.review_support import principal

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("migrated")]

S = LinkPlanStatus


@pytest.fixture
async def a(sessionmaker: SM, tmp_path: Path) -> Autonomy:
    return await autonomy(sessionmaker, tmp_path)


def _plan(a: Autonomy) -> uuid.UUID:
    plan_id: uuid.UUID = a.world.plans[(EV, CHARGING)].id
    return plan_id


def _service(a: Autonomy) -> DatabaseOverrideService:
    return DatabaseOverrideService(sessionmaker=a.sessionmaker, settings=a.settings, clock=a.clock)


def _request(
    action: OverrideAction, status: LinkPlanStatus, **kw: object
) -> OperatorOverrideRequest:
    return OperatorOverrideRequest(
        operation_id=cast(uuid.UUID, kw.pop("op", uuid.uuid4())),
        action=action,
        reason=str(kw.pop("reason", "the destination was unprotected by mistake")),
        expected_status=status,
    )


async def _blocked(a: Autonomy, plan_id: uuid.UUID) -> None:
    """Get the plan to ``blocked`` by protecting its destination, the honest way."""
    await protect(a.sessionmaker, a.world.plans[(EV, CHARGING)].target_page_id)
    guarded = a.with_mode("guarded")
    evaluation = await guarded.engine().evaluate_and_record(plan_id=plan_id)
    assert evaluation.decision is AutonomyDecision.BLOCK
    assert BlockCode.PROTECTED_PAGE in evaluation.blocks


async def _unprotect(a: Autonomy) -> None:
    from sqlalchemy import text

    async with a.sessionmaker() as session, session.begin():
        await session.execute(
            text(
                "UPDATE page_protections SET active = false, deactivated_at = now(), "
                "deactivated_by = 'user:otto' WHERE active"
            )
        )


# --------------------------------------------------------------------------- #
# A deterministic block is never overridable
# --------------------------------------------------------------------------- #
async def test_a_publish_override_is_refused_while_a_block_still_fires(
    sessionmaker: SM, a: Autonomy
) -> None:
    plan_id = _plan(a)
    await _blocked(a, plan_id)

    response = await _service(a).override(
        principal=a.world.operator,
        plan_id=plan_id,
        request=_request(OverrideAction.PUBLISH, S.BLOCKED),
    )

    assert response.applied is False
    assert response.refusal == OVERRIDE_BLOCKED_BY_CODE
    assert BlockCode.PROTECTED_PAGE in response.blocks
    assert await plan_status(sessionmaker, plan_id) is S.BLOCKED
    assert len(await decisions(sessionmaker, plan_id)) == 1, "no override row was written"


async def test_once_the_blocking_fact_is_fixed_the_override_publishes_through_every_check(
    sessionmaker: SM, a: Autonomy
) -> None:
    plan_id = _plan(a)
    await _blocked(a, plan_id)
    await _unprotect(a)
    guarded = a.with_mode("guarded")

    response = await _service(guarded).override(
        principal=a.world.operator,
        plan_id=plan_id,
        request=_request(OverrideAction.PUBLISH, S.BLOCKED),
    )

    assert response.applied is True
    assert response.blocks == ()
    assert response.actor == a.world.operator.actor
    assert await plan_status(sessionmaker, plan_id) is S.AUTONOMY_DECIDED
    rows = await decisions(sessionmaker, plan_id)
    assert len(rows) == 2
    assert rows[-1].actor == a.world.operator.actor
    assert rows[-1].override_reason == "the destination was unprotected by mistake"

    # The override is HONOURED by the publisher, and the publish still runs the whole path.
    result = await guarded.publisher().publish_plan(plan_id=plan_id)
    assert result.outcome is PublishOutcome.PUBLISHED, result.reasons
    assert await plan_status(sessionmaker, plan_id) is S.VERIFIED
    walked = await transitions(sessionmaker, plan_id)
    assert walked[-3:] == ["validated", "deployed", "verified"]


async def test_an_override_cannot_publish_a_plan_whose_block_returned(
    sessionmaker: SM, a: Autonomy
) -> None:
    """The publisher re-evaluates: an override recorded before the page was protected again
    does NOT publish."""
    plan_id = _plan(a)
    await _blocked(a, plan_id)
    await _unprotect(a)
    guarded = a.with_mode("guarded")
    await _service(guarded).override(
        principal=a.world.operator,
        plan_id=plan_id,
        request=_request(OverrideAction.PUBLISH, S.BLOCKED),
    )
    await protect(sessionmaker, a.world.plans[(EV, CHARGING)].target_page_id)

    result = await guarded.publisher().publish_plan(plan_id=plan_id)

    assert result.outcome is PublishOutcome.BLOCKED
    assert "protected_page" in result.reasons
    assert await count(sessionmaker, "deployments") == 0


# --------------------------------------------------------------------------- #
# A human may always say no
# --------------------------------------------------------------------------- #
async def test_a_block_override_always_applies(sessionmaker: SM, a: Autonomy) -> None:
    """A human may always say no, at any point before the link is written."""
    plan_id = _plan(a)
    assert await plan_status(sessionmaker, plan_id) is S.PROPOSED

    response = await _service(a).override(
        principal=a.world.operator,
        plan_id=plan_id,
        request=_request(OverrideAction.BLOCK, S.PROPOSED, reason="a human said no"),
    )

    assert response.applied is True
    assert response.plan_status is S.BLOCKED
    assert await plan_status(sessionmaker, plan_id) is S.BLOCKED
    rows = await decisions(sessionmaker, plan_id)
    assert rows[-1].override_reason == "a human said no"
    assert rows[-1].actor == a.world.operator.actor


async def test_a_block_override_stops_a_plan_the_engine_would_have_published(
    sessionmaker: SM, a: Autonomy
) -> None:
    plan_id = _plan(a)
    autonomous = a.with_mode("autonomous", autonomy_autonomous_acknowledged=True)
    await autonomous.engine().evaluate_and_record(plan_id=plan_id)
    assert await plan_status(sessionmaker, plan_id) is S.AUTONOMY_DECIDED

    await _service(autonomous).override(
        principal=a.world.operator,
        plan_id=plan_id,
        request=_request(OverrideAction.BLOCK, S.AUTONOMY_DECIDED, reason="not this one"),
    )

    result = await autonomous.publisher().publish_plan(plan_id=plan_id)
    assert result.outcome is PublishOutcome.DEFERRED
    assert result.reasons == ("plan_not_decidable",)
    assert await count(sessionmaker, "deployments") == 0


# --------------------------------------------------------------------------- #
# Version awareness, idempotency and permission
# --------------------------------------------------------------------------- #
async def test_a_stale_expected_status_is_refused(sessionmaker: SM, a: Autonomy) -> None:
    plan_id = _plan(a)
    await _blocked(a, plan_id)

    response = await _service(a).override(
        principal=a.world.operator,
        plan_id=plan_id,
        request=_request(OverrideAction.PUBLISH, S.AUTONOMY_DECIDED),
    )

    assert response.applied is False
    assert response.refusal == OVERRIDE_STATUS_MOVED


async def test_a_replay_returns_the_stored_result_instead_of_acting_twice(
    sessionmaker: SM, a: Autonomy
) -> None:
    plan_id = _plan(a)
    await _blocked(a, plan_id)
    await _unprotect(a)
    guarded = a.with_mode("guarded")
    request = _request(OverrideAction.PUBLISH, S.BLOCKED)

    first = await _service(guarded).override(
        principal=a.world.operator, plan_id=plan_id, request=request
    )
    again = await _service(guarded).override(
        principal=a.world.operator, plan_id=plan_id, request=request
    )

    assert first.applied and again.applied
    assert again.occurred_at == first.occurred_at
    assert len(await decisions(sessionmaker, plan_id)) == 2, "one override row, not two"


async def test_a_role_without_deploy_cannot_override(sessionmaker: SM, a: Autonomy) -> None:
    plan_id = _plan(a)
    await _blocked(a, plan_id)
    reviewer = await principal(sessionmaker, "rhea", role=Role.REVIEWER)

    with pytest.raises(OverridePermissionError):
        await _service(a).override(
            principal=reviewer,
            plan_id=plan_id,
            request=_request(OverrideAction.PUBLISH, S.BLOCKED),
        )
    assert len(await decisions(sessionmaker, plan_id)) == 1


@pytest.mark.parametrize("action", [OverrideAction.PUBLISH, OverrideAction.BLOCK])
async def test_an_override_on_a_shadow_installation_is_never_a_shadow_decision(
    sessionmaker: SM, a: Autonomy, action: OverrideAction
) -> None:
    """The database says it twice: the deferred guard refuses a shadow decision that MOVED
    its plan, and ``shadow_never_publishes`` refuses a shadow row with a publish decision. A
    human acting on one plan is stepping outside the shadow regime either way."""
    plan_id = _plan(a)
    if action is OverrideAction.PUBLISH:
        await _blocked(a, plan_id)
        await _unprotect(a)
        request = _request(action, S.BLOCKED)
    else:
        request = _request(action, S.PROPOSED, reason="a human said no")

    response = await _service(a).override(
        principal=a.world.operator, plan_id=plan_id, request=request
    )

    assert response.applied is True
    rows = await decisions(sessionmaker, plan_id)
    assert rows[-1].mode == AutonomyMode.GUARDED.value
    assert rows[-1].would_have_decision is None


# --------------------------------------------------------------------------- #
# The reader
# --------------------------------------------------------------------------- #
async def test_the_reader_reports_the_mode_honestly(sessionmaker: SM, a: Autonomy) -> None:
    reader = SqlAutonomyReader(sessionmaker=sessionmaker, settings=a.settings)

    health = await reader.get_health()

    assert health.mode is AutonomyMode.SHADOW
    assert health.publishing_enabled is False, "a shadow installation can publish nothing"
    assert health.autonomous_acknowledged is False
    assert health.allow_production is False
    assert health.policy_version >= 1


async def test_the_reader_lists_decisions_blocked_actions_and_attempts(
    sessionmaker: SM, a: Autonomy
) -> None:
    plan_id = _plan(a)
    await _blocked(a, plan_id)
    autonomous = a.with_mode("autonomous", autonomy_autonomous_acknowledged=True)
    other = a.world.plans[(EV, LI)].id
    await autonomous.publisher().publish_plan(plan_id=other)
    reader = SqlAutonomyReader(sessionmaker=sessionmaker, settings=autonomous.settings)

    listed = await reader.list_decisions(limit=50, offset=0)
    blocked = await reader.list_blocked(limit=50, offset=0)
    attempted = await reader.list_attempts(limit=50, offset=0)
    summary = await reader.get_summary()

    assert listed.page.total >= 2
    assert {v.plan_id for v in listed.items} >= {plan_id, other}
    assert [v.plan_id for v in blocked.items] == [plan_id]
    assert BlockCode.PROTECTED_PAGE in blocked.items[0].blocks
    assert blocked.items[0].overridden is False
    assert [v.plan_id for v in attempted.items] == [other]
    assert summary.blocks_by_code["protected_page"] == 1
    assert summary.auto_published == 1
    assert summary.blocked_plans == 1


async def test_a_blocked_action_reports_itself_overridden_once_a_human_acts(
    sessionmaker: SM, a: Autonomy
) -> None:
    plan_id = _plan(a)
    await _blocked(a, plan_id)
    await _unprotect(a)
    guarded = a.with_mode("guarded")
    await _service(guarded).override(
        principal=a.world.operator,
        plan_id=plan_id,
        request=_request(OverrideAction.PUBLISH, S.BLOCKED),
    )

    reader = SqlAutonomyReader(sessionmaker=sessionmaker, settings=guarded.settings)
    blocked = await reader.list_blocked(limit=50, offset=0)

    assert [v.plan_id for v in blocked.items] == [plan_id]
    assert blocked.items[0].overridden is True, "the append-only row was not edited"
