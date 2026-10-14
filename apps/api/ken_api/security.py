"""Authentication and permission dependencies (Phase 5, ADR-0013).

FROZEN SIGNATURES (orchestrator); the backend implements the bodies. Deny by default:
every router except the public allowlist depends on :func:`require`.
"""

from __future__ import annotations

from collections.abc import Callable, Coroutine
from typing import Annotated, Any

from fastapi import Depends, HTTPException

from ken_contracts.auth import Permission, Principal

PUBLIC_ROUTES: frozenset[tuple[str, str]] = frozenset(
    {("GET", "/health"), ("GET", "/ready"), ("POST", "/auth/login")}
)
"""(method, path) pairs reachable without a session. ``/docs`` and ``/openapi.json`` are
added only in local/ci environments."""


async def get_principal() -> Principal:
    """The authenticated principal of the request (401 ``unauthenticated`` otherwise)."""
    raise HTTPException(status_code=501, detail="not implemented")


def require(permission: Permission) -> Callable[..., Coroutine[Any, Any, Principal]]:
    """Dependency factory: the principal, if it holds ``permission`` (403 otherwise)."""

    async def dependency(
        principal: Annotated[Principal, Depends(get_principal)],
    ) -> Principal:
        raise HTTPException(status_code=501, detail="not implemented")

    dependency.__name__ = f"require_{permission.value}"
    return dependency


CurrentPrincipal = Annotated[Principal, Depends(get_principal)]
Reader = Annotated[Principal, Depends(require(Permission.READ))]
Reviewer = Annotated[Principal, Depends(require(Permission.REVIEW))]
Auditor = Annotated[Principal, Depends(require(Permission.READ_AUDIT))]
SettingsReader = Annotated[Principal, Depends(require(Permission.READ_SETTINGS))]
