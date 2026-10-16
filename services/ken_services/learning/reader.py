"""``SqlLearningReader``: the frozen :class:`~ken_contracts.learning.LearningReader` over
PostgreSQL (ADR-0016 section 10). The API's ONLY route to stored learning data.

Every method opens its own session from the factory the API hands in, which is the READ ONLY
factory, so a reader cannot write. Shapes are validated by the contract models on the way out:

* **``frozen_fields_untouched`` is honest by construction.** A policy proposal's stored body is
  re-validated by :class:`~ken_contracts.learning.PolicyProposalBody`, whose validators refuse a
  frozen or non-allowlisted path. A stored row that fails that validation (it can only exist if
  something bypassed the boundary) is NEVER served with the flag cleared: the read raises
  :class:`CorruptProposalError` instead, which the API reports as an internal error without
  leaking the row.
* Candidate DOCUMENTS are returned only by :meth:`SqlLearningReader.get_candidate`, whose route
  is ``read_settings``. Everything else carries the digest and never the document.
* A shadow evaluation's ``metrics`` carry one extra ``shadow`` object (the plan-set diff). It is
  removed before :class:`~ken_contracts.learning.EvaluationMetrics` validates the rest, and is
  not part of any frozen response shape.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import timedelta
from typing import Any, Final

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ken_contracts.errors import ListResponse, PageInfo
from ken_contracts.learning import (
    CorpusBanner,
    EvalSuite,
    EvaluationDetail,
    EvaluationMetrics,
    EvaluationMode,
    EvaluationNotFoundError,
    EvaluationSummary,
    ExperimentArm,
    ExperimentAssignmentView,
    ExperimentDetail,
    ExperimentKind,
    ExperimentNotFoundError,
    ExperimentStatus,
    ExperimentUnit,
    ExperimentView,
    GateReason,
    GateResult,
    LearningError,
    LearningOverview,
    LearningPolicy,
    PolicyCandidateRef,
    PolicyCandidateView,
    PolicyProposalBody,
    PromotionPreview,
    PromotionRefusal,
    ProposalDetail,
    ProposalEvidence,
    ProposalKind,
    ProposalNotFoundError,
    ProposalStatus,
    ProposalSummary,
    ProposalTransitionView,
    SkillDetail,
    SkillNotFoundError,
    SkillSource,
    SkillStatus,
    SkillSummary,
    TargetKind,
)
from ken_contracts.model import ModelRole
from ken_core.config import Settings
from ken_core.db.models.learning import (
    Experiment,
    LearningProposal,
    PolicyCandidate,
    PolicyEvaluation,
    Skill,
)
from ken_core.db.repositories import learning as repo
from ken_services.jobs.runner import utc_now
from ken_services.learning.lifecycle import (
    missing_stages,
    policy_key,
    shadow_requirements_met,
    stages_reached,
)
from ken_services.learning.policy import PolicyError, load_configured_learning_policy
from ken_services.learning.promote import plan_promoted_files
from ken_services.recommendations.policy import load_recommendation_policy

__all__ = [
    "CorruptProposalError",
    "SqlLearningReader",
    "build_proposal_detail",
    "transition_view",
]

MAX_ASSIGNMENTS_SERVED: Final = 1000
BANNER_NOTE: Final = (
    "Demonstration data: this corpus is small and synthetic, so no number on this page is "
    "evidence about the live site and no proposal on it may be promoted."
)


class CorruptProposalError(LearningError):
    """A stored proposal does not satisfy its own contract. Never served."""

    def __init__(self) -> None:
        super().__init__("a stored learning proposal failed contract validation")


# --------------------------------------------------------------------------- #
# Row -> view
# --------------------------------------------------------------------------- #
def transition_view(row: Any) -> ProposalTransitionView:
    return ProposalTransitionView.model_validate(
        {
            "id": row.id,
            "proposal_id": row.proposal_id,
            "from_status": row.from_status,
            "to_status": row.to_status,
            "actor": row.actor,
            "reason": row.reason,
            "note": row.note,
            "evaluation_run_id": row.evaluation_run_id,
            "created_at": row.created_at,
        }
    )


def _summary(row: LearningProposal) -> ProposalSummary:
    return ProposalSummary(
        id=row.id,
        kind=ProposalKind(row.kind),
        status=ProposalStatus(row.status),
        target_kind=None if row.target_kind is None else TargetKind(row.target_kind),
        base_version=row.base_version,
        title=row.title,
        fingerprint=row.fingerprint,
        proposed_by=row.proposed_by,
        job_run_id=row.job_run_id,
        model=row.model,
        fallback_used=row.fallback_used,
        supersedes=row.supersedes,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _candidate_ref(row: PolicyCandidate) -> PolicyCandidateRef:
    return PolicyCandidateRef(
        id=row.id,
        proposal_id=row.proposal_id,
        policy_kind=TargetKind(row.policy_kind),
        base_version=row.base_version,
        candidate_version=row.candidate_version,
        document_sha256=row.document_sha256,
        created_at=row.created_at,
    )


def _skill_summary(row: Skill) -> SkillSummary:
    return SkillSummary(
        id=row.id,
        name=row.name,
        version=row.version,
        description=row.description,
        status=SkillStatus(row.status),
        source=SkillSource(row.source),
        proposal_id=row.proposal_id,
        evaluation_run_id=row.evaluation_run_id,
        evaluation_score=row.evaluation_score,
        promoted_by=row.promoted_by,
        promoted_at=row.promoted_at,
        created_at=row.created_at,
    )


def _evaluation_summary(row: PolicyEvaluation) -> EvaluationSummary:
    return EvaluationSummary(
        id=row.id,
        job_run_id=row.job_run_id,
        mode=EvaluationMode(row.mode),
        proposal_id=row.proposal_id,
        candidate_id=row.candidate_id,
        skill_id=row.skill_id,
        proposal_fingerprint=row.proposal_fingerprint,
        suites=tuple(EvalSuite(name) for name in row.suites),
        gate_result=GateResult(row.gate_result),
        baseline_evaluation_id=row.baseline_evaluation_id,
        runner_version=row.runner_version,
        model_provider=row.model_provider,
        git_sha=row.git_sha,
        started_at=row.started_at,
        finished_at=row.finished_at,
    )


def _metrics_of(row: PolicyEvaluation) -> EvaluationMetrics:
    return EvaluationMetrics.model_validate(
        {key: value for key, value in row.metrics.items() if key != "shadow"}
    )


def _evaluation_detail(row: PolicyEvaluation) -> EvaluationDetail:
    base = _evaluation_summary(row).model_dump()
    return EvaluationDetail(
        **base,
        dataset_manifests=dict(row.dataset_manifests),
        metrics=_metrics_of(row),
        gate_reasons=tuple(GateReason.model_validate(item) for item in row.gate_reasons),
        model_role_models={ModelRole(k): str(v) for k, v in row.model_role_models.items()},
        seed=row.seed,
        policy_shas={},
    )


# --------------------------------------------------------------------------- #
# The proposal detail (shared with the promotion service)
# --------------------------------------------------------------------------- #
def _blocked_by(
    *,
    proposal: LearningProposal,
    policy: LearningPolicy,
    settings: Settings,
    history: Sequence[Any],
    evaluations: Sequence[PolicyEvaluation],
    now_age_limit: timedelta,
) -> tuple[PromotionRefusal, ...]:
    refusals: list[PromotionRefusal] = []
    if settings.learning_demonstration_mode:
        refusals.append(PromotionRefusal.DEMONSTRATION_MODE)
    key = policy_key(proposal)
    reached = stages_reached(history)
    if missing_stages(policy=policy, key=key, reached=reached):
        refusals.append(PromotionRefusal.REQUIRED_STAGE_MISSING)
    elif ProposalStatus.SHADOW in policy.gates.required_stages.get(key, ()):
        shadow = [
            dict(run.metrics.get("shadow") or {})
            for run in evaluations
            if run.mode == EvaluationMode.SHADOW.value
        ]
        if not shadow_requirements_met(
            policy=policy, fingerprint=proposal.fingerprint, shadow_metrics=shadow
        ):
            refusals.append(PromotionRefusal.REQUIRED_STAGE_MISSING)
    offline = [run for run in evaluations if run.mode == EvaluationMode.OFFLINE.value]
    if not offline:
        refusals.append(PromotionRefusal.EVALUATION_MISSING)
    else:
        latest = offline[0]
        if latest.proposal_fingerprint != proposal.fingerprint:
            refusals.append(PromotionRefusal.FINGERPRINT_MISMATCH)
        elif latest.gate_result != GateResult.PASS.value:
            refusals.append(PromotionRefusal.EVALUATION_NOT_PASSED)
        elif utc_now() - (latest.finished_at or latest.started_at) > now_age_limit:
            refusals.append(PromotionRefusal.EVALUATION_TOO_OLD)
    return tuple(dict.fromkeys(refusals))


def _preview(
    *,
    proposal: LearningProposal,
    candidate: PolicyCandidate,
    settings: Settings,
    policy: LearningPolicy,
    history: Sequence[Any],
    evaluations: Sequence[PolicyEvaluation],
) -> PromotionPreview:
    try:
        active = load_recommendation_policy(settings.recommendation_policy_path)
    except (PolicyError, OSError):
        active = None
    try:
        files = plan_promoted_files(
            target_kind=str(proposal.target_kind),
            candidate_document=dict(candidate.document),
            proposal_id=proposal.id,
            active_recommendation=active,
        )
        paths = files.paths
    except (LearningError, ValueError):
        paths = ()
    if not paths:
        paths = (f"policies/{proposal.target_kind}/",)
    blocked = _blocked_by(
        proposal=proposal,
        policy=policy,
        settings=settings,
        history=history,
        evaluations=evaluations,
        now_age_limit=timedelta(days=policy.gates.promotion_max_eval_age_days),
    )
    return PromotionPreview(
        target_paths=paths,
        next_version=candidate.candidate_version,
        rollback_to_version=proposal.base_version,
        requires_operator_restart=True,
        blocked=bool(blocked),
        blocked_by=blocked,
    )


async def build_proposal_detail(
    session: AsyncSession,
    *,
    settings: Settings,
    policy: LearningPolicy,
    proposal_id: uuid.UUID,
) -> ProposalDetail:
    """The full detail of one proposal.

    Raises:
        ProposalNotFoundError: no such proposal.
        CorruptProposalError: the stored body or evidence fails its contract.
    """
    proposal = await repo.proposal_by_id(session, proposal_id)
    if proposal is None:
        raise ProposalNotFoundError
    history = await repo.proposal_transitions(session, proposal.id)
    skill = await repo.skill_for_proposal(session, proposal.id)
    evaluations = await repo.evaluations_for_proposal(
        session, proposal.id, skill_id=None if skill is None else skill.id
    )
    candidate = await repo.candidate_for_proposal(session, proposal.id)
    changes: tuple[Any, ...] = ()
    try:
        evidence = ProposalEvidence.model_validate(proposal.evidence)
        if proposal.kind == ProposalKind.POLICY.value:
            changes = PolicyProposalBody.model_validate(proposal.body).changes
    except ValueError:
        raise CorruptProposalError from None
    key = policy_key(proposal)
    preview = None
    if proposal.kind == ProposalKind.POLICY.value and candidate is not None:
        preview = _preview(
            proposal=proposal,
            candidate=candidate,
            settings=settings,
            policy=policy,
            history=history,
            evaluations=evaluations,
        )
    summary = _summary(proposal).model_dump()
    return ProposalDetail(
        **summary,
        rationale=proposal.rationale,
        changes=changes,
        evidence=evidence,
        transitions=tuple(transition_view(row) for row in history),
        evaluations=tuple(_evaluation_summary(row) for row in evaluations),
        candidate=None if candidate is None else _candidate_ref(candidate),
        skill=None if skill is None else _skill_summary(skill),
        promotion=preview,
        required_stages=tuple(policy.gates.required_stages.get(key, ())),
        stages_completed=stages_reached(history),
    )


# --------------------------------------------------------------------------- #
# The reader
# --------------------------------------------------------------------------- #
def _experiment_view(row: Experiment, counts: dict[ExperimentArm, int]) -> ExperimentView:
    return ExperimentView(
        id=row.id,
        key=row.key,
        description=row.description,
        unit=ExperimentUnit(row.unit),
        kind=ExperimentKind(row.kind),
        status=ExperimentStatus(row.status),
        candidate_id=row.candidate_id,
        proposal_id=row.proposal_id,
        treatment_fraction=row.treatment_fraction,
        created_by=row.created_by,
        created_at=row.created_at,
        updated_at=row.updated_at,
        started_at=row.started_at,
        ended_at=row.ended_at,
        control_pages=counts[ExperimentArm.CONTROL],
        treatment_pages=counts[ExperimentArm.TREATMENT],
    )


class SqlLearningReader:
    """:class:`~ken_contracts.learning.LearningReader` over PostgreSQL."""

    def __init__(
        self,
        *,
        sessionmaker: async_sessionmaker[AsyncSession],
        settings: Settings,
        policy: LearningPolicy | None = None,
    ) -> None:
        self._sessionmaker = sessionmaker
        self._settings = settings
        self._policy = policy

    def _learning_policy(self) -> LearningPolicy:
        return self._policy or load_configured_learning_policy(self._settings)

    async def overview(self) -> LearningOverview:
        policy = self._learning_policy()
        async with self._sessionmaker() as session:
            banner = CorpusBanner(
                demonstration_mode=self._settings.learning_demonstration_mode,
                pages_total=await repo.count_rows(session, "pages"),
                plans_total=await repo.count_rows(session, "link_plans"),
                review_actions_total=await repo.count_rows(session, "review_actions"),
                min_sample_count=policy.gates.min_sample_count,
                note=BANNER_NOTE,
            )

            async def grouped(table: str, column: str) -> dict[str, int]:
                # Fixed (table, column) pairs only; never caller-supplied.
                rows = await session.execute(
                    text(f"SELECT {column}, count(*) FROM {table} GROUP BY {column}")  # noqa: S608
                )
                return {str(key): int(str(n)) for key, n in rows.all()}

            reflection = await session.scalar(
                text(
                    "SELECT id FROM job_runs WHERE kind = 'reflection' "
                    "ORDER BY started_at DESC LIMIT 1"
                )
            )
            evaluation = await session.scalar(
                text(
                    "SELECT id FROM job_runs WHERE kind = 'evaluation' "
                    "ORDER BY started_at DESC LIMIT 1"
                )
            )
            return LearningOverview(
                banner=banner,
                proposals_by_status=await grouped("learning_proposals", "status"),
                skills_by_status=await grouped("skills", "status"),
                evaluations_by_gate_result=await grouped("policy_evaluations", "gate_result"),
                experiments_by_status=await grouped("experiments", "status"),
                latest_reflection_run_id=reflection,
                latest_evaluation_run_id=evaluation,
                policy_version=policy.version,
            )

    async def list_proposals(
        self,
        *,
        limit: int,
        offset: int,
        kind: ProposalKind | None = None,
        status: ProposalStatus | None = None,
        target_kind: TargetKind | None = None,
    ) -> ListResponse[ProposalSummary]:
        filters = []
        if kind is not None:
            filters.append(LearningProposal.kind == kind.value)
        if status is not None:
            filters.append(LearningProposal.status == status.value)
        if target_kind is not None:
            filters.append(LearningProposal.target_kind == target_kind.value)
        async with self._sessionmaker() as session:
            total = int(
                await session.scalar(
                    select(func.count()).select_from(LearningProposal).where(*filters)
                )
                or 0
            )
            rows = await session.scalars(
                select(LearningProposal)
                .where(*filters)
                .order_by(LearningProposal.created_at.desc(), LearningProposal.id)
                .limit(limit)
                .offset(offset)
            )
            items = [_summary(row) for row in rows]
        return ListResponse(items=items, page=PageInfo(total=total, limit=limit, offset=offset))

    async def get_proposal(self, *, proposal_id: uuid.UUID) -> ProposalDetail:
        policy = self._learning_policy()
        async with self._sessionmaker() as session:
            return await build_proposal_detail(
                session, settings=self._settings, policy=policy, proposal_id=proposal_id
            )

    async def get_candidate(self, *, proposal_id: uuid.UUID) -> PolicyCandidateView:
        async with self._sessionmaker() as session:
            candidate = await repo.candidate_for_proposal(session, proposal_id)
            if candidate is None:
                raise ProposalNotFoundError
            return PolicyCandidateView(
                **_candidate_ref(candidate).model_dump(), document=dict(candidate.document)
            )

    async def list_evaluations(
        self,
        *,
        limit: int,
        offset: int,
        mode: EvaluationMode | None = None,
        gate_result: GateResult | None = None,
        proposal_id: uuid.UUID | None = None,
    ) -> ListResponse[EvaluationSummary]:
        filters = []
        if mode is not None:
            filters.append(PolicyEvaluation.mode == mode.value)
        if gate_result is not None:
            filters.append(PolicyEvaluation.gate_result == gate_result.value)
        if proposal_id is not None:
            filters.append(PolicyEvaluation.proposal_id == proposal_id)
        async with self._sessionmaker() as session:
            total = int(
                await session.scalar(
                    select(func.count()).select_from(PolicyEvaluation).where(*filters)
                )
                or 0
            )
            rows = await session.scalars(
                select(PolicyEvaluation)
                .where(*filters)
                .order_by(PolicyEvaluation.started_at.desc(), PolicyEvaluation.id)
                .limit(limit)
                .offset(offset)
            )
            items = [_evaluation_summary(row) for row in rows]
        return ListResponse(items=items, page=PageInfo(total=total, limit=limit, offset=offset))

    async def get_evaluation(self, *, evaluation_id: uuid.UUID) -> EvaluationDetail:
        async with self._sessionmaker() as session:
            row = await repo.evaluation_by_id(session, evaluation_id)
            if row is None:
                raise EvaluationNotFoundError
            return _evaluation_detail(row)

    async def list_skills(
        self, *, limit: int, offset: int, status: SkillStatus | None = None
    ) -> ListResponse[SkillSummary]:
        filters = [] if status is None else [Skill.status == status.value]
        async with self._sessionmaker() as session:
            total = int(
                await session.scalar(select(func.count()).select_from(Skill).where(*filters)) or 0
            )
            rows = await session.scalars(
                select(Skill)
                .where(*filters)
                .order_by(Skill.name, Skill.version.desc())
                .limit(limit)
                .offset(offset)
            )
            items = [_skill_summary(row) for row in rows]
        return ListResponse(items=items, page=PageInfo(total=total, limit=limit, offset=offset))

    async def get_skill(self, *, name: str) -> SkillDetail:
        async with self._sessionmaker() as session:
            rows = list(
                await session.scalars(
                    select(Skill).where(Skill.name == name).order_by(Skill.version.desc())
                )
            )
            if not rows:
                raise SkillNotFoundError
            promoted = next((row for row in rows if row.status == SkillStatus.PROMOTED.value), None)
            head = promoted or rows[0]
            return SkillDetail(
                **_skill_summary(head).model_dump(),
                instructions=head.instructions,
                input_schema=dict(head.input_schema),
                output_schema=dict(head.output_schema),
                suites=tuple(EvalSuite(item) for item in head.suites),
                model_role=ModelRole(head.model_role),
                versions=tuple(_skill_summary(row) for row in rows),
            )

    async def list_experiments(
        self, *, limit: int, offset: int, status: ExperimentStatus | None = None
    ) -> ListResponse[ExperimentView]:
        filters = [] if status is None else [Experiment.status == status.value]
        async with self._sessionmaker() as session:
            total = int(
                await session.scalar(select(func.count()).select_from(Experiment).where(*filters))
                or 0
            )
            rows = list(
                await session.scalars(
                    select(Experiment)
                    .where(*filters)
                    .order_by(Experiment.created_at.desc(), Experiment.key)
                    .limit(limit)
                    .offset(offset)
                )
            )
            items = [
                _experiment_view(row, await repo.assignment_arm_counts(session, row.id))
                for row in rows
            ]
        return ListResponse(items=items, page=PageInfo(total=total, limit=limit, offset=offset))

    async def get_experiment(self, *, key: str) -> ExperimentDetail:
        async with self._sessionmaker() as session:
            row = await repo.experiment_by_key(session, key)
            if row is None:
                raise ExperimentNotFoundError
            counts = await repo.assignment_arm_counts(session, row.id)
            assignments = (await repo.assignments_of(session, row.id))[:MAX_ASSIGNMENTS_SERVED]
            view = _experiment_view(row, counts).model_dump()
            return ExperimentDetail(
                **view,
                assignments=tuple(
                    ExperimentAssignmentView(
                        experiment_id=row.id,
                        page_id=item.page_id,
                        arm=item.arm,
                        assigned_by=item.assigned_by,
                        assigned_at=item.assigned_at,
                    )
                    for item in assignments
                ),
                comparison=(),
            )
