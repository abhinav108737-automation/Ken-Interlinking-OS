"""Phase 5 authentication API (ADR-0013). Route functions carry no docstrings on purpose."""

from __future__ import annotations

from typing import Any, Final

from fastapi import APIRouter, Response

from ken_api.security import CurrentPrincipal
from ken_contracts.auth import LoginRequest, PrincipalView, SessionView
from ken_contracts.errors import ErrorResponse

router = APIRouter(prefix="/auth", tags=["auth"])

_UNAUTH: Final[dict[int | str, dict[str, Any]]] = {
    401: {"model": ErrorResponse, "description": "Not authenticated."},
}


@router.post(
    "/login",
    operation_id="login",
    summary="Exchange a username and password for a bearer session token.",
    response_model=SessionView,
    responses={**_UNAUTH, 422: {"model": ErrorResponse, "description": "Invalid body."}},
)
async def login(body: LoginRequest) -> SessionView:
    raise NotImplementedError


@router.post(
    "/logout",
    operation_id="logout",
    summary="Revoke the current session.",
    status_code=204,
    responses=_UNAUTH,
)
async def logout(principal: CurrentPrincipal) -> Response:
    raise NotImplementedError


@router.get(
    "/me",
    operation_id="getCurrentUser",
    summary="The authenticated user, role and permissions.",
    response_model=PrincipalView,
    responses=_UNAUTH,
)
async def me(principal: CurrentPrincipal) -> PrincipalView:
    raise NotImplementedError
