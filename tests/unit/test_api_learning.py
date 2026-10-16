"""The frozen /learning routes over fakes (no database), ADR-0016 section 10 as amended by 15.

What these pin:

* every GET delegates to the :class:`~ken_contracts.learning.LearningReader` with the parsed
  parameters, and ``read`` (a viewer) is enough -- EXCEPT the candidate route, which returns a
  policy DOCUMENT and therefore needs ``read_settings`` (ADR-0013 section 7 reserves policy
  documents to ``operator_admin``; serving a candidate under ``read`` would be a downgrade);
* the single write route needs the new ``promote`` permission, ``operator_admin`` only;
* the actor is NEVER in the request body: the service receives the PRINCIPAL (15.7.3);
* a refusal becomes a 409 whose detail ``code`` is the stable
  :class:`~ken_contracts.learning.PromotionRefusal` value -- and in Phase 8 the refusal a
  caller actually gets is ``demonstration_mode``, because nothing on a synthetic corpus may be
  promoted (16.1).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

import pytest
from fastapi.testclient import TestClient

from ken_api.deps import get_learning_reader, get_learning_service
from ken_contracts.auth import Permission, Role
from ken_contracts.errors import ListResponse, PageInfo
from ken_contracts.learning import (
    AdvanceProposalRequest,
    AdvanceProposalResponse,
    CorpusBanner,
    EvaluationMode,
    GateResult,
    LearningOverview,
    PromotionRefusal,
    PromotionRefusedError,
    ProposalNotFoundError,
    ProposalStatus,
    SkillStatus,
)
from tests.conftest import make_app

PROPOSAL = uuid.UUID("11111111-1111-4111-8111-111111111111")
EVALUATION = uuid.UUID("22222222-2222-4222-8222-222222222222")
OPERATION = uuid.UUID("33333333-3333-4333-8333-333333333333")
NOW = datetime(2026, 10, 16, 9, 0, tzinfo=UTC)


def _overview() -> LearningOverview:
    return LearningOverview(
        banner=CorpusBanner(
            demonstration_mode=True,
            pages_total=65,
            plans_total=48,
            review_actions_total=12,
            min_sample_count=30,
            note="65 synthetic pages: no proposal on this data may be promoted.",
        ),
        proposals_by_status={},
        skills_by_status={},
        evaluations_by_gate_result={},
        experiments_by_status={},
        policy_version=1,
    )


def _empty(limit: int, offset: int) -> ListResponse[Any]:
    return ListResponse[Any](items=[], page=PageInfo(total=0, limit=limit, offset=offset))


class FakeReader:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.raises: BaseException | None = None

    def _record(self, name: str, kwargs: dict[str, Any]) -> None:
        self.calls.append((name, kwargs))
        if self.raises is not None:
            raise self.raises

    async def overview(self) -> LearningOverview:
        self._record("overview", {})
        return _overview()

    async def list_proposals(self, **kwargs: Any) -> ListResponse[Any]:
        self._record("list_proposals", kwargs)
        return _empty(kwargs["limit"], kwargs["offset"])

    async def get_proposal(self, **kwargs: Any) -> Any:
        self._record("get_proposal", kwargs)
        raise ProposalNotFoundError

    async def get_candidate(self, **kwargs: Any) -> Any:
        self._record("get_candidate", kwargs)
        raise ProposalNotFoundError

    async def list_evaluations(self, **kwargs: Any) -> ListResponse[Any]:
        self._record("list_evaluations", kwargs)
        return _empty(kwargs["limit"], kwargs["offset"])

    async def get_evaluation(self, **kwargs: Any) -> Any:
        self._record("get_evaluation", kwargs)
        raise ProposalNotFoundError

    async def list_skills(self, **kwargs: Any) -> ListResponse[Any]:
        self._record("list_skills", kwargs)
        return _empty(kwargs["limit"], kwargs["offset"])

    async def get_skill(self, **kwargs: Any) -> Any:
        self._record("get_skill", kwargs)
        raise ProposalNotFoundError

    async def list_experiments(self, **kwargs: Any) -> ListResponse[Any]:
        self._record("list_experiments", kwargs)
        return _empty(kwargs["limit"], kwargs["offset"])

    async def get_experiment(self, **kwargs: Any) -> Any:
        self._record("get_experiment", kwargs)
        raise ProposalNotFoundError


class FakeService:
    def __init__(self, refusal: PromotionRefusal | None = None) -> None:
        self.refusal = refusal
        self.calls: list[dict[str, Any]] = []

    async def advance(
        self, *, principal: Any, proposal_id: uuid.UUID, request: AdvanceProposalRequest
    ) -> AdvanceProposalResponse:
        self.calls.append({"principal": principal, "proposal_id": proposal_id, "request": request})
        if self.refusal is not None:
            raise PromotionRefusedError(self.refusal)
        raise ProposalNotFoundError


def _client(
    reader: FakeReader | None = None,
    service: FakeService | None = None,
    role: Role = Role.OPERATOR_ADMIN,
) -> tuple[TestClient, FakeReader, FakeService]:
    fake_reader = reader or FakeReader()
    fake_service = service or FakeService()
    app = make_app(role=role)
    app.dependency_overrides[get_learning_reader] = lambda: fake_reader
    app.dependency_overrides[get_learning_service] = lambda: fake_service
    return TestClient(app), fake_reader, fake_service


# --------------------------------------------------------------------------- #
# GET routes
# --------------------------------------------------------------------------- #
def test_the_overview_carries_the_demonstration_banner() -> None:
    client, reader, _ = _client(role=Role.VIEWER)
    body = client.get("/learning").json()
    assert body["banner"]["demonstration_mode"] is True
    assert body["banner"]["pages_total"] == 65
    assert reader.calls == [("overview", {})]


def test_list_proposals_passes_every_filter_through() -> None:
    client, reader, _ = _client(role=Role.VIEWER)
    response = client.get(
        "/learning/proposals",
        params={
            "kind": "policy",
            "status": "offline_test",
            "target_kind": "ranking",
            "limit": 10,
            "offset": 5,
        },
    )
    assert response.status_code == 200
    name, kwargs = reader.calls[0]
    assert name == "list_proposals"
    assert kwargs == {
        "limit": 10,
        "offset": 5,
        "kind": "policy",
        "status": ProposalStatus.OFFLINE_TEST,
        "target_kind": "ranking",
    }


def test_list_evaluations_passes_the_mode_and_gate_filters() -> None:
    client, reader, _ = _client(role=Role.VIEWER)
    client.get(
        "/learning/evaluations",
        params={"mode": "offline", "gate_result": "inconclusive", "proposal_id": str(PROPOSAL)},
    )
    _name, kwargs = reader.calls[0]
    assert kwargs["mode"] is EvaluationMode.OFFLINE
    assert kwargs["gate_result"] is GateResult.INCONCLUSIVE
    assert kwargs["proposal_id"] == PROPOSAL


def test_list_skills_passes_the_status_filter() -> None:
    client, reader, _ = _client(role=Role.VIEWER)
    client.get("/learning/skills", params={"status": "promoted"})
    _name, kwargs = reader.calls[0]
    assert kwargs["status"] is SkillStatus.PROMOTED


def test_a_missing_record_is_a_404_not_an_exception() -> None:
    client, _reader, _service = _client(role=Role.VIEWER)
    response = client.get(f"/learning/proposals/{PROPOSAL}")
    assert response.status_code == 404
    assert response.json()["code"] == "not_found"


@pytest.mark.parametrize(
    "path",
    [
        "/learning/evaluations/{id}",
        "/learning/proposals/{id}",
    ],
)
def test_an_unparseable_identifier_is_a_422(path: str) -> None:
    client, _reader, _service = _client(role=Role.VIEWER)
    assert client.get(path.format(id="not-a-uuid")).status_code == 422


@pytest.mark.parametrize("name", ["Upper_Case", "ab", "has-a-dash", "x" * 70])
def test_a_skill_name_is_validated_at_the_boundary(name: str) -> None:
    client, _reader, _service = _client(role=Role.VIEWER)
    assert client.get(f"/learning/skills/{name}").status_code == 422


@pytest.mark.parametrize("key", ["Upper", "ab", "x" * 70])
def test_an_experiment_key_is_validated_at_the_boundary(key: str) -> None:
    client, _reader, _service = _client(role=Role.VIEWER)
    assert client.get(f"/learning/experiments/{key}").status_code == 422


@pytest.mark.parametrize(("limit", "offset"), [(0, 0), (201, 0), (10, -1)])
def test_pagination_bounds_are_422s(limit: int, offset: int) -> None:
    client, _reader, _service = _client(role=Role.VIEWER)
    response = client.get("/learning/proposals", params={"limit": limit, "offset": offset})
    assert response.status_code == 422


# --------------------------------------------------------------------------- #
# The candidate document is read_settings, not read
# --------------------------------------------------------------------------- #
def test_a_viewer_cannot_read_a_candidate_policy_document() -> None:
    """ADR-0013 section 7 reserves policy documents to ``operator_admin``. A candidate IS a
    policy document, so the route that returns one is ``read_settings``."""
    client, reader, _ = _client(role=Role.VIEWER)
    assert client.get(f"/learning/proposals/{PROPOSAL}/candidate").status_code == 403
    assert reader.calls == []


def test_a_reviewer_cannot_read_a_candidate_policy_document() -> None:
    client, _reader, _service = _client(role=Role.REVIEWER)
    assert client.get(f"/learning/proposals/{PROPOSAL}/candidate").status_code == 403


def test_an_operator_can_read_a_candidate_policy_document() -> None:
    client, reader, _ = _client()
    assert client.get(f"/learning/proposals/{PROPOSAL}/candidate").status_code == 404
    assert reader.calls == [("get_candidate", {"proposal_id": PROPOSAL})]


# --------------------------------------------------------------------------- #
# The one write route
# --------------------------------------------------------------------------- #
def _advance_body(**overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "operation_id": str(OPERATION),
        "to_status": "promoted",
        "evaluation_run_id": str(EVALUATION),
    }
    body.update(overrides)
    return body


@pytest.mark.parametrize("role", [Role.VIEWER, Role.REVIEWER])
def test_only_an_operator_may_advance_a_proposal(role: Role) -> None:
    """``promote`` is granted to ``operator_admin`` alone: promoting a policy is categorically
    different from editorial judgement, so reusing ``review`` would have been wrong."""
    client, _reader, service = _client(role=role)
    response = client.post(f"/learning/proposals/{PROPOSAL}/advance", json=_advance_body())
    assert response.status_code == 403
    assert service.calls == []


def test_the_permission_is_promote_and_only_operators_hold_it() -> None:
    from ken_contracts.auth import ROLE_PERMISSIONS

    holders = {role for role, perms in ROLE_PERMISSIONS.items() if Permission.PROMOTE in perms}
    assert holders == {Role.OPERATOR_ADMIN}


def test_the_service_receives_the_principal_and_never_an_actor_string() -> None:
    """ADR-0016 15.7.3: the actor is derived from the session, so the body cannot carry one."""
    client, _reader, service = _client()
    client.post(f"/learning/proposals/{PROPOSAL}/advance", json=_advance_body())
    call = service.calls[0]
    assert call["proposal_id"] == PROPOSAL
    assert call["principal"].actor.startswith("user:")
    assert "actor" not in call["request"].model_dump()


def test_an_actor_in_the_body_is_rejected_outright() -> None:
    client, _reader, service = _client()
    response = client.post(
        f"/learning/proposals/{PROPOSAL}/advance", json=_advance_body(actor="user:someone-else")
    )
    assert response.status_code == 422
    assert service.calls == []


def test_a_promotion_without_an_evaluation_run_is_a_422() -> None:
    client, _reader, service = _client()
    body = _advance_body()
    del body["evaluation_run_id"]
    assert client.post(f"/learning/proposals/{PROPOSAL}/advance", json=body).status_code == 422
    assert service.calls == []


def test_demonstration_mode_is_the_refusal_phase_8_actually_returns() -> None:
    """The phase exits with zero promotions BY CONSTRUCTION: the service refuses before any
    other check while ``Settings.learning_demonstration_mode`` is set, which is the default."""
    client, _reader, service = _client(service=FakeService(PromotionRefusal.DEMONSTRATION_MODE))
    response = client.post(f"/learning/proposals/{PROPOSAL}/advance", json=_advance_body())
    assert response.status_code == 409
    body = response.json()
    assert body["code"] == "conflict"
    assert [detail["code"] for detail in body["details"]] == ["demonstration_mode"]
    assert service.calls


@pytest.mark.parametrize(
    ("refusal", "status"),
    [
        (PromotionRefusal.DEMONSTRATION_MODE, 409),
        (PromotionRefusal.FINGERPRINT_MISMATCH, 409),
        (PromotionRefusal.EVALUATION_NOT_PASSED, 409),
        (PromotionRefusal.REQUIRED_STAGE_MISSING, 409),
        (PromotionRefusal.EVALUATION_NOT_FOUND, 404),
        (PromotionRefusal.NOT_A_HUMAN, 403),
        (PromotionRefusal.PERMISSION_DENIED, 403),
    ],
)
def test_every_refusal_maps_to_a_status_and_keeps_its_code(
    refusal: PromotionRefusal, status: int
) -> None:
    client, _reader, _service = _client(service=FakeService(refusal))
    response = client.post(f"/learning/proposals/{PROPOSAL}/advance", json=_advance_body())
    assert response.status_code == status
    assert [detail["code"] for detail in response.json()["details"]] == [refusal.value]


def test_a_refusal_never_leaks_an_internal_message() -> None:
    client, _reader, _service = _client(service=FakeService(PromotionRefusal.DATASET_CHANGED))
    body = client.post(f"/learning/proposals/{PROPOSAL}/advance", json=_advance_body()).json()
    assert body["message"] == "The proposal was not advanced."
    assert all("sql" not in str(value).lower() for value in body.values())


def test_a_rejection_needs_its_structured_reason_at_the_boundary() -> None:
    client, _reader, service = _client()
    assert (
        client.post(
            f"/learning/proposals/{PROPOSAL}/advance",
            json={"operation_id": str(OPERATION), "to_status": "rejected"},
        ).status_code
        == 422
    )
    assert service.calls == []
    client.post(
        f"/learning/proposals/{PROPOSAL}/advance",
        json={
            "operation_id": str(OPERATION),
            "to_status": "rejected",
            "reason": "demonstration_only",
        },
    )
    assert service.calls[0]["request"].reason is not None
