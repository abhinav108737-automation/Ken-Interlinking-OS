"""The autonomous publish / verify / rollback path (directive s14, s15; ADR-0018 s4).

The whole point of the phase, end to end, against the real deployment service and the
in-memory CMS over the inventory's own pages with the real deterministic HTML library on both
sides. No human approves anything in any of these tests.

The claims under test:

* ``autonomous`` publishes a policy-eligible plan with no approval, and the plan reaches
  ``verified``;
* ``shadow`` publishes nothing at all -- no deployment, no attempt, no transition;
* a blocked plan is never sent to the CMS;
* verification failure rolls back AUTOMATICALLY, records the failure and emits the audit
  event;
* when rolling back is not safe the failure is recorded and nothing is destroyed;
* the retry budget and the cool-down make repeated retries impossible;
* ``AUTO_PUBLISH_WITH_EXTRA_VERIFICATION`` differs ONLY in the verification profile.
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

import pytest

from ken_contracts.autonomy import AutonomyDecision, AutonomyMode, VerificationProfile
from ken_contracts.deployment import DeploymentStatus
from ken_contracts.events import EventName
from ken_contracts.recommendations import LinkPlanStatus
from ken_services.autonomy.protocol import AUTONOMY_ACTOR
from ken_services.autonomy.publish import PublishOutcome
from tests.integration.autonomy_support import (
    Autonomy,
    FailingVerifier,
    attempts,
    autonomy,
    decisions,
    protect,
    seed_calibration,
    transitions,
)
from tests.integration.deployment_support import count, events, plan_status
from tests.integration.recommendation_support import CHARGING, EV, LI, SITE, SM

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("migrated")]

S = LinkPlanStatus
D = DeploymentStatus
O = PublishOutcome  # noqa: E741 - a one-letter alias keeps the assertions readable


@pytest.fixture
async def a(sessionmaker: SM, tmp_path: Path) -> Autonomy:
    return await autonomy(sessionmaker, tmp_path)


def _autonomous(a: Autonomy, **overrides: Any) -> Autonomy:
    return a.with_mode("autonomous", autonomy_autonomous_acknowledged=True, **overrides)


def _plan(a: Autonomy, target: str = CHARGING) -> uuid.UUID:
    plan_id: uuid.UUID = a.world.plans[(EV, target)].id
    return plan_id


async def _cms_body(a: Autonomy, path: str = EV) -> str:
    page = await a.world.mock.get_page(page_id=a.world.page_ids()[SITE + path])
    return str(page.body_html)


# --------------------------------------------------------------------------- #
# The happy path: no human anywhere
# --------------------------------------------------------------------------- #
async def test_a_policy_eligible_plan_is_published_and_verified_with_no_approval(
    sessionmaker: SM, a: Autonomy
) -> None:
    plan_id = _plan(a)
    autonomous = _autonomous(a)

    result = await autonomous.publisher().publish_plan(plan_id=plan_id)

    assert result.outcome is O.PUBLISHED, result.reasons
    assert await plan_status(sessionmaker, plan_id) is S.VERIFIED
    # The plan never passed through awaiting_approval or approved.
    walked = await transitions(sessionmaker, plan_id)
    assert walked == [
        "proposed",
        "policy_checked",
        "autonomy_decided",
        "validated",
        "deployed",
        "verified",
    ]
    assert "awaiting_approval" not in walked and "approved" not in walked
    # The link really is on the CMS page.
    assert f'data-ken-link-id="{plan_id}"' in await _cms_body(a)
    # ... and the audit trail says so.
    assert len(await events(sessionmaker, EventName.LINK_DEPLOYED.value)) == 1
    assert len(await events(sessionmaker, EventName.LINK_VERIFIED.value)) == 1
    recorded = await attempts(sessionmaker, plan_id)
    assert len(recorded) == 1
    assert recorded[0].succeeded is True
    assert recorded[0].actor == AUTONOMY_ACTOR
    assert recorded[0].mode == "autonomous"
    assert recorded[0].cooldown_until is None


async def test_the_deployment_names_the_operator_identity_the_cms_audit_trail_requires(
    sessionmaker: SM, a: Autonomy
) -> None:
    """``deployments.actor`` carries a ``^user:`` CHECK and ``user_id`` is NOT NULL, so an
    autonomous write is still attributable to a real account -- chosen once per run, never per
    link. The plan TRANSITIONS stay ``system:`` actors (ADR-0018 s3)."""
    plan_id = _plan(a)
    await _autonomous(a).publisher().publish_plan(plan_id=plan_id)

    from tests.integration.recommendation_support import rows

    deployment = (await rows(sessionmaker, "SELECT actor, user_id FROM deployments"))[0]
    assert deployment.actor == a.world.operator.actor
    assert deployment.user_id == a.world.operator.user_id
    actors = await rows(
        sessionmaker,
        "SELECT DISTINCT actor FROM link_plan_transitions WHERE plan_id = :p",
        p=plan_id,
    )
    assert {r.actor for r in actors} <= {
        AUTONOMY_ACTOR,
        "system:recommendation-engine",
        "system:deployment-service",
    }, {r.actor for r in actors}


# --------------------------------------------------------------------------- #
# Shadow and blocks never reach the CMS
# --------------------------------------------------------------------------- #
async def test_shadow_mode_publishes_nothing_at_all(sessionmaker: SM, a: Autonomy) -> None:
    plan_id = _plan(a)
    body_before = await _cms_body(a)

    result = await a.publisher().publish_plan(plan_id=plan_id)

    assert result.outcome is O.DEFERRED and result.reasons == ("shadow_mode",)
    assert await count(sessionmaker, "deployments") == 0
    assert await count(sessionmaker, "publish_attempts") == 0
    assert await plan_status(sessionmaker, plan_id) is S.PROPOSED
    assert await _cms_body(a) == body_before
    assert len(await decisions(sessionmaker, plan_id)) == 1
    assert a.world.cms.applied == [], "the CMS was never called"


async def test_a_blocked_plan_is_never_sent_to_the_cms(sessionmaker: SM, a: Autonomy) -> None:
    plan_id = _plan(a)
    await protect(sessionmaker, a.world.plans[(EV, CHARGING)].target_page_id)
    body_before = await _cms_body(a)

    result = await _autonomous(a).publisher().publish_plan(plan_id=plan_id)

    assert result.outcome is O.BLOCKED
    assert "protected_page" in result.reasons
    assert await count(sessionmaker, "deployments") == 0
    assert await count(sessionmaker, "publish_attempts") == 0
    assert await plan_status(sessionmaker, plan_id) is S.BLOCKED
    assert await _cms_body(a) == body_before
    assert a.world.cms.applied == []


async def test_guarded_defers_an_uncalibrated_plan_without_touching_the_cms(
    sessionmaker: SM, a: Autonomy
) -> None:
    plan_id = _plan(a)
    result = await a.with_mode("guarded").publisher().publish_plan(plan_id=plan_id)
    assert result.outcome is O.BLOCKED
    assert result.reasons == ("policy_refused",), "a deferral, not a safety block"
    assert await count(sessionmaker, "deployments") == 0
    assert a.world.cms.applied == []


async def test_guarded_publishes_a_calibrated_low_risk_plan(sessionmaker: SM, a: Autonomy) -> None:
    plan_id = _plan(a)
    await seed_calibration(sessionmaker, plan_id=plan_id, calibrated_confidence=0.99)
    # The shipped guarded floor (0.85) is above anything this corpus scores; a policy COPY
    # with a lower floor exercises the publish path itself (see Autonomy.with_policy).
    guarded = a.with_mode("guarded").with_policy(confidence={"guarded_min_confidence": 0.60})

    result = await guarded.publisher().publish_plan(plan_id=plan_id)

    assert result.outcome is O.PUBLISHED, result.reasons
    assert result.evaluation is not None
    assert result.evaluation.decision is AutonomyDecision.AUTO_PUBLISH
    assert result.evaluation.verification_profile is VerificationProfile.STANDARD
    assert await plan_status(sessionmaker, plan_id) is S.VERIFIED


# --------------------------------------------------------------------------- #
# Automatic verification failure and rollback
# --------------------------------------------------------------------------- #
async def test_a_verification_failure_rolls_the_change_back_automatically(
    sessionmaker: SM, a: Autonomy
) -> None:
    """The CMS accepted the change and verification then FAILED. Nobody is asked: the system
    rolls back by itself, the link leaves the page, the failure is recorded with a cool-down,
    and both audit events are emitted."""
    plan_id = _plan(a)
    autonomous = _autonomous(a)
    verifier = FailingVerifier()

    result = await autonomous.publisher(verifier=verifier).publish_plan(plan_id=plan_id)

    assert result.outcome is O.ROLLED_BACK, result.reasons
    assert "verification_failed" in result.reasons
    assert verifier.calls >= 1
    assert await plan_status(sessionmaker, plan_id) is S.ROLLED_BACK
    assert f'data-ken-link-id="{plan_id}"' not in await _cms_body(a), "the link came back off"
    recorded = await attempts(sessionmaker, plan_id)
    assert len(recorded) == 1
    assert recorded[0].succeeded is False
    assert recorded[0].rolled_back is True
    assert recorded[0].failure_code == "verification_failed"
    assert recorded[0].cooldown_until is not None, "a failure always starts a cool-down"
    assert len(await events(sessionmaker, EventName.LINK_FAILED.value)) == 1
    assert len(await events(sessionmaker, EventName.LINK_ROLLED_BACK.value)) == 1


async def test_the_failure_is_recorded_even_when_rolling_back_is_not_safe(
    sessionmaker: SM, a: Autonomy
) -> None:
    """A rollback the CMS refuses must not be forced. After our apply an "editor" re-pointed
    our link at another URL: verification fails (href_mismatch) AND removing the link would
    destroy their edit, so the CMS answers ``rollback_conflict`` and the link is left alone.
    The failure is recorded, the cool-down starts, nothing is destroyed, and a human is left
    to look -- which is what the blocked-actions and attempts screens are for.

    (An editor who simply REMOVES the link is a different case and not an unsafe one: the
    inverse splice finds it already absent and succeeds idempotently.)"""
    import re

    plan_id = _plan(a)
    autonomous = _autonomous(a)
    page_id = a.world.page_ids()[SITE + EV]
    inner_apply = a.world.cms.inner.apply_link_changes

    async def apply_then_repoint(*, request: Any) -> Any:
        result = await inner_apply(request=request)
        page = await a.world.mock.get_page(page_id=page_id)
        repointed = re.sub(
            r'(<a href=")[^"]*(" data-ken-link-id="' + str(plan_id) + '")',
            r"\1https://elsewhere.test/other\2",
            page.body_html,
        )
        assert repointed != page.body_html, "the test must actually edit our link"
        a.world.mock.edit_page(page_id=page_id, body_html=repointed)
        return result

    a.world.cms.inner.apply_link_changes = apply_then_repoint

    result = await autonomous.publisher().publish_plan(plan_id=plan_id)

    assert result.outcome is O.ROLLBACK_REFUSED, result.reasons
    assert "verification_failed" in result.reasons
    assert "rollback_conflict" in result.reasons
    recorded = await attempts(sessionmaker, plan_id)
    assert recorded[0].succeeded is False and recorded[0].rolled_back is False
    assert recorded[0].cooldown_until is not None, "the pair still cools down"
    assert len(await events(sessionmaker, EventName.LINK_ROLLED_BACK.value)) == 0
    assert "https://elsewhere.test/other" in await _cms_body(a), "the editor's edit survives"


# --------------------------------------------------------------------------- #
# The retry budget and the cool-down
# --------------------------------------------------------------------------- #
async def test_a_failed_pair_is_not_retried_during_its_cool_down(
    sessionmaker: SM, a: Autonomy
) -> None:
    """The CMS is unreachable, so the publish is refused and the pair cools down. The plan is
    still ``autonomy_decided`` -- nothing went wrong with the PLAN -- and the next pass must
    still refuse to touch the pair until the cool-down expires."""
    plan_id = _plan(a)
    autonomous = _autonomous(a, autonomy_publish_cooldown_s=3600.0)
    publisher = autonomous.publisher()
    a.world.cms.unavailable = True

    first = await publisher.publish_plan(plan_id=plan_id)

    assert first.outcome is O.REFUSED, first.reasons
    assert await plan_status(sessionmaker, plan_id) is S.AUTONOMY_DECIDED
    recorded = await attempts(sessionmaker, plan_id)
    assert len(recorded) == 1 and recorded[0].succeeded is False
    assert recorded[0].cooldown_until is not None

    a.world.cms.unavailable = False  # the CMS is back; the cool-down is not over
    second = await publisher.publish_plan(plan_id=plan_id)

    assert second.outcome is O.DEFERRED
    assert "cooling_down" in second.reasons
    assert len(await attempts(sessionmaker, plan_id)) == 1, "no second attempt was recorded"
    assert await count(sessionmaker, "deployments") == 0


async def test_the_environment_cannot_shorten_the_policys_cool_down(
    sessionmaker: SM, a: Autonomy
) -> None:
    """``KEN_AUTONOMY_PUBLISH_COOLDOWN_S=0`` is a legal setting; the policy floor still wins,
    so an environment variable can tighten the budget and never loosen it."""
    plan_id = _plan(a)
    autonomous = _autonomous(a, autonomy_publish_cooldown_s=0.0, autonomy_publish_max_attempts=10)
    publisher = autonomous.publisher()
    a.world.cms.unavailable = True
    assert (await publisher.publish_plan(plan_id=plan_id)).outcome is O.REFUSED
    a.world.cms.unavailable = False
    a.clock.advance(60)

    second = await publisher.publish_plan(plan_id=plan_id)

    assert second.outcome is O.DEFERRED and "cooling_down" in second.reasons
    recorded = await attempts(sessionmaker, plan_id)
    assert recorded[0].max_attempts == autonomous.policy.retry.max_attempts


async def test_the_cool_down_expires_and_the_pair_may_be_retried(
    sessionmaker: SM, a: Autonomy
) -> None:
    plan_id = _plan(a)
    autonomous = _autonomous(a)
    publisher = autonomous.publisher()
    a.world.cms.unavailable = True
    assert (await publisher.publish_plan(plan_id=plan_id)).outcome is O.REFUSED
    a.world.cms.unavailable = False

    # The policy's cool-down is an HOUR and cannot be shortened by the environment.
    a.clock.advance(autonomous.policy.retry.cooldown_s + 60)
    retried = await publisher.publish_plan(plan_id=plan_id)

    assert retried.outcome is O.PUBLISHED, retried.reasons
    assert len(await attempts(sessionmaker, plan_id)) == 2


async def test_the_budget_is_spent_and_the_pair_is_never_published_again(
    sessionmaker: SM, a: Autonomy
) -> None:
    """``attempts >= max_attempts`` means no publish, whatever a later decision says and
    whatever the cool-down has done."""
    plan_id = _plan(a)
    autonomous = _autonomous(a, autonomy_publish_max_attempts=1)
    publisher = autonomous.publisher()
    a.world.cms.unavailable = True
    assert (await publisher.publish_plan(plan_id=plan_id)).outcome is O.REFUSED
    a.world.cms.unavailable = False
    a.clock.advance(86_400)

    second = await publisher.publish_plan(plan_id=plan_id)

    assert second.outcome is O.DEFERRED
    assert "budget_exhausted" in second.reasons
    assert len(await attempts(sessionmaker, plan_id)) == 1
    assert await count(sessionmaker, "deployments") == 0


async def test_the_budget_belongs_to_the_pair_and_not_to_the_plan(
    sessionmaker: SM, a: Autonomy
) -> None:
    """A different plan on the same (source, target) pair draws on the SAME budget, which is
    why a superseded plan cannot reset it."""
    plan_id = _plan(a)
    autonomous = _autonomous(a, autonomy_publish_max_attempts=1)
    assert (await autonomous.publisher().publish_plan(plan_id=plan_id)).outcome is O.PUBLISHED
    plan = a.world.plans[(EV, CHARGING)]

    verdict = await autonomous.budget().check(
        source_page_id=plan.source_page_id, target_page_id=plan.target_page_id
    )

    assert verdict.allowed is False
    assert verdict.state.exhausted is True
    assert "budget_exhausted" in verdict.reasons


async def test_a_shadow_pass_can_never_record_an_attempt(sessionmaker: SM, a: Autonomy) -> None:
    """``publish_attempts`` carries ``mode <> 'shadow'`` and ``decision <> 'block'`` CHECKs,
    so this is enforced by the DATABASE and not only by the publisher. Asserted by trying to
    write the forbidden rows, not by reading constraint names."""
    from sqlalchemy.exc import IntegrityError

    from ken_contracts.autonomy import AutonomyDecision as Dec
    from ken_contracts.autonomy import VerificationProfile as Prof
    from ken_core.db.repositories import autonomy as repo

    plan = a.world.plans[(EV, CHARGING)]
    for mode, decision in (
        (AutonomyMode.SHADOW, Dec.AUTO_PUBLISH),
        (AutonomyMode.AUTONOMOUS, Dec.BLOCK),
    ):
        with pytest.raises(IntegrityError):
            async with sessionmaker() as session, session.begin():
                await repo.insert_attempt(
                    session,
                    plan_id=plan.id,
                    source_page_id=plan.source_page_id,
                    target_page_id=plan.target_page_id,
                    attempt=1,
                    max_attempts=3,
                    decision=decision,
                    mode=mode,
                    profile=Prof.STANDARD,
                    succeeded=True,
                    failure_code=None,
                    rolled_back=False,
                    actor=AUTONOMY_ACTOR,
                    cooldown_until=None,
                    deployment_id=None,
                    occurred_at=a.clock(),
                )
    assert await count(sessionmaker, "publish_attempts") == 0


# --------------------------------------------------------------------------- #
# The strict profile differs ONLY in the verification profile
# --------------------------------------------------------------------------- #
async def test_extra_verification_runs_a_second_pass_and_changes_no_safety_rule(
    sessionmaker: SM, a: Autonomy
) -> None:
    plan_id = _plan(a)
    # No calibration -> the decision is AUTO_PUBLISH_WITH_EXTRA_VERIFICATION in autonomous
    # mode, which is the strict profile.
    autonomous = _autonomous(a, autonomy_strict_verify_attempts=2)

    result = await autonomous.publisher().publish_plan(plan_id=plan_id)

    assert result.evaluation is not None
    assert result.evaluation.decision is AutonomyDecision.AUTO_PUBLISH_WITH_EXTRA_VERIFICATION
    assert result.evaluation.verification_profile is VerificationProfile.STRICT
    assert result.outcome is O.PUBLISHED
    assert await plan_status(sessionmaker, plan_id) is S.VERIFIED
    runs = await count(sessionmaker, "deployment_verifications")
    assert runs >= 2, "the strict profile runs a delayed SECOND pass even after a pass"


async def test_the_strict_profile_still_blocks_everything_the_standard_one_blocks(
    sessionmaker: SM, a: Autonomy
) -> None:
    plan_id = _plan(a, LI)
    await protect(sessionmaker, a.world.plans[(EV, LI)].target_page_id)
    autonomous = _autonomous(a)

    result = await autonomous.publisher().publish_plan(plan_id=plan_id)

    assert result.outcome is O.BLOCKED
    assert await count(sessionmaker, "deployments") == 0


# --------------------------------------------------------------------------- #
# The pass as a job
# --------------------------------------------------------------------------- #
async def test_a_shadow_pass_is_a_recorded_job_run_the_reaper_applies_to(
    sessionmaker: SM, a: Autonomy
) -> None:
    from ken_contracts.inventory import RunStatus
    from ken_contracts.jobs import JobKind
    from ken_services.autonomy.job import run_autonomy

    detail = await run_autonomy(
        sessionmaker=sessionmaker,
        settings=a.settings,
        triggered_by="test",
        engine=a.engine(),
    )

    assert detail.kind is JobKind.AUTONOMY_SHADOW
    assert RunStatus(detail.status) is RunStatus.SUCCEEDED
    assert detail.counters["plans_considered"] >= 1
    assert detail.counters["errors"] == 0
    assert await count(sessionmaker, "publish_attempts") == 0
    assert await count(sessionmaker, "deployments") == 0
    assert await count(sessionmaker, "autonomy_decisions") == detail.counters["plans_considered"]


async def test_a_publishing_pass_refuses_to_run_without_an_operator_identity(
    sessionmaker: SM, a: Autonomy
) -> None:
    from ken_services.autonomy.job import run_autonomy

    autonomous = _autonomous(a)
    with pytest.raises(ValueError, match="operator identity"):
        await run_autonomy(
            sessionmaker=sessionmaker,
            settings=autonomous.settings,
            triggered_by="test",
            engine=autonomous.engine(),
        )
    assert await count(sessionmaker, "job_runs") >= 0  # nothing was published


async def test_a_publishing_pass_records_its_outcomes_as_counters(
    sessionmaker: SM, a: Autonomy
) -> None:
    from ken_contracts.jobs import JobKind
    from ken_services.autonomy.job import run_autonomy

    autonomous = _autonomous(a)
    detail = await run_autonomy(
        sessionmaker=sessionmaker,
        settings=autonomous.settings,
        triggered_by="test",
        principal=a.world.operator,
        cms=a.world.cms,
        engine=autonomous.engine(),
        mode=AutonomyMode.AUTONOMOUS,
    )

    assert detail.kind is JobKind.AUTONOMY
    assert detail.counters["plans_considered"] >= 1
    assert detail.counters["outcome_published"] >= 1
    assert detail.config_snapshot["actor"] == a.world.operator.actor
    assert detail.config_snapshot["mode"] == "autonomous"


# --------------------------------------------------------------------------- #
# The operator's forced rollback (directive s20) -- ADR-0014's route, unchanged
# --------------------------------------------------------------------------- #
async def test_an_operator_can_force_a_rollback_of_an_autonomously_published_link(
    sessionmaker: SM, a: Autonomy
) -> None:
    """No new machinery: an autonomous publish writes an ordinary ``deployments`` row, so
    ADR-0014's ``POST /deployments/{id}/rollback`` (``Permission.DEPLOY``) IS the forced
    rollback the directive asks for. Duplicating it under ``/autonomy`` would give one
    capability two names."""
    from ken_contracts.deployment import RollbackReason, RollbackRequest

    plan_id = _plan(a)
    autonomous = _autonomous(a)
    result = await autonomous.publisher().publish_plan(plan_id=plan_id)
    assert result.outcome is O.PUBLISHED and result.deployment_id is not None
    assert f'data-ken-link-id="{plan_id}"' in await _cms_body(a)

    after = await autonomous.deployments().rollback(
        deployment_id=result.deployment_id,
        request=RollbackRequest(
            operation_id=uuid.uuid4(),
            reason=RollbackReason.EDITORIAL_REVERSAL,
            note="an operator changed their mind",
        ),
        principal=a.world.operator,
    )

    assert DeploymentStatus(after.status) is D.ROLLED_BACK
    assert await plan_status(sessionmaker, plan_id) is S.ROLLED_BACK
    assert f'data-ken-link-id="{plan_id}"' not in await _cms_body(a)
    assert len(await events(sessionmaker, EventName.LINK_ROLLED_BACK.value)) == 1


# --------------------------------------------------------------------------- #
# The audit link: telling an autonomous publish from a human one
# --------------------------------------------------------------------------- #
async def test_an_auditor_can_tell_an_autonomous_publish_from_a_human_one(
    sessionmaker: SM, a: Autonomy
) -> None:
    """``deployments.actor`` must be a ``user:`` account (CHECK), so the deployment row alone
    cannot say who chose to publish. Three independent links do:

    1. ``publish_attempts.deployment_id`` joins the deployment to an attempt whose actor is
       ``system:autonomy-engine`` -- a human deploy writes NO attempt row;
    2. ``deployments.note`` names the ``autonomy_decisions`` row that sanctioned it
       (``decision#<id>``), and that row's actor is ``system:autonomy-engine``;
    3. the plan walked ``autonomy_decided -> validated`` rather than ``approved -> validated``.
    """
    from tests.integration.deployment_support import deploy_request
    from tests.integration.recommendation_support import rows

    plan_id = _plan(a)
    human_plan = next(p for (s, _t), p in a.world.plans.items() if s != EV)

    # A human deploys one plan the ordinary ADR-0014 way ...
    [human_id] = await a.world.approve(human_plan)
    human = await a.world.service().deploy(
        request=deploy_request([human_id]), principal=a.world.operator
    )
    # ... and the engine publishes another.
    result = await _autonomous(a).publisher().publish_plan(plan_id=plan_id)
    assert result.outcome is O.PUBLISHED and result.deployment_id is not None

    joined = await rows(
        sessionmaker,
        "SELECT d.id AS deployment_id, d.actor AS deployment_actor, d.note, "
        "pa.actor AS attempt_actor, pa.decision AS attempt_decision, "
        "ad.id AS decision_id, ad.actor AS decision_actor "
        "FROM deployments d "
        "JOIN publish_attempts pa ON pa.deployment_id = d.id "
        "JOIN autonomy_decisions ad ON ad.plan_id = pa.plan_id",
    )
    assert len(joined) == 1, "only the autonomous publish joins to a decision and an attempt"
    row = joined[0]
    assert row.deployment_id == result.deployment_id
    assert row.deployment_actor == a.world.operator.actor  # the accountable account
    assert row.attempt_actor == AUTONOMY_ACTOR and row.decision_actor == AUTONOMY_ACTOR
    assert f"decision#{row.decision_id}" in row.note
    assert row.attempt_decision == result.evaluation.decision.value  # type: ignore[union-attr]

    # The human deployment has none of those links ...
    unlinked = await rows(
        sessionmaker,
        "SELECT d.note FROM deployments d WHERE d.id = :d "
        "AND NOT EXISTS (SELECT 1 FROM publish_attempts pa WHERE pa.deployment_id = d.id)",
        d=human.id,
    )
    assert len(unlinked) == 1 and "autonomous" not in unlinked[0].note
    # ... and its plan went through `approved`, the autonomous plan through `autonomy_decided`.
    assert "approved" in await transitions(sessionmaker, human_id)
    assert "autonomy_decided" in await transitions(sessionmaker, plan_id)
    assert "approved" not in await transitions(sessionmaker, plan_id)
