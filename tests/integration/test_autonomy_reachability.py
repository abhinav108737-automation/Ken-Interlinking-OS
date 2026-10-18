"""Exactly which facts reach each of the three decisions in ``autonomous`` mode, and whether
plain ``AUTO_PUBLISH`` is reachable at all without real history.

The precise statement, proven below against the real calibration reader, risk classifier and
block evaluator on the real corpus:

``AUTO_PUBLISH`` (plain) needs ALL of
    * no deterministic block fired;
    * the plan's decision class is CALIBRATED: a ``policy_evaluations`` run that PASSED its
      gate, finished within the learning policy's maximum evaluation age, carries a row for
      that exact class, whose ``sufficient`` flag is set AND whose ``sample_count`` meets the
      learning policy's floor (30), and which records a ``calibrated_confidence``;
    * the calibrated confidence is at least ``confidence.autonomous_min_confidence``;
    * the risk tier is not ``high`` (no commercial page, protection nearby, earlier failure of
      the pair or measured rollback);
    * the critic review, if there is one, was an ``accept`` that the run handed on (an
      ``uncertain`` review is declined outright, see below);
    * the configured default verification profile is ``standard``.

``AUTO_PUBLISH_WITH_EXTRA_VERIFICATION`` is the same gate with ANY of: no calibration, a high
risk tier, or ``strict`` configured. The safety rules are identical; only
the verification profile differs.

``BLOCK``: a deterministic block, a critic rejection, a confidence under the floor, or no
rollback.

NO DECISION AT ALL: an ``uncertain`` (or failed, or unparseable) critic review is NO_ACTION
always -- the engine raises ``CriticDeclinedError`` and writes no row. It never reaches any of
the three decisions, and in particular never an extra-verification publish.

CONSEQUENCE: on a corpus with no evaluation history every class is uncalibrated, so NO plan
there can reach plain ``AUTO_PUBLISH`` -- only ``AUTO_PUBLISH_WITH_EXTRA_VERIFICATION`` or
``BLOCK``. That is the honest behaviour, not a bug.
"""

from __future__ import annotations

import uuid
from datetime import timedelta
from pathlib import Path

import pytest

from ken_contracts.autonomy import (
    AutonomyDecision,
    AutonomyMode,
    BlockCode,
    RiskTier,
    VerificationProfile,
)
from ken_contracts.learning import GateResult
from ken_core.db.repositories import autonomy as autonomy_repo
from ken_services.autonomy.protocol import AUTONOMY_ACTOR
from tests.integration.autonomy_support import (
    T0,
    Autonomy,
    autonomy,
    protect,
    seed_calibration,
)
from tests.integration.recommendation_support import CHARGING, EV, SM

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("migrated")]

D = AutonomyDecision
PLAIN = D.AUTO_PUBLISH
EXTRA = D.AUTO_PUBLISH_WITH_EXTRA_VERIFICATION
BLOCK = D.BLOCK


@pytest.fixture
async def a(sessionmaker: SM, tmp_path: Path) -> Autonomy:
    return await autonomy(sessionmaker, tmp_path)


def _autonomous(a: Autonomy) -> Autonomy:
    return a.with_mode("autonomous", autonomy_autonomous_acknowledged=True)


def _plan(a: Autonomy) -> uuid.UUID:
    plan_id: uuid.UUID = a.world.plans[(EV, CHARGING)].id
    return plan_id


async def _decide(a: Autonomy, plan_id: uuid.UUID) -> D:
    return (await _autonomous(a).engine().evaluate(plan_id=plan_id)).decision


# --------------------------------------------------------------------------- #
# No history: plain AUTO_PUBLISH is unreachable
# --------------------------------------------------------------------------- #
async def test_with_no_history_no_plan_in_the_corpus_reaches_plain_auto_publish(
    sessionmaker: SM, a: Autonomy
) -> None:
    seen: set[D] = set()
    for (source, target), plan in a.world.plans.items():
        evaluation = await _autonomous(a).engine().evaluate(plan_id=plan.id)
        assert evaluation.calibration_version is None, (source, target)
        assert evaluation.decision is not PLAIN, (source, target, evaluation.confidence)
        seen.add(evaluation.decision)
        if evaluation.decision is EXTRA:
            assert evaluation.verification_profile is VerificationProfile.STRICT
    assert seen <= {EXTRA, BLOCK}
    assert EXTRA in seen, "the corpus must exercise the extra-verification path"


# --------------------------------------------------------------------------- #
# What it takes to reach each decision
# --------------------------------------------------------------------------- #
async def test_a_calibrated_class_is_the_only_way_to_plain_auto_publish(
    sessionmaker: SM, a: Autonomy
) -> None:
    plan_id = _plan(a)
    assert await _decide(a, plan_id) is EXTRA
    await seed_calibration(sessionmaker, plan_id=plan_id, calibrated_confidence=1.0)
    evaluation = await _autonomous(a).engine().evaluate(plan_id=plan_id)
    assert evaluation.decision is PLAIN
    assert evaluation.calibration_version is not None
    assert evaluation.risk_tier is RiskTier.LOW
    assert evaluation.verification_profile is VerificationProfile.STANDARD


async def test_a_class_below_the_learning_sample_floor_is_not_calibrated(
    sessionmaker: SM, a: Autonomy
) -> None:
    """The evaluation says ``sufficient`` but only 10 samples back it; the learning policy's
    floor is 30, so it is NOT a calibration."""
    plan_id = _plan(a)
    await seed_calibration(sessionmaker, plan_id=plan_id, sample_count=10)
    evaluation = await _autonomous(a).engine().evaluate(plan_id=plan_id)
    assert evaluation.calibration_version is None
    assert evaluation.decision is EXTRA


async def test_a_stale_or_failing_evaluation_is_not_a_calibration(
    sessionmaker: SM, a: Autonomy
) -> None:
    plan_id = _plan(a)
    await seed_calibration(sessionmaker, plan_id=plan_id, finished_at=T0 - timedelta(days=60))
    await seed_calibration(sessionmaker, plan_id=plan_id, gate=GateResult.INCONCLUSIVE)
    await seed_calibration(sessionmaker, plan_id=plan_id, gate=GateResult.FAIL)
    assert await _decide(a, plan_id) is EXTRA


async def test_a_calibrated_but_high_risk_plan_still_gets_extra_verification(
    sessionmaker: SM, a: Autonomy
) -> None:
    """An earlier FAILED publish of the pair makes it high risk."""
    plan_id = _plan(a)
    plan = a.world.plans[(EV, CHARGING)]
    await seed_calibration(sessionmaker, plan_id=plan_id, calibrated_confidence=1.0)
    async with sessionmaker() as session, session.begin():
        await autonomy_repo.insert_attempt(
            session,
            plan_id=plan_id,
            source_page_id=plan.source_page_id,
            target_page_id=plan.target_page_id,
            attempt=1,
            max_attempts=3,
            decision=EXTRA,
            mode=AutonomyMode.AUTONOMOUS,
            profile=VerificationProfile.STRICT,
            succeeded=False,
            failure_code="verification_failed",
            rolled_back=True,
            actor=AUTONOMY_ACTOR,
            cooldown_until=None,
            deployment_id=None,
            occurred_at=T0,
        )
    evaluation = await _autonomous(a).engine().evaluate(plan_id=plan_id)
    assert evaluation.risk_tier is RiskTier.HIGH
    assert evaluation.decision is EXTRA


async def test_strict_configured_makes_even_a_calibrated_plan_extra_verification(
    sessionmaker: SM, a: Autonomy
) -> None:
    plan_id = _plan(a)
    await seed_calibration(sessionmaker, plan_id=plan_id, calibrated_confidence=1.0)
    strict = a.with_mode(
        "autonomous", autonomy_autonomous_acknowledged=True, autonomy_verification_profile="strict"
    )
    evaluation = await strict.engine().evaluate(plan_id=plan_id)
    assert evaluation.verification_profile is VerificationProfile.STRICT


async def test_a_confidence_under_the_floor_is_a_block_even_when_calibrated(
    sessionmaker: SM, a: Autonomy
) -> None:
    plan_id = _plan(a)
    await seed_calibration(sessionmaker, plan_id=plan_id, calibrated_confidence=0.30)
    evaluation = await _autonomous(a).engine().evaluate(plan_id=plan_id)
    assert evaluation.confidence < a.policy.confidence.autonomous_min_confidence
    assert evaluation.decision is BLOCK and evaluation.deferred


async def test_a_deterministic_block_outranks_a_perfect_calibration(
    sessionmaker: SM, a: Autonomy
) -> None:
    plan_id = _plan(a)
    await seed_calibration(sessionmaker, plan_id=plan_id, calibrated_confidence=1.0)
    await protect(sessionmaker, a.world.plans[(EV, CHARGING)].target_page_id)
    evaluation = await _autonomous(a).engine().evaluate(plan_id=plan_id)
    assert evaluation.decision is BLOCK
    assert BlockCode.PROTECTED_PAGE in evaluation.blocks
    assert not evaluation.deferred


# --------------------------------------------------------------------------- #
# A legacy `uncertain` review is declined, never decided and never published
# --------------------------------------------------------------------------- #
async def _legacy_uncertain_review(sessionmaker: SM, a: Autonomy, plan_id: uuid.UUID) -> None:
    """The row only data written BEFORE the critic change can carry: ``uncertain`` with
    ``handed_off = true``. Today a fresh uncertain review is ``handed_off = false`` and the
    plan stays ``proposed``."""
    from tests.integration.analytics_support import record_agent_review

    plan = a.world.plans[(EV, CHARGING)]
    await record_agent_review(
        sessionmaker,
        plan_id,
        source_page_id=plan.source_page_id,
        source_url=plan.source_url,
        at=T0,
        verdict="uncertain",
    )


@pytest.mark.parametrize("mode", ["shadow", "guarded", "autonomous"])
async def test_a_legacy_uncertain_review_is_declined_in_every_mode(
    sessionmaker: SM, a: Autonomy, mode: str
) -> None:
    from ken_services.autonomy.engine import CriticDeclinedError
    from tests.integration.autonomy_support import attempts, decisions
    from tests.integration.deployment_support import count, plan_status

    plan_id = _plan(a)
    await _legacy_uncertain_review(sessionmaker, a, plan_id)
    stack = a.with_mode(mode, autonomy_autonomous_acknowledged=True)

    with pytest.raises(CriticDeclinedError):
        await stack.engine().evaluate(plan_id=plan_id)
    with pytest.raises(CriticDeclinedError):
        await stack.engine().evaluate_and_record(plan_id=plan_id)

    assert await decisions(sessionmaker, plan_id) == [], "no decision row, ever"
    assert await attempts(sessionmaker, plan_id) == []
    assert await count(sessionmaker, "deployments") == 0
    assert (await plan_status(sessionmaker, plan_id)).value == "proposed", "the plan did not move"


async def test_the_publisher_and_a_sweep_count_a_legacy_uncertain_review_as_declined(
    sessionmaker: SM, a: Autonomy
) -> None:
    from ken_services.autonomy.job import run_autonomy
    from ken_services.autonomy.publish import PublishOutcome
    from tests.integration.deployment_support import count

    plan_id = _plan(a)
    await _legacy_uncertain_review(sessionmaker, a, plan_id)
    autonomous = _autonomous(a)

    result = await autonomous.publisher().publish_plan(plan_id=plan_id)
    assert result.outcome is PublishOutcome.DECLINED
    assert await count(sessionmaker, "deployments") == 0

    detail = await run_autonomy(
        sessionmaker=sessionmaker,
        settings=autonomous.settings,
        triggered_by="test",
        principal=a.world.operator,
        cms=a.world.cms,
        engine=autonomous.engine(),
        mode=AutonomyMode.AUTONOMOUS,
        plan_ids=[plan_id],
    )
    assert detail.counters["outcome_declined"] == 1
    assert detail.counters.get("outcome_published", 0) == 0
    assert a.world.cms.applied == [], "the CMS was never called"
