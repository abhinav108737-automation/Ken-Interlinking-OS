"""Phase 5 authentication and authorisation at the API boundary (ADR-0013 s2, s3, s7).

No database: the auth provider is a fake installed on ``app.state``. The real-auth matrix
over every route, with real users and sessions, is tests/integration/test_api_auth.py.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

import pytest
from fastapi.dependencies.models import Dependant
from fastapi.testclient import TestClient
from pydantic import SecretStr

from ken_api.security import PUBLIC_ROUTES, bearer_token, get_principal
from ken_contracts.auth import Principal, Role, SessionInvalidError
from ken_contracts.errors import ErrorResponse
from tests.conftest import make_app, make_principal, make_settings
from tests.support.route_permissions import (
    PUBLIC,
    SESSION,
    api_routes,
    concrete_path,
    expected_permission,
)

GOOD_TOKEN = "A" * 43
EXPIRES = datetime(2026, 10, 14, 21, 0, tzinfo=UTC)


class FakeAuthProvider:
    """Accepts exactly GOOD_TOKEN (as ``role``); records every call."""

    def __init__(self, role: Role = Role.VIEWER) -> None:
        self.role = role
        self.tokens: list[str] = []
        self.logouts: list[uuid.UUID] = []

    async def login(self, *, username: str, password: SecretStr) -> Any:
        raise NotImplementedError

    async def authenticate(self, *, token: str) -> tuple[Principal, datetime]:
        self.tokens.append(token)
        if token != GOOD_TOKEN:
            raise SessionInvalidError
        return make_principal(self.role), EXPIRES

    async def logout(self, *, session_id: uuid.UUID) -> None:
        self.logouts.append(session_id)


def _real_app(role: Role = Role.VIEWER) -> tuple[Any, FakeAuthProvider]:
    app = make_app(real_auth=True)
    provider = FakeAuthProvider(role)
    app.state.auth_provider = provider
    return app, provider


# --------------------------------------------------------------------------- #
# Route introspection: deny by default
# --------------------------------------------------------------------------- #
def _dependency_names(dependant: Dependant) -> set[str]:
    names: set[str] = set()
    for sub in dependant.dependencies:
        call = sub.call
        if call is not None:
            names.add(getattr(call, "__name__", repr(call)))
        names |= _dependency_names(sub)
    return names


def test_every_route_is_public_or_declares_a_permission() -> None:
    app = make_app(real_auth=True)
    routes = api_routes(app)
    assert routes
    for method, path, route in routes:
        names = _dependency_names(route.dependant)
        required = {n.removeprefix("require_") for n in names if n.startswith("require_")}
        expected = expected_permission(method, path)
        assert expected is not None, f"{method} {path} has no row in the ADR-0013 table"
        if expected == PUBLIC:
            assert (method, path) in PUBLIC_ROUTES
            assert "get_principal" not in names, (method, path)
            continue
        assert (method, path) not in PUBLIC_ROUTES
        assert "get_principal" in names, f"{method} {path} is reachable without a session"
        if expected == SESSION:
            assert required == set(), (method, path)
        else:
            assert required == {expected}, (method, path, required)


def test_the_public_allowlist_is_exactly_the_probes_and_login() -> None:
    assert {("GET", "/health"), ("GET", "/ready"), ("POST", "/auth/login")} == PUBLIC_ROUTES


def test_docs_are_only_published_in_local_and_ci() -> None:
    for environment in ("local", "ci"):
        client = TestClient(make_app(make_settings(environment=environment), real_auth=True))
        assert client.get("/openapi.json").status_code == 200
        assert client.get("/docs").status_code == 200


# --------------------------------------------------------------------------- #
# 401: one identical body for every failure
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "header",
    [
        None,
        "",
        "Bearer",
        "Bearer ",
        f"Basic {GOOD_TOKEN}",
        f"Token {GOOD_TOKEN}",
        "Bearer not-the-token-but-long-enough-to-look-like-one-xxxxxxx",
        f"Bearer {GOOD_TOKEN} extra",
        "Bearer " + "A" * 600,
    ],
)
def test_every_authentication_failure_is_the_same_401(header: str | None) -> None:
    app, _provider = _real_app()
    client = TestClient(app)
    headers = {"X-Request-ID": "r-1"} | ({} if header is None else {"Authorization": header})
    response = client.get("/meta", headers=headers)
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"
    assert response.json() == {
        "code": "unauthenticated",
        "message": "Unauthorized",
        "details": [],
        "request_id": "r-1",
    }
    assert "retry-after" not in response.headers


def test_the_token_is_read_only_from_the_authorization_header() -> None:
    app, provider = _real_app()
    client = TestClient(app)
    client.cookies.set("token", GOOD_TOKEN)
    assert client.get("/meta", params={"token": GOOD_TOKEN}).status_code == 401
    assert client.get("/meta", params={"access_token": GOOD_TOKEN}).status_code == 401
    assert provider.tokens == []  # never even consulted
    ok = client.get("/meta", headers={"Authorization": f"bearer {GOOD_TOKEN}"})
    assert ok.status_code == 200
    assert provider.tokens == [GOOD_TOKEN]


def test_probes_and_unknown_routes_need_no_session() -> None:
    app, _provider = _real_app()
    client = TestClient(app)
    assert client.get("/health").status_code == 200
    assert client.get("/does-not-exist").status_code == 404


@pytest.mark.parametrize(
    ("header", "token"),
    [
        (f"Bearer {GOOD_TOKEN}", GOOD_TOKEN),
        (f"bearer   {GOOD_TOKEN}  ", GOOD_TOKEN),
        ("Bearer", None),
        ("Basic abc", None),
        (None, None),
        ("Bearer a b", None),
    ],
)
def test_bearer_token_parsing(header: str | None, token: str | None) -> None:
    assert bearer_token(header) == token


# --------------------------------------------------------------------------- #
# 403
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("role", "path"),
    [
        (Role.VIEWER, "/review/actions"),
        (Role.VIEWER, "/events"),
        (Role.VIEWER, "/settings/policies"),
        (Role.VIEWER, f"/recommendations/{uuid.uuid4()}/target-options"),
        (Role.REVIEWER, "/review/actions"),
        (Role.REVIEWER, "/events"),
        (Role.REVIEWER, "/settings/policies"),
    ],
)
def test_a_role_below_the_permission_is_403(role: Role, path: str) -> None:
    response = TestClient(make_app(role=role)).get(path, headers={"X-Request-ID": "r-2"})
    assert response.status_code == 403
    body = ErrorResponse.model_validate(response.json())
    assert body.code.value == "permission_denied"
    assert body.request_id == "r-2"


@pytest.mark.parametrize(
    "path",
    [
        "/recommendations/{id}/approve",
        "/recommendations/{id}/reject",
        "/recommendations/{id}/edit-anchor",
        "/recommendations/{id}/replace-target",
        "/recommendations/bulk-review",
    ],
)
def test_a_viewer_cannot_review_and_is_refused_before_the_body_is_read(path: str) -> None:
    client = TestClient(make_app(role=Role.VIEWER))
    response = client.post(path.replace("{id}", str(uuid.uuid4())), json={"garbage": True})
    assert response.status_code == 403


def test_auth_me_reflects_the_principal_and_is_not_cached() -> None:
    app, _provider = _real_app(Role.REVIEWER)
    response = TestClient(app).get("/auth/me", headers={"Authorization": f"Bearer {GOOD_TOKEN}"})
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.json() == {
        "username": "test-user",
        "display_name": "Test User",
        "role": "reviewer",
        "permissions": ["read", "review"],
        "session_expires_at": "2026-10-14T21:00:00Z",
    }


def test_logout_revokes_the_current_session() -> None:
    app, provider = _real_app()
    client = TestClient(app)
    response = client.post("/auth/logout", headers={"Authorization": f"Bearer {GOOD_TOKEN}"})
    assert response.status_code == 204
    assert provider.logouts == [make_principal().session_id]
    assert client.post("/auth/logout").status_code == 401


def test_make_app_overrides_only_through_dependency_overrides() -> None:
    app = make_app(role=Role.VIEWER)
    assert get_principal in app.dependency_overrides
    assert get_principal not in make_app(real_auth=True).dependency_overrides


def test_every_guarded_route_documents_401_and_403() -> None:
    app = make_app(real_auth=True)
    paths = app.openapi()["paths"]
    for method, path, _route in api_routes(app):
        if expected_permission(method, path) in (PUBLIC, None):
            continue
        responses = paths[path][method.lower()]["responses"]
        assert "401" in responses, (method, path)
        if expected_permission(method, path) != SESSION:
            assert "403" in responses, (method, path)


def test_every_route_answers_401_without_a_token() -> None:
    app, _provider = _real_app()
    client = TestClient(app, raise_server_exceptions=False)
    for method, path, _route in api_routes(app):
        if expected_permission(method, path) == PUBLIC:
            continue
        response = client.request(method, concrete_path(path), json={})
        assert response.status_code == 401, (method, path, response.status_code)


# --------------------------------------------------------------------------- #
# Error mapping and validation hygiene (ADR-0013 s6, s7)
# --------------------------------------------------------------------------- #
def test_405_is_method_not_allowed() -> None:
    response = TestClient(make_app()).put("/health")
    assert response.status_code == 405
    assert response.json()["code"] == "method_not_allowed"


class _NullDependency:
    """Stands in for a reader/service: these requests fail validation before any call."""


def _app_without_db() -> Any:
    from ken_api import deps

    app = make_app()
    for dependency in (deps.get_recommendation_reader, deps.get_review_service):
        app.dependency_overrides[dependency] = _NullDependency
    return app


def test_a_malformed_uuid_is_not_echoed() -> None:
    response = TestClient(_app_without_db()).get("/recommendations/zz9q-secret-xyz")
    assert response.status_code == 422
    body = ErrorResponse.model_validate(response.json())
    assert body.details[0].location == "path.plan_id"
    assert body.details[0].message == "Invalid value."
    assert body.details[0].code == "uuid_parsing"
    for fragment in ("zz9q", "secret", "`z`", "found"):
        assert fragment not in response.text


def test_an_unknown_body_key_is_not_echoed_and_constraint_messages_survive() -> None:
    client = TestClient(_app_without_db())
    response = client.post(
        f"/recommendations/{uuid.uuid4()}/approve",
        json={
            "operation_id": str(uuid.uuid4()),
            "expected_status": "proposed",
            "<script>alert(1)</script>": 1,
            "note": "x" * 501,
        },
    )
    assert response.status_code == 422
    assert "<script>" not in response.text
    details = ErrorResponse.model_validate(response.json()).details
    locations = {d.location for d in details}
    assert "body.?" in locations
    too_long = next(d for d in details if d.location == "body.note")
    assert "500" in too_long.message  # a constraint, not the value


def test_the_login_password_is_never_echoed_on_422() -> None:
    app, _provider = _real_app()
    client = TestClient(app)
    secret = "hunter2-" + "s" * 300
    response = client.post("/auth/login", json={"username": "alice", "password": secret})
    assert response.status_code == 422
    assert "hunter2" not in response.text


def test_review_routes_are_wired_to_the_service(monkeypatch: pytest.MonkeyPatch) -> None:
    """The route hands body, plan id and the SESSION principal to the service."""
    from ken_api import deps
    from ken_contracts.recommendations import LinkPlanStatus
    from ken_contracts.review import ReviewAction, ReviewResult

    calls: list[dict[str, Any]] = []

    class FakeService:
        async def approve(self, **kwargs: Any) -> ReviewResult:
            calls.append(kwargs)
            return ReviewResult(
                action_id=7,
                operation_id=kwargs["request"].operation_id,
                action=ReviewAction.APPROVE,
                plan_id=kwargs["plan_id"],
                status=LinkPlanStatus.APPROVED,
            )

    app = make_app(role=Role.REVIEWER)
    app.dependency_overrides[deps.get_review_service] = FakeService
    plan_id, op = uuid.uuid4(), uuid.uuid4()
    response = TestClient(app).post(
        f"/recommendations/{plan_id}/approve",
        json={"operation_id": str(op), "expected_status": "awaiting_approval", "actor": "x"},
    )
    assert response.status_code == 422  # an "actor" in the body is refused, never trusted
    response = TestClient(app).post(
        f"/recommendations/{plan_id}/approve",
        json={"operation_id": str(op), "expected_status": "awaiting_approval"},
    )
    assert response.status_code == 200, response.text
    assert calls[0]["plan_id"] == plan_id
    assert calls[0]["principal"].role is Role.REVIEWER
    assert calls[0]["principal"].actor == "user:test-user"
