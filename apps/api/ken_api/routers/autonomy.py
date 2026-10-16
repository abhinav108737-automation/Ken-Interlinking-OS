"""Phase 9 autonomy API (docs/adr/ADR-0018-autonomy-first-publishing.md sections 5 and 6).

FROZEN SIGNATURES (orchestrator). GET-only except ONE write route. Every body delegates to
the :class:`~ken_contracts.autonomy.AutonomyReader` (READ ONLY sessions) or to the
:class:`~ken_services.autonomy.protocol.OperatorOverrideService`. Route functions carry no
docstrings on purpose.

This is what replaces the approval inbox. Its surfaces are the ones ADR-0018 section 5 leaves
to humans -- monitoring, investigating blocks, and intervening on an exception -- and
deliberately NOT a queue of links to wave through.

Permissions:

* ``read`` -- the decisions, the blocked actions, the publish attempts, the summary and the
  mode. All of it is operational metadata; none of it is a policy document.
* ``deploy`` -- ``POST /autonomy/plans/{plan_id}/override``. **Reusing ``deploy`` rather than
  inventing a permission is the point**: an override either publishes a link or withdraws one
  from publication, which is exactly the capability ADR-0014 gave ``deploy`` (operators only),
  and the route can therefore grant nothing that the holder could not already do through
  ``POST /deployments``. A new permission would widen the role matrix without widening the
  capability, and ADR-0013's role table would then have two names for one power.
  ``review`` would be wrong in the other direction: a reviewer may approve a proposal, not
  write to the live site.

Everything that RUNS autonomy is CLI-only (``make autonomy``, ``make autonomy-shadow``): no UI
button starts an evaluation pass or a publish sweep, for the same reason no UI button starts a
model run in Phase 8.
"""

from __future__ import annotations

from typing import Annotated, Any, Final
from uuid import UUID

from fastapi import APIRouter, Path, Query

from ken_api.deps import AutonomyReaderDep, AutonomyServiceDep
from ken_api.security import Deployer, Reader
from ken_contracts.autonomy import (
    AutonomyDecision,
    AutonomyDecisionView,
    AutonomyHealth,
    AutonomyMode,
    AutonomySummary,
    BlockCode,
    BlockedActionView,
    OperatorOverrideRequest,
    OperatorOverrideResponse,
    PublishAttemptView,
)
from ken_contracts.errors import ErrorResponse, ListResponse

router = APIRouter(tags=["autonomy"])

_AUTH: Final[dict[int | str, dict[str, Any]]] = {
    401: {"model": ErrorResponse, "description": "Not authenticated."},
    403: {"model": ErrorResponse, "description": "The role lacks the permission."},
    422: {"model": ErrorResponse, "description": "A parameter is invalid."},
}
_NOT_FOUND: Final[dict[int | str, dict[str, Any]]] = {
    404: {"model": ErrorResponse, "description": "No such plan, or it was never evaluated."}
}
_CONFLICT: Final[dict[int | str, dict[str, Any]]] = {
    409: {
        "model": ErrorResponse,
        "description": (
            "The override was refused: a deterministic block still fires, the plan's status "
            "moved on, or the retry budget is spent."
        ),
    }
}

Limit = Annotated[int, Query(ge=1, le=200)]
Offset = Annotated[int, Query(ge=0, le=10_000_000)]


@router.get(
    "/autonomy/mode",
    operation_id="getAutonomyMode",
    summary="The autonomy mode in force, and whether anything can actually be published.",
    response_model=AutonomyHealth,
    responses=_AUTH,
)
async def get_mode(principal: Reader, reader: AutonomyReaderDep) -> AutonomyHealth:
    return await reader.get_health()


@router.get(
    "/autonomy",
    operation_id="getAutonomySummary",
    summary="Decision, mode and block-code counts for the dashboard.",
    response_model=AutonomySummary,
    responses=_AUTH,
)
async def get_summary(principal: Reader, reader: AutonomyReaderDep) -> AutonomySummary:
    return await reader.get_summary()


@router.get(
    "/autonomy/decisions",
    operation_id="listAutonomyDecisions",
    summary="Recorded autonomy decisions, newest first.",
    response_model=ListResponse[AutonomyDecisionView],
    responses=_AUTH,
)
async def list_decisions(
    principal: Reader,
    reader: AutonomyReaderDep,
    decision: AutonomyDecision | None = None,
    mode: AutonomyMode | None = None,
    plan_id: UUID | None = None,
    limit: Limit = 50,
    offset: Offset = 0,
) -> ListResponse[AutonomyDecisionView]:
    return await reader.list_decisions(
        limit=limit, offset=offset, decision=decision, mode=mode, plan_id=plan_id
    )


@router.get(
    "/autonomy/blocked",
    operation_id="listBlockedActions",
    summary="Blocked plans and the deterministic codes that fired, for investigation.",
    response_model=ListResponse[BlockedActionView],
    responses=_AUTH,
)
async def list_blocked(
    principal: Reader,
    reader: AutonomyReaderDep,
    code: BlockCode | None = None,
    limit: Limit = 50,
    offset: Offset = 0,
) -> ListResponse[BlockedActionView]:
    return await reader.list_blocked(limit=limit, offset=offset, code=code)


@router.get(
    "/autonomy/attempts",
    operation_id="listPublishAttempts",
    summary="Publish attempts per (source, target): the retry budget and its cool-down.",
    response_model=ListResponse[PublishAttemptView],
    responses=_AUTH,
)
async def list_attempts(
    principal: Reader,
    reader: AutonomyReaderDep,
    plan_id: UUID | None = None,
    limit: Limit = 50,
    offset: Offset = 0,
) -> ListResponse[PublishAttemptView]:
    return await reader.list_attempts(limit=limit, offset=offset, plan_id=plan_id)


@router.post(
    "/autonomy/plans/{plan_id}/override",
    operation_id="overrideAutonomyDecision",
    summary="Operator override: publish or block one plan, with a recorded reason.",
    response_model=OperatorOverrideResponse,
    responses={**_AUTH, **_NOT_FOUND, **_CONFLICT},
)
async def override(
    principal: Deployer,
    service: AutonomyServiceDep,
    plan_id: Annotated[UUID, Path()],
    request: OperatorOverrideRequest,
) -> OperatorOverrideResponse:
    return await service.override(principal=principal, plan_id=plan_id, request=request)
