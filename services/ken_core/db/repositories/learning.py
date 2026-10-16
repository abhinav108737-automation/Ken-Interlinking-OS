"""Learning repository: SQL primitives for migration 0009 (ADR-0016 section 9, 15).

The caller owns the transaction: nothing here commits. Three rules shape this module:

* :func:`insert_transition` is the ONLY function that writes ``learning_proposal_transitions``.
  It is deliberately in ``ken_core`` and takes the actor as a parameter, so no module under
  ``ken_services/learning`` names the table; the callers (``lifecycle`` for system stages,
  ``promote`` for every human stage) are the two that decide who the actor is.
* :func:`set_proposal_status` must run in the SAME transaction as the transition insert --
  migration 0009's deferred guard compares ``txid`` and refuses a status change with no
  matching same-transaction row.
* Nothing here sets ``ken.learning_promoter`` or ``ken.learning_experiment_activator``. Those
  two settings are established by exactly one service each (``promote`` and ``experiments``).
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import func, insert, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from ken_contracts.learning import (
    EvaluationMode,
    ExperimentArm,
    ExperimentKind,
    ExperimentStatus,
    ProposalStatus,
    SkillStatus,
)
from ken_core.db.models.inventory import Page
from ken_core.db.models.learning import (
    Experiment,
    ExperimentAssignment,
    LearningProposal,
    LearningProposalTransition,
    PolicyCandidate,
    PolicyEvaluation,
    Skill,
)
from ken_core.db.models.recommendations import LinkPlan

__all__ = [
    "AssignmentRow",
    "assignment_arm_counts",
    "assignments_of",
    "candidate_by_id",
    "candidate_for_proposal",
    "count_rows",
    "evaluation_by_id",
    "evaluations_for_proposal",
    "experiment_by_id",
    "experiment_by_key",
    "insert_assignments",
    "insert_experiment",
    "insert_shadow_evaluation",
    "insert_transition",
    "live_holdout_control_targets",
    "lock_experiment",
    "lock_proposal",
    "page_ids_for_unit",
    "plan_ids_of_pages",
    "previous_skill_version",
    "proposal_by_id",
    "proposal_transitions",
    "set_experiment_status",
    "set_proposal_status",
    "skill_by_name_status",
    "skill_for_proposal",
    "update_skill_status",
]


# --------------------------------------------------------------------------- #
# Proposals and their transitions
# --------------------------------------------------------------------------- #
async def proposal_by_id(session: AsyncSession, proposal_id: uuid.UUID) -> LearningProposal | None:
    return await session.get(LearningProposal, proposal_id)


async def lock_proposal(session: AsyncSession, proposal_id: uuid.UUID) -> LearningProposal | None:
    """The proposal row ``FOR UPDATE``: two concurrent advances serialise on it."""
    return await session.scalar(
        select(LearningProposal)
        .where(LearningProposal.id == proposal_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )


async def proposal_transitions(
    session: AsyncSession, proposal_id: uuid.UUID
) -> list[LearningProposalTransition]:
    rows = await session.scalars(
        select(LearningProposalTransition)
        .where(LearningProposalTransition.proposal_id == proposal_id)
        .order_by(LearningProposalTransition.id)
    )
    return list(rows)


async def insert_transition(
    session: AsyncSession,
    *,
    proposal_id: uuid.UUID,
    from_status: ProposalStatus,
    to_status: ProposalStatus,
    actor: str,
    reason: str | None = None,
    note: str | None = None,
    evaluation_run_id: uuid.UUID | None = None,
) -> LearningProposalTransition:
    """Append one transition row. ``txid`` is a server default (``pg_current_xact_id()``)."""
    row = LearningProposalTransition(
        proposal_id=proposal_id,
        from_status=from_status.value,
        to_status=to_status.value,
        actor=actor,
        reason=reason,
        note=note,
        evaluation_run_id=evaluation_run_id,
    )
    session.add(row)
    await session.flush()
    await session.refresh(row, attribute_names=["id", "created_at"])
    return row


async def set_proposal_status(
    session: AsyncSession, *, proposal_id: uuid.UUID, status: ProposalStatus, now: datetime
) -> None:
    """Change ``status`` through the ORM so the locked row object stays current (the content
    guard allows exactly ``status`` and ``updated_at`` to move)."""
    row = await session.get(LearningProposal, proposal_id)
    if row is None:
        raise LookupError("proposal vanished while locked")
    row.status = status.value
    row.updated_at = now
    await session.flush()


# --------------------------------------------------------------------------- #
# Candidates, evaluations, skills
# --------------------------------------------------------------------------- #
async def candidate_for_proposal(
    session: AsyncSession, proposal_id: uuid.UUID
) -> PolicyCandidate | None:
    return await session.scalar(
        select(PolicyCandidate)
        .where(PolicyCandidate.proposal_id == proposal_id)
        .order_by(PolicyCandidate.created_at.desc(), PolicyCandidate.id)
        .limit(1)
    )


async def candidate_by_id(session: AsyncSession, candidate_id: uuid.UUID) -> PolicyCandidate | None:
    return await session.get(PolicyCandidate, candidate_id)


async def evaluation_by_id(
    session: AsyncSession, evaluation_id: uuid.UUID
) -> PolicyEvaluation | None:
    return await session.get(PolicyEvaluation, evaluation_id)


async def evaluations_for_proposal(
    session: AsyncSession,
    proposal_id: uuid.UUID,
    *,
    mode: EvaluationMode | None = None,
    skill_id: uuid.UUID | None = None,
) -> list[PolicyEvaluation]:
    """Evaluations of a proposal (and, for a skill proposal, of its skill), newest first."""
    subject = PolicyEvaluation.proposal_id == proposal_id
    if skill_id is not None:
        subject = subject | (PolicyEvaluation.skill_id == skill_id)
    statement = select(PolicyEvaluation).where(subject)
    if mode is not None:
        statement = statement.where(PolicyEvaluation.mode == mode.value)
    rows = await session.scalars(
        statement.order_by(PolicyEvaluation.started_at.desc(), PolicyEvaluation.id)
    )
    return list(rows)


async def insert_shadow_evaluation(
    session: AsyncSession,
    *,
    evaluation_id: uuid.UUID,
    job_run_id: uuid.UUID,
    proposal_id: uuid.UUID,
    candidate_id: uuid.UUID,
    proposal_fingerprint: str,
    suites: Sequence[str],
    metrics: dict[str, Any],
    gate_reasons: Sequence[dict[str, Any]],
    runner_version: str,
    started_at: datetime,
    finished_at: datetime,
) -> None:
    """One ``mode = shadow`` row. A shadow run is NEVER a gate: ``gate_result`` is always
    ``inconclusive``, so no shadow row can satisfy the promotion service's ``pass`` check."""
    await session.execute(
        insert(PolicyEvaluation).values(
            id=evaluation_id,
            job_run_id=job_run_id,
            mode=EvaluationMode.SHADOW.value,
            proposal_id=proposal_id,
            candidate_id=candidate_id,
            skill_id=None,
            proposal_fingerprint=proposal_fingerprint,
            suites=list(suites),
            dataset_manifests={},
            baseline_evaluation_id=None,
            metrics=metrics,
            gate_result="inconclusive",
            gate_reasons=list(gate_reasons),
            runner_version=runner_version,
            model_provider="none",
            model_role_models={},
            seed=None,
            git_sha=None,
            started_at=started_at,
            finished_at=finished_at,
        )
    )


async def skill_for_proposal(session: AsyncSession, proposal_id: uuid.UUID) -> Skill | None:
    return await session.scalar(
        select(Skill)
        .where(Skill.proposal_id == proposal_id)
        .order_by(Skill.version.desc())
        .limit(1)
    )


async def skill_by_name_status(
    session: AsyncSession, *, name: str, status: SkillStatus
) -> Skill | None:
    return await session.scalar(
        select(Skill).where(Skill.name == name, Skill.status == status.value).limit(1)
    )


async def previous_skill_version(session: AsyncSession, *, name: str, below: int) -> int | None:
    """The highest version of ``name`` below ``below`` that was ever promoted (what a skill
    rollback would restore)."""
    return await session.scalar(
        select(func.max(Skill.version)).where(
            Skill.name == name,
            Skill.version < below,
            Skill.promoted_at.is_not(None),
        )
    )


async def update_skill_status(
    session: AsyncSession,
    *,
    skill_id: uuid.UUID,
    status: SkillStatus,
    promoted_by: str | None = None,
    promoted_at: datetime | None = None,
    evaluation_run_id: uuid.UUID | None = None,
    evaluation_score: float | None = None,
) -> None:
    row = await session.get(Skill, skill_id)
    if row is None:
        raise LookupError("skill vanished while locked")
    row.status = status.value
    if promoted_by is not None:
        row.promoted_by = promoted_by
        row.promoted_at = promoted_at
        row.evaluation_run_id = evaluation_run_id
        row.evaluation_score = evaluation_score
    await session.flush()


async def count_rows(session: AsyncSession, table: str) -> int:
    """Row count of one of a FIXED set of tables (never caller-supplied SQL)."""
    allowed = {
        "pages",
        "link_plans",
        "review_actions",
        "learning_proposals",
        "policy_evaluations",
        "experiments",
    }
    if table not in allowed:
        raise ValueError("unsupported table")
    return int(await session.scalar(text(f"SELECT count(*) FROM {table}")) or 0)  # noqa: S608


# --------------------------------------------------------------------------- #
# Experiments
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class AssignmentRow:
    page_id: uuid.UUID
    arm: ExperimentArm
    assigned_by: str
    assigned_at: datetime


async def insert_experiment(
    session: AsyncSession,
    *,
    experiment_id: uuid.UUID,
    key: str,
    description: str,
    kind: ExperimentKind,
    unit: str,
    candidate_id: uuid.UUID | None,
    proposal_id: uuid.UUID | None,
    treatment_fraction: float,
    created_by: str,
    now: datetime,
) -> None:
    await session.execute(
        insert(Experiment).values(
            id=experiment_id,
            key=key,
            description=description,
            unit=unit,
            kind=kind.value,
            candidate_id=candidate_id,
            proposal_id=proposal_id,
            treatment_fraction=treatment_fraction,
            status=ExperimentStatus.DRAFT.value,
            created_by=created_by,
            created_at=now,
            updated_at=now,
        )
    )


async def experiment_by_key(session: AsyncSession, key: str) -> Experiment | None:
    return await session.scalar(select(Experiment).where(Experiment.key == key))


async def experiment_by_id(session: AsyncSession, experiment_id: uuid.UUID) -> Experiment | None:
    return await session.get(Experiment, experiment_id)


async def lock_experiment(session: AsyncSession, key: str) -> Experiment | None:
    return await session.scalar(
        select(Experiment)
        .where(Experiment.key == key)
        .with_for_update()
        .execution_options(populate_existing=True)
    )


async def set_experiment_status(
    session: AsyncSession,
    *,
    experiment_id: uuid.UUID,
    status: ExperimentStatus,
    now: datetime,
) -> None:
    row = await session.get(Experiment, experiment_id)
    if row is None:
        raise LookupError("experiment vanished while locked")
    row.status = status.value
    row.updated_at = now
    if status is ExperimentStatus.ACTIVE:
        row.started_at = now
    if status is ExperimentStatus.ENDED:
        row.ended_at = now
    await session.flush()


async def insert_assignments(
    session: AsyncSession, *, experiment_id: uuid.UUID, rows: Iterable[AssignmentRow]
) -> int:
    values = [
        {
            "experiment_id": experiment_id,
            "page_id": row.page_id,
            "arm": row.arm.value,
            "assigned_by": row.assigned_by,
            "assigned_at": row.assigned_at,
        }
        for row in rows
    ]
    if not values:
        return 0
    await session.execute(insert(ExperimentAssignment), values)
    return len(values)


async def assignments_of(session: AsyncSession, experiment_id: uuid.UUID) -> list[AssignmentRow]:
    rows = await session.scalars(
        select(ExperimentAssignment)
        .where(ExperimentAssignment.experiment_id == experiment_id)
        .order_by(ExperimentAssignment.id)
    )
    return [
        AssignmentRow(
            page_id=row.page_id,
            arm=ExperimentArm(row.arm),
            assigned_by=row.assigned_by,
            assigned_at=row.assigned_at,
        )
        for row in rows
    ]


async def assignment_arm_counts(
    session: AsyncSession, experiment_id: uuid.UUID
) -> dict[ExperimentArm, int]:
    rows = (
        await session.execute(
            select(ExperimentAssignment.arm, func.count())
            .where(ExperimentAssignment.experiment_id == experiment_id)
            .group_by(ExperimentAssignment.arm)
        )
    ).all()
    counts = {ExperimentArm(arm): int(n) for arm, n in rows}
    return {arm: counts.get(arm, 0) for arm in ExperimentArm}


async def live_holdout_control_targets(session: AsyncSession) -> frozenset[uuid.UUID]:
    """Target pages with a ``control`` assignment in an ACTIVE ``holdout`` -- the whole input
    of ``SqlExperimentGuard`` (one query per refresh, never per pair)."""
    rows = await session.scalars(
        select(ExperimentAssignment.page_id)
        .join(Experiment, Experiment.id == ExperimentAssignment.experiment_id)
        .where(
            Experiment.kind == ExperimentKind.HOLDOUT.value,
            Experiment.status == ExperimentStatus.ACTIVE.value,
            ExperimentAssignment.arm == ExperimentArm.CONTROL.value,
        )
    )
    return frozenset(rows)


async def page_ids_for_unit(session: AsyncSession) -> list[uuid.UUID]:
    """Every live, published page id in a stable order -- the population an assignment draws
    from. (Eligibility for linking is the pipeline's decision, not the experiment's.)"""
    rows = await session.scalars(
        select(Page.id)
        .where(Page.gone_at.is_(None), Page.publication_state == "published")
        .order_by(Page.canonical_url, Page.id)
    )
    return list(rows)


async def plan_ids_of_pages(
    session: AsyncSession, *, page_ids: Sequence[uuid.UUID], role: str
) -> list[uuid.UUID]:
    """Plan ids whose ``source`` or ``target`` page is in ``page_ids``."""
    if not page_ids:
        return []
    column = LinkPlan.source_page_id if role == "source" else LinkPlan.target_page_id
    rows = await session.scalars(select(LinkPlan.id).where(column.in_(list(page_ids))))
    return list(rows)
