"""The proposal stage ladder (ADR-0016 section 5, as amended by section 15 and 17.1).

Pure ladder logic plus ONE database writer, :func:`record_stage`, which does what migration
0009's deferred guard demands: the ``learning_proposal_transitions`` row and the
``learning_proposals.status`` update are written by the SAME transaction (the guard compares
``txid``), so a stage change with no audit row cannot commit.

Who may call what:

* the system stages ``offline_test`` and ``shadow`` -- :func:`advance_system_stage`, called by
  jobs, with the ``system:learning`` actor. Each has a real gate (below);
* every other stage -- ``canary``, ``experiment``, ``promoted``, ``rejected``,
  ``rolled_back``, ``withdrawn`` -- is HUMAN-only and goes through
  :class:`ken_services.learning.promote.DatabasePromotionService`, which derives the actor from
  an authenticated :class:`~ken_contracts.auth.Principal` and then calls :func:`record_stage`
  here. :func:`record_stage` is public for that one caller. It refuses ``promoted`` unless the
  caller states it already established ``ken.learning_promoter`` (the database refuses it too).

CANARY and EXPERIMENT are STAGE-DEFINED ONLY in Phase 8 (ADR-0016 17.1): they exist in the
ladder, a human may record them, and no machinery uses a candidate policy for real. SHADOW is
the stage with working machinery (:mod:`ken_services.learning.shadow`).

Gates for the two system stages:

* ``offline_test`` needs an ``offline`` evaluation of THIS proposal, bound to its CURRENT
  fingerprint, with ``gate_result = pass``. ``inconclusive`` and ``fail`` never advance.
* ``shadow`` needs ``offline_test`` already reached, and only a ``ranking`` or
  ``recommendation`` candidate may be shadowed: nothing can be shadowed for ``agents``, and
  nothing consumes a skill at runtime (the gate policy's ``required_stages`` agree).
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final, Protocol

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ken_contracts.learning import (
    LEARNING_ACTOR,
    EvaluationMode,
    GateResult,
    LearningPolicy,
    PromotionRefusal,
    PromotionRefusedError,
    ProposalKind,
    ProposalNotFoundError,
    ProposalStatus,
    TargetKind,
)
from ken_core.db.models.learning import LearningProposal
from ken_core.db.repositories import learning as repo
from ken_services.learning.protocol import required_stages_met

__all__ = [
    "SYSTEM_STAGES",
    "HasToStatus",
    "SkillKey",
    "StageGate",
    "advance_system_stage",
    "check_stage_gate",
    "missing_stages",
    "policy_key",
    "required_stages_ok",
    "shadow_requirements_met",
    "stages_reached",
]

SkillKey: Final = "skill"
SYSTEM_STAGES: Final = frozenset({ProposalStatus.OFFLINE_TEST, ProposalStatus.SHADOW})
"""The only stages a job (the ``system`` actor class) may record."""
SHADOWABLE: Final = frozenset({TargetKind.RANKING.value, TargetKind.RECOMMENDATION.value})


class HasToStatus(Protocol):
    """Anything shaped like a recorded transition (the writer lives in ``promote.py``)."""

    @property
    def to_status(self) -> str: ...


def policy_key(proposal: LearningProposal) -> str:
    """The ``GatePolicy.required_stages`` key of a proposal: its target kind, or ``skill``."""
    if proposal.kind == ProposalKind.SKILL.value or proposal.target_kind is None:
        return SkillKey
    return str(proposal.target_kind)


def stages_reached(transitions: Sequence[HasToStatus]) -> tuple[ProposalStatus, ...]:
    """Every stage the proposal has been in, in order. ``proposed`` is always first (a proposal
    is INSERTED there; it has no transition row of its own)."""
    reached: list[ProposalStatus] = [ProposalStatus.PROPOSED]
    for row in transitions:
        status = ProposalStatus(row.to_status)
        if status not in reached:
            reached.append(status)
    return tuple(reached)


def missing_stages(
    *, policy: LearningPolicy, key: str, reached: Sequence[ProposalStatus]
) -> tuple[ProposalStatus, ...]:
    """The required stages not yet reached -- the UI's "what is still missing" list, computed
    by the same predicate the promotion service uses."""
    return tuple(
        stage for stage in policy.gates.required_stages.get(key, ()) if stage not in set(reached)
    )


@dataclass(frozen=True, slots=True)
class StageGate:
    """The outcome of a system-stage gate check (for the CLI line and for tests)."""

    stage: ProposalStatus
    evaluation_id: uuid.UUID | None


def shadow_requirements_met(
    *, policy: LearningPolicy, fingerprint: str, shadow_metrics: Sequence[Mapping[str, Any]]
) -> bool:
    """Whether the recorded shadow passes meet ``shadow_min_runs`` and ``shadow_min_plans``.

    ``shadow_metrics`` are the ``metrics["shadow"]`` objects of this proposal's shadow
    evaluations bound to ``fingerprint``. The plan count is the number of (source, target)
    pairs actually compared, summed over runs -- a shadow over an empty corpus never counts.
    """
    runs = [item for item in shadow_metrics if item.get("fingerprint") == fingerprint]
    compared = sum(int(item.get("compared_pairs", 0)) for item in runs)
    return len(runs) >= policy.gates.shadow_min_runs and compared >= policy.gates.shadow_min_plans


def _refuse(refusal: PromotionRefusal) -> PromotionRefusedError:
    return PromotionRefusedError(refusal)


async def advance_system_stage(
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    proposal_id: uuid.UUID,
    to_status: ProposalStatus,
    now: datetime | None = None,
) -> StageGate:
    """Advance a proposal into ``offline_test`` or ``shadow`` as the learning system actor.

    Raises:
        ProposalNotFoundError: no such proposal.
        PromotionRefusedError: the target is not a system stage, the ladder forbids the step,
            the gate is not met (``EVALUATION_MISSING`` / ``EVALUATION_NOT_PASSED`` /
            ``FINGERPRINT_MISMATCH`` / ``REQUIRED_STAGE_MISSING``).
    """
    if to_status not in SYSTEM_STAGES:
        raise _refuse(PromotionRefusal.NOT_A_HUMAN)
    moment = now or datetime.now(UTC)
    async with sessionmaker() as session, session.begin():
        proposal = await repo.lock_proposal(session, proposal_id)
        if proposal is None:
            raise ProposalNotFoundError
        gate_evaluation = await check_stage_gate(session, proposal, to_status)
        from ken_services.learning.promote import record_stage

        await record_stage(
            session,
            proposal=proposal,
            to_status=to_status,
            actor=LEARNING_ACTOR,
            now=moment,
            evaluation_run_id=None,
        )
    return StageGate(stage=to_status, evaluation_id=gate_evaluation)


async def check_stage_gate(
    session: AsyncSession, proposal: LearningProposal, to_status: ProposalStatus
) -> uuid.UUID | None:
    """The gate for entering ``offline_test`` or ``shadow``, whoever records the stage.

    Returns the passing evaluation id for ``offline_test`` (``None`` for ``shadow``). A human
    moving a proposal into one of these stages meets the SAME gate: the stage means "the work
    was done", not "someone said so".
    """
    if to_status is ProposalStatus.OFFLINE_TEST:
        return await _offline_gate(session, proposal)
    if to_status is ProposalStatus.SHADOW:
        transitions = await repo.proposal_transitions(session, proposal.id)
        if ProposalStatus.OFFLINE_TEST not in stages_reached(transitions):
            raise _refuse(PromotionRefusal.REQUIRED_STAGE_MISSING)
        if proposal.target_kind not in SHADOWABLE:
            raise _refuse(PromotionRefusal.TRANSITION_NOT_ALLOWED)
    return None


async def _offline_gate(session: AsyncSession, proposal: LearningProposal) -> uuid.UUID:
    skill = await repo.skill_for_proposal(session, proposal.id)
    runs = await repo.evaluations_for_proposal(
        session,
        proposal.id,
        mode=EvaluationMode.OFFLINE,
        skill_id=None if skill is None else skill.id,
    )
    if not runs:
        raise _refuse(PromotionRefusal.EVALUATION_MISSING)
    latest = runs[0]
    if latest.proposal_fingerprint != proposal.fingerprint:
        raise _refuse(PromotionRefusal.FINGERPRINT_MISMATCH)
    if latest.gate_result != GateResult.PASS.value:
        raise _refuse(PromotionRefusal.EVALUATION_NOT_PASSED)
    return latest.id


def required_stages_ok(
    *, policy: LearningPolicy, key: str, transitions: Sequence[HasToStatus]
) -> bool:
    """:func:`required_stages_met` over a proposal's recorded history."""
    return required_stages_met(policy=policy, key=key, reached=stages_reached(transitions))
