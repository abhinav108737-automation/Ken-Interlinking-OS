"""The investigation view never shows "blocked" with an empty reason.

A guarded-mode deferral is a ``blocked`` plan with an EMPTY block list (no deterministic rule
fired), so the words have to come from somewhere. These tests pin what they say, and that they
come from the record plus the policy and nothing else.
"""

from __future__ import annotations

from typing import Any

from ken_contracts.autonomy import (
    AutonomyDecision,
    AutonomyEvaluation,
    AutonomyMode,
    BlockCode,
    CriticOutcome,
    RiskTier,
)
from ken_services.autonomy.policy import load_autonomy_policy
from ken_services.autonomy.reasons import MAX_NOTE_CHARS, block_note, explain
from tests.unit.autonomy._support import inputs
from tests.unit.autonomy.pure import pure_engine

POLICY = load_autonomy_policy()


def _evaluate(mode: AutonomyMode, **kw: Any) -> AutonomyEvaluation:
    evaluation: AutonomyEvaluation = pure_engine()._assemble(inputs(**kw), mode=mode).evaluation
    return evaluation


def test_a_guarded_deferral_names_the_risk_confidence_and_calibration_facts() -> None:
    evaluation = _evaluate(
        AutonomyMode.GUARDED, tier=RiskTier.MEDIUM, confidence=0.42, calibration_version=None
    )
    assert evaluation.deferred
    reasons = explain(evaluation, POLICY)
    text = " ".join(reasons)
    assert "calibration absent" in text
    assert "risk tier medium" in text
    assert "confidence 0.42" in text and "guarded floor 0.85" in text
    note = block_note(evaluation, POLICY)
    assert note.startswith("blocked: deferred -- ")
    assert f"policy v{POLICY.version}" in note and "mode guarded" in note


def test_a_calibrated_low_risk_plan_under_the_floor_is_deferred_for_confidence_alone() -> None:
    evaluation = _evaluate(AutonomyMode.GUARDED, tier=RiskTier.LOW, confidence=0.60)
    assert explain(evaluation, POLICY) == ("confidence 0.60 below the guarded floor 0.85",)


def test_a_fired_block_is_named_by_its_codes_and_is_not_called_a_deferral() -> None:
    evaluation = _evaluate(AutonomyMode.AUTONOMOUS, blocks=(BlockCode.PROTECTED_PAGE,))
    assert not evaluation.deferred
    note = block_note(evaluation, POLICY)
    assert note.startswith("blocked: deterministic block protected_page")
    assert "deferred" not in note


def test_a_critic_rejection_and_the_autonomous_floor_have_their_own_words() -> None:
    rejected = _evaluate(
        AutonomyMode.AUTONOMOUS, critic=CriticOutcome.REJECT, verdict=None, confidence=1.0
    )
    assert explain(rejected, POLICY) == ("the critic rejected the plan",)
    weak = _evaluate(AutonomyMode.AUTONOMOUS, confidence=0.40)
    assert "autonomous floor 0.50" in " ".join(explain(weak, POLICY))


def test_a_publish_decision_has_no_blocking_reasons() -> None:
    published = _evaluate(AutonomyMode.AUTONOMOUS, confidence=1.0)
    assert published.decision is not AutonomyDecision.BLOCK
    assert explain(published, POLICY) == ()


def test_the_note_fits_the_transition_table_bound() -> None:
    every_code = tuple(BlockCode)
    evaluation = _evaluate(AutonomyMode.AUTONOMOUS, blocks=every_code)
    assert len(block_note(evaluation, POLICY)) <= MAX_NOTE_CHARS
