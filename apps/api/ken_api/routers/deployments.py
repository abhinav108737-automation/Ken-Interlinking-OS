"""Phase 6 controlled deployment API (ADR-0014 section 7). FROZEN SIGNATURES (orchestrator);
the backend implements the bodies. Route functions carry no docstrings on purpose."""

from __future__ import annotations

from typing import Annotated, Any, Final
from uuid import UUID

from fastapi import APIRouter, Path, Query

from ken_api.security import Deployer, Reader
from ken_contracts.deployment import (
    DeploymentDetail,
    DeploymentStatus,
    DeploymentView,
    DeployRequest,
    DryRunRequest,
    DryRunResult,
    RollbackRequest,
)
from ken_contracts.errors import ErrorResponse, ListResponse

router = APIRouter(tags=["deployments"])

_AUTH: Final[dict[int | str, dict[str, Any]]] = {
    401: {"model": ErrorResponse, "description": "Not authenticated."},
    403: {"model": ErrorResponse, "description": "The role lacks the permission."},
    422: {"model": ErrorResponse, "description": "A parameter or the body is invalid."},
}
_MUTATION: Final[dict[int | str, dict[str, Any]]] = {
    **_AUTH,
    404: {"model": ErrorResponse, "description": "No such plan or deployment."},
    409: {
        "model": ErrorResponse,
        "description": "Refused by validation, CMS version conflict, or reused operation id.",
    },
    503: {"model": ErrorResponse, "description": "The CMS is unavailable."},
}
DeploymentId = Annotated[UUID, Path()]


@router.post(
    "/deployments/dry-run",
    operation_id="dryRunDeployment",
    summary="Validate and preview deploying approved plans of one source page (writes nothing).",
    response_model=DryRunResult,
    responses=_MUTATION,
)
async def dry_run(principal: Deployer, body: DryRunRequest) -> DryRunResult:
    raise NotImplementedError


@router.post(
    "/deployments",
    operation_id="createDeployment",
    summary="Deploy approved plans of one source page through the CMS adapter, then verify.",
    response_model=DeploymentDetail,
    responses=_MUTATION,
)
async def deploy(principal: Deployer, body: DeployRequest) -> DeploymentDetail:
    raise NotImplementedError


@router.post(
    "/deployments/{deployment_id}/verify",
    operation_id="verifyDeployment",
    summary="Re-run post-publish verification of a deployment.",
    response_model=DeploymentDetail,
    responses=_MUTATION,
)
async def verify(principal: Deployer, deployment_id: DeploymentId) -> DeploymentDetail:
    raise NotImplementedError


@router.post(
    "/deployments/{deployment_id}/rollback",
    operation_id="rollbackDeployment",
    summary="Roll a deployment back through the CMS adapter (idempotent).",
    response_model=DeploymentDetail,
    responses=_MUTATION,
)
async def rollback(
    principal: Deployer, deployment_id: DeploymentId, body: RollbackRequest
) -> DeploymentDetail:
    raise NotImplementedError


@router.get(
    "/deployments",
    operation_id="listDeployments",
    summary="Deployment history, newest first.",
    response_model=ListResponse[DeploymentView],
    responses=_AUTH,
)
async def list_deployments(
    principal: Reader,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0, le=10_000_000)] = 0,
    status: DeploymentStatus | None = None,
    source_page_id: UUID | None = None,
) -> ListResponse[DeploymentView]:
    raise NotImplementedError


@router.get(
    "/deployments/{deployment_id}",
    operation_id="getDeployment",
    summary="One deployment with items, preview and verification runs.",
    response_model=DeploymentDetail,
    responses={**_AUTH, 404: {"model": ErrorResponse, "description": "No such deployment."}},
)
async def get_deployment(principal: Reader, deployment_id: DeploymentId) -> DeploymentDetail:
    raise NotImplementedError


@router.get(
    "/recommendations/{plan_id}/deployments",
    operation_id="listPlanDeployments",
    summary="The deployments that included one plan.",
    response_model=list[DeploymentView],
    responses={**_AUTH, 404: {"model": ErrorResponse, "description": "No such plan."}},
)
async def plan_deployments(
    principal: Reader, plan_id: Annotated[UUID, Path()]
) -> list[DeploymentView]:
    raise NotImplementedError
