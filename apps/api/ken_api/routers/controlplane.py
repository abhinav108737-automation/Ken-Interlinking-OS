"""Phase 5 control-plane reads: dashboard, event log, active policies (ADR-0013 section 7).
GET-only. Route functions carry no docstrings on purpose."""

from __future__ import annotations

from typing import Annotated, Any, Final

from fastapi import APIRouter, Query

from ken_api.security import Auditor, Reader, SettingsReader
from ken_contracts.controlplane import DashboardSummary, EventRecord, PoliciesView
from ken_contracts.errors import ErrorResponse, ListResponse
from ken_contracts.events import EventName

router = APIRouter(tags=["control-plane"])

_AUTH: Final[dict[int | str, dict[str, Any]]] = {
    401: {"model": ErrorResponse, "description": "Not authenticated."},
    403: {"model": ErrorResponse, "description": "The role lacks the permission."},
    422: {"model": ErrorResponse, "description": "A parameter is invalid."},
}


@router.get(
    "/dashboard",
    operation_id="getDashboard",
    summary="Reviewer home-screen summary.",
    response_model=DashboardSummary,
    responses=_AUTH,
)
async def dashboard(principal: Reader) -> DashboardSummary:
    raise NotImplementedError


@router.get(
    "/events",
    operation_id="listEvents",
    summary="The append-only event log, newest first (audit).",
    response_model=ListResponse[EventRecord],
    responses=_AUTH,
)
async def events(
    principal: Auditor,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0, le=10_000_000)] = 0,
    event_name: EventName | None = None,
    subject_id: Annotated[str | None, Query(max_length=200)] = None,
) -> ListResponse[EventRecord]:
    raise NotImplementedError


@router.get(
    "/settings/policies",
    operation_id="getPolicies",
    summary="The active, validated policy documents (read-only).",
    response_model=PoliciesView,
    responses=_AUTH,
)
async def policies(principal: SettingsReader) -> PoliciesView:
    raise NotImplementedError
