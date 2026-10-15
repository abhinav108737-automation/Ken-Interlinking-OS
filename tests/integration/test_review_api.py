"""Review and control-plane routes end to end on the ``*_test`` database (ADR-0013 s5, s7).

The principal is a REAL user row (``review_actions.user_id`` has a foreign key) installed
through ``app.dependency_overrides`` (make_app); everything below the route is real.
"""

from __future__ import annotations

import hashlib
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from ken_contracts.auth import Role
from ken_contracts.controlplane import DashboardSummary, EventRecord, PoliciesView
from ken_contracts.errors import ErrorResponse, ListResponse
from ken_contracts.intelligence import ProtectionSource
from ken_contracts.review import BulkReviewResult, ReviewActionView, ReviewResult, TargetOption
from ken_core.config import REPO_ROOT
from ken_core.db.repositories.intelligence import insert_protection
from ken_services.recommendations.policy import load_recommendation_policy
from tests.conftest import FakeCMSProvider, FakeModelProvider, make_app
from tests.integration.recommendation_support import (
    CHARGING,
    EV,
    LI,
    SETTINGS,
    SM,
    SOLAR,
    page_ids,
)
from tests.integration.review_support import api_client, principal, queue, seeded

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("migrated")]

LI_BLOCK = (
    "Vehicle makers depend on imported lithium-ion batteries, and local cell plants are "
    "planned to cut that dependence over the coming decade."
)


@pytest.fixture
async def plans(sessionmaker: SM, tmp_path: Path) -> dict[tuple[str, str], Any]:
    return await seeded(sessionmaker, tmp_path)


async def _client(sessionmaker: SM, role: Role = Role.OPERATOR_ADMIN) -> Any:
    from ken_api.security import get_principal

    user = await principal(sessionmaker, "alice", role=role)
    app = make_app(SETTINGS)
    app.dependency_overrides[get_principal] = lambda: user
    app.state.sessionmaker = sessionmaker
    app.state.model_provider = FakeModelProvider()
    app.state.cms_provider = FakeCMSProvider()
    return api_client(app)


def _error(response: Any, status: int) -> ErrorResponse:
    assert response.status_code == status, response.text
    return ErrorResponse.model_validate(response.json())


async def test_approve_replay_and_refusals_over_http(sessionmaker: SM, plans: Any) -> None:
    client = await _client(sessionmaker)
    plan = plans[(EV, LI)]
    body = {
        "operation_id": str(uuid.uuid4()),
        "expected_status": "proposed",
        "reasons": ["good_as_is"],
    }
    url = f"/recommendations/{plan.id}/approve"
    first = await client.post(url, json=body)
    assert first.status_code == 200, first.text
    result = ReviewResult.model_validate(first.json())
    assert result.status.value == "approved" and not result.idempotent_replay
    again = ReviewResult.model_validate((await client.post(url, json=body)).json())
    assert again.idempotent_replay and again.action_id == result.action_id

    reused = _error(await client.post(url, json=body | {"note": "different"}), 409)
    assert reused.code.value == "conflict"
    assert reused.details[0].code == "operation_id_reused"
    assert reused.details[0].location == "body.operation_id"

    mismatch = _error(await client.post(url, json=body | {"operation_id": str(uuid.uuid4())}), 409)
    assert mismatch.details[0].code == "expected_status_mismatch"
    assert mismatch.message == "The review action conflicts with the plan's current state."

    missing = _error(
        await client.post(
            f"/recommendations/{uuid.uuid4()}/approve",
            json=body | {"operation_id": str(uuid.uuid4())},
        ),
        404,
    )
    assert missing.code.value == "not_found"


async def test_edit_errors_map_to_422_and_409_with_reasons(sessionmaker: SM, plans: Any) -> None:
    client = await _client(sessionmaker)
    plan = plans[(EV, LI)]
    start = LI_BLOCK.index("imported")
    base = {
        "expected_status": "proposed",
        "reasons": ["unnatural_anchor"],
        "block_context_hash": plan.context_hash,
        "start": start + 2,
        "end": LI_BLOCK.index(", and"),
    }
    url = f"/recommendations/{plan.id}/edit-anchor"
    bad = _error(await client.post(url, json=base | {"operation_id": str(uuid.uuid4())}), 422)
    assert bad.code.value == "validation_failed"
    assert (bad.details[0].code, bad.details[0].location) == ("span_not_word_aligned", "body.start")
    assert bad.message == "The edit breaks a placement, anchor or pair rule."
    ok = await client.post(
        url, json=base | {"operation_id": str(uuid.uuid4()), "start": start, "approve": True}
    )
    assert ok.status_code == 200, ok.text
    edited = ReviewResult.model_validate(ok.json())
    assert edited.new_plan_status is not None and edited.new_plan_status.value == "approved"
    # The old plan is superseded now.
    late = _error(await client.post(url, json=base | {"operation_id": str(uuid.uuid4())}), 409)
    assert late.details[0].code == "expected_status_mismatch"


async def test_protected_and_duplicate_destinations_over_http(sessionmaker: SM, plans: Any) -> None:
    client = await _client(sessionmaker)
    plan = plans[(EV, LI)]
    ids = await page_ids(sessionmaker)
    url = f"/recommendations/{plan.id}/replace-target"
    body = {"expected_status": "proposed", "reasons": ["better_destination_exists"]}
    duplicate = _error(
        await client.post(
            url,
            json=body | {"operation_id": str(uuid.uuid4()), "target_page_id": str(ids[CHARGING])},
        ),
        409,
    )
    assert duplicate.details[0].code == "pair_duplicate_recommendation"
    async with sessionmaker() as session, session.begin():
        await insert_protection(
            session,
            page_id=plan.source_page_id,
            source=ProtectionSource.MANUAL.value,
            rule_id=None,
            reason="legal hold",
            created_by="user:ops",
            created_at=datetime.now(UTC),
        )
    protected = _error(
        await client.post(
            url, json=body | {"operation_id": str(uuid.uuid4()), "target_page_id": str(ids[SOLAR])}
        ),
        409,
    )
    assert protected.code.value == "protected_page"
    assert protected.details[0].code == "source_ineligible"
    assert {d.code for d in protected.details[1:]} == {"protected"}


async def test_bulk_target_options_and_history_over_http(sessionmaker: SM, plans: Any) -> None:
    client = await _client(sessionmaker)
    plan = plans[(EV, LI)]
    await queue(sessionmaker, plan.id)
    bulk = await client.post(
        "/recommendations/bulk-review",
        json={
            "operation_id": str(uuid.uuid4()),
            "action": "approve",
            "plan_ids": [str(plan.id), str(uuid.uuid4())],
        },
    )
    assert bulk.status_code == 200, bulk.text
    result = BulkReviewResult.model_validate(bulk.json())
    assert [i.ok for i in result.items] == [True, False]
    assert result.items[1].error_code == "not_found"

    options = await client.get(f"/recommendations/{plans[(EV, CHARGING)].id}/target-options")
    assert options.status_code == 200, options.text
    parsed = [TargetOption.model_validate(o) for o in options.json()]
    assert parsed and all(o.page_id != plans[(EV, CHARGING)].target_page_id for o in parsed)
    assert (await client.get(f"/recommendations/{uuid.uuid4()}/target-options")).status_code == 404

    history = await client.get(f"/recommendations/{plan.id}/review-actions")
    assert history.status_code == 200
    views = [ReviewActionView.model_validate(v) for v in history.json()]
    assert [v.action.value for v in views] == ["approve"]
    assert views[0].bulk_operation_id == result.operation_id
    listed = await client.get("/review/actions", params={"action": "approve", "limit": 5})
    page = ListResponse[ReviewActionView].model_validate(listed.json())
    assert page.page.total == 1 and page.items[0].actor == "user:alice"


async def test_dashboard_events_and_policies(sessionmaker: SM, plans: Any) -> None:
    client = await _client(sessionmaker)
    plan = plans[(EV, LI)]
    await client.post(
        f"/recommendations/{plan.id}/reject",
        json={
            "operation_id": str(uuid.uuid4()),
            "expected_status": "proposed",
            "reasons": ["over_linking"],
        },
    )
    dashboard = DashboardSummary.model_validate((await client.get("/dashboard")).json())
    assert dashboard.pages_total == 5 and dashboard.pages_live == 5
    assert dashboard.pages_analyzed == 5 and dashboard.pages_protected == 0
    assert dashboard.plans_by_status["rejected"] == 1
    assert sum(dashboard.plans_by_status.values()) == len(plans)
    assert dashboard.reviews_by_action == {"reject": 1}
    assert [a.action.value for a in dashboard.recent_review_actions] == ["reject"]
    assert {j.kind.value for j in dashboard.latest_jobs} == {"analysis", "recommendation"}
    assert dashboard.auto_approve_enabled is False

    events = ListResponse[EventRecord].model_validate(
        (await client.get("/events", params={"limit": 2})).json()
    )
    assert len(events.items) == 2 and events.items[0].id > events.items[1].id
    assert events.items[0].event_name.value == "link.rejected"
    by_subject = ListResponse[EventRecord].model_validate(
        (await client.get("/events", params={"subject_id": str(plan.id)})).json()
    )
    assert {e.event_name.value for e in by_subject.items} == {"link.proposed", "link.rejected"}
    named = ListResponse[EventRecord].model_validate(
        (await client.get("/events", params={"event_name": "link.rejected"})).json()
    )
    assert named.page.total == 1

    policies = PoliciesView.model_validate((await client.get("/settings/policies")).json())
    assert [p.kind for p in policies.policies] == [
        "taxonomy",
        "recommendation",
        "ranking",
        "agents",
        "analytics",
    ]
    for document in policies.policies:
        assert not document.path.startswith("/")
        path = REPO_ROOT / document.path
        assert hashlib.sha256(path.read_bytes()).hexdigest() == document.sha256
    recommendation = load_recommendation_policy(SETTINGS.recommendation_policy_path)
    assert policies.policies[1].content == recommendation.model_dump(mode="json")
    assert policies.auto_approve_enabled is False
