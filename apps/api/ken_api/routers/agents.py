"""Phase 4 agent read API: supervised review runs, their steps and plan reviews.

GET-ONLY (ADR-0012 section 11). No endpoint starts, resumes or edits a run: runs start from
the command line (``make agents``), and no unauthenticated endpoint may trigger model work
or move a plan. The Phase 5 reviewer reads a plan's agent review from
``/agents/plans/{plan_id}/reviews``.

Route functions carry no docstrings on purpose (see routers/health.py).
"""

from __future__ import annotations

from typing import Annotated, Any, Final
from uuid import UUID

from fastapi import APIRouter, Depends, Path, Query

from ken_api.deps import AgentReaderDep
from ken_api.security import AUTH_RESPONSES, require
from ken_contracts.agents import (
    AgentOutcome,
    AgentPlanReview,
    AgentRunDetail,
    AgentRunStatus,
    AgentRunSummary,
)
from ken_contracts.auth import Permission
from ken_contracts.errors import ErrorResponse, ListResponse

# Deny by default (ADR-0013 s7): every route here needs the `read` permission.
_READ: Final = [Depends(require(Permission.READ))]
router = APIRouter(prefix="/agents", tags=["agents"], dependencies=_READ, responses=AUTH_RESPONSES)

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
    "/runs",
    operation_id="listAgentRuns",
    summary="Agent review runs, newest first.",
    response_model=ListResponse[AgentRunSummary],
    responses=_ERRORS,
)
async def list_runs(
    reader: AgentReaderDep,
    limit: Limit = 50,
    offset: Offset = 0,
    status: AgentRunStatus | None = None,
    outcome: AgentOutcome | None = None,
    source_page_id: UUID | None = None,
) -> ListResponse[AgentRunSummary]:
    return await reader.list_runs(
        status=status,
        outcome=outcome,
        source_page_id=source_page_id,
        limit=limit,
        offset=offset,
    )


@router.get(
    "/runs/{run_id}",
    operation_id="getAgentRun",
    summary="One agent run with its step timeline and plan reviews.",
    response_model=AgentRunDetail,
    responses=_NOT_FOUND,
)
async def get_run(reader: AgentReaderDep, run_id: Annotated[UUID, Path()]) -> AgentRunDetail:
    return await reader.get_run(run_id=run_id)


@router.get(
    "/plans/{plan_id}/reviews",
    operation_id="listAgentPlanReviews",
    summary="The agent review(s) of one link plan (empty if no agent reviewed it).",
    response_model=list[AgentPlanReview],
    responses=_ERRORS,
)
async def list_plan_reviews(
    reader: AgentReaderDep, plan_id: Annotated[UUID, Path()]
) -> list[AgentPlanReview]:
    return await reader.reviews_for_plan(plan_id=plan_id)
