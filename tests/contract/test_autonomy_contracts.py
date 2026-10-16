"""Contract tests for the frozen autonomy shapes (ADR-0018).

The one guarantee this phase rests on is that a deterministic block cannot become a publish.
These tests assert that it is unrepresentable in the contract -- not merely discouraged -- and
that the default mode publishes nothing.
"""

from __future__ import annotations

import inspect
import itertools
import typing
import uuid
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from ken_contracts.agents import CriticVerdict
from ken_contracts.autonomy import (
    DETERMINISTIC_BLOCK_CODES,
    AutonomyDecision,
    AutonomyEngine,
    AutonomyEvaluation,
    AutonomyMode,
    AutonomyReader,
    BlockCode,
    CriticOutcome,
    OperatorOverrideRequest,
    OverrideAction,
    RetryBudgetState,
    RiskTier,
    VerificationProfile,
)
from ken_contracts.recommendations import (
    ALLOWED_TRANSITIONS,
    OPEN_STATUSES,
    TRANSITION_AUTHORITY,
    LinkPlanStatus,
)
from ken_services.autonomy.protocol import (
    DeterministicBlockEvaluator,
    OperatorOverrideService,
    RetryBudgetGate,
    RiskClassifier,
    budget_from_attempts,
    decision_for,
    profile_for,
    target_status_for,
)

NOW = datetime(2026, 10, 16, 12, 0, tzinfo=UTC)


def _evaluation(**overrides: object) -> AutonomyEvaluation:
    kwargs: dict[str, object] = {
        "plan_id": uuid.uuid4(),
        "decision": AutonomyDecision.AUTO_PUBLISH,
        "mode": AutonomyMode.AUTONOMOUS,
        "risk_tier": RiskTier.LOW,
        "confidence": 0.9,
        "critic_outcome": CriticOutcome.ACCEPT,
        "policy_version": 1,
        "rollback_available": True,
        "evaluated_at": NOW,
        "engine_version": "autonomy-engine-1",
    }
    kwargs.update(overrides)
    return AutonomyEvaluation(**kwargs)


# --------------------------------------------------------------------------- #
# Vocabulary
# --------------------------------------------------------------------------- #
def test_the_three_decisions_and_three_modes_are_exactly_the_adr_s() -> None:
    assert [d.value for d in AutonomyDecision] == [
        "block",
        "auto_publish",
        "auto_publish_with_extra_verification",
    ]
    assert [m.value for m in AutonomyMode] == ["shadow", "guarded", "autonomous"]
    assert [o.value for o in CriticOutcome] == ["accept", "reject", "no_action"]
    assert [p.value for p in VerificationProfile] == ["standard", "strict"]
    assert [t.value for t in RiskTier] == ["low", "medium", "high"]
    # The retired approval classes are NOT in the vocabulary under any spelling.
    values = {d.value for d in AutonomyDecision}
    assert "require_approval" not in values
    assert "auto_approve_eligible" not in values


def test_the_block_set_is_closed_and_every_code_is_deterministic() -> None:
    """ADR-0018 / directive section 9. If a non-deterministic "block" is ever added, this
    fails and the design decision has to be made explicitly."""
    assert frozenset(BlockCode) == DETERMINISTIC_BLOCK_CODES
    assert {code.value for code in BlockCode} == {
        "self_link",
        "external_destination",
        "protected_page",
        "unpublished_destination",
        "broken_target",
        "stale_source_version",
        "duplicate_contextual_link",
        "prohibited_page_type",
        "policy_disallowed_destination",
        "invalid_canonical",
        "unsupported_redirect",
        "invalid_page_state",
        "invalid_anchor",
        "missing_rollback_metadata",
        "stale_vector",
    }


# --------------------------------------------------------------------------- #
# The evaluation record: the dangerous combinations are unrepresentable
# --------------------------------------------------------------------------- #
def test_a_fired_block_forces_a_block_decision() -> None:
    for decision in (
        AutonomyDecision.AUTO_PUBLISH,
        AutonomyDecision.AUTO_PUBLISH_WITH_EXTRA_VERIFICATION,
    ):
        with pytest.raises(ValidationError):
            _evaluation(decision=decision, blocks=(BlockCode.SELF_LINK,))
    assert _evaluation(decision=AutonomyDecision.BLOCK, blocks=(BlockCode.SELF_LINK,)).blocks == (
        BlockCode.SELF_LINK,
    )


def test_nothing_is_published_that_cannot_be_rolled_back() -> None:
    with pytest.raises(ValidationError):
        _evaluation(rollback_available=False)
    assert (
        _evaluation(
            decision=AutonomyDecision.BLOCK,
            blocks=(BlockCode.MISSING_ROLLBACK_METADATA,),
            rollback_available=False,
        ).decision
        is AutonomyDecision.BLOCK
    )


def test_block_codes_are_unique_and_in_enum_order() -> None:
    with pytest.raises(ValidationError):
        _evaluation(
            decision=AutonomyDecision.BLOCK,
            blocks=(BlockCode.SELF_LINK, BlockCode.SELF_LINK),
        )
    with pytest.raises(ValidationError):
        _evaluation(
            decision=AutonomyDecision.BLOCK,
            blocks=(BlockCode.STALE_VECTOR, BlockCode.SELF_LINK),
        )
    assert _evaluation(
        decision=AutonomyDecision.BLOCK,
        blocks=(BlockCode.SELF_LINK, BlockCode.STALE_VECTOR),
    ).blocks


def test_shadow_publishes_nothing_and_must_carry_a_counterfactual() -> None:
    with pytest.raises(ValidationError):  # a shadow publish is not representable
        _evaluation(mode=AutonomyMode.SHADOW, would_have_decision=AutonomyDecision.AUTO_PUBLISH)
    with pytest.raises(ValidationError):  # shadow without the counterfactual
        _evaluation(mode=AutonomyMode.SHADOW, decision=AutonomyDecision.BLOCK)
    with pytest.raises(ValidationError):  # a counterfactual outside shadow
        _evaluation(would_have_decision=AutonomyDecision.AUTO_PUBLISH)

    shadow = _evaluation(
        mode=AutonomyMode.SHADOW,
        decision=AutonomyDecision.BLOCK,
        would_have_decision=AutonomyDecision.AUTO_PUBLISH,
    )
    assert shadow.decision is AutonomyDecision.BLOCK
    assert shadow.would_have_decision is AutonomyDecision.AUTO_PUBLISH


def test_extra_verification_means_the_strict_profile() -> None:
    with pytest.raises(ValidationError):
        _evaluation(
            decision=AutonomyDecision.AUTO_PUBLISH_WITH_EXTRA_VERIFICATION,
            verification_profile=VerificationProfile.STANDARD,
        )
    assert (
        _evaluation(
            decision=AutonomyDecision.AUTO_PUBLISH_WITH_EXTRA_VERIFICATION,
            verification_profile=VerificationProfile.STRICT,
        ).verification_profile
        is VerificationProfile.STRICT
    )


def test_no_action_is_an_earlier_outcome_not_a_block() -> None:
    with pytest.raises(ValidationError):
        _evaluation(decision=AutonomyDecision.BLOCK, critic_outcome=CriticOutcome.NO_ACTION)


def test_raw_confidence_is_recorded_as_raw() -> None:
    assert _evaluation().calibration_version is None
    assert _evaluation(calibration_version="cal-1").calibration_version == "cal-1"


def test_an_uncertain_critic_verdict_is_preserved_rather_than_folded_into_accept() -> None:
    evaluation = _evaluation(critic_verdict=CriticVerdict.UNCERTAIN)
    assert evaluation.critic_verdict is CriticVerdict.UNCERTAIN
    assert evaluation.critic_outcome is CriticOutcome.ACCEPT


# --------------------------------------------------------------------------- #
# Lifecycle wiring
# --------------------------------------------------------------------------- #
def test_the_normal_autonomous_path_exists_end_to_end() -> None:
    """ADR-0018 section 3: proposed -> ... -> autonomy_decided -> validated -> ... and no
    approval state on it."""
    path = [
        LinkPlanStatus.PROPOSED,
        LinkPlanStatus.CRITIQUED,
        LinkPlanStatus.POLICY_CHECKED,
        LinkPlanStatus.AUTONOMY_DECIDED,
        LinkPlanStatus.VALIDATED,
        LinkPlanStatus.DEPLOYED,
        LinkPlanStatus.VERIFIED,
        LinkPlanStatus.MEASURED,
        LinkPlanStatus.LEARNING_CAPTURED,
    ]
    for current, target in itertools.pairwise(path):
        assert target in ALLOWED_TRANSITIONS[current], f"{current} -> {target}"
    assert LinkPlanStatus.AWAITING_APPROVAL not in path
    assert LinkPlanStatus.APPROVED not in path


def test_the_approval_states_remain_reachable_for_an_exception() -> None:
    assert LinkPlanStatus.AWAITING_APPROVAL in ALLOWED_TRANSITIONS[LinkPlanStatus.AUTONOMY_DECIDED]
    assert LinkPlanStatus.APPROVED in ALLOWED_TRANSITIONS[LinkPlanStatus.AWAITING_APPROVAL]
    assert LinkPlanStatus.VALIDATED in ALLOWED_TRANSITIONS[LinkPlanStatus.APPROVED]


def test_blocked_is_terminal_except_for_an_operator_override() -> None:
    assert ALLOWED_TRANSITIONS[LinkPlanStatus.BLOCKED] == frozenset(
        {LinkPlanStatus.AUTONOMY_DECIDED, LinkPlanStatus.SUPERSEDED, LinkPlanStatus.EXPIRED}
    )
    # A blocked plan must not be deployable without passing the autonomy step again.
    assert LinkPlanStatus.VALIDATED not in ALLOWED_TRANSITIONS[LinkPlanStatus.BLOCKED]
    assert LinkPlanStatus.DEPLOYED not in ALLOWED_TRANSITIONS[LinkPlanStatus.BLOCKED]


def test_blocked_does_not_occupy_the_pair_but_autonomy_decided_does() -> None:
    assert LinkPlanStatus.AUTONOMY_DECIDED in OPEN_STATUSES
    assert LinkPlanStatus.BLOCKED not in OPEN_STATUSES


def test_an_agent_can_neither_decide_nor_block() -> None:
    """ADR-0012 stands: a model may not publish and may not withhold publication."""
    assert TRANSITION_AUTHORITY[LinkPlanStatus.AUTONOMY_DECIDED] == frozenset({"system", "user"})
    assert TRANSITION_AUTHORITY[LinkPlanStatus.BLOCKED] == frozenset({"system", "user"})


def test_every_status_has_an_authority_and_a_transition_row() -> None:
    assert set(TRANSITION_AUTHORITY) == set(LinkPlanStatus)
    assert set(ALLOWED_TRANSITIONS) == set(LinkPlanStatus)


# --------------------------------------------------------------------------- #
# Override request
# --------------------------------------------------------------------------- #
def test_an_override_needs_an_operation_id_a_reason_and_an_expected_status() -> None:
    request = OperatorOverrideRequest(
        operation_id=uuid.uuid4(),
        action=OverrideAction.PUBLISH,
        reason="the destination was republished",
        expected_status=LinkPlanStatus.BLOCKED,
    )
    assert request.action is OverrideAction.PUBLISH
    with pytest.raises(ValidationError):  # a blank reason is not a reason
        OperatorOverrideRequest(
            operation_id=uuid.uuid4(),
            action=OverrideAction.BLOCK,
            reason="",
            expected_status=LinkPlanStatus.AUTONOMY_DECIDED,
        )


# --------------------------------------------------------------------------- #
# Shared pure helpers
# --------------------------------------------------------------------------- #
def test_decision_for_puts_blocks_first_whatever_the_mode() -> None:
    for mode in AutonomyMode:
        assert (
            decision_for(
                blocks=(BlockCode.SELF_LINK,),
                mode=mode,
                risk_tier=RiskTier.LOW,
                extra_verification=False,
            )
            is AutonomyDecision.BLOCK
        )


def test_decision_for_never_publishes_in_shadow() -> None:
    assert (
        decision_for(
            blocks=(), mode=AutonomyMode.SHADOW, risk_tier=RiskTier.LOW, extra_verification=False
        )
        is AutonomyDecision.BLOCK
    )


def test_guarded_publishes_only_low_risk() -> None:
    assert (
        decision_for(
            blocks=(), mode=AutonomyMode.GUARDED, risk_tier=RiskTier.LOW, extra_verification=False
        )
        is AutonomyDecision.AUTO_PUBLISH
    )
    for tier in (RiskTier.MEDIUM, RiskTier.HIGH):
        assert (
            decision_for(
                blocks=(), mode=AutonomyMode.GUARDED, risk_tier=tier, extra_verification=False
            )
            is AutonomyDecision.BLOCK
        )


def test_high_risk_demands_extra_verification_in_autonomous_mode() -> None:
    assert (
        decision_for(
            blocks=(),
            mode=AutonomyMode.AUTONOMOUS,
            risk_tier=RiskTier.HIGH,
            extra_verification=False,
        )
        is AutonomyDecision.AUTO_PUBLISH_WITH_EXTRA_VERIFICATION
    )


def test_profile_for_never_downgrades_strict() -> None:
    assert (
        profile_for(
            AutonomyDecision.AUTO_PUBLISH_WITH_EXTRA_VERIFICATION,
            default=VerificationProfile.STANDARD,
        )
        is VerificationProfile.STRICT
    )
    assert (
        profile_for(AutonomyDecision.AUTO_PUBLISH, default=VerificationProfile.STRICT)
        is VerificationProfile.STRICT
    )


def test_the_budget_is_exhausted_by_its_counts_not_by_a_flag() -> None:
    state = budget_from_attempts(
        source_page_id=uuid.uuid4(),
        target_page_id=uuid.uuid4(),
        attempts=3,
        failures=3,
        max_attempts=3,
        cooldown_until=None,
        last_attempt_at=NOW,
    )
    assert isinstance(state, RetryBudgetState)
    assert state.exhausted is True
    assert (
        budget_from_attempts(
            source_page_id=uuid.uuid4(),
            target_page_id=uuid.uuid4(),
            attempts=1,
            failures=1,
            max_attempts=3,
            cooldown_until=None,
            last_attempt_at=NOW,
        ).exhausted
        is False
    )


# --------------------------------------------------------------------------- #
# Protocols
# --------------------------------------------------------------------------- #
def test_the_contract_protocols_are_async_and_keyword_only() -> None:
    for protocol, names in (
        (AutonomyEngine, ("deterministic_blocks", "evaluate", "retry_budget")),
        (
            AutonomyReader,
            ("get_health", "get_summary", "list_decisions", "list_blocked", "list_attempts"),
        ),
    ):
        for name in names:
            fn = getattr(protocol, name)
            assert inspect.iscoroutinefunction(fn), name
            parameters = list(inspect.signature(fn).parameters.values())[1:]
            assert all(p.kind is inspect.Parameter.KEYWORD_ONLY for p in parameters), name


def test_the_block_evaluator_is_synchronous_and_pure() -> None:
    """A function with no I/O cannot smuggle in an inference call, which is the whole
    guarantee: ``evaluate`` is deliberately NOT a coroutine."""
    assert not inspect.iscoroutinefunction(DeterministicBlockEvaluator.evaluate)
    assert inspect.iscoroutinefunction(DeterministicBlockEvaluator.facts)


def test_the_service_protocols_take_a_principal_never_an_actor_string() -> None:
    """The database can check that an actor LOOKS like ``user:<name>``; only a session
    proves it is one (the ADR-0016 15.7.3 rule, applied to overrides)."""
    from ken_contracts.auth import Principal

    hints = typing.get_type_hints(OperatorOverrideService.override)
    assert hints["principal"] is Principal
    assert "actor" not in inspect.signature(OperatorOverrideService.override).parameters


def test_the_risk_classifier_and_budget_gate_are_async_keyword_only() -> None:
    for fn in (RiskClassifier.classify, RetryBudgetGate.check, RetryBudgetGate.record):
        assert inspect.iscoroutinefunction(fn)
        parameters = list(inspect.signature(fn).parameters.values())[1:]
        assert all(p.kind is inspect.Parameter.KEYWORD_ONLY for p in parameters)


def test_a_guarded_deferral_is_recorded_as_blocked_with_an_empty_block_tuple() -> None:
    """``decision_for`` returns BLOCK both for a fired hard block and for a guarded plan that
    is merely too risky, because the ADR has no DEFER decision. The orchestrator's decision:
    BOTH are recorded as ``blocked`` -- there is no separate "deferred" state in the
    operational vocabulary, migration 0010's guard maps every ``block`` decision to ``blocked``,
    and an operator can still publish-override it. What tells a deferral from a safety block
    is an EMPTY ``blocks`` tuple, made explicit as ``AutonomyEvaluation.deferred`` rather than
    left as a convention.

    (This test used to assert ``awaiting_approval``. That value was irreconcilable with the
    database guard, so it was changed deliberately, not softened.)"""
    deferred = decision_for(
        blocks=(), mode=AutonomyMode.GUARDED, risk_tier=RiskTier.HIGH, extra_verification=False
    )
    assert deferred is AutonomyDecision.BLOCK
    assert (
        target_status_for(decision=deferred, mode=AutonomyMode.GUARDED, blocks=())
        is LinkPlanStatus.BLOCKED
    )
    # A fired block is a real block, whatever the mode -- the same status, told apart by the codes.
    assert (
        target_status_for(
            decision=AutonomyDecision.BLOCK,
            mode=AutonomyMode.GUARDED,
            blocks=(BlockCode.SELF_LINK,),
        )
        is LinkPlanStatus.BLOCKED
    )


def test_deferred_is_a_block_with_no_code_outside_shadow_and_not_a_critic_rejection() -> None:
    deferral = {"decision": AutonomyDecision.BLOCK, "mode": AutonomyMode.GUARDED}
    assert _evaluation(**deferral).deferred is True
    # a fired code is a safety block, not a deferral
    assert _evaluation(**deferral, blocks=(BlockCode.SELF_LINK,)).deferred is False
    # a critic's rejection is a refusal, not a postponement
    assert _evaluation(**deferral, critic_outcome=CriticOutcome.REJECT).deferred is False
    # every shadow decision is BLOCK by definition: nothing was deferred
    assert (
        _evaluation(
            decision=AutonomyDecision.BLOCK,
            mode=AutonomyMode.SHADOW,
            would_have_decision=AutonomyDecision.AUTO_PUBLISH,
        ).deferred
        is False
    )
    # a publish decision is never a deferral
    assert _evaluation().deferred is False


def test_shadow_moves_nothing_and_a_publish_takes_the_autonomous_path() -> None:
    assert (
        target_status_for(decision=AutonomyDecision.BLOCK, mode=AutonomyMode.SHADOW, blocks=())
        is None
    )
    for decision in (
        AutonomyDecision.AUTO_PUBLISH,
        AutonomyDecision.AUTO_PUBLISH_WITH_EXTRA_VERIFICATION,
    ):
        assert (
            target_status_for(decision=decision, mode=AutonomyMode.AUTONOMOUS, blocks=())
            is LinkPlanStatus.AUTONOMY_DECIDED
        )


def test_every_status_target_for_status_can_name_is_reachable_from_policy_checked() -> None:
    """A helper that names an unreachable status would be a trap, not a convenience."""
    reachable = ALLOWED_TRANSITIONS[LinkPlanStatus.POLICY_CHECKED]
    for status in (
        LinkPlanStatus.BLOCKED,
        LinkPlanStatus.AUTONOMY_DECIDED,
    ):
        assert status in reachable, status
