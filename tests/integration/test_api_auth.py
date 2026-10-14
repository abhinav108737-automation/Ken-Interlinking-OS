"""Real authentication over HTTP on the ``*_test`` database (ADR-0013 s2, s3, s7).

* login / me / logout with real users and sessions;
* every credential failure -- unknown user, wrong password, disabled, LOCKED -- is the same
  401 body, with no Retry-After;
* the permission MATRIX over EVERY route: 401 without a token, 403 for each role below the
  route's permission, neither for a role that holds it;
* redaction: the Authorization header, the token and the password never reach a log line.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from typing import Any

import pytest
import structlog
from sqlalchemy import text

from ken_contracts.auth import Role
from ken_core import logging as ken_logging
from tests.conftest import FakeCMSProvider, FakeModelProvider, make_app
from tests.integration.recommendation_support import SM
from tests.integration.review_support import PASSWORD, api_client, auth_provider, create_user
from tests.support.route_permissions import (
    PUBLIC,
    ROLE_PERMISSIONS,
    SESSION,
    api_routes,
    concrete_path,
    expected_permission,
)

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("migrated")]


def _app(sessionmaker: SM) -> Any:
    app = make_app(real_auth=True)
    app.state.sessionmaker = sessionmaker
    app.state.model_provider = FakeModelProvider()
    app.state.cms_provider = FakeCMSProvider()
    app.state.auth_provider = auth_provider(sessionmaker)
    return app


async def _login(client: Any, username: str, password: str = PASSWORD) -> Any:
    return await client.post("/auth/login", json={"username": username, "password": password})


def _strip(body: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in body.items() if k != "request_id"}


async def test_login_me_logout_over_http(sessionmaker: SM) -> None:
    await create_user(sessionmaker, "alice", role=Role.REVIEWER)
    client = api_client(_app(sessionmaker))
    response = await _login(client, "alice")
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    body = response.json()
    token = body["token"]
    assert body["user"] == {
        "username": "alice",
        "display_name": "Alice",
        "role": "reviewer",
        "permissions": ["read", "review"],
        "session_expires_at": body["expires_at"],
    }
    auth = {"Authorization": f"Bearer {token}"}
    me = await client.get("/auth/me", headers=auth)
    assert me.status_code == 200 and me.json()["username"] == "alice"
    assert (await client.post("/auth/logout", headers=auth)).status_code == 204
    assert (await client.get("/auth/me", headers=auth)).status_code == 401
    assert (await client.post("/auth/logout", headers=auth)).status_code == 401


async def test_every_login_failure_is_the_same_401(sessionmaker: SM) -> None:
    await create_user(sessionmaker, "alice")
    await create_user(sessionmaker, "bob")
    await create_user(sessionmaker, "carol")
    async with sessionmaker() as session, session.begin():
        await session.execute(text("UPDATE users SET disabled = true WHERE username = 'bob'"))
    client = api_client(_app(sessionmaker))
    for _ in range(5):
        assert (await _login(client, "carol", "not the password at all")).status_code == 401
    bodies = []
    for username, password in (
        ("nobody", PASSWORD),
        ("alice", "not the password at all"),
        ("bob", PASSWORD),  # disabled
        ("carol", PASSWORD),  # locked: the right password, the same answer
    ):
        response = await _login(client, username, password)
        assert response.status_code == 401, (username, response.text)
        assert "retry-after" not in response.headers
        assert response.headers["www-authenticate"] == "Bearer"
        bodies.append(_strip(response.json()))
    assert all(b == bodies[0] for b in bodies)
    assert bodies[0] == {"code": "unauthenticated", "message": "Unauthorized", "details": []}


async def test_the_permission_matrix_over_every_route(sessionmaker: SM) -> None:
    for role in Role:
        await create_user(sessionmaker, role.value.replace("_", "-"), role=role)
    app = _app(sessionmaker)
    client = api_client(app, raise_errors=False)
    tokens: dict[str, str] = {
        role.value: (await _login(client, role.value.replace("_", "-"))).json()["token"]
        for role in Role
    }
    checked = 0
    # Logout revokes the session it is called with: check it LAST.
    routes = sorted(api_routes(app), key=lambda r: (r[0], r[1]) == ("POST", "/auth/logout"))
    for method, path, _route in routes:
        required = expected_permission(method, path)
        assert required is not None, (method, path)
        if required == PUBLIC:
            continue
        url = concrete_path(path)
        anonymous = await client.request(method, url, json={})
        assert anonymous.status_code == 401, (method, path, anonymous.status_code)
        for role_name, token in tokens.items():
            response = await client.request(
                method, url, json={}, headers={"Authorization": f"Bearer {token}"}
            )
            allowed = required == SESSION or required in ROLE_PERMISSIONS[role_name]
            if allowed:
                assert response.status_code not in (401, 403), (method, path, role_name)
            else:
                assert response.status_code == 403, (method, path, role_name, response.status_code)
                assert response.json()["code"] == "permission_denied"
            checked += 1
    assert checked >= 3 * 40


@pytest.fixture
def isolated_logging() -> Iterator[None]:
    """Snapshot and restore global logging state (as tests/unit/test_logging.py)."""
    root = logging.getLogger()
    saved_handlers, saved_level = list(root.handlers), root.level
    saved_structlog = structlog.get_config()
    uvicorn_access = logging.getLogger("uvicorn.access")
    saved_access = (list(uvicorn_access.handlers), uvicorn_access.propagate)
    ken_logging.reset_logging()
    try:
        yield
    finally:
        ken_logging.reset_logging()
        structlog.configure(**saved_structlog)
        root.handlers[:] = saved_handlers
        root.setLevel(saved_level)
        uvicorn_access.handlers[:], uvicorn_access.propagate = saved_access


@pytest.mark.usefixtures("isolated_logging")
async def test_tokens_and_passwords_never_reach_the_logs(
    sessionmaker: SM, caplog: pytest.LogCaptureFixture, capsys: pytest.CaptureFixture[str]
) -> None:
    ken_logging.configure_logging(level="DEBUG", fmt="json")
    caplog.set_level(logging.DEBUG)
    await create_user(sessionmaker, "alice")
    client = api_client(_app(sessionmaker), raise_errors=False)
    login = await _login(client, "alice")
    token = login.json()["token"]
    auth = {"Authorization": f"Bearer {token}"}
    await client.get("/auth/me", headers=auth)
    await client.get("/inventory/pages", headers=auth)
    await client.get("/meta", headers={"Authorization": "Bearer " + "Z" * 43})
    await _login(client, "alice", "a wrong password value")
    await client.post("/auth/logout", headers=auth)
    captured = capsys.readouterr()
    logged = caplog.text + captured.err + captured.out
    assert "http_request" in logged  # the access log did run
    for secret in (token, PASSWORD, "a wrong password value", "Z" * 43, "Bearer"):
        assert secret not in logged, secret
    assert "authorization" not in logged.lower()
