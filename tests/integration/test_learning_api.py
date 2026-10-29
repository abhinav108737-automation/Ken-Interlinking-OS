"""The ``/learning`` routes over the REAL reader and promotion service (workstream D;
ADR-0016 section 10): RBAC, the one write route, and the Phase 8 default that a promotion is
refused with the typed ``demonstration_mode`` code -- end to end through HTTP.
"""

from __future__ import annotations

import uuid
from typing import Any

import httpx
import pytest
from sqlalchemy import text

from ken_contracts.auth import Role
from ken_contracts.errors import ErrorResponse
from ken_contracts.learning import (
    AdvanceProposalResponse,
    LearningOverview,
    PolicyCandidateView,
    ProposalDetail,
)
from tests.conftest import FakeCMSProvider, FakeModelProvider, make_app, make_settings
from tests.integration.learning_lifecycle_support import (
    SM,
    force_stage,
    insert_evaluation,
    insert_shadow_rows,
    seed_proposal,
)
from tests.integration.review_support import PASSWORD, api_client, auth_provider, create_user

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("migrated")]


async def _client(sm: SM, username: str, role: Role, **overrides: Any) -> httpx.AsyncClient:
    await create_user(sm, username, role=role)
    app = make_app(make_settings(**overrides), real_auth=True)
    app.state.sessionmaker = sm
    app.state.model_provider = FakeModelProvider()
    app.state.cms_provider = FakeCMSProvider()
    app.state.auth_provider = auth_provider(sm)
    client = api_client(app, raise_errors=False)
    login = await client.post("/auth/login", json={"username": username, "password": PASSWORD})
    assert login.status_code == 200, login.text
    client.headers["Authorization"] = f"Bearer {login.json()['token']}"
    return client


def _code(response: httpx.Response, status: int) -> str:
    assert response.status_code == status, response.text
    error = ErrorResponse.model_validate(response.json())
    return error.details[0].code if error.details and error.details[0].code else error.code.value


def _body(to_status: str, **extra: Any) -> dict[str, Any]:
    return {"operation_id": str(uuid.uuid4()), "to_status": to_status, **extra}


async def test_the_overview_carries_the_demonstration_banner(sessionmaker: SM) -> None:
    await seed_proposal(sessionmaker)
    client = await _client(sessionmaker, "rita", Role.REVIEWER)
    response = await client.get("/learning")
    assert response.status_code == 200, response.text
    overview = LearningOverview.model_validate(response.json())
    assert overview.banner.demonstration_mode is True
    assert overview.proposals_by_status == {"proposed": 1}


async def test_a_reviewer_reads_metadata_but_not_the_candidate_document(
    sessionmaker: SM,
) -> None:
    seeded = await seed_proposal(sessionmaker)
    reviewer = await _client(sessionmaker, "rita", Role.REVIEWER)
    listing = await reviewer.get("/learning/proposals")
    assert listing.status_code == 200 and listing.json()["page"]["total"] == 1
    detail = await reviewer.get(f"/learning/proposals/{seeded.proposal_id}")
    assert detail.status_code == 200, detail.text
    parsed = ProposalDetail.model_validate(detail.json())
    assert parsed.frozen_fields_untouched is True
    assert parsed.candidate is not None
    assert '"document":' not in detail.text
    denied = await reviewer.get(f"/learning/proposals/{seeded.proposal_id}/candidate")
    assert denied.status_code == 403

    operator = await _client(sessionmaker, "otto", Role.OPERATOR_ADMIN)
    served = await operator.get(f"/learning/proposals/{seeded.proposal_id}/candidate")
    assert served.status_code == 200, served.text
    assert PolicyCandidateView.model_validate(served.json()).document["policy"] == "ranking"


async def test_an_unknown_proposal_is_a_404(sessionmaker: SM) -> None:
    client = await _client(sessionmaker, "rita", Role.REVIEWER)
    assert (await client.get(f"/learning/proposals/{uuid.uuid4()}")).status_code == 404
    assert (await client.get("/learning/evaluations/" + str(uuid.uuid4()))).status_code == 404
    assert (await client.get("/learning/experiments/no-such-experiment")).status_code == 404
    assert (await client.get("/learning/skills/no_such_skill")).status_code == 404


async def test_a_promotion_over_http_is_refused_with_the_demonstration_code(
    sessionmaker: SM,
) -> None:
    """The expected Phase 8 behaviour, end to end: a 409 whose detail code is
    ``demonstration_mode`` even for an operator holding a complete, passing set of evidence."""
    seeded = await seed_proposal(sessionmaker)
    await force_stage(sessionmaker, seeded.proposal_id, *_ladder())
    await insert_shadow_rows(sessionmaker, seeded, runs=3, compared_pairs=10)
    evaluation = await insert_evaluation(
        sessionmaker, proposal_id=seeded.proposal_id, fingerprint=seeded.fingerprint
    )
    operator = await _client(sessionmaker, "otto", Role.OPERATOR_ADMIN)

    response = await operator.post(
        f"/learning/proposals/{seeded.proposal_id}/advance",
        json=_body("promoted", evaluation_run_id=str(evaluation)),
    )

    assert _code(response, 409) == "demonstration_mode"
    async with sessionmaker() as session:
        status = await session.scalar(
            text("SELECT status FROM learning_proposals WHERE id = :id"),
            {"id": seeded.proposal_id},
        )
        promoted_events = await session.scalar(
            text("SELECT count(*) FROM events WHERE event_name = 'policy.promoted'")
        )
    assert status == "shadow"
    assert promoted_events == 0


def _ladder() -> tuple[Any, Any]:
    from ken_contracts.learning import ProposalStatus

    return ProposalStatus.OFFLINE_TEST, ProposalStatus.SHADOW


async def test_a_reviewer_cannot_use_the_advance_route(sessionmaker: SM) -> None:
    seeded = await seed_proposal(sessionmaker)
    reviewer = await _client(sessionmaker, "rita", Role.REVIEWER)
    response = await reviewer.post(
        f"/learning/proposals/{seeded.proposal_id}/advance",
        json=_body("rejected", reason="out_of_scope"),
    )
    assert response.status_code == 403


async def test_an_unauthenticated_caller_is_a_401(sessionmaker: SM) -> None:
    seeded = await seed_proposal(sessionmaker)
    app = make_app(make_settings(), real_auth=True)
    app.state.sessionmaker = sessionmaker
    app.state.auth_provider = auth_provider(sessionmaker)
    anonymous = api_client(app, raise_errors=False)
    assert (await anonymous.get("/learning")).status_code == 401
    assert (
        await anonymous.post(
            f"/learning/proposals/{seeded.proposal_id}/advance", json=_body("withdrawn")
        )
    ).status_code in (401, 422)


async def test_an_operator_may_reject_and_the_actor_comes_from_the_session(
    sessionmaker: SM,
) -> None:
    seeded = await seed_proposal(sessionmaker)
    operator = await _client(sessionmaker, "otto", Role.OPERATOR_ADMIN)
    response = await operator.post(
        f"/learning/proposals/{seeded.proposal_id}/advance",
        json=_body("rejected", reason="demonstration_only", note="synthetic corpus"),
    )
    assert response.status_code == 200, response.text
    parsed = AdvanceProposalResponse.model_validate(response.json())
    assert parsed.proposal.status.value == "rejected"
    assert parsed.transition.actor == "user:otto"
    assert parsed.replayed is False
    # An actor smuggled in the body is refused, not honoured.
    smuggled = await operator.post(
        f"/learning/proposals/{seeded.proposal_id}/advance",
        json={**_body("withdrawn"), "actor": "user:someone-else"},
    )
    assert smuggled.status_code == 422


async def test_a_promotion_without_an_evaluation_is_a_422(sessionmaker: SM) -> None:
    seeded = await seed_proposal(sessionmaker)
    operator = await _client(sessionmaker, "otto", Role.OPERATOR_ADMIN)
    response = await operator.post(
        f"/learning/proposals/{seeded.proposal_id}/advance", json=_body("promoted")
    )
    assert response.status_code == 422
