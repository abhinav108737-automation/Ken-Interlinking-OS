"""Phase 6 controlled deployment API (ADR-0014 section 7). FROZEN SIGNATURES (orchestrator);
the backend implements the bodies. Route functions carry no docstrings on purpose."""

from __future__ import annotations

from typing import Annotated, Any, Final
from uuid import UUID

from fastapi import APIRouter, HTTPException, Path, Query

from ken_api.deps import DeploymentReaderDep, DeploymentServiceDep
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
async def dry_run(
    principal: Deployer, body: DryRunRequest, service: DeploymentServiceDep
) -> DryRunResult:
    try:
        return await service.dry_run(request=body, principal=principal)
    except PermissionError:  # the service's defence in depth (DeploymentPermissionError)
        raise HTTPException(status_code=403) from None


@router.post(
    "/deployments",
    operation_id="createDeployment",
    summary="Deploy approved plans of one source page through the CMS adapter, then verify.",
    response_model=DeploymentDetail,
    responses=_MUTATION,
)
async def deploy(
    principal: Deployer, body: DeployRequest, service: DeploymentServiceDep
) -> DeploymentDetail:
    try:
        return await service.deploy(request=body, principal=principal)
    except PermissionError:
        raise HTTPException(status_code=403) from None


@router.post(
    "/deployments/{deployment_id}/verify",
    operation_id="verifyDeployment",
    summary="Re-run post-publish verification of a deployment.",
    response_model=DeploymentDetail,
    responses=_MUTATION,
)
async def verify(
    principal: Deployer, deployment_id: DeploymentId, service: DeploymentServiceDep
) -> DeploymentDetail:
    try:
        return await service.verify(deployment_id=deployment_id, principal=principal)
    except PermissionError:
        raise HTTPException(status_code=403) from None


@router.post(
    "/deployments/{deployment_id}/rollback",
    operation_id="rollbackDeployment",
    summary="Roll a deployment back through the CMS adapter (idempotent).",
    response_model=DeploymentDetail,
    responses=_MUTATION,
)
async def rollback(
    principal: Deployer,
    deployment_id: DeploymentId,
    body: RollbackRequest,
    service: DeploymentServiceDep,
) -> DeploymentDetail:
    try:
        return await service.rollback(
            deployment_id=deployment_id, request=body, principal=principal
        )
    except PermissionError:
        raise HTTPException(status_code=403) from None


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
    *,
    reader: DeploymentReaderDep,
) -> ListResponse[DeploymentView]:
    return await reader.list_deployments(
        limit=limit, offset=offset, status=status, source_page_id=source_page_id
    )


@router.get(
    "/deployments/{deployment_id}",
    operation_id="getDeployment",
    summary="One deployment with items, preview and verification runs.",
    response_model=DeploymentDetail,
    responses={**_AUTH, 404: {"model": ErrorResponse, "description": "No such deployment."}},
)
async def get_deployment(
    principal: Reader, deployment_id: DeploymentId, reader: DeploymentReaderDep
) -> DeploymentDetail:
    return await reader.get_deployment(deployment_id=deployment_id)


@router.get(
    "/recommendations/{plan_id}/deployments",
    operation_id="listPlanDeployments",
    summary="The deployments that included one plan.",
    response_model=list[DeploymentView],
    responses={**_AUTH, 404: {"model": ErrorResponse, "description": "No such plan."}},
)
async def plan_deployments(
    principal: Reader, plan_id: Annotated[UUID, Path()], reader: DeploymentReaderDep
) -> list[DeploymentView]:
    return await reader.deployments_for_plan(plan_id=plan_id)
