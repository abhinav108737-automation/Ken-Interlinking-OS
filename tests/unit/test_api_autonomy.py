"""The autonomy API routes (ADR-0018 s5/s6), against stub reader and override services.

The routes are GET-only except ONE write route. What matters here is the wiring and the
permissions, not the SQL -- the reader's own behaviour is covered by the integration tests.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

import pytest
from fastapi.testclient import TestClient

from ken_api.deps import get_autonomy_reader, get_autonomy_service
from ken_contracts.auth import Role
from ken_contracts.autonomy import (
    AutonomyDecision,
    AutonomyDecisionView,
    AutonomyHealth,
    AutonomyMode,
    AutonomySummary,
    BlockCode,
    BlockedActionView,
    CriticOutcome,
    OperatorOverrideRequest,
    OperatorOverrideResponse,
    OverrideAction,
    PublishAttemptView,
    RiskTier,
    VerificationProfile,
)
from ken_contracts.errors import ListResponse, PageInfo
from ken_contracts.recommendations import LinkPlanStatus
from tests.conftest import make_app

NOW = datetime(2026, 10, 16, 12, 0, tzinfo=UTC)
PLAN = uuid.UUID("55555555-5555-4555-8555-555555555555")
SOURCE = uuid.UUID("66666666-6666-4666-8666-666666666666")
TARGET = uuid.UUID("77777777-7777-4777-8777-777777777777")


def _decision_view() -> AutonomyDecisionView:
    return AutonomyDecisionView(
        id=1,
        plan_id=PLAN,
        plan_status=LinkPlanStatus.BLOCKED,
        source_url="https://site.test/a",
        target_url="https://site.test/b",
        decision=AutonomyDecision.BLOCK,
        mode=AutonomyMode.GUARDED,
        risk_tier=RiskTier.HIGH,
        confidence=0.4,
        calibration_version=None,
        critic_outcome=CriticOutcome.ACCEPT,
        critic_verdict=None,
        policy_version=1,
        blocks=(BlockCode.PROTECTED_PAGE,),
        rollback_available=True,
        verification_profile=VerificationProfile.STANDARD,
        would_have_decision=None,
        evaluated_at=NOW,
        engine_version="autonomy-engine-1",
    )


class StubReader:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def get_health(self) -> AutonomyHealth:
        self.calls.append("health")
        return AutonomyHealth(
            mode=AutonomyMode.SHADOW,
            autonomous_acknowledged=False,
            allow_production=False,
            cms_provider="mock",
            cms_writes_enabled=True,
            publishing_enabled=False,
            verification_profile_default=VerificationProfile.STANDARD,
            calibration_version=None,
            policy_version=1,
        )

    async def get_summary(self) -> AutonomySummary:
        self.calls.append("summary")
        return AutonomySummary(
            decisions_by_decision={"block": 1},
            decisions_by_mode={"shadow": 1},
            blocks_by_code={"protected_page": 1},
            blocked_plans=1,
            auto_published=0,
            rolled_back=0,
            mode=AutonomyMode.SHADOW,
        )

    async def list_decisions(self, **kw: Any) -> ListResponse[AutonomyDecisionView]:
        self.calls.append(f"decisions:{sorted(kw.items())}")
        return ListResponse[AutonomyDecisionView](
            items=[_decision_view()],
            page=PageInfo(total=1, limit=kw["limit"], offset=kw["offset"]),
        )

    async def list_blocked(self, **kw: Any) -> ListResponse[BlockedActionView]:
        self.calls.append(f"blocked:{sorted(kw.items())}")
        return ListResponse[BlockedActionView](
            items=[
                BlockedActionView(
                    plan_id=PLAN,
                    source_page_id=SOURCE,
                    source_url="https://site.test/a",
                    target_page_id=TARGET,
                    target_url="https://site.test/b",
                    anchor_text="batteries",
                    blocks=(BlockCode.PROTECTED_PAGE,),
                    risk_tier=RiskTier.HIGH,
                    mode=AutonomyMode.GUARDED,
                    decided_at=NOW,
                )
            ],
            page=PageInfo(total=1, limit=kw["limit"], offset=kw["offset"]),
        )

    async def list_attempts(self, **kw: Any) -> ListResponse[PublishAttemptView]:
        self.calls.append(f"attempts:{sorted(kw.items())}")
        return ListResponse[PublishAttemptView](
            items=[], page=PageInfo(total=0, limit=kw["limit"], offset=kw["offset"])
        )


class StubOverride:
    def __init__(self) -> None:
        self.seen: list[tuple[str, uuid.UUID, OverrideAction]] = []

    async def override(
        self, *, principal: Any, plan_id: uuid.UUID, request: OperatorOverrideRequest
    ) -> OperatorOverrideResponse:
        action = OverrideAction(request.action)
        self.seen.append((principal.actor, plan_id, action))
        return OperatorOverrideResponse(
            plan_id=plan_id,
            action=action,
            applied=False,
            plan_status=LinkPlanStatus.BLOCKED,
            actor=principal.actor,
            blocks=(BlockCode.PROTECTED_PAGE,),
            refusal="a deterministic block still fires",
            occurred_at=NOW,
        )


@pytest.fixture
def wired() -> tuple[Any, StubReader, StubOverride]:
    app = make_app()
    reader, service = StubReader(), StubOverride()
    app.dependency_overrides[get_autonomy_reader] = lambda: reader
    app.dependency_overrides[get_autonomy_service] = lambda: service
    return app, reader, service


def test_the_mode_route_reports_whether_anything_can_be_published(
    wired: tuple[Any, StubReader, StubOverride],
) -> None:
    app, reader, _ = wired
    with TestClient(app) as client:
        response = client.get("/autonomy/mode")
    assert response.status_code == 200
    body = response.json()
    assert body["mode"] == "shadow"
    assert body["publishing_enabled"] is False
    assert reader.calls == ["health"]


def test_the_summary_and_list_routes_delegate_to_the_reader(
    wired: tuple[Any, StubReader, StubOverride],
) -> None:
    app, reader, _ = wired
    with TestClient(app) as client:
        assert client.get("/autonomy").status_code == 200
        assert client.get("/autonomy/decisions?decision=block&limit=10").status_code == 200
        assert client.get("/autonomy/blocked?code=protected_page").status_code == 200
        assert client.get("/autonomy/attempts").status_code == 200
    assert reader.calls[0] == "summary"
    assert "('decision', <AutonomyDecision.BLOCK: 'block'>)" in reader.calls[1]
    assert "('limit', 10)" in reader.calls[1]
    assert "('code', <BlockCode.PROTECTED_PAGE: 'protected_page'>)" in reader.calls[2]


def test_a_blocked_action_carries_the_codes_that_fired(
    wired: tuple[Any, StubReader, StubOverride],
) -> None:
    app, _reader, _ = wired
    with TestClient(app) as client:
        body = client.get("/autonomy/blocked").json()
    assert body["items"][0]["blocks"] == ["protected_page"]
    assert body["items"][0]["overridden"] is False


def test_an_unknown_block_code_is_a_422_not_a_500(
    wired: tuple[Any, StubReader, StubOverride],
) -> None:
    app, _reader, _ = wired
    with TestClient(app) as client:
        assert client.get("/autonomy/blocked?code=not_a_code").status_code == 422


def test_the_override_route_passes_the_session_principal_and_never_a_body_actor(
    wired: tuple[Any, StubReader, StubOverride],
) -> None:
    app, _reader, service = wired
    payload = {
        "operation_id": str(uuid.uuid4()),
        "action": "publish",
        "reason": "the protection lapsed",
        "expected_status": "blocked",
        "actor": "user:someone-else",  # ignored: the model forbids extra fields
    }
    with TestClient(app) as client:
        response = client.post(f"/autonomy/plans/{PLAN}/override", json=payload)
    # ContractModel forbids unknown fields, so a smuggled actor is a 422 rather than silently
    # ignored -- which is the stronger behaviour.
    assert response.status_code == 422
    assert service.seen == []


def test_a_refused_override_reports_the_codes_that_still_fire(
    wired: tuple[Any, StubReader, StubOverride],
) -> None:
    app, _reader, service = wired
    payload = {
        "operation_id": str(uuid.uuid4()),
        "action": "publish",
        "reason": "the protection lapsed",
        "expected_status": "blocked",
    }
    with TestClient(app) as client:
        response = client.post(f"/autonomy/plans/{PLAN}/override", json=payload)
    assert response.status_code == 200
    body = response.json()
    assert body["applied"] is False
    assert body["blocks"] == ["protected_page"]
    assert body["refusal"]
    assert len(service.seen) == 1
    assert service.seen[0][0].startswith("user:")


def test_a_viewer_may_read_the_autonomy_screens_but_not_override() -> None:
    app = make_app(role=Role.VIEWER)
    reader, service = StubReader(), StubOverride()
    app.dependency_overrides[get_autonomy_reader] = lambda: reader
    app.dependency_overrides[get_autonomy_service] = lambda: service
    payload = {
        "operation_id": str(uuid.uuid4()),
        "action": "block",
        "reason": "no",
        "expected_status": "proposed",
    }
    with TestClient(app) as client:
        assert client.get("/autonomy/mode").status_code == 200
        assert client.get("/autonomy/blocked").status_code == 200
        assert client.post(f"/autonomy/plans/{PLAN}/override", json=payload).status_code == 403
    assert service.seen == []


def test_a_reviewer_may_not_override_either() -> None:
    """``review`` is the wrong permission in the other direction: a reviewer may approve a
    proposal, not write to the live site (the route's own docstring)."""
    app = make_app(role=Role.REVIEWER)
    service = StubOverride()
    app.dependency_overrides[get_autonomy_service] = lambda: service
    payload = {
        "operation_id": str(uuid.uuid4()),
        "action": "block",
        "reason": "no",
        "expected_status": "proposed",
    }
    with TestClient(app) as client:
        assert client.post(f"/autonomy/plans/{PLAN}/override", json=payload).status_code == 403
    assert service.seen == []
