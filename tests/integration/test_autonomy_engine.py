"""The autonomy engine on the ``*_test`` database (ADR-0018 s1, s2, s3, s6).

Facts come from the real inventory, the real analyses and the real protections; the plans are
real engine plans. What the tests vary is the MODE and the stored facts.

The claims under test:

* a shadow pass writes ONE ``autonomy_decisions`` row and NOTHING else -- no deployment, no
  publish attempt, no plan transition (the deferred database guard enforces the last one);
* an active protection BLOCKS, and the block is terminal for the engine;
* a publish decision walks the ADR-0018 s3 ladder in one transaction;
* ``guarded`` refuses an uncalibrated class and publishes a calibrated, low-risk one;
* ``autonomous`` publishes but still applies every hard rule;
* re-running a pass is idempotent.
"""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest

from ken_contracts.autonomy import (
    AutonomyDecision,
    AutonomyMode,
    BlockCode,
    CriticOutcome,
    RiskTier,
    VerificationProfile,
)
from ken_contracts.recommendations import LinkPlanStatus
from ken_services.autonomy.protocol import AUTONOMY_ACTOR, ENGINE_VERSION
from tests.integration.autonomy_support import (
    Autonomy,
    attempts,
    autonomy,
    decisions,
    protect,
    seed_calibration,
    to_policy_checked,
    transitions,
)
from tests.integration.deployment_support import count
from tests.integration.recommendation_support import CHARGING, EV, LI, SM

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("migrated")]

S = LinkPlanStatus
D = AutonomyDecision


@pytest.fixture
async def a(sessionmaker: SM, tmp_path: Path) -> Autonomy:
    return await autonomy(sessionmaker, tmp_path)


def _plan(a: Autonomy, target: str = CHARGING) -> uuid.UUID:
    plan_id: uuid.UUID = a.world.plans[(EV, target)].id
    return plan_id


# --------------------------------------------------------------------------- #
# Shadow: the default, and it writes one thing
# --------------------------------------------------------------------------- #
async def test_the_default_mode_is_shadow_and_it_publishes_nothing(
    sessionmaker: SM, a: Autonomy
) -> None:
    assert a.settings.autonomy_mode == "shadow"
    plan_id = _plan(a)
    await to_policy_checked(sessionmaker, plan_id)
    before = {
        t: await count(sessionmaker, t)
        for t in ("deployments", "publish_attempts", "link_plan_transitions")
    }

    evaluation = await a.engine().evaluate_and_record(plan_id=plan_id)

    assert evaluation.mode is AutonomyMode.SHADOW
    assert evaluation.decision is D.BLOCK, "shadow publishes nothing, ever"
    assert evaluation.would_have_decision is not None, "the counterfactual is the whole point"
    rows = await decisions(sessionmaker, plan_id)
    assert len(rows) == 1
    assert rows[0].actor == AUTONOMY_ACTOR
    assert rows[0].engine_version == ENGINE_VERSION
    assert rows[0].override_reason is None
    after = {
        t: await count(sessionmaker, t)
        for t in ("deployments", "publish_attempts", "link_plan_transitions")
    }
    assert after == before, "a shadow pass moves nothing and attempts nothing"
    assert await transitions(sessionmaker, plan_id) == ["proposed", "policy_checked"]


async def test_a_shadow_decision_that_would_publish_records_the_publish_counterfactual(
    sessionmaker: SM, a: Autonomy
) -> None:
    plan_id = _plan(a)
    await to_policy_checked(sessionmaker, plan_id)
    evaluation = await a.engine().evaluate_and_record(plan_id=plan_id)
    assert evaluation.decision is D.BLOCK
    assert evaluation.would_have_decision in {
        D.AUTO_PUBLISH,
        D.AUTO_PUBLISH_WITH_EXTRA_VERIFICATION,
    }
    assert evaluation.blocks == ()


# --------------------------------------------------------------------------- #
# The deterministic layer, against real stored facts
# --------------------------------------------------------------------------- #
async def test_an_active_protection_blocks_and_the_plan_becomes_blocked(
    sessionmaker: SM, a: Autonomy
) -> None:
    plan_id = _plan(a)
    await to_policy_checked(sessionmaker, plan_id)
    await protect(sessionmaker, a.world.plans[(EV, CHARGING)].target_page_id)
    guarded = a.with_mode("guarded")

    evaluation = await guarded.engine().evaluate_and_record(plan_id=plan_id)

    assert evaluation.decision is D.BLOCK
    assert BlockCode.PROTECTED_PAGE in evaluation.blocks
    assert (await transitions(sessionmaker, plan_id))[-1] == "blocked"
    assert await attempts(sessionmaker, plan_id) == [], "a block is never attempted"


async def test_the_blocks_are_computed_with_no_model_call(sessionmaker: SM, a: Autonomy) -> None:
    """The same answer with the model server switched off: there is no model provider in the
    block evaluator's constructor at all, so this is a property of the build."""
    plan_id = _plan(a)
    evaluator = a.blocks()
    facts = await evaluator.facts(plan_id=plan_id)
    assert evaluator.evaluate(facts) == evaluator.evaluate(facts) == ()
    await protect(sessionmaker, a.world.plans[(EV, CHARGING)].source_page_id)
    again = await evaluator.facts(plan_id=plan_id)
    assert BlockCode.PROTECTED_PAGE in evaluator.evaluate(again)


async def test_a_plan_the_engine_has_blocked_is_not_decidable_again(
    sessionmaker: SM, a: Autonomy
) -> None:
    """``blocked`` is terminal for the ENGINE: only an operator override re-enters."""
    plan_id = _plan(a)
    await to_policy_checked(sessionmaker, plan_id)
    await protect(sessionmaker, a.world.plans[(EV, CHARGING)].target_page_id)
    engine = a.with_mode("guarded").engine()
    await engine.evaluate_and_record(plan_id=plan_id)

    assert plan_id not in await engine.decidable_plans()


# --------------------------------------------------------------------------- #
# The ladder
# --------------------------------------------------------------------------- #
async def test_a_publish_decision_walks_the_ladder_in_one_transaction(
    sessionmaker: SM, a: Autonomy
) -> None:
    """``proposed -> policy_checked -> autonomy_decided``: the deterministic block evaluation
    IS the policy check, and the deferred guard requires the move and the row together."""
    plan_id = _plan(a)
    guarded = a.with_mode("autonomous", autonomy_autonomous_acknowledged=True)

    evaluation = await guarded.engine().evaluate_and_record(plan_id=plan_id)

    assert evaluation.decision is not D.BLOCK
    assert await transitions(sessionmaker, plan_id) == [
        "proposed",
        "policy_checked",
        "autonomy_decided",
    ]
    rows = await decisions(sessionmaker, plan_id)
    assert len(rows) == 1 and rows[0].decision == evaluation.decision.value


async def test_re_running_a_pass_on_a_decided_plan_records_nothing_new(
    sessionmaker: SM, a: Autonomy
) -> None:
    plan_id = _plan(a)
    engine = a.with_mode("autonomous", autonomy_autonomous_acknowledged=True).engine()
    await engine.evaluate_and_record(plan_id=plan_id)
    await engine.evaluate_and_record(plan_id=plan_id)

    assert len(await decisions(sessionmaker, plan_id)) == 1
    assert await transitions(sessionmaker, plan_id) == [
        "proposed",
        "policy_checked",
        "autonomy_decided",
    ]


async def test_a_decided_plan_whose_facts_turned_bad_is_blocked_by_the_next_pass(
    sessionmaker: SM, a: Autonomy
) -> None:
    """This is why ``autonomy_decided`` stays decidable: a decision minutes old must not
    publish a link onto a page that has been protected since."""
    plan_id = _plan(a)
    engine = a.with_mode("autonomous", autonomy_autonomous_acknowledged=True).engine()
    await engine.evaluate_and_record(plan_id=plan_id)
    await protect(sessionmaker, a.world.plans[(EV, CHARGING)].target_page_id)

    again = await engine.evaluate_and_record(plan_id=plan_id)

    assert again.decision is D.BLOCK
    assert BlockCode.PROTECTED_PAGE in again.blocks
    assert (await transitions(sessionmaker, plan_id))[-1] == "blocked"


# --------------------------------------------------------------------------- #
# Calibration and the guarded floor
# --------------------------------------------------------------------------- #
async def test_guarded_refuses_an_uncalibrated_class(sessionmaker: SM, a: Autonomy) -> None:
    plan_id = _plan(a)
    evaluation = await a.with_mode("guarded").engine().evaluate_and_record(plan_id=plan_id)
    assert evaluation.calibration_version is None, "no evaluation row exists for this class"
    assert evaluation.decision is D.BLOCK
    assert evaluation.blocks == (), "it is a deferral, not a safety block"
    assert evaluation.risk_tier is not RiskTier.LOW, "an uncalibrated class is not low risk"


async def test_guarded_publishes_nothing_on_the_demo_corpus_even_when_calibrated(
    sessionmaker: SM, a: Autonomy
) -> None:
    """THE HONEST OUTCOME. The shipped policy asks ``guarded`` for a calibrated confidence of
    0.85. Calibration can only LOWER a score, and the best pairs in this synthetic corpus
    score about 0.65, so no plan here can clear the floor however good its class's history.
    The floor was chosen for what 'high confidence' means, not so that this corpus publishes;
    ``guarded`` publishing nothing on it is correct, and that is stated, not hidden."""
    guarded = a.with_mode("guarded")
    for (source, target), plan in a.world.plans.items():
        await seed_calibration(sessionmaker, plan_id=plan.id, calibrated_confidence=1.0)
        evaluation = await guarded.engine().evaluate(plan_id=plan.id)
        assert evaluation.calibration_version is not None, (source, target)
        assert evaluation.confidence < guarded.policy.confidence.guarded_min_confidence
        assert evaluation.decision is D.BLOCK and evaluation.deferred, (source, target)


async def test_guarded_publishes_a_calibrated_low_risk_plan_above_the_floor(
    sessionmaker: SM, a: Autonomy
) -> None:
    """The guarded PUBLISH path itself. The demo corpus cannot reach the shipped floor, so
    this runs under a policy COPY with a lower floor (see ``Autonomy.with_policy``: scaffolding
    that skips validation on purpose -- the loader's refusal of such a file is tested in
    ``test_policy.py``). It proves the path works when a plan really does clear the floor."""
    plan_id = _plan(a)
    await seed_calibration(sessionmaker, plan_id=plan_id, calibrated_confidence=0.99)
    guarded = a.with_mode("guarded").with_policy(confidence={"guarded_min_confidence": 0.60})

    evaluation = await guarded.engine().evaluate_and_record(plan_id=plan_id)

    assert evaluation.calibration_version is not None
    assert evaluation.calibration_version.startswith("autonomy-calib-1:")
    assert evaluation.decision is D.AUTO_PUBLISH
    assert evaluation.risk_tier is RiskTier.LOW
    assert evaluation.verification_profile is VerificationProfile.STANDARD


async def test_every_decision_records_the_autonomy_policy_version(
    sessionmaker: SM, a: Autonomy
) -> None:
    plan_id = _plan(a)
    versioned = a.with_policy()
    versioned.policy = versioned.policy.model_copy(update={"version": 4})
    evaluation = await versioned.engine().evaluate_and_record(plan_id=plan_id)
    assert evaluation.policy_version == 4
    assert (await decisions(sessionmaker, plan_id))[0].policy_version == 4


async def test_a_guarded_deferral_is_blocked_with_its_reasons_in_words(
    sessionmaker: SM, a: Autonomy
) -> None:
    """No separate 'deferred' state: the plan is ``blocked`` with an EMPTY block tuple, and
    the transition note written in the same transaction says why."""
    from tests.integration.recommendation_support import rows

    plan_id = _plan(a)
    evaluation = await a.with_mode("guarded").engine().evaluate_and_record(plan_id=plan_id)

    assert evaluation.decision is D.BLOCK and evaluation.blocks == () and evaluation.deferred
    assert (await transitions(sessionmaker, plan_id))[-1] == "blocked"
    note = (
        await rows(
            sessionmaker,
            "SELECT note FROM link_plan_transitions WHERE plan_id = :p AND to_status = 'blocked'",
            p=plan_id,
        )
    )[0].note
    assert note.startswith("blocked: deferred -- ")
    assert "calibration absent" in note
    assert "guarded floor 0.85" in note
    assert f"policy v{a.policy.version}" in note


async def test_calibration_lowers_a_confidence_and_never_raises_it(
    sessionmaker: SM, a: Autonomy
) -> None:
    plan_id = _plan(a)
    raw = (await a.engine().evaluated(plan_id=plan_id)).evaluation.confidence
    await seed_calibration(sessionmaker, plan_id=plan_id, calibrated_confidence=0.2)

    lowered = await a.engine().evaluate(plan_id=plan_id)

    assert lowered.confidence <= raw
    assert lowered.confidence == pytest.approx(0.2, abs=1e-6)


async def test_a_generous_calibration_cannot_rescue_a_weak_plan(
    sessionmaker: SM, a: Autonomy
) -> None:
    """The frozen protocol says the calibrator REDUCES: a class that performs brilliantly can
    confirm a confident plan and can never lift an unconfident one. The consequence is stated
    rather than hidden -- ``guarded`` mode publishes only plans whose RAW score already clears
    its floor."""
    plan_id = _plan(a)
    raw = (await a.engine().evaluated(plan_id=plan_id)).evaluation.confidence
    await seed_calibration(sessionmaker, plan_id=plan_id, calibrated_confidence=1.0)

    confirmed = await a.engine().evaluate(plan_id=plan_id)

    assert confirmed.confidence == pytest.approx(raw, abs=1e-6)
    assert confirmed.calibration_version is not None


async def test_a_failing_evaluation_run_calibrates_nothing(sessionmaker: SM, a: Autonomy) -> None:
    """A calibration nobody could promote is not one a publisher may lean on."""
    from ken_contracts.learning import GateResult

    plan_id = _plan(a)
    await seed_calibration(sessionmaker, plan_id=plan_id, gate=GateResult.FAIL)
    evaluation = await a.with_mode("guarded").engine().evaluate(plan_id=plan_id)
    assert evaluation.calibration_version is None
    assert evaluation.decision is D.BLOCK


async def test_a_behavioural_rollback_rate_lowers_the_confidence(
    sessionmaker: SM, a: Autonomy
) -> None:
    plan_id = _plan(a)
    await seed_calibration(
        sessionmaker, plan_id=plan_id, calibrated_confidence=0.99, rollback_rate=0.9
    )
    evaluation = await a.with_mode("guarded").engine().evaluate(plan_id=plan_id)
    assert evaluation.confidence <= 0.1
    assert evaluation.decision is D.BLOCK, "below the guarded floor"


async def test_a_good_behavioural_rate_never_raises_a_confidence(
    sessionmaker: SM, a: Autonomy
) -> None:
    """ADR-0015 s12: first-party counts are spoofable. A perfect measured outcome rate must
    change nothing, or a spoofed one would buy a publish."""
    plan_id = _plan(a, LI)
    await seed_calibration(
        sessionmaker,
        plan_id=plan_id,
        calibrated_confidence=0.9,
        positive_outcome_rate=1.0,
    )
    with_good = await a.engine().evaluate(plan_id=plan_id)
    assert with_good.confidence <= 0.9


# --------------------------------------------------------------------------- #
# Autonomous
# --------------------------------------------------------------------------- #
async def test_autonomous_publishes_an_uncalibrated_plan_with_the_strict_profile(
    sessionmaker: SM, a: Autonomy
) -> None:
    plan_id = _plan(a)
    autonomous = a.with_mode("autonomous", autonomy_autonomous_acknowledged=True)

    evaluation = await autonomous.engine().evaluate_and_record(plan_id=plan_id)

    assert evaluation.calibration_version is None
    assert evaluation.decision is D.AUTO_PUBLISH_WITH_EXTRA_VERIFICATION
    assert evaluation.verification_profile is VerificationProfile.STRICT
    assert evaluation.critic_outcome is CriticOutcome.ACCEPT


async def test_autonomous_still_applies_every_hard_rule(sessionmaker: SM, a: Autonomy) -> None:
    plan_id = _plan(a)
    await protect(sessionmaker, a.world.plans[(EV, CHARGING)].source_page_id)
    autonomous = a.with_mode("autonomous", autonomy_autonomous_acknowledged=True)

    evaluation = await autonomous.engine().evaluate_and_record(plan_id=plan_id)

    assert evaluation.decision is D.BLOCK
    assert BlockCode.PROTECTED_PAGE in evaluation.blocks


async def test_a_source_sweep_decides_every_plan_of_one_source(
    sessionmaker: SM, a: Autonomy
) -> None:
    source_id = a.world.plans[(EV, CHARGING)].source_page_id
    autonomous = a.with_mode("autonomous", autonomy_autonomous_acknowledged=True)

    found = await autonomous.engine().evaluate_source(source_page_id=source_id)

    assert len(found) >= 2
    assert {e.plan_id for e in found} == {p.id for (s, _t), p in a.world.plans.items() if s == EV}
    assert all(e.mode is AutonomyMode.AUTONOMOUS for e in found)
