"""The GET-only /agents API over a fake reader (no database)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

import pytest
from fastapi.testclient import TestClient

from ken_api.deps import get_agent_reader
from ken_contracts.agents import (
    AgentNode,
    AgentOutcome,
    AgentPlanReview,
    AgentReader,
    AgentRunDetail,
    AgentRunNotFoundError,
    AgentRunStatus,
    AgentRunSummary,
    AgentStepView,
    AgentTrigger,
    CriticReason,
    CriticVerdict,
    StepStatus,
    ToolCallRecord,
    ToolName,
    ToolPermission,
    TriageDecision,
)
from ken_contracts.errors import ErrorResponse, ListResponse, PageInfo
from ken_core.db.engine import create_engine
from ken_core.db.session import create_sessionmaker
from tests.conftest import make_app, make_settings

NOW = datetime(2026, 10, 14, 9, 0, tzinfo=UTC)
RUN_ID = uuid.UUID("11111111-1111-4111-8111-111111111111")
JOB_ID = uuid.UUID("22222222-2222-4222-8222-222222222222")
SOURCE_ID = uuid.UUID("33333333-3333-4333-8333-333333333333")
PLAN_ID = uuid.UUID("44444444-4444-4444-8444-444444444444")
CORRELATION = uuid.UUID("55555555-5555-4555-8555-555555555555")
UNKNOWN = uuid.UUID("99999999-9999-4999-8999-999999999999")


def _summary() -> AgentRunSummary:
    return AgentRunSummary(
        id=RUN_ID,
        job_run_id=JOB_ID,
        source_page_id=SOURCE_ID,
        source_page_version=1,
        source_url="https://site.test/reports/ev",
        trigger=AgentTrigger.BATCH,
        status=AgentRunStatus.AWAITING_APPROVAL,
        outcome=AgentOutcome.RECOMMEND,
        triage_decision=TriageDecision.OPTIMIZE,
        handed_off_plan_ids=[PLAN_ID],
        correlation_id=CORRELATION,
        trace_id=CORRELATION.hex,
        graph_version="graph-v1",
        started_at=NOW,
        finished_at=NOW,
    )


def _review() -> AgentPlanReview:
    return AgentPlanReview(
        run_id=RUN_ID,
        plan_id=PLAN_ID,
        verdict=CriticVerdict.ACCEPT,
        reasons=[CriticReason.NATURAL_ANCHOR],
        explanation="<b>reads naturally</b>",
        handed_off=True,
        fallback_used=False,
        model="reasoning-model",
        reviewed_at=NOW,
    )


def _detail() -> AgentRunDetail:
    step = AgentStepView(
        run_id=RUN_ID,
        seq=0,
        node=AgentNode.LOAD_PAGE,
        status=StepStatus.SUCCEEDED,
        attempt=1,
        started_at=NOW,
        finished_at=NOW,
        duration_ms=3,
        tool_calls=[
            ToolCallRecord(
                tool=ToolName.GET_PAGE, permission=ToolPermission.READ, ok=True, duration_ms=2
            )
        ],
        output={"found": True},
        trace_id=CORRELATION.hex,
        span_id="0123456789abcdef",
    )
    return AgentRunDetail(
        **_summary().model_dump(),
        steps=[step],
        reviews=[_review()],
        config_snapshot={"graph_version": "graph-v1"},
        review_notes=[],
    )


class FakeAgentReader:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def list_runs(self, **kwargs: Any) -> ListResponse[AgentRunSummary]:
        self.calls.append(("list_runs", kwargs))
        items = [_summary()]
        limit, offset = kwargs["limit"], kwargs["offset"]
        return ListResponse[AgentRunSummary](
            items=items[offset : offset + limit],
            page=PageInfo(total=len(items), limit=limit, offset=offset),
        )

    async def get_run(self, *, run_id: uuid.UUID) -> AgentRunDetail:
        self.calls.append(("get_run", {"run_id": run_id}))
        if run_id != RUN_ID:
            raise AgentRunNotFoundError
        return _detail()

    async def reviews_for_plan(self, *, plan_id: uuid.UUID) -> list[AgentPlanReview]:
        self.calls.append(("reviews_for_plan", {"plan_id": plan_id}))
        return [_review()] if plan_id == PLAN_ID else []


@pytest.fixture
def reader() -> FakeAgentReader:
    return FakeAgentReader()


@pytest.fixture
def client(reader: FakeAgentReader) -> TestClient:
    app = make_app()
    app.dependency_overrides[get_agent_reader] = lambda: reader
    return TestClient(app)


def test_the_fake_satisfies_the_protocol(reader: FakeAgentReader) -> None:
    assert isinstance(reader, AgentReader)


def test_list_runs_returns_the_contract_model(client: TestClient) -> None:
    response = client.get("/agents/runs")
    assert response.status_code == 200, response.text
    body = response.json()
    body["page"].pop("has_more")  # computed, output-only
    parsed = ListResponse[AgentRunSummary].model_validate(body)
    assert parsed.items[0].handed_off_plan_ids == [PLAN_ID]
    assert parsed.items[0].trace_id == CORRELATION.hex


def test_run_detail_carries_steps_reviews_and_snapshot(client: TestClient) -> None:
    response = client.get(f"/agents/runs/{RUN_ID}")
    assert response.status_code == 200, response.text
    detail = AgentRunDetail.model_validate(response.json())
    assert [s.node for s in detail.steps] == [AgentNode.LOAD_PAGE]
    assert detail.steps[0].tool_calls[0].tool is ToolName.GET_PAGE
    assert detail.reviews[0].verdict is CriticVerdict.ACCEPT
    assert detail.config_snapshot == {"graph_version": "graph-v1"}
    # Critic text is data: returned verbatim as a JSON string, never interpreted.
    assert response.json()["reviews"][0]["explanation"] == "<b>reads naturally</b>"


def test_plan_reviews_route(client: TestClient) -> None:
    response = client.get(f"/agents/plans/{PLAN_ID}/reviews")
    assert response.status_code == 200
    assert [AgentPlanReview.model_validate(r).plan_id for r in response.json()] == [PLAN_ID]
    empty = client.get(f"/agents/plans/{UNKNOWN}/reviews")
    assert empty.status_code == 200
    assert empty.json() == []


def test_defaults_and_parameters_reach_the_reader(
    client: TestClient, reader: FakeAgentReader
) -> None:
    client.get("/agents/runs")
    client.get(
        f"/agents/runs?status=completed&outcome=needs_human_review&source_page_id={SOURCE_ID}"
        "&limit=10&offset=5"
    )
    assert reader.calls[0] == (
        "list_runs",
        {"status": None, "outcome": None, "source_page_id": None, "limit": 50, "offset": 0},
    )
    assert reader.calls[1] == (
        "list_runs",
        {
            "status": AgentRunStatus.COMPLETED,
            "outcome": AgentOutcome.NEEDS_HUMAN_REVIEW,
            "source_page_id": SOURCE_ID,
            "limit": 10,
            "offset": 5,
        },
    )


def test_an_unknown_run_is_404_without_echoing_the_id(client: TestClient) -> None:
    response = client.get(f"/agents/runs/{UNKNOWN}")
    assert response.status_code == 404
    body = ErrorResponse.model_validate(response.json())
    assert body.code.value == "not_found"
    assert body.message == "Agent run not found."
    assert str(UNKNOWN) not in response.text


@pytest.mark.parametrize(
    "path",
    [
        "/agents/runs/not-a-uuid",
        "/agents/runs?limit=0",
        "/agents/runs?limit=201",
        "/agents/runs?offset=-1",
        "/agents/runs?status=sleeping",
        "/agents/runs?outcome=approve",
        "/agents/runs?source_page_id=x",
        "/agents/plans/not-a-uuid/reviews",
    ],
)
def test_invalid_parameters_are_422_error_responses(client: TestClient, path: str) -> None:
    response = client.get(path)
    assert response.status_code == 422, response.text
    assert ErrorResponse.model_validate(response.json()).code.value == "validation_failed"


@pytest.mark.parametrize("method", ["post", "put", "patch", "delete"])
@pytest.mark.parametrize(
    "path", ["/agents/runs", f"/agents/runs/{RUN_ID}", f"/agents/plans/{PLAN_ID}/reviews"]
)
def test_there_are_no_mutation_routes(client: TestClient, method: str, path: str) -> None:
    response = client.request(method.upper(), path)
    assert response.status_code == 405
    ErrorResponse.model_validate(response.json())


def test_every_agent_route_is_get_only() -> None:
    paths = make_app().openapi()["paths"]
    ours = {path: set(ops) for path, ops in paths.items() if path.startswith("/agents")}
    assert set(ours) == {
        "/agents/runs",
        "/agents/runs/{run_id}",
        "/agents/plans/{plan_id}/reviews",
    }
    assert all(methods == {"get"} for methods in ours.values()), ours


async def test_the_default_dependency_builds_the_database_reader() -> None:
    from ken_services.agents.reader import DatabaseAgentReader

    settings = make_settings()
    engine = create_engine(settings)  # creating an engine performs no I/O
    try:
        reader = get_agent_reader(create_sessionmaker(engine))
        assert isinstance(reader, DatabaseAgentReader)
        assert isinstance(reader, AgentReader)
    finally:
        await engine.dispose()
