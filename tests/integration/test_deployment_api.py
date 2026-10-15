"""The /deployments routes end to end on the ``*_test`` database: real service (real HTML
library, in-memory CMS over the inventory's pages), real READ ONLY reader, real auth.
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

import httpx
import pytest
from sqlalchemy import text

from ken_contracts.auth import Role
from ken_contracts.deployment import DeploymentDetail, DeploymentView, DryRunResult
from ken_contracts.errors import ErrorResponse, ListResponse
from ken_services.deployment.service import deploy_lock_key
from tests.conftest import FakeModelProvider, make_app
from tests.integration.deployment_support import World, world
from tests.integration.recommendation_support import CHARGING, EV, LI, SETTINGS, SM
from tests.integration.review_support import api_client, auth_provider, create_user

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("migrated")]


@pytest.fixture
async def w(sessionmaker: SM, tmp_path: Path) -> World:
    return await world(sessionmaker, tmp_path)


async def _client(w: World, username: str, role: Role) -> httpx.AsyncClient:
    """Real Bearer authentication for a real user; the deployment service installed on the
    app state (everything below the route is real)."""
    await create_user(w.sessionmaker, username, role=role)
    app = make_app(SETTINGS, real_auth=True)
    app.state.sessionmaker = w.sessionmaker
    app.state.model_provider = FakeModelProvider()
    app.state.cms_provider = w.cms
    app.state.auth_provider = auth_provider(w.sessionmaker)
    app.state.deployment_service = w.service()
    client = api_client(app, raise_errors=False)
    from tests.integration.review_support import PASSWORD

    login = await client.post("/auth/login", json={"username": username, "password": PASSWORD})
    assert login.status_code == 200, login.text
    client.headers["Authorization"] = f"Bearer {login.json()['token']}"
    return client


def _error(response: Any, status: int) -> ErrorResponse:
    assert response.status_code == status, response.text
    return ErrorResponse.model_validate(response.json())


async def test_deploy_over_http_from_dry_run_to_rollback(sessionmaker: SM, w: World) -> None:
    ids = [str(p) for p in await w.approve(w.plans[(EV, CHARGING)], w.plans[(EV, LI)])]
    client = await _client(w, "oper", Role.OPERATOR_ADMIN)

    dry = await client.post(
        "/deployments/dry-run", json={"operation_id": str(uuid.uuid4()), "plan_ids": ids}
    )
    assert dry.status_code == 200, dry.text
    assert DryRunResult.model_validate(dry.json()).valid

    body = {"operation_id": str(uuid.uuid4()), "plan_ids": ids, "note": "ship it"}
    created = await client.post("/deployments", json=body)
    assert created.status_code == 200, created.text
    detail = DeploymentDetail.model_validate(created.json())
    assert detail.status.value == "verified" and detail.actor == "user:oper"

    replay = DeploymentDetail.model_validate((await client.post("/deployments", json=body)).json())
    assert replay.idempotent_replay and replay.id == detail.id
    reused = _error(await client.post("/deployments", json=body | {"note": "other"}), 409)
    assert reused.code.value == "conflict" and reused.details == []

    listed = ListResponse[DeploymentView].model_validate((await client.get("/deployments")).json())
    assert [d.id for d in listed.items] == [detail.id]
    got = DeploymentDetail.model_validate((await client.get(f"/deployments/{detail.id}")).json())
    assert got.status.value == "verified" and len(got.verifications) == 1
    for_plan = (await client.get(f"/recommendations/{ids[0]}/deployments")).json()
    assert [d["id"] for d in for_plan] == [str(detail.id)]

    verified = await client.post(f"/deployments/{detail.id}/verify")
    assert verified.status_code == 200 and len(verified.json()["verifications"]) == 2

    rollback = {"operation_id": str(uuid.uuid4()), "reason": "other", "note": "test rollback"}
    rolled = await client.post(f"/deployments/{detail.id}/rollback", json=rollback)
    assert rolled.status_code == 200, rolled.text
    assert rolled.json()["status"] == "rolled_back"
    assert rolled.json()["rollback_reason"] == "other"
    again = await client.post(f"/deployments/{detail.id}/rollback", json=rollback)
    assert again.status_code == 200 and again.json()["idempotent_replay"] is True


async def test_refusals_over_http_carry_typed_details(sessionmaker: SM, w: World) -> None:
    charging, li = w.plans[(EV, CHARGING)], w.plans[(EV, LI)]
    await w.approve(charging)
    client = await _client(w, "oper", Role.OPERATOR_ADMIN)
    body = {"operation_id": str(uuid.uuid4()), "plan_ids": [str(charging.id), str(li.id)]}

    dry = DryRunResult.model_validate((await client.post("/deployments/dry-run", json=body)).json())
    assert not dry.valid and [(i.code.value, i.plan_id) for i in dry.issues] == [
        ("plan_not_approved", li.id)
    ]
    refused = _error(await client.post("/deployments", json=body), 409)
    assert refused.code.value == "conflict"
    assert [(d.code, d.location) for d in refused.details] == [("plan_not_approved", str(li.id))]

    # Another deployment of the source in flight: retryable 409, plans unchanged.
    await w.approve(li)
    key = deploy_lock_key(li.source_page_id)
    async with sessionmaker() as holder:
        await holder.execute(text("SELECT pg_advisory_lock(:k)"), {"k": key})
        busy = _error(await client.post("/deployments", json=body), 409)
        await holder.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": key})
        await holder.commit()
    assert [(d.code, d.location) for d in busy.details] == [("deployment_in_progress", None)]

    missing = _error(await client.get(f"/deployments/{uuid.uuid4()}"), 404)
    assert missing.message == "Deployment not found."
    assert _error(await client.post(f"/deployments/{uuid.uuid4()}/verify"), 404).code.value == (
        "not_found"
    )
    unknown_plan = {"operation_id": str(uuid.uuid4()), "plan_ids": [str(uuid.uuid4())]}
    assert _error(await client.post("/deployments", json=unknown_plan), 404).code.value == (
        "not_found"
    )


async def test_reviewers_and_viewers_cannot_deploy(sessionmaker: SM, w: World) -> None:
    ids = [str(p) for p in await w.approve(w.plans[(EV, LI)])]
    for username, role in (("rev", Role.REVIEWER), ("view", Role.VIEWER)):
        client = await _client(w, username, role)
        body = {"operation_id": str(uuid.uuid4()), "plan_ids": ids}
        for path in ("/deployments/dry-run", "/deployments"):
            assert _error(await client.post(path, json=body), 403).code.value == (
                "permission_denied"
            )
        assert (await client.get("/deployments")).status_code == 200
    async with sessionmaker() as session:
        count = (await session.execute(text("SELECT count(*) FROM deployments"))).scalar()
    assert count == 0


async def test_get_routes_use_the_read_only_factory(sessionmaker: SM, w: World) -> None:
    """The reader dependency is the READ ONLY factory (ADR-0014 s8): its transactions refuse
    writes even if a reader bug tried one."""
    client = await _client(w, "view", Role.VIEWER)
    assert (await client.get("/deployments")).status_code == 200
    app: Any = client._transport.app  # type: ignore[attr-defined]
    readonly = app.state.readonly_sessionmaker
    async with readonly() as session:
        with pytest.raises(Exception) as info:
            await session.execute(text("DELETE FROM link_plan_transitions"))
    assert "read-only" in str(info.value).lower() or "25006" in str(info.value)
