"""How each of the three decisions is reached, and what can and cannot change one.

The invariants, each as a test:

* a fired block is ``BLOCK`` in every mode, whatever anything else says;
* ``shadow`` never publishes and always records a counterfactual;
* ``guarded`` publishes only LOW risk, high confidence and CALIBRATED;
* ``autonomous`` publishes policy-eligible plans and still applies every hard rule;
* ``AUTO_PUBLISH_WITH_EXTRA_VERIFICATION`` differs ONLY in the verification profile;
* a run can force a more conservative mode and never a laxer one;
* no rollback, no publish.
"""

from __future__ import annotations

import pytest

from ken_contracts.agents import CriticVerdict
from ken_contracts.autonomy import (
    AutonomyDecision,
    AutonomyMode,
    BlockCode,
    CriticOutcome,
    RiskTier,
    VerificationProfile,
)
from ken_services.autonomy.engine import critic_outcome_of
from ken_services.autonomy.gate import publishing_enabled
from ken_services.autonomy.policy import load_autonomy_policy
from tests.unit.autonomy._support import clean_facts, inputs, settings
from tests.unit.autonomy.pure import pure_engine

AUTO = AutonomyDecision.AUTO_PUBLISH
EXTRA = AutonomyDecision.AUTO_PUBLISH_WITH_EXTRA_VERIFICATION
BLOCK = AutonomyDecision.BLOCK
POLICY = load_autonomy_policy()
GUARDED_MIN_CONFIDENCE = POLICY.confidence.guarded_min_confidence
AUTONOMOUS_MIN_CONFIDENCE = POLICY.confidence.autonomous_min_confidence


# --------------------------------------------------------------------------- #
# Blocks win, in every mode
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("mode", list(AutonomyMode))
def test_a_fired_block_is_a_block_in_every_mode(mode: AutonomyMode) -> None:
    engine = pure_engine()
    decided = engine._decide(
        inputs(blocks=(BlockCode.PROTECTED_PAGE,), confidence=1.0, tier=RiskTier.LOW),
        mode=mode,
    )
    assert decided is BLOCK


def test_no_rollback_means_no_publish() -> None:
    engine = pure_engine()
    facts = clean_facts(rollback_metadata_present=False)
    decided = engine._decide(
        inputs(facts=facts, confidence=1.0, tier=RiskTier.LOW), mode=AutonomyMode.AUTONOMOUS
    )
    assert decided is BLOCK


def test_a_rejecting_critic_blocks_even_with_no_fired_code() -> None:
    engine = pure_engine()
    decided = engine._decide(
        inputs(critic=CriticOutcome.REJECT, verdict=CriticVerdict.REJECT, confidence=1.0),
        mode=AutonomyMode.AUTONOMOUS,
    )
    assert decided is BLOCK


# --------------------------------------------------------------------------- #
# The modes
# --------------------------------------------------------------------------- #
def test_shadow_publishes_nothing_and_records_the_counterfactual() -> None:
    engine = pure_engine()
    evaluated = engine._assemble(
        inputs(confidence=1.0, tier=RiskTier.LOW), mode=AutonomyMode.SHADOW
    )
    evaluation = evaluated.evaluation
    assert evaluation.decision is BLOCK
    assert evaluation.would_have_decision is AUTO
    assert evaluation.mode is AutonomyMode.SHADOW


def test_shadow_records_a_block_counterfactual_for_a_blocked_plan() -> None:
    engine = pure_engine()
    evaluated = engine._assemble(inputs(blocks=(BlockCode.STALE_VECTOR,)), mode=AutonomyMode.SHADOW)
    assert evaluated.evaluation.decision is BLOCK
    assert evaluated.evaluation.would_have_decision is BLOCK


def test_guarded_publishes_low_risk_high_confidence_calibrated_only() -> None:
    engine = pure_engine()
    ok = inputs(confidence=GUARDED_MIN_CONFIDENCE, tier=RiskTier.LOW)
    assert engine._decide(ok, mode=AutonomyMode.GUARDED) is AUTO


@pytest.mark.parametrize(
    "variant",
    [
        {"tier": RiskTier.MEDIUM},
        {"tier": RiskTier.HIGH},
        {"confidence": GUARDED_MIN_CONFIDENCE - 0.01},
        {"calibration_version": None},
    ],
    ids=["medium_risk", "high_risk", "below_floor", "uncalibrated"],
)
def test_guarded_defers_everything_else(variant: dict[str, object]) -> None:
    engine = pure_engine()
    assert engine._decide(inputs(**variant), mode=AutonomyMode.GUARDED) is BLOCK  # type: ignore[arg-type]


def test_guarded_refuses_raw_confidence_even_at_1_0() -> None:
    """ADR-0011: no threshold may treat RAW as calibrated. This is what keeps Phase 9
    calibration on the critical path instead of making it optional."""
    engine = pure_engine()
    raw = inputs(confidence=1.0, calibration_version=None, tier=RiskTier.LOW)
    assert engine._decide(raw, mode=AutonomyMode.GUARDED) is BLOCK


def test_autonomous_publishes_an_uncalibrated_plan_with_extra_verification() -> None:
    """``autonomous`` is allowed to act on raw confidence -- an operator asked for that
    explicitly, twice -- but it does not pretend the number is calibrated: the plan gets the
    STRICT profile, which is more checking and not a weaker gate."""
    engine = pure_engine()
    raw = inputs(confidence=0.9, calibration_version=None, extra_verification=True)
    assert engine._decide(raw, mode=AutonomyMode.AUTONOMOUS) is EXTRA


def test_autonomous_still_refuses_a_plan_it_barely_believes_in() -> None:
    engine = pure_engine()
    weak = inputs(confidence=AUTONOMOUS_MIN_CONFIDENCE - 0.01)
    assert engine._decide(weak, mode=AutonomyMode.AUTONOMOUS) is BLOCK


def test_high_risk_gets_extra_verification_not_a_different_rule() -> None:
    engine = pure_engine()
    high = inputs(tier=RiskTier.HIGH, confidence=1.0)
    decision = engine._decide(high, mode=AutonomyMode.AUTONOMOUS)
    assert decision is EXTRA
    # ... and the SAME high-risk plan with a fired code is still BLOCK: the extra-verification
    # decision is not a bypass.
    blocked = inputs(tier=RiskTier.HIGH, confidence=1.0, blocks=(BlockCode.SELF_LINK,))
    assert engine._decide(blocked, mode=AutonomyMode.AUTONOMOUS) is BLOCK


@pytest.mark.parametrize("handed_off", [True, False, None])
def test_an_uncertain_critic_is_never_an_accept_whatever_the_stored_row_says(
    handed_off: bool | None,
) -> None:
    """FAIL CLOSED (ADR-0019). ``uncertain`` is NO_ACTION always -- including the LEGACY
    combination ``handed_off=True``, which only rows written before the critic change can carry.
    It must not raise a tier or demand extra verification and publish anyway: that was the
    fail-open mapping this test used to encode (it asserted ACCEPT, a MEDIUM tier and an
    extra-verification publish). Changed deliberately, not softened."""
    assert (
        critic_outcome_of(CriticVerdict.UNCERTAIN, handed_off=handed_off) is CriticOutcome.NO_ACTION
    )


def test_no_action_is_an_earlier_outcome_than_a_decision() -> None:
    """A critic that found no defect and still did not hand the plan on means "nothing worth
    doing" (ADR-0018 s1). It is not evaluated: the contract refuses to pair ``no_action`` with
    a BLOCK decision, and the ADR refuses to pair it with a publish, so the only consistent
    reading is that such a plan never reaches the engine."""
    assert critic_outcome_of(CriticVerdict.ACCEPT, handed_off=False) is CriticOutcome.NO_ACTION
    assert critic_outcome_of(CriticVerdict.ACCEPT, handed_off=True) is CriticOutcome.ACCEPT
    assert critic_outcome_of(None, handed_off=None) is CriticOutcome.ACCEPT
    assert critic_outcome_of(CriticVerdict.REJECT, handed_off=False) is CriticOutcome.REJECT


# --------------------------------------------------------------------------- #
# The verification profile is the ONLY difference
# --------------------------------------------------------------------------- #
def test_extra_verification_differs_only_in_the_profile() -> None:
    engine = pure_engine()
    plain = engine._assemble(inputs(confidence=1.0), mode=AutonomyMode.AUTONOMOUS).evaluation
    extra = engine._assemble(
        inputs(confidence=1.0, extra_verification=True), mode=AutonomyMode.AUTONOMOUS
    ).evaluation
    assert plain.decision is AUTO and plain.verification_profile is VerificationProfile.STANDARD
    assert extra.decision is EXTRA and extra.verification_profile is VerificationProfile.STRICT
    # Everything else about the two evaluations is identical.
    ignore = {"decision", "verification_profile"}
    assert {k: v for k, v in plain.model_dump().items() if k not in ignore} == {
        k: v for k, v in extra.model_dump().items() if k not in ignore
    }


def test_the_strict_profile_is_never_downgraded_by_the_settings_default() -> None:
    engine = pure_engine(settings(autonomy_verification_profile="standard"))
    extra = engine._assemble(
        inputs(confidence=1.0, extra_verification=True), mode=AutonomyMode.AUTONOMOUS
    ).evaluation
    assert extra.verification_profile is VerificationProfile.STRICT


def test_the_settings_default_profile_applies_to_a_plain_auto_publish() -> None:
    engine = pure_engine(settings(autonomy_verification_profile="strict"))
    plain = engine._assemble(inputs(confidence=1.0), mode=AutonomyMode.AUTONOMOUS).evaluation
    assert plain.decision is AUTO
    assert plain.verification_profile is VerificationProfile.STRICT


# --------------------------------------------------------------------------- #
# Mode resolution
# --------------------------------------------------------------------------- #
def test_a_run_may_force_a_more_conservative_mode() -> None:
    engine = pure_engine(settings(autonomy_mode="guarded"))
    assert engine.resolve_mode(AutonomyMode.SHADOW) is AutonomyMode.SHADOW
    assert engine.resolve_mode(None) is AutonomyMode.GUARDED


@pytest.mark.parametrize(
    ("configured", "requested"),
    [
        ("shadow", AutonomyMode.GUARDED),
        ("shadow", AutonomyMode.AUTONOMOUS),
        ("guarded", AutonomyMode.AUTONOMOUS),
    ],
)
def test_a_run_may_never_force_a_laxer_mode(configured: str, requested: AutonomyMode) -> None:
    engine = pure_engine(settings(autonomy_mode=configured))
    with pytest.raises(ValueError, match="more permissive"):
        engine.resolve_mode(requested)


def test_the_default_installation_publishes_nothing() -> None:
    """Asked of the ONE shared gate: the engine's own copy of this rule was deleted, because a
    rule that lives in three places drifts (the reader's copy already disagreed)."""
    built = settings()
    engine = pure_engine(built)
    assert engine.configured_mode is AutonomyMode.SHADOW
    assert publishing_enabled(built) is False


# --------------------------------------------------------------------------- #
# Where a decision sends the plan (the frozen target_status_for, obeyed without exception)
# --------------------------------------------------------------------------- #
def test_the_target_status_comes_from_the_frozen_helper_and_a_deferral_is_blocked() -> None:
    from ken_contracts.autonomy import BlockCode as Code
    from ken_contracts.recommendations import LinkPlanStatus
    from ken_services.autonomy.protocol import target_status_for

    engine = pure_engine()
    shadow = engine._assemble(inputs(confidence=1.0), mode=AutonomyMode.SHADOW).evaluation
    assert (
        target_status_for(decision=shadow.decision, mode=shadow.mode, blocks=shadow.blocks) is None
    ), "a shadow pass moves nothing"
    blocked = engine._assemble(
        inputs(blocks=(Code.SELF_LINK,)), mode=AutonomyMode.AUTONOMOUS
    ).evaluation
    published = engine._assemble(inputs(confidence=1.0), mode=AutonomyMode.AUTONOMOUS).evaluation
    deferral = engine._assemble(
        inputs(tier=RiskTier.HIGH, confidence=1.0), mode=AutonomyMode.GUARDED
    ).evaluation
    assert blocked.deferred is False and deferral.deferred is True
    for evaluation, expected in (
        (blocked, LinkPlanStatus.BLOCKED),
        (deferral, LinkPlanStatus.BLOCKED),  # same status, told apart by the empty block tuple
        (published, LinkPlanStatus.AUTONOMY_DECIDED),
    ):
        assert (
            target_status_for(
                decision=evaluation.decision, mode=evaluation.mode, blocks=evaluation.blocks
            )
            is expected
        )


def test_the_evaluation_records_the_autonomy_policy_version_not_the_recommendation_one() -> None:
    engine = pure_engine()
    evaluated = engine._assemble(inputs(confidence=1.0), mode=AutonomyMode.AUTONOMOUS)
    assert evaluated.evaluation.policy_version == POLICY.version
    # the plan's facts carry the RECOMMENDATION policy version (1 in the fixture); a different
    # autonomy policy version must win, so the field cannot be quietly the wrong document's.
    other = POLICY.model_copy(update={"version": 7})
    again = pure_engine(autonomy_policy=other)._assemble(
        inputs(confidence=1.0), mode=AutonomyMode.AUTONOMOUS
    )
    assert again.evaluation.policy_version == 7


def test_the_floors_come_from_the_policy_and_not_from_the_code() -> None:
    """Move the floor and the decision moves with it: proof the number is not hard-coded."""
    strict = POLICY.model_copy(
        update={
            "confidence": POLICY.confidence.model_copy(
                update={"autonomous_min_confidence": 0.95, "guarded_min_confidence": 0.99}
            )
        }
    )
    at_90 = inputs(confidence=0.90, tier=RiskTier.LOW)
    assert pure_engine()._decide(at_90, mode=AutonomyMode.AUTONOMOUS) is not BLOCK
    assert pure_engine(autonomy_policy=strict)._decide(at_90, mode=AutonomyMode.AUTONOMOUS) is BLOCK
