"""The /deployments API over fake service and reader doubles (no database): request
validation, the permission split (``deploy`` vs ``read``), and the EXACT error mapping of
ADR-0014 s7/s8:

* ``DeploymentRefusedError`` -> 409 ``conflict``; ``DeploymentVersionConflictError`` -> 409
  ``cms_version_conflict``; ``CMSUnavailableDeploymentError`` -> 503 ``cms_unavailable``;
* ``details`` = one ``ErrorDetail`` per ``DeploymentIssue``: ``code`` = the issue code,
  ``location`` = the plan id (bare UUID) or null, a FIXED message per code;
* a reused operation id -> 409 ``conflict`` with empty details;
* a dry run with issues -> 200 ``valid: false``; a deploy whose outcome is failed/conflict ->
  200 with the ``DeploymentDetail``.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

import pytest
from fastapi.testclient import TestClient

from ken_api.deps import get_deployment_reader, get_deployment_service
from ken_api.errors import DEPLOYMENT_ISSUE_MESSAGES
from ken_contracts.auth import Role
from ken_contracts.deployment import (
    CMSUnavailableDeploymentError,
    DeploymentDetail,
    DeploymentIssue,
    DeploymentIssueCode,
    DeploymentNotFoundError,
    DeploymentRefusedError,
    DeploymentStatus,
    DeploymentVersionConflictError,
    DeploymentView,
    DryRunResult,
)
from ken_contracts.errors import ErrorResponse, ListResponse, PageInfo
from ken_contracts.recommendations import PlanNotFoundError
from ken_services.deployment.errors import (
    DeploymentPermissionError,
    DeploymentStateError,
    OperationReusedDeploymentError,
)
from tests.conftest import make_app

NOW = datetime(2026, 10, 15, 9, 0, tzinfo=UTC)
PLAN = uuid.UUID("11111111-1111-4111-8111-111111111111")
OTHER = uuid.UUID("33333333-3333-4333-8333-333333333333")
DEPLOYMENT = uuid.UUID("22222222-2222-4222-8222-222222222222")
SOURCE = uuid.UUID("44444444-4444-4444-8444-444444444444")
C = DeploymentIssueCode


def _view(status: DeploymentStatus = DeploymentStatus.VERIFIED, **kw: Any) -> dict[str, Any]:
    return {
        "id": DEPLOYMENT,
        "operation_id": uuid.uuid4(),
        "source_page_id": SOURCE,
        "source_url": "https://site.test/a",
        "cms_provider": "mock",
        "cms_page_id": "cms-a",
        "status": status,
        "plan_ids": [PLAN],
        "actor": "user:test-user",
        "created_at": NOW,
        **kw,
    }


def detail(status: DeploymentStatus = DeploymentStatus.VERIFIED, **kw: Any) -> DeploymentDetail:
    return DeploymentDetail(**_view(status, **kw), items=[], preview=[], verifications=[])


class FakeService:
    def __init__(self) -> None:
        self.raises: BaseException | None = None
        self.result: Any = detail()
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def _go(self, name: str, **kw: Any) -> Any:
        self.calls.append((name, kw))
        if self.raises is not None:
            raise self.raises
        return self.result

    async def dry_run(self, **kw: Any) -> Any:
        return await self._go("dry_run", **kw)

    async def deploy(self, **kw: Any) -> Any:
        return await self._go("deploy", **kw)

    async def verify(self, **kw: Any) -> Any:
        return await self._go("verify", **kw)

    async def rollback(self, **kw: Any) -> Any:
        return await self._go("rollback", **kw)


class FakeReader:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.missing = False

    async def list_deployments(self, **kw: Any) -> ListResponse[DeploymentView]:
        self.calls.append(("list", kw))
        return ListResponse[DeploymentView](
            items=[DeploymentView(**_view())], page=PageInfo(total=1, limit=kw["limit"], offset=0)
        )

    async def get_deployment(self, **kw: Any) -> DeploymentDetail:
        self.calls.append(("get", kw))
        if self.missing:
            raise DeploymentNotFoundError
        return detail()

    async def deployments_for_plan(self, **kw: Any) -> list[DeploymentView]:
        self.calls.append(("plan", kw))
        if self.missing:
            raise PlanNotFoundError
        return [DeploymentView(**_view())]


@pytest.fixture
def service() -> FakeService:
    return FakeService()


@pytest.fixture
def reader() -> FakeReader:
    return FakeReader()


def _client(service: FakeService, reader: FakeReader, role: Role | None = None) -> TestClient:
    app = make_app(role=role)
    app.dependency_overrides[get_deployment_service] = lambda: service
    app.dependency_overrides[get_deployment_reader] = lambda: reader
    return TestClient(app, raise_server_exceptions=False)


def _deploy_body(**kw: Any) -> dict[str, Any]:
    return {"operation_id": str(uuid.uuid4()), "plan_ids": [str(PLAN)], **kw}


def _rollback_body(**kw: Any) -> dict[str, Any]:
    return {"operation_id": str(uuid.uuid4()), "reason": "editorial_reversal", **kw}


ROUTES = [
    ("/deployments/dry-run", _deploy_body),
    ("/deployments", _deploy_body),
    (f"/deployments/{DEPLOYMENT}/verify", None),
    (f"/deployments/{DEPLOYMENT}/rollback", _rollback_body),
]


def _post(client: TestClient, path: str, body: Any) -> Any:
    return client.post(path, json=None if body is None else body())


def _error(response: Any, status: int) -> ErrorResponse:
    assert response.status_code == status, response.text
    return ErrorResponse.model_validate(response.json())


# --------------------------------------------------------------------------- #
# Error mapping
# --------------------------------------------------------------------------- #
def test_every_issue_code_has_a_fixed_message() -> None:
    assert set(DEPLOYMENT_ISSUE_MESSAGES) == {c.value for c in DeploymentIssueCode}
    assert all(m and m.endswith(".") for m in DEPLOYMENT_ISSUE_MESSAGES.values())


@pytest.mark.parametrize(("path", "body"), ROUTES)
def test_a_refusal_is_409_conflict_with_one_detail_per_issue(
    service: FakeService, reader: FakeReader, path: str, body: Any
) -> None:
    service.raises = DeploymentRefusedError(
        [
            DeploymentIssue(code=C.PLAN_NOT_APPROVED, plan_id=PLAN),
            DeploymentIssue(code=C.DENSITY_EXCEEDED),
            DeploymentIssue(code=C.ALREADY_LINKED, plan_id=OTHER),
        ]
    )
    error = _error(_post(_client(service, reader), path, body), 409)
    assert error.code.value == "conflict"
    assert error.message == "The deployment request was refused."
    assert [(d.code, d.location, d.message) for d in error.details] == [
        ("plan_not_approved", str(PLAN), DEPLOYMENT_ISSUE_MESSAGES["plan_not_approved"]),
        ("density_exceeded", None, DEPLOYMENT_ISSUE_MESSAGES["density_exceeded"]),
        ("already_linked", str(OTHER), DEPLOYMENT_ISSUE_MESSAGES["already_linked"]),
    ]


@pytest.mark.parametrize(
    "code",
    [C.DEPLOYMENT_IN_PROGRESS, C.ROLLBACK_CONFLICT, C.NOTHING_TO_ROLL_BACK],
)
def test_lock_and_rollback_refusals_are_409_with_the_issue(
    service: FakeService, reader: FakeReader, code: DeploymentIssueCode
) -> None:
    plan = PLAN if code is C.ROLLBACK_CONFLICT else None
    service.raises = DeploymentRefusedError([DeploymentIssue(code=code, plan_id=plan)])
    client = _client(service, reader)
    for path, body in ROUTES:
        error = _error(_post(client, path, body), 409)
        assert error.code.value == "conflict"
        assert [(d.code, d.location) for d in error.details] == [
            (code.value, None if plan is None else str(plan))
        ]


def test_a_reused_operation_id_is_409_with_empty_details(
    service: FakeService, reader: FakeReader
) -> None:
    service.raises = OperationReusedDeploymentError([])
    for path, body in ROUTES:
        error = _error(_post(_client(service, reader), path, body), 409)
        assert error.code.value == "conflict" and error.details == []


def test_a_state_refusal_is_409_conflict(service: FakeService, reader: FakeReader) -> None:
    service.raises = DeploymentStateError([])
    error = _error(_post(_client(service, reader), f"/deployments/{DEPLOYMENT}/verify", None), 409)
    assert error.code.value == "conflict" and error.details == []


def test_a_version_conflict_is_409_cms_version_conflict(
    service: FakeService, reader: FakeReader
) -> None:
    service.raises = DeploymentVersionConflictError([])
    for path, body in ROUTES:
        error = _error(_post(_client(service, reader), path, body), 409)
        assert error.code.value == "cms_version_conflict" and error.details == []


def test_an_unavailable_cms_is_503_cms_unavailable(
    service: FakeService, reader: FakeReader
) -> None:
    service.raises = CMSUnavailableDeploymentError([DeploymentIssue(code=C.CMS_UNAVAILABLE)])
    for path, body in ROUTES:
        response = _post(_client(service, reader), path, body)
        error = _error(response, 503)
        assert error.code.value == "cms_unavailable"
        assert response.headers["retry-after"] == "30"
        assert [(d.code, d.location) for d in error.details] == [("cms_unavailable", None)]


def test_not_found_is_404(service: FakeService, reader: FakeReader) -> None:
    service.raises = DeploymentNotFoundError()
    error = _error(_post(_client(service, reader), f"/deployments/{DEPLOYMENT}/verify", None), 404)
    assert error.code.value == "not_found" and error.message == "Deployment not found."
    service.raises = PlanNotFoundError()
    error = _error(_post(_client(service, reader), "/deployments", _deploy_body), 404)
    assert error.message == "Link plan not found."


def test_the_service_permission_check_is_403(service: FakeService, reader: FakeReader) -> None:
    service.raises = DeploymentPermissionError()
    for path, body in ROUTES:
        error = _error(_post(_client(service, reader), path, body), 403)
        assert error.code.value == "permission_denied"


def test_error_bodies_never_echo_input(service: FakeService, reader: FakeReader) -> None:
    secret_note = "NOTE-<script>-do-not-echo"
    service.raises = DeploymentRefusedError([DeploymentIssue(code=C.PLAN_INVALID, plan_id=PLAN)])
    response = _client(service, reader).post("/deployments", json=_deploy_body(note=secret_note))
    assert response.status_code == 409
    assert secret_note not in response.text


# --------------------------------------------------------------------------- #
# Success shapes
# --------------------------------------------------------------------------- #
def test_a_dry_run_with_issues_is_200_valid_false(service: FakeService, reader: FakeReader) -> None:
    service.result = DryRunResult(
        source_page_id=SOURCE,
        valid=False,
        changes=[],
        issues=[DeploymentIssue(code=C.PLAN_STALE, plan_id=PLAN)],
    )
    response = _client(service, reader).post("/deployments/dry-run", json=_deploy_body())
    assert response.status_code == 200
    result = DryRunResult.model_validate(response.json())
    assert not result.valid and result.issues[0].code is C.PLAN_STALE


@pytest.mark.parametrize("status", [DeploymentStatus.FAILED, DeploymentStatus.CONFLICT])
def test_a_deploy_that_ended_failed_or_in_conflict_is_200_with_its_detail(
    service: FakeService, reader: FakeReader, status: DeploymentStatus
) -> None:
    service.result = detail(status, error_code="cms_version_conflict")
    response = _client(service, reader).post("/deployments", json=_deploy_body())
    assert response.status_code == 200
    assert DeploymentDetail.model_validate(response.json()).status is status


def test_the_service_receives_the_session_principal_and_the_body(
    service: FakeService, reader: FakeReader
) -> None:
    client = _client(service, reader)
    body = _deploy_body(note="go")
    assert client.post("/deployments", json=body).status_code == 200
    name, kwargs = service.calls[-1]
    assert name == "deploy" and kwargs["principal"].username == "test-user"
    assert (
        kwargs["request"].note == "go"
        and str(kwargs["request"].operation_id) == body["operation_id"]
    )
    assert (
        client.post(f"/deployments/{DEPLOYMENT}/rollback", json=_rollback_body()).status_code == 200
    )
    assert service.calls[-1][1]["deployment_id"] == DEPLOYMENT


# --------------------------------------------------------------------------- #
# Validation and permissions
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("path", "body"),
    [
        ("/deployments", {"operation_id": str(uuid.uuid4()), "plan_ids": []}),
        ("/deployments", {"operation_id": "nope", "plan_ids": [str(PLAN)]}),
        ("/deployments", _deploy_body(plan_ids=[str(PLAN), str(PLAN)])),
        ("/deployments", _deploy_body(plan_ids=[str(uuid.uuid4()) for _ in range(21)])),
        ("/deployments", _deploy_body(note="x" * 501)),
        ("/deployments", _deploy_body(extra="field")),
        ("/deployments/dry-run", {"plan_ids": [str(PLAN)]}),
        (f"/deployments/{DEPLOYMENT}/rollback", _rollback_body(reason="other")),
        (f"/deployments/{DEPLOYMENT}/rollback", _rollback_body(reason="because")),
        (f"/deployments/{DEPLOYMENT}/rollback", _rollback_body(plan_ids=[str(PLAN)] * 2)),
        ("/deployments/not-a-uuid/verify", None),
    ],
)
def test_invalid_requests_are_422(
    service: FakeService, reader: FakeReader, path: str, body: Any
) -> None:
    response = _client(service, reader).post(path, json=body)
    assert _error(response, 422).code.value == "validation_failed"
    assert service.calls == []


@pytest.mark.parametrize("role", [Role.VIEWER, Role.REVIEWER])
def test_only_operators_may_deploy_but_everyone_may_read(
    service: FakeService, reader: FakeReader, role: Role
) -> None:
    client = _client(service, reader, role=role)
    for path, body in ROUTES:
        assert _error(_post(client, path, body), 403).code.value == "permission_denied"
    assert service.calls == []
    assert client.get("/deployments").status_code == 200
    assert client.get(f"/deployments/{DEPLOYMENT}").status_code == 200
    assert client.get(f"/recommendations/{PLAN}/deployments").status_code == 200


def test_the_reader_routes(service: FakeService, reader: FakeReader) -> None:
    client = _client(service, reader)
    listed = client.get(f"/deployments?limit=5&offset=0&status=failed&source_page_id={SOURCE}")
    assert listed.status_code == 200
    assert ListResponse[DeploymentView].model_validate(listed.json()).page.total == 1
    assert reader.calls[-1] == (
        "list",
        {"limit": 5, "offset": 0, "status": DeploymentStatus.FAILED, "source_page_id": SOURCE},
    )
    assert DeploymentDetail.model_validate(client.get(f"/deployments/{DEPLOYMENT}").json()).id == (
        DEPLOYMENT
    )
    assert reader.calls[-1] == ("get", {"deployment_id": DEPLOYMENT})
    assert len(client.get(f"/recommendations/{PLAN}/deployments").json()) == 1
    assert client.get("/deployments?limit=0").status_code == 422
    assert client.get("/deployments?status=deployed").status_code == 422
    reader.missing = True
    assert _error(client.get(f"/deployments/{DEPLOYMENT}"), 404).message == "Deployment not found."
    assert _error(client.get(f"/recommendations/{PLAN}/deployments"), 404).code.value == (
        "not_found"
    )


def test_the_default_dependencies_build_the_real_service_and_reader() -> None:
    """Composition root: the SQL reader over the READ ONLY factory; the service with the real
    HtmlMutator / HtmlLinkVerifier resolved by name."""
    from types import SimpleNamespace

    from ken_api.deps import get_readonly_sessionmaker
    from ken_core.db.engine import create_engine
    from ken_core.db.session import ReadOnlySession, create_sessionmaker
    from ken_services.deployment.mutation import HtmlMutator
    from ken_services.deployment.reader import SqlDeploymentReader
    from ken_services.deployment.service import DatabaseDeploymentService
    from ken_services.deployment.verification import HtmlLinkVerifier
    from tests.conftest import FakeCMSProvider, make_settings

    settings = make_settings()
    engine = create_engine(settings)
    state = SimpleNamespace(sessionmaker=create_sessionmaker(engine))
    request: Any = SimpleNamespace(app=SimpleNamespace(state=state))
    readonly = get_readonly_sessionmaker(request)
    assert readonly.class_ is not None and readonly.kw["sync_session_class"] is ReadOnlySession
    assert get_readonly_sessionmaker(request) is readonly  # derived once
    reader = get_deployment_reader(readonly)
    assert isinstance(reader, SqlDeploymentReader)
    built = get_deployment_service(request, settings, state.sessionmaker, FakeCMSProvider())
    assert isinstance(built, DatabaseDeploymentService)
    assert isinstance(built._mutator, HtmlMutator)
    assert isinstance(built._verifier, HtmlLinkVerifier)
    assert built._sessionmaker is state.sessionmaker  # the read-write factory
