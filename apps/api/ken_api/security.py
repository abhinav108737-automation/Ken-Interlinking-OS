"""Authentication and permission dependencies (Phase 5, ADR-0013).

FROZEN SIGNATURES (orchestrator); the backend implements the bodies. Deny by default:
every router except the public allowlist depends on :func:`require`.

* :func:`get_principal` reads ONLY ``Authorization: Bearer <token>`` (no cookie, no query
  parameter) and asks the configured :class:`~ken_contracts.auth.AuthProvider` to
  authenticate it. Every failure -- no header, another scheme, a malformed, unknown,
  expired or revoked token, a disabled user -- is the SAME 401 ``unauthenticated`` with
  ``WWW-Authenticate: Bearer``. The token is never logged.
* :func:`require` returns the principal if its role holds the permission, else 403
  ``permission_denied``.

There is NO setting that disables authentication: tests replace :func:`get_principal`
through ``app.dependency_overrides`` only (``tests.conftest.make_app``).
"""

from __future__ import annotations

from collections.abc import Callable, Coroutine
from typing import Annotated, Any, Final

from fastapi import Depends, HTTPException, Request

from ken_contracts.auth import AuthProvider, Permission, Principal, SessionInvalidError
from ken_contracts.errors import ErrorResponse

PUBLIC_ROUTES: frozenset[tuple[str, str]] = frozenset(
    {("GET", "/health"), ("GET", "/ready"), ("POST", "/auth/login")}
)
"""(method, path) pairs reachable without a session. ``/docs`` and ``/openapi.json`` are
added only in local/ci environments."""

AUTH_RESPONSES: Final[dict[int | str, dict[str, Any]]] = {
    401: {"model": ErrorResponse, "description": "Not authenticated."},
    403: {"model": ErrorResponse, "description": "The role lacks the permission."},
}
"""OpenAPI declaration of the two auth failures, for routers guarded by :func:`require`."""
BEARER_CHALLENGE: Final = {"WWW-Authenticate": "Bearer"}
_SCHEME: Final = "bearer"
_MAX_HEADER_CHARS: Final = 512


def unauthenticated() -> HTTPException:
    """The ONE 401 of this API (identical for every authentication failure)."""
    return HTTPException(status_code=401, headers=dict(BEARER_CHALLENGE))


def bearer_token(header: str | None) -> str | None:
    """The token of an ``Authorization: Bearer <token>`` header value, else ``None``."""
    if header is None or len(header) > _MAX_HEADER_CHARS:
        return None
    scheme, _, token = header.strip().partition(" ")
    token = token.strip()
    if scheme.lower() != _SCHEME or not token or " " in token:
        return None
    return token


def get_auth_provider(request: Request) -> AuthProvider:
    """``app.state.auth_provider`` if a caller installed one, else the local provider over
    the application's sessionmaker (resolved lazily, like the readers in ``ken_api.deps``)."""
    provider = getattr(request.app.state, "auth_provider", None)
    if provider is not None:
        return provider  # type: ignore[no-any-return]
    from ken_api.deps import get_sessionmaker
    from ken_services.auth.provider import LocalAuthProvider

    return LocalAuthProvider(sessionmaker=get_sessionmaker(request))


async def get_principal(request: Request) -> Principal:
    """The authenticated principal of the request (401 ``unauthenticated`` otherwise)."""
    token = bearer_token(request.headers.get("authorization"))
    if token is None:
        raise unauthenticated()
    try:
        principal, expires_at = await get_auth_provider(request).authenticate(token=token)
    except SessionInvalidError:
        raise unauthenticated() from None
    request.state.session_expires_at = expires_at
    return principal


def require(permission: Permission) -> Callable[..., Coroutine[Any, Any, Principal]]:
    """Dependency factory: the principal, if it holds ``permission`` (403 otherwise)."""

    async def dependency(
        principal: Annotated[Principal, Depends(get_principal)],
    ) -> Principal:
        if permission not in principal.permissions:
            raise HTTPException(status_code=403)
        return principal

    dependency.__name__ = f"require_{permission.value}"
    return dependency


CurrentPrincipal = Annotated[Principal, Depends(get_principal)]
Reader = Annotated[Principal, Depends(require(Permission.READ))]
Reviewer = Annotated[Principal, Depends(require(Permission.REVIEW))]
Auditor = Annotated[Principal, Depends(require(Permission.READ_AUDIT))]
SettingsReader = Annotated[Principal, Depends(require(Permission.READ_SETTINGS))]
Deployer = Annotated[Principal, Depends(require(Permission.DEPLOY))]
