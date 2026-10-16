"""Why a plan was not published, in words (the investigation view's "blocked: deferred ...").

There is no separate "deferred" state in the operational vocabulary (proposed, blocked,
auto-published, verification-pending, verified, rolled-back, failed). A guarded-mode deferral
is therefore a ``blocked`` plan whose ``blocks`` tuple is EMPTY -- no deterministic rule
fired; the mode, risk tier, confidence or calibration declined to publish it unattended -- and
an empty list on the investigation screen would read as "blocked for no reason".
:func:`explain` derives the reasons from the stored record plus the policy that governed it,
and :func:`block_note` renders them into the sentence written on the plan's transition row in
the SAME transaction as the decision, which is where the screen reads it from.

Pure: no I/O, no clock, no model. The same record and policy give the same words.
"""

from __future__ import annotations

from typing import Final

from ken_contracts.autonomy import (
    AutonomyDecision,
    AutonomyEvaluation,
    AutonomyMode,
    AutonomyPolicy,
    CriticOutcome,
    RiskTier,
)

__all__ = ["MAX_NOTE_CHARS", "block_note", "explain"]

MAX_NOTE_CHARS: Final = 500
"""``link_plan_transitions.note`` is bounded at 500 characters."""


def explain(evaluation: AutonomyEvaluation, policy: AutonomyPolicy) -> tuple[str, ...]:
    """The reasons this evaluation did not publish. Empty for a publish decision."""
    if evaluation.decision is not AutonomyDecision.BLOCK:
        return ()
    if evaluation.blocks:
        return tuple(f"deterministic block {code.value}" for code in evaluation.blocks)
    if evaluation.mode is AutonomyMode.SHADOW:
        return ("shadow mode publishes nothing",)
    if evaluation.critic_outcome is CriticOutcome.REJECT:
        return ("the critic rejected the plan",)
    reasons: list[str] = []
    if not evaluation.rollback_available:
        reasons.append("no rollback is available")
    confidence = evaluation.confidence
    if evaluation.mode is AutonomyMode.GUARDED:
        floor = policy.confidence.guarded_min_confidence
        if evaluation.calibration_version is None:
            reasons.append("calibration absent (guarded publishes only a calibrated class)")
        if evaluation.risk_tier is not RiskTier.LOW:
            reasons.append(f"risk tier {evaluation.risk_tier.value} (guarded needs low)")
        if confidence < floor:
            reasons.append(f"confidence {confidence:.2f} below the guarded floor {floor:.2f}")
    elif evaluation.mode is AutonomyMode.AUTONOMOUS:
        floor = policy.confidence.autonomous_min_confidence
        if confidence < floor:
            reasons.append(f"confidence {confidence:.2f} below the autonomous floor {floor:.2f}")
    return tuple(reasons) or ("declined by policy",)


def block_note(evaluation: AutonomyEvaluation, policy: AutonomyPolicy) -> str:
    """The sentence recorded with the plan's move to ``blocked``.

    ``blocked: deferred -- <reasons>`` for a deferral, ``blocked: <reasons>`` otherwise, then
    the policy version and mode that governed the decision.
    """
    reasons = "; ".join(explain(evaluation, policy))
    lead = f"blocked: deferred -- {reasons}" if evaluation.deferred else f"blocked: {reasons}"
    return f"{lead} [policy v{policy.version}, mode {evaluation.mode.value}]"[:MAX_NOTE_CHARS]
