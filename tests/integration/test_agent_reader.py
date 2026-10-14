"""``DatabaseAgentReader`` (and the /agents API over it) against the ``*_test`` database."""

from __future__ import annotations

import uuid
from datetime import timedelta
from pathlib import Path

import httpx
import pytest

from ken_contracts.agents import (
    AgentErrorCode,
    AgentNode,
    AgentOutcome,
    AgentPlanReview,
    AgentReader,
    AgentRunDetail,
    AgentRunNotFoundError,
    AgentRunStatus,
    AgentRunSummary,
    CriticReason,
    CriticVerdict,
    FallbackReason,
    StepStatus,
    ToolCallRecord,
    ToolName,
    ToolPermission,
    TriageDecision,
)
from ken_contracts.errors import ListResponse
from ken_contracts.model import ModelRole
from ken_services.agents.protocol import NewAgentRun, PlanReviewInput
from ken_services.agents.reader import MAX_LIMIT, DatabaseAgentReader
from tests.conftest import make_app
from tests.integration.agent_support import (
    CHARGING,
    EV,
    LI,
    SM,
    T0,
    backend,
    plans_corpus,
    start,
    step,
    store,
)

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("migrated")]

RS = AgentRunStatus
AO = AgentOutcome


def _reader(sessionmaker: SM) -> DatabaseAgentReader:
    return DatabaseAgentReader(sessionmaker=sessionmaker)


async def _history(sessionmaker: SM, tmp_path: Path) -> dict[str, NewAgentRun]:
    """Three finished runs and one running run, started one minute apart.

    ``handed``  EV, recommend: the charging plan accepted, the LI plan rejected.
    ``nothing`` LI, completed / no_action.
    ``broken``  CHARGING, failed / page_not_found.
    ``live``    EV again, still running.
    """
    ids = await plans_corpus(sessionmaker, tmp_path)
    runs = store(sessionmaker)
    tools = backend(sessionmaker)

    handed = await start(sessionmaker, ids[EV], started_at=T0)
    plans = {
        p.target_page_id: p
        for p in await tools.list_proposed_plans(source_page_id=ids[EV], limit=10)
    }
    call = ToolCallRecord(
        tool=ToolName.LIST_PROPOSED_PLANS, permission=ToolPermission.READ, ok=True, duration_ms=4
    )
    await runs.append_step(step=step(handed, 1, node=AgentNode.TRIAGE))
    await runs.append_step(step=step(handed, 0, tool_calls=[call], output={"plans": 2}))
    await runs.append_step(
        step=step(
            handed,
            2,
            node=AgentNode.CRITIQUE,
            model_role=ModelRole.REASONING,
            fallback_used=True,
            fallback_reason=FallbackReason.MODEL_UNAVAILABLE,
            status=StepStatus.SUCCEEDED,
        )
    )
    await tools.hand_off(
        agent_run_id=handed.id,
        reviews=[
            PlanReviewInput(
                plan_id=plans[ids[CHARGING]].id,
                verdict=CriticVerdict.ACCEPT,
                reasons=(CriticReason.NATURAL_ANCHOR,),
                explanation="Natural.",
                fallback_reason=FallbackReason.MODEL_UNAVAILABLE,
            ),
            PlanReviewInput(plan_id=plans[ids[LI]].id, verdict=CriticVerdict.REJECT),
        ],
        reviewed_at=T0 + timedelta(seconds=3),
    )
    await runs.finish_run(
        run_id=handed.id,
        status=RS.AWAITING_APPROVAL,
        outcome=AO.RECOMMEND,
        finished_at=T0 + timedelta(seconds=4),
        triage_decision=TriageDecision.OPTIMIZE,
        source_page_version=1,
    )

    nothing = await start(sessionmaker, ids[LI], started_at=T0 + timedelta(minutes=1))
    await runs.finish_run(
        run_id=nothing.id,
        status=RS.COMPLETED,
        outcome=AO.NO_ACTION,
        finished_at=T0 + timedelta(minutes=1, seconds=1),
        triage_decision=TriageDecision.NO_ACTION,
        review_notes=["nothing to review"],
    )
    broken = await start(sessionmaker, ids[CHARGING], started_at=T0 + timedelta(minutes=2))
    await runs.finish_run(
        run_id=broken.id,
        status=RS.FAILED,
        outcome=AO.FAILED,
        finished_at=T0 + timedelta(minutes=2, seconds=1),
        error_code=AgentErrorCode.PAGE_NOT_FOUND,
    )
    live = await start(sessionmaker, ids[EV], started_at=T0 + timedelta(minutes=3))
    return {"handed": handed, "nothing": nothing, "broken": broken, "live": live}


def test_the_reader_satisfies_the_protocol(sessionmaker: SM) -> None:
    assert isinstance(_reader(sessionmaker), AgentReader)


async def test_list_runs_newest_first_with_filters_and_paging(
    sessionmaker: SM, tmp_path: Path
) -> None:
    runs = await _history(sessionmaker, tmp_path)
    reader = _reader(sessionmaker)
    every = await reader.list_runs()
    order = ["live", "broken", "nothing", "handed"]
    assert [r.id for r in every.items] == [runs[name].id for name in order]
    assert every.page.total == 4
    handed = every.items[3]
    assert handed.status is RS.AWAITING_APPROVAL and handed.outcome is AO.RECOMMEND
    assert handed.triage_decision is TriageDecision.OPTIMIZE
    assert handed.source_page_version == 1
    assert handed.trace_id == runs["handed"].correlation_id.hex
    assert len(handed.handed_off_plan_ids) == 1  # the rejected plan is not handed off
    assert every.items[0].status is RS.RUNNING and every.items[0].finished_at is None

    async def ids(**filters: object) -> list[uuid.UUID]:
        found = await reader.list_runs(**filters)  # type: ignore[arg-type]
        return [r.id for r in found.items]

    assert await ids(status=RS.FAILED) == [runs["broken"].id]
    assert await ids(outcome=AO.NO_ACTION) == [runs["nothing"].id]
    ev = runs["handed"].source_page_id
    assert await ids(source_page_id=ev) == [runs["live"].id, runs["handed"].id]
    assert await ids(source_page_id=ev, status=RS.RUNNING) == [runs["live"].id]
    assert await ids(source_page_id=uuid.uuid4()) == []
    page = await reader.list_runs(limit=2, offset=1)
    assert [r.id for r in page.items] == [runs["broken"].id, runs["nothing"].id]
    assert (page.page.total, page.page.limit, page.page.offset) == (4, 2, 1)
    for limit, offset in ((0, 0), (MAX_LIMIT + 1, 0), (1, -1)):
        with pytest.raises(ValueError):
            await reader.list_runs(limit=limit, offset=offset)


async def test_run_detail_has_steps_reviews_snapshot_and_notes(
    sessionmaker: SM, tmp_path: Path
) -> None:
    runs = await _history(sessionmaker, tmp_path)
    reader = _reader(sessionmaker)
    detail = await reader.get_run(run_id=runs["handed"].id)
    assert [s.seq for s in detail.steps] == [0, 1, 2]
    assert [s.node for s in detail.steps] == [
        AgentNode.LOAD_PAGE,
        AgentNode.TRIAGE,
        AgentNode.CRITIQUE,
    ]
    assert detail.steps[0].tool_calls[0].tool is ToolName.LIST_PROPOSED_PLANS
    assert detail.steps[0].output == {"plans": 2}
    assert detail.steps[2].fallback_reason is FallbackReason.MODEL_UNAVAILABLE
    assert detail.steps[2].model_role is ModelRole.REASONING
    assert {s.trace_id for s in detail.steps} == {detail.trace_id}
    assert [(r.verdict, r.handed_off) for r in detail.reviews] == [
        (CriticVerdict.ACCEPT, True),
        (CriticVerdict.REJECT, False),
    ]
    assert detail.handed_off_plan_ids == [detail.reviews[0].plan_id]
    assert detail.reviews[0].reasons == [CriticReason.NATURAL_ANCHOR]
    assert detail.reviews[0].fallback_used is True
    assert detail.config_snapshot == {"graph_version": "graph-v1", "agents_policy": {"version": 1}}
    notes = await reader.get_run(run_id=runs["nothing"].id)
    assert notes.review_notes == ["nothing to review"]
    assert notes.steps == [] and notes.reviews == []
    with pytest.raises(AgentRunNotFoundError):
        await reader.get_run(run_id=uuid.uuid4())


async def test_reviews_for_plan(sessionmaker: SM, tmp_path: Path) -> None:
    runs = await _history(sessionmaker, tmp_path)
    reader = _reader(sessionmaker)
    detail = await reader.get_run(run_id=runs["handed"].id)
    for review in detail.reviews:
        assert await reader.reviews_for_plan(plan_id=review.plan_id) == [review]
    assert await reader.reviews_for_plan(plan_id=uuid.uuid4()) == []


async def test_the_api_serves_the_database_reader(sessionmaker: SM, tmp_path: Path) -> None:
    runs = await _history(sessionmaker, tmp_path)
    app = make_app()
    app.state.sessionmaker = sessionmaker  # what the lifespan would set
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        listed = await client.get("/agents/runs", params={"outcome": "recommend"})
        assert listed.status_code == 200, listed.text
        body = listed.json()
        body["page"].pop("has_more")
        parsed = ListResponse[AgentRunSummary].model_validate(body)
        assert [r.id for r in parsed.items] == [runs["handed"].id]

        detail = await client.get(f"/agents/runs/{runs['handed'].id}")
        assert detail.status_code == 200, detail.text
        run = AgentRunDetail.model_validate(detail.json())
        assert len(run.steps) == 3

        plan_id = run.reviews[0].plan_id
        reviews = await client.get(f"/agents/plans/{plan_id}/reviews")
        assert reviews.status_code == 200
        assert [AgentPlanReview.model_validate(r) for r in reviews.json()] == [run.reviews[0]]

        missing = await client.get(f"/agents/runs/{uuid.uuid4()}")
        assert missing.status_code == 404
        assert missing.json()["code"] == "not_found"
        assert (await client.get("/agents/runs/not-a-uuid")).status_code == 422
