"""Phase 5 authentication API (ADR-0013). Route functions carry no docstrings on purpose."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Final

from fastapi import APIRouter, Request, Response

from ken_api.deps import AuthProviderDep
from ken_api.security import CurrentPrincipal, unauthenticated
from ken_contracts.auth import (
    AccountLockedError,
    InvalidCredentialsError,
    LoginRequest,
    Principal,
    PrincipalView,
    SessionView,
)
from ken_contracts.errors import ErrorResponse

router = APIRouter(prefix="/auth", tags=["auth"])

_UNAUTH: Final[dict[int | str, dict[str, Any]]] = {
    401: {"model": ErrorResponse, "description": "Not authenticated."},
}
_NO_STORE: Final = "no-store"


def _view(principal: Principal, expires_at: datetime) -> PrincipalView:
    return PrincipalView(
        username=principal.username,
        display_name=principal.display_name,
        role=principal.role,
        permissions=sorted(principal.permissions),
        session_expires_at=expires_at,
    )


@router.post(
    "/login",
    operation_id="login",
    summary="Exchange a username and password for a bearer session token.",
    response_model=SessionView,
    responses={**_UNAUTH, 422: {"model": ErrorResponse, "description": "Invalid body."}},
)
async def login(body: LoginRequest, provider: AuthProviderDep, response: Response) -> SessionView:
    # Unknown user, wrong password, disabled or LOCKED account: one identical 401 (no
    # Retry-After): the response never reveals which (ADR-0013 s7).
    try:
        token, principal, expires_at = await provider.login(
            username=body.username, password=body.password
        )
    except (InvalidCredentialsError, AccountLockedError):
        raise unauthenticated() from None
    response.headers["Cache-Control"] = _NO_STORE  # the token is shown once
    return SessionView(token=token, expires_at=expires_at, user=_view(principal, expires_at))


@router.post(
    "/logout",
    operation_id="logout",
    summary="Revoke the current session.",
    status_code=204,
    responses=_UNAUTH,
)
async def logout(principal: CurrentPrincipal, provider: AuthProviderDep) -> Response:
    await provider.logout(session_id=principal.session_id)
    return Response(status_code=204)


@router.get(
    "/me",
    operation_id="getCurrentUser",
    summary="The authenticated user, role and permissions.",
    response_model=PrincipalView,
    responses=_UNAUTH,
)
async def me(principal: CurrentPrincipal, request: Request, response: Response) -> PrincipalView:
    expires_at = getattr(request.state, "session_expires_at", None)
    if expires_at is None:  # no session behind the principal (never via get_principal)
        raise unauthenticated()
    response.headers["Cache-Control"] = _NO_STORE
    return _view(principal, expires_at)
