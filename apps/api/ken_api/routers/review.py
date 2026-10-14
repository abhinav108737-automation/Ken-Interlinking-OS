"""Phase 5 human review API (ADR-0013 sections 4 and 7). Route functions carry no
docstrings on purpose. Every mutation derives the actor from the session."""

from __future__ import annotations

from typing import Annotated, Any, Final
from uuid import UUID

from fastapi import APIRouter, Path, Query

from ken_api.security import Auditor, Reader, Reviewer
from ken_contracts.errors import ErrorResponse, ListResponse
from ken_contracts.review import (
    ApproveRequest,
    BulkReviewRequest,
    BulkReviewResult,
    EditAnchorRequest,
    RejectRequest,
    ReplaceTargetRequest,
    ReviewAction,
    ReviewActionView,
    ReviewResult,
    TargetOption,
)

router = APIRouter(tags=["review"])

_AUTH: Final[dict[int | str, dict[str, Any]]] = {
    401: {"model": ErrorResponse, "description": "Not authenticated."},
    403: {"model": ErrorResponse, "description": "The role lacks the permission."},
    422: {"model": ErrorResponse, "description": "A parameter or the body is invalid."},
}
_MUTATION: Final[dict[int | str, dict[str, Any]]] = {
    **_AUTH,
    404: {"model": ErrorResponse, "description": "No such plan."},
    409: {
        "model": ErrorResponse,
        "description": "Status mismatch, stale plan, invalid pair or reused operation id.",
    },
}

PlanId = Annotated[UUID, Path()]
Limit = Annotated[int, Query(ge=1, le=200)]
Offset = Annotated[int, Query(ge=0, le=10_000_000)]


@router.post(
    "/recommendations/{plan_id}/approve",
    operation_id="approvePlan",
    summary="Approve a plan (via awaiting_approval when needed).",
    response_model=ReviewResult,
    responses=_MUTATION,
)
async def approve(principal: Reviewer, plan_id: PlanId, body: ApproveRequest) -> ReviewResult:
    raise NotImplementedError


@router.post(
    "/recommendations/{plan_id}/reject",
    operation_id="rejectPlan",
    summary="Reject a plan (or reverse an approval) with structured reasons.",
    response_model=ReviewResult,
    responses=_MUTATION,
)
async def reject(principal: Reviewer, plan_id: PlanId, body: RejectRequest) -> ReviewResult:
    raise NotImplementedError


@router.post(
    "/recommendations/{plan_id}/edit-anchor",
    operation_id="editPlanAnchor",
    summary="Supersede a plan with a new verbatim span in the same block.",
    response_model=ReviewResult,
    responses=_MUTATION,
)
async def edit_anchor(
    principal: Reviewer, plan_id: PlanId, body: EditAnchorRequest
) -> ReviewResult:
    raise NotImplementedError


@router.post(
    "/recommendations/{plan_id}/replace-target",
    operation_id="replacePlanTarget",
    summary="Supersede a plan with the same anchor span pointing at another page.",
    response_model=ReviewResult,
    responses=_MUTATION,
)
async def replace_target(
    principal: Reviewer, plan_id: PlanId, body: ReplaceTargetRequest
) -> ReviewResult:
    raise NotImplementedError


@router.post(
    "/recommendations/bulk-review",
    operation_id="bulkReviewPlans",
    summary="Approve or reject up to 50 awaiting_approval plans (per-plan results).",
    response_model=BulkReviewResult,
    responses=_MUTATION,
)
async def bulk_review(principal: Reviewer, body: BulkReviewRequest) -> BulkReviewResult:
    raise NotImplementedError


@router.get(
    "/recommendations/{plan_id}/target-options",
    operation_id="listPlanTargetOptions",
    summary="Eligible replacement destinations for a plan's anchor.",
    response_model=list[TargetOption],
    responses={**_AUTH, 404: {"model": ErrorResponse, "description": "No such plan."}},
)
async def target_options(
    principal: Reviewer, plan_id: PlanId, limit: Annotated[int, Query(ge=1, le=50)] = 20
) -> list[TargetOption]:
    raise NotImplementedError


@router.get(
    "/recommendations/{plan_id}/review-actions",
    operation_id="listPlanReviewActions",
    summary="The human review history of one plan.",
    response_model=list[ReviewActionView],
    responses={**_AUTH, 404: {"model": ErrorResponse, "description": "No such plan."}},
)
async def plan_review_actions(principal: Reader, plan_id: PlanId) -> list[ReviewActionView]:
    raise NotImplementedError


@router.get(
    "/review/actions",
    operation_id="listReviewActions",
    summary="Every human review action, newest first (audit).",
    response_model=ListResponse[ReviewActionView],
    responses=_AUTH,
)
async def list_actions(
    principal: Auditor,
    limit: Limit = 50,
    offset: Offset = 0,
    action: ReviewAction | None = None,
    actor: Annotated[str | None, Query(max_length=210)] = None,
) -> ListResponse[ReviewActionView]:
    raise NotImplementedError
