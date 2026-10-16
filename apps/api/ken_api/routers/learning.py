"""Phase 8 learning API (docs/adr/ADR-0016-learning.md section 10 as amended by section 15).

FROZEN SIGNATURES (orchestrator). GET-only except ONE write route. Every body delegates to the
:class:`~ken_contracts.learning.LearningReader` (READ ONLY sessions) or to the
:class:`~ken_services.learning.protocol.PromotionService`. Route functions carry no docstrings
on purpose.

Permissions, deliberately split:

* ``read`` -- proposal, evaluation, skill and experiment METADATA, and the overview banner.
  None of it contains a policy document.
* ``read_settings`` -- ``GET /learning/proposals/{id}/candidate``, the only route that returns
  a candidate POLICY DOCUMENT. ADR-0013 section 7 reserves policy documents to
  ``operator_admin`` through ``read_settings``, so serving a candidate under ``read`` would be
  a permission downgrade.
* ``promote`` -- ``POST /learning/proposals/{id}/advance``, ``operator_admin`` only. Even that
  cannot promote while ``Settings.learning_demonstration_mode`` is set, which is the Phase 8
  default: the service raises ``DEMONSTRATION_MODE`` before any other check.

Everything else is CLI-only (``make reflect``, ``make eval``, ``make shadow``,
``make policy-promote``): no UI button starts a model run, writes a policy file or triggers
egress.
"""

from __future__ import annotations

from typing import Annotated, Any, Final
from uuid import UUID

from fastapi import APIRouter, Path, Query

from ken_api.deps import LearningReaderDep, LearningServiceDep
from ken_api.security import Promoter, Reader, SettingsReader
from ken_contracts.errors import ErrorResponse, ListResponse
from ken_contracts.learning import (
    AdvanceProposalRequest,
    AdvanceProposalResponse,
    EvaluationDetail,
    EvaluationMode,
    EvaluationSummary,
    ExperimentDetail,
    ExperimentStatus,
    ExperimentView,
    GateResult,
    LearningOverview,
    PolicyCandidateView,
    ProposalDetail,
    ProposalKind,
    ProposalStatus,
    ProposalSummary,
    SkillDetail,
    SkillStatus,
    SkillSummary,
    TargetKind,
)

router = APIRouter(tags=["learning"])

_AUTH: Final[dict[int | str, dict[str, Any]]] = {
    401: {"model": ErrorResponse, "description": "Not authenticated."},
    403: {"model": ErrorResponse, "description": "The role lacks the permission."},
    422: {"model": ErrorResponse, "description": "A parameter is invalid."},
}
_NOT_FOUND: Final[dict[int | str, dict[str, Any]]] = {
    404: {"model": ErrorResponse, "description": "No such record."}
}
_CONFLICT: Final[dict[int | str, dict[str, Any]]] = {
    409: {
        "model": ErrorResponse,
        "description": "The advance was refused; one detail carries the PromotionRefusal code.",
    }
}

Limit = Annotated[int, Query(ge=1, le=200)]
Offset = Annotated[int, Query(ge=0, le=10_000_000)]


@router.get(
    "/learning",
    operation_id="getLearningOverview",
    summary="Corpus banner plus proposal, skill, evaluation and experiment counts.",
    response_model=LearningOverview,
    responses=_AUTH,
)
async def overview(principal: Reader, reader: LearningReaderDep) -> LearningOverview:
    return await reader.overview()


@router.get(
    "/learning/proposals",
    operation_id="listLearningProposals",
    summary="Learning proposals, newest first.",
    response_model=ListResponse[ProposalSummary],
    responses=_AUTH,
)
async def list_proposals(
    principal: Reader,
    reader: LearningReaderDep,
    kind: ProposalKind | None = None,
    status: ProposalStatus | None = None,
    target_kind: TargetKind | None = None,
    limit: Limit = 50,
    offset: Offset = 0,
) -> ListResponse[ProposalSummary]:
    return await reader.list_proposals(
        limit=limit, offset=offset, kind=kind, status=status, target_kind=target_kind
    )


@router.get(
    "/learning/proposals/{proposal_id}",
    operation_id="getLearningProposal",
    summary="One proposal: the parameter diff, the evidence, every stage transition.",
    response_model=ProposalDetail,
    responses={**_AUTH, **_NOT_FOUND},
)
async def get_proposal(
    principal: Reader, reader: LearningReaderDep, proposal_id: Annotated[UUID, Path()]
) -> ProposalDetail:
    return await reader.get_proposal(proposal_id=proposal_id)


@router.get(
    "/learning/proposals/{proposal_id}/candidate",
    operation_id="getLearningProposalCandidate",
    summary="The full candidate policy document (operator only).",
    response_model=PolicyCandidateView,
    responses={**_AUTH, **_NOT_FOUND},
)
async def get_candidate(
    principal: SettingsReader,
    reader: LearningReaderDep,
    proposal_id: Annotated[UUID, Path()],
) -> PolicyCandidateView:
    return await reader.get_candidate(proposal_id=proposal_id)


@router.get(
    "/learning/evaluations",
    operation_id="listLearningEvaluations",
    summary="Evaluation runs, newest first.",
    response_model=ListResponse[EvaluationSummary],
    responses=_AUTH,
)
async def list_evaluations(
    principal: Reader,
    reader: LearningReaderDep,
    mode: EvaluationMode | None = None,
    gate_result: GateResult | None = None,
    proposal_id: UUID | None = None,
    limit: Limit = 50,
    offset: Offset = 0,
) -> ListResponse[EvaluationSummary]:
    return await reader.list_evaluations(
        limit=limit, offset=offset, mode=mode, gate_result=gate_result, proposal_id=proposal_id
    )


@router.get(
    "/learning/evaluations/{evaluation_id}",
    operation_id="getLearningEvaluation",
    summary="One run: per-suite and per-decision-class metrics, the gate and its reasons.",
    response_model=EvaluationDetail,
    responses={**_AUTH, **_NOT_FOUND},
)
async def get_evaluation(
    principal: Reader, reader: LearningReaderDep, evaluation_id: Annotated[UUID, Path()]
) -> EvaluationDetail:
    return await reader.get_evaluation(evaluation_id=evaluation_id)


@router.get(
    "/learning/skills",
    operation_id="listSkills",
    summary="The skill registry.",
    response_model=ListResponse[SkillSummary],
    responses=_AUTH,
)
async def list_skills(
    principal: Reader,
    reader: LearningReaderDep,
    status: SkillStatus | None = None,
    limit: Limit = 50,
    offset: Offset = 0,
) -> ListResponse[SkillSummary]:
    return await reader.list_skills(limit=limit, offset=offset, status=status)


@router.get(
    "/learning/skills/{name}",
    operation_id="getSkill",
    summary="One skill by name: the promoted version, with its history.",
    response_model=SkillDetail,
    responses={**_AUTH, **_NOT_FOUND},
)
async def get_skill(
    principal: Reader,
    reader: LearningReaderDep,
    name: Annotated[str, Path(pattern=r"^[a-z][a-z0-9_]{2,63}$")],
) -> SkillDetail:
    return await reader.get_skill(name=name)


@router.get(
    "/learning/experiments",
    operation_id="listExperiments",
    summary="Experiments. Phase 8 ships policy_ab plumbing only.",
    response_model=ListResponse[ExperimentView],
    responses=_AUTH,
)
async def list_experiments(
    principal: Reader,
    reader: LearningReaderDep,
    status: ExperimentStatus | None = None,
    limit: Limit = 50,
    offset: Offset = 0,
) -> ListResponse[ExperimentView]:
    return await reader.list_experiments(limit=limit, offset=offset, status=status)


@router.get(
    "/learning/experiments/{key}",
    operation_id="getExperiment",
    summary="One experiment: arms, assignments and the observed (not causal) comparison.",
    response_model=ExperimentDetail,
    responses={**_AUTH, **_NOT_FOUND},
)
async def get_experiment(
    principal: Reader,
    reader: LearningReaderDep,
    key: Annotated[str, Path(pattern=r"^[a-z0-9][a-z0-9_-]{2,63}$")],
) -> ExperimentDetail:
    return await reader.get_experiment(key=key)


@router.post(
    "/learning/proposals/{proposal_id}/advance",
    operation_id="advanceLearningProposal",
    summary="Advance a proposal's stage. The only write route of the phase.",
    response_model=AdvanceProposalResponse,
    responses={**_AUTH, **_NOT_FOUND, **_CONFLICT},
)
async def advance(
    principal: Promoter,
    service: LearningServiceDep,
    proposal_id: Annotated[UUID, Path()],
    request: AdvanceProposalRequest,
) -> AdvanceProposalResponse:
    return await service.advance(principal=principal, proposal_id=proposal_id, request=request)
