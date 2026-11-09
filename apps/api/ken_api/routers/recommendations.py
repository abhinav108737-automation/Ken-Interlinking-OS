"""Phase 3 recommendation read API: link plans, candidates, opportunities, modules.

READ-ONLY. Recommendation runs start from the command line (``make recommend``);
approving, rejecting and editing plans is the Phase 5 review API
(``ken_api.routers.review``). Every route here needs the ``read`` permission
(ADR-0013, router-level). Nothing here can publish.

Route functions carry no docstrings on purpose (see routers/health.py).
"""

from __future__ import annotations

from typing import Annotated, Any, Final
from uuid import UUID

from fastapi import APIRouter, Depends, Path, Query

from ken_api.deps import RecommendationReaderDep
from ken_api.security import AUTH_RESPONSES, require
from ken_contracts.auth import Permission
from ken_contracts.errors import ErrorResponse, ListResponse
from ken_contracts.recommendations import (
    CandidateView,
    LinkPlan,
    LinkPlanDetail,
    LinkPlanStatus,
    ModuleKind,
    OpportunityKind,
    OpportunityView,
    PageModuleView,
    RecommendationSummary,
)

# Deny by default (ADR-0013 s7): every route here needs the `read` permission.
_READ: Final = [Depends(require(Permission.READ))]
router = APIRouter(
    prefix="/recommendations",
    tags=["recommendations"],
    dependencies=_READ,
    responses=AUTH_RESPONSES,
)

_ERRORS: Final[dict[int | str, dict[str, Any]]] = {
    422: {"model": ErrorResponse, "description": "A parameter is invalid."},
}
_NOT_FOUND: Final[dict[int | str, dict[str, Any]]] = {
    404: {"model": ErrorResponse, "description": "No such resource."},
    **_ERRORS,
}

Limit = Annotated[int, Query(ge=1, le=200)]
Offset = Annotated[int, Query(ge=0, le=10_000_000)]


@router.get(
    "/summary",
    operation_id="getRecommendationSummary",
    summary="Plan counts by status, coverage and opportunity counts.",
    response_model=RecommendationSummary,
)
async def get_summary(reader: RecommendationReaderDep) -> RecommendationSummary:
    return await reader.get_summary()


@router.get(
    "/candidates",
    operation_id="listLinkCandidates",
    summary="Retrieved candidates of a run or source page, kept or rejected.",
    response_model=ListResponse[CandidateView],
    responses=_ERRORS,
)
async def list_candidates(
    reader: RecommendationReaderDep,
    limit: Limit = 50,
    offset: Offset = 0,
    run_id: UUID | None = None,
    source_page_id: UUID | None = None,
    rejected: bool | None = None,
) -> ListResponse[CandidateView]:
    return await reader.list_candidates(
        limit=limit, offset=offset, run_id=run_id, source_page_id=source_page_id, rejected=rejected
    )


@router.get(
    "/opportunities",
    operation_id="listOpportunities",
    summary="Orphan, under-linked and link-capacity pages, strongest first.",
    response_model=ListResponse[OpportunityView],
    responses=_ERRORS,
)
async def list_opportunities(
    reader: RecommendationReaderDep,
    limit: Limit = 50,
    offset: Offset = 0,
    kind: OpportunityKind | None = None,
) -> ListResponse[OpportunityView]:
    return await reader.list_opportunities(limit=limit, offset=offset, kind=kind)


@router.get(
    "/modules",
    operation_id="listPageModules",
    summary="Related-report modules proposed for a page, items in render order.",
    response_model=ListResponse[PageModuleView],
    responses=_ERRORS,
)
async def list_modules(
    reader: RecommendationReaderDep,
    limit: Limit = 50,
    offset: Offset = 0,
    kind: ModuleKind | None = None,
    page_id: UUID | None = None,
) -> ListResponse[PageModuleView]:
    return await reader.list_modules(limit=limit, offset=offset, kind=kind, page_id=page_id)


@router.get(
    "",
    operation_id="listLinkPlans",
    summary="Link plans (recommendations), highest score first.",
    response_model=ListResponse[LinkPlan],
    responses=_ERRORS,
)
async def list_plans(
    reader: RecommendationReaderDep,
    limit: Limit = 50,
    offset: Offset = 0,
    status: LinkPlanStatus | None = None,
    source_page_id: UUID | None = None,
    target_page_id: UUID | None = None,
    min_score: Annotated[float | None, Query(ge=0.0, le=1.0)] = None,
) -> ListResponse[LinkPlan]:
    return await reader.list_plans(
        limit=limit,
        offset=offset,
        status=status,
        source_page_id=source_page_id,
        target_page_id=target_page_id,
        min_score=min_score,
    )


@router.get(
    "/{plan_id}",
    operation_id="getLinkPlan",
    summary="One link plan with its block text and status history.",
    response_model=LinkPlanDetail,
    responses=_NOT_FOUND,
)
async def get_plan(
    reader: RecommendationReaderDep, plan_id: Annotated[UUID, Path()]
) -> LinkPlanDetail:
    return await reader.get_plan(plan_id=plan_id)
