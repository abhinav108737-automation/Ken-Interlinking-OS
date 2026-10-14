"""The ADR-0013 s7 route -> permission table, stated independently of the code under test.

``expected_permission(method, path)`` is ``"public"``, ``"session"`` (any authenticated
principal) or a :class:`~ken_contracts.auth.Permission` value. The introspection test and the
real-auth matrix test both check every route of the app against it, so a new route without a
row here fails both.
"""

from __future__ import annotations

import re
import uuid
from typing import Any, Final

PUBLIC: Final = "public"
SESSION: Final = "session"

_EXACT: Final[dict[tuple[str, str], str]] = {
    ("GET", "/health"): PUBLIC,
    ("GET", "/ready"): PUBLIC,
    ("POST", "/auth/login"): PUBLIC,
    ("GET", "/auth/me"): SESSION,
    ("POST", "/auth/logout"): SESSION,
    ("GET", "/meta"): "read",
    ("GET", "/dashboard"): "read",
    ("GET", "/recommendations/{plan_id}/review-actions"): "read",
    ("POST", "/recommendations/{plan_id}/approve"): "review",
    ("POST", "/recommendations/{plan_id}/reject"): "review",
    ("POST", "/recommendations/{plan_id}/edit-anchor"): "review",
    ("POST", "/recommendations/{plan_id}/replace-target"): "review",
    ("POST", "/recommendations/bulk-review"): "review",
    ("GET", "/recommendations/{plan_id}/target-options"): "review",
    ("GET", "/review/actions"): "read_audit",
    ("GET", "/events"): "read_audit",
    ("GET", "/settings/policies"): "read_settings",
}
_READ_PREFIXES: Final = ("/inventory", "/intelligence", "/jobs", "/recommendations", "/agents")


def expected_permission(method: str, path: str) -> str | None:
    """The permission ADR-0013 s7 assigns to a route, or ``None`` if it has no row."""
    if (method, path) in _EXACT:
        return _EXACT[(method, path)]
    if method == "GET" and path.startswith(_READ_PREFIXES):
        return "read"
    return None


ROLE_PERMISSIONS: Final[dict[str, frozenset[str]]] = {
    "viewer": frozenset({"read"}),
    "reviewer": frozenset({"read", "review"}),
    "operator_admin": frozenset({"read", "review", "read_audit", "read_settings"}),
}
"""Independent copy of the ADR-0013 s3 matrix."""

_PARAM = re.compile(r"\{([^}]+)\}")


def concrete_path(path: str) -> str:
    """A routable instance of a templated path (UUIDs, version 1, a node id)."""

    def fill(match: re.Match[str]) -> str:
        name = match.group(1)
        if name == "version":
            return "1"
        if name == "node_id":
            return "ev"
        return str(uuid.uuid4())

    return _PARAM.sub(fill, path)


def api_routes(app: Any) -> list[tuple[str, str, Any]]:
    """``(method, path, route)`` of every EFFECTIVE APIRoute (router prefixes and router-level
    dependencies applied; HEAD/OPTIONS excluded)."""
    from fastapi.routing import APIRoute, iter_route_contexts

    out = []
    for context in iter_route_contexts(app.routes):
        route = context.route
        if isinstance(route, APIRoute):
            for method in sorted(set(route.methods or ()) - {"HEAD", "OPTIONS"}):
                out.append((method, route.path, route))
    return out
