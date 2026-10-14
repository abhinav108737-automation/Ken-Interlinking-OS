"""State integrity, step records, trace ids, size bounds, failure handling and batches."""

from __future__ import annotations

import asyncio
import json
import re
import uuid
from typing import Any

import pytest
from pydantic import ValidationError

from ken_agents import graph as graph_module
from ken_agents.graph import StepRecorder, build_graph, run_graph
from ken_agents.nodes import NodeContext
from ken_agents.recording import StepScratch, bound_output, bound_tool_calls, json_size
from ken_agents.runner import COUNTERS, make_batch_body, run_agent_batch
from ken_agents.state import (
    AgentState,
    DiscoverUpdate,
    LoadPageUpdate,
    StateContractError,
    TriageUpdate,
    validate_update,
)
from ken_agents.tools import ToolBox, ToolError
from ken_contracts.agents import (
    GRAPH_VERSION,
    MAX_STEP_OUTPUT_BYTES,
    AgentErrorCode,
    AgentNode,
    AgentOutcome,
    AgentRunStatus,
    AgentTrigger,
    StepStatus,
    ToolCallRecord,
    ToolName,
    ToolPermission,
)
from ken_contracts.inventory import RunStatus
from ken_contracts.jobs import JobKind, JobRunDetail
from tests.unit.agents._support import (
    NOW,
    SOURCE_ID,
    Clock,
    FakeBackend,
    FakeStore,
    make_page,
    make_plan,
    make_policy,
    run_source,
    snapshot,
)


# --------------------------------------------------------------------------- state guard
def test_validate_update_rejects_an_undeclared_key() -> None:
    with pytest.raises(StateContractError, match="sneaky"):
        validate_update(TriageUpdate, {"triage_decision": "optimize", "sneaky": 1})


def test_validate_update_rejects_another_update_model() -> None:
    with pytest.raises(StateContractError):
        validate_update(TriageUpdate, DiscoverUpdate(candidates=[]))


def test_validate_update_dumps_exclude_unset_and_keeps_objects() -> None:
    page = make_page()
    assert validate_update(LoadPageUpdate, LoadPageUpdate(source_page=page)) == {
        "source_page": page
    }


def test_agent_state_approval_is_always_required() -> None:
    with pytest.raises(ValidationError):
        AgentState(
            run_id=uuid.uuid4(),
            source_page_id=SOURCE_ID,
            trigger=AgentTrigger.MANUAL,
            approval_required=False,
        )


def _ctx(backend: FakeBackend, run_id: uuid.UUID) -> NodeContext:
    policy = make_policy(critic=False)
    return NodeContext(
        tools=ToolBox(backend=backend, policy=policy, agent_run_id=run_id, clock=Clock()),
        policy=policy,
        planner=None,
        critic=None,
    )


async def _record(_: Any) -> None:
    return None


async def test_a_node_returning_an_undeclared_key_fails_the_graph() -> None:
    """LangGraph would silently DROP the key; the guard makes it fail (ADR-0012 section 4)."""

    async def bad_triage(state, ctx, step):
        return {"triage_decision": "optimize", "not_a_state_field": True}

    backend = FakeBackend.ready([make_plan(0)])
    run_id = uuid.uuid4()
    recorder = StepRecorder(run_id=run_id, trace_id="0" * 32, sink=_record, clock=Clock())
    compiled = build_graph(
        ctx=_ctx(backend, run_id),
        recorder=recorder,
        overrides={AgentNode.TRIAGE: (bad_triage, TriageUpdate)},
    )
    initial = AgentState(run_id=run_id, source_page_id=SOURCE_ID, trigger=AgentTrigger.MANUAL)
    with pytest.raises(StateContractError):
        await run_graph(compiled, initial)
    assert recorder.steps[-1].node is AgentNode.TRIAGE
    assert recorder.steps[-1].status is StepStatus.FAILED
    assert recorder.steps[-1].error_code is AgentErrorCode.INTERNAL


async def test_runner_finishes_a_crashed_run_failed_internal_exactly_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def crash(state, ctx, step):
        raise RuntimeError("boom with secret text")

    real_build = graph_module.build_graph

    def build(**kwargs: Any) -> Any:
        return real_build(**kwargs, overrides={AgentNode.UNDERSTAND: (crash, TriageUpdate)})

    monkeypatch.setattr("ken_agents.runner.build_graph", build)
    store = FakeStore()
    result = await run_source(FakeBackend.ready([make_plan(0)]), store, None, make_policy())
    assert result.status is AgentRunStatus.FAILED
    assert result.error_code is AgentErrorCode.INTERNAL
    assert result.state is None
    assert store.finished[result.run_id]["outcome"] is AgentOutcome.FAILED
    assert [s.node for s in store.steps] == [
        AgentNode.LOAD_PAGE,
        AgentNode.TRIAGE,
        AgentNode.UNDERSTAND,
    ]
    assert "secret" not in store.steps[-1].model_dump_json()


async def test_a_node_timeout_routes_to_finalize(monkeypatch: pytest.MonkeyPatch) -> None:
    async def stall(state, ctx, step):
        await asyncio.sleep(5)

    real_build = graph_module.build_graph

    def build(**kwargs: Any) -> Any:
        return real_build(**kwargs, overrides={AgentNode.DISCOVER: (stall, DiscoverUpdate)})

    monkeypatch.setattr("ken_agents.runner.build_graph", build)
    result = await run_source(
        FakeBackend.ready([make_plan(0)]), FakeStore(), None, make_policy(node_timeout=0.05)
    )
    assert result.error_code is AgentErrorCode.TIMEOUT
    assert [s.node for s in result.steps][-2:] == [AgentNode.DISCOVER, AgentNode.FINALIZE]
    assert result.steps[-2].error_code is AgentErrorCode.TIMEOUT


async def test_a_slow_read_tool_times_out_and_is_retried() -> None:
    backend = FakeBackend.ready()
    backend.slow["get_eligibility"] = 1.0
    policy = make_policy(node_timeout=0.1, retries=1)
    tools = ToolBox(backend=backend, policy=policy, agent_run_id=uuid.uuid4(), clock=Clock())
    step = StepScratch()
    tools.bind(step)
    with pytest.raises(ToolError) as caught:
        await tools.get_eligibility(page_id=SOURCE_ID)
    assert caught.value.code is AgentErrorCode.TIMEOUT
    assert [(c.attempt, c.error_code) for c in step.tool_calls] == [
        (1, AgentErrorCode.TIMEOUT),
        (2, AgentErrorCode.TIMEOUT),
    ]
    assert backend.calls.count("get_eligibility") == 2


async def test_a_read_tool_recovers_on_retry() -> None:
    backend = FakeBackend.ready()
    backend.failures["get_page"] = 1
    tools = ToolBox(backend=backend, policy=make_policy(), agent_run_id=uuid.uuid4(), clock=Clock())
    step = StepScratch()
    tools.bind(step)
    assert await tools.get_page(page_id=SOURCE_ID) is not None
    assert [(c.attempt, c.ok) for c in step.tool_calls] == [(1, False), (2, True)]
    assert step.tool_calls[0].summary == {"error": "RuntimeError"}


async def test_unbound_toolbox_records_nothing() -> None:
    tools = ToolBox(
        backend=FakeBackend.ready(), policy=make_policy(), agent_run_id=uuid.uuid4(), clock=Clock()
    )
    assert await tools.get_page(page_id=SOURCE_ID) is not None


# --------------------------------------------------------------------------- steps / trace
async def test_steps_sequence_trace_and_span_ids() -> None:
    store = FakeStore()
    result = await run_source(
        FakeBackend.ready([make_plan(0), make_plan(1)]), store, None, make_policy(critic=False)
    )
    run = store.runs[result.run_id]
    assert run.trace_id == run.correlation_id.hex
    assert run.graph_version == GRAPH_VERSION
    assert run.source_url == "https://site.test/reports/ev"
    assert run.trigger is AgentTrigger.MANUAL
    assert [s.seq for s in store.steps] == list(range(len(AgentNode)))
    assert [s.node for s in store.steps] == list(AgentNode)
    assert {s.trace_id for s in store.steps} == {run.trace_id}
    spans = [s.span_id for s in store.steps]
    assert len(set(spans)) == len(spans)
    assert all(re.fullmatch(r"[0-9a-f]{16}", s) for s in spans)
    assert all(s.started_at <= s.finished_at and s.attempt == 1 for s in store.steps)
    load = store.steps[0]
    assert [c.tool for c in load.tool_calls] == [ToolName.GET_PAGE]
    gate = next(s for s in store.steps if s.node is AgentNode.POLICY_GATE)
    assert [(c.tool, c.permission) for c in gate.tool_calls] == [
        (ToolName.HAND_OFF_PLANS, ToolPermission.REVIEW)
    ]
    assert store.finished[result.run_id]["source_page_version"] == 2
    assert result.state is not None and result.state.step_seq == len(AgentNode)


def test_bound_output_is_deterministic_and_within_limit() -> None:
    huge = {"text": "x" * 100_000, "items": list(range(1000)), "nested": {"k": ["y" * 500] * 50}}
    first = bound_output(huge)
    assert first == bound_output(huge)
    assert json_size(first) <= MAX_STEP_OUTPUT_BYTES
    assert first["text"].endswith("...") and len(first["text"]) == 300
    assert first["items"][-1] == "... 980 more"
    many = {f"key{i}": "z" * 299 for i in range(200)}
    bounded = bound_output(many)
    assert bounded["truncated"] is True
    assert json_size(bounded) <= MAX_STEP_OUTPUT_BYTES


def test_bound_tool_calls_within_limit() -> None:
    record = ToolCallRecord(
        tool=ToolName.CHECK_PLAN,
        permission=ToolPermission.READ,
        ok=True,
        duration_ms=1,
        summary={f"k{i}": "v" * 299 for i in range(19)},
    )
    calls = bound_tool_calls([record] * 50)
    assert json_size([c.model_dump(mode="json") for c in calls]) <= MAX_STEP_OUTPUT_BYTES
    assert calls, "records are kept (summaries dropped first)"


async def test_long_titles_and_notes_stay_bounded() -> None:
    plans = [make_plan(i, title="T" * 5000) for i in range(10)]
    backend = FakeBackend.ready(plans)
    from ken_services.agents.protocol import PlanCheck

    for p in plans:
        backend.checks[p.id] = PlanCheck(ok=False, problems=("p" * 400,))
    store = FakeStore()  # its append_step/finish_run assert the byte and note bounds
    result = await run_source(backend, store, None, make_policy(critic=False, max_plans=10))
    assert result.outcome is AgentOutcome.NEEDS_HUMAN_REVIEW


# --------------------------------------------------------------------------- runner edges
async def test_start_refused_returns_none_and_finish_never_called() -> None:
    from ken_agents.runner import run_one_source

    store = FakeStore(refuse_start=True)
    policy = make_policy()
    result = await run_one_source(
        source_page_id=SOURCE_ID,
        job_run_id=uuid.uuid4(),
        trigger=AgentTrigger.MANUAL,
        backend=FakeBackend.ready([make_plan(0)]),
        store=store,
        provider=None,
        policy=policy,
        config_snapshot=snapshot(policy),
    )
    assert result is None
    assert store.finished == {}


class FakeJob:
    def __init__(self) -> None:
        self.run_id = uuid.uuid4()
        self._counters: dict[str, int] = dict.fromkeys(COUNTERS, 0)
        self.flushes = 0

    @property
    def counters(self) -> dict[str, int]:
        return dict(self._counters)

    def increment(self, name: str, by: int = 1) -> None:
        self._counters[name] = self._counters.get(name, 0) + by

    async def flush(self) -> None:
        self.flushes += 1


async def test_batch_trigger_uses_sources_to_review_and_reaps_first() -> None:
    other = uuid.uuid4()
    backend = FakeBackend.ready([make_plan(0, confidence=0.9)])
    backend.pages[other] = make_page(other)  # no analysis: reindex
    store = FakeStore(sources=[SOURCE_ID, other, uuid.uuid4()])
    policy = make_policy(critic=False)
    results: list[Any] = []
    body = make_batch_body(
        backend=backend,
        store=store,
        provider=None,
        policy=policy,
        config_snapshot=snapshot(policy),
        source_page_ids=None,
        clock=Clock(),
        results=results,
    )
    job = FakeJob()
    status = await body(job)
    assert store.reap_calls == 1
    assert store.sources_limit == policy.max_sources_per_batch
    assert job.counters == {
        "sources_considered": 3,
        "runs_succeeded": 2,
        "runs_failed": 0,
        "plans_reviewed": 1,
        "plans_handed_off": 1,
        "fallbacks": 0,
        "errors": 1,  # the unknown source: no run started
    }
    assert status is RunStatus.COMPLETED_WITH_ERRORS
    assert all(r.state.trigger is AgentTrigger.BATCH for r in results)
    assert all(store.runs[r.run_id].job_run_id == job.run_id for r in results)
    assert job.flushes == 3


async def test_manual_trigger_dedupes_and_succeeds() -> None:
    backend = FakeBackend.ready([make_plan(0, confidence=0.9)])
    store = FakeStore()
    policy = make_policy(critic=False)
    body = make_batch_body(
        backend=backend,
        store=store,
        provider=None,
        policy=policy,
        config_snapshot=snapshot(policy),
        source_page_ids=[SOURCE_ID, SOURCE_ID],
        clock=Clock(),
    )
    job = FakeJob()
    assert await body(job) is RunStatus.SUCCEEDED
    assert job.counters["sources_considered"] == 1
    assert store.sources_limit is None, "explicit sources: no selection query"
    assert {r.trigger for r in store.runs.values()} == {AgentTrigger.MANUAL}


async def test_a_failed_finish_is_counted_and_the_batch_continues() -> None:
    store = FakeStore(fail_finish=True)
    policy = make_policy(critic=False)
    body = make_batch_body(
        backend=FakeBackend.ready([make_plan(0)]),
        store=store,
        provider=None,
        policy=policy,
        config_snapshot=snapshot(policy),
        source_page_ids=[SOURCE_ID],
        clock=Clock(),
    )
    job = FakeJob()
    assert await body(job) is RunStatus.COMPLETED_WITH_ERRORS
    assert job.counters["errors"] == 1


async def test_run_agent_batch_starts_one_agent_job() -> None:
    policy = make_policy(critic=False)
    seen: dict[str, Any] = {}

    async def start_job(*, kind, triggered_by, body, config_snapshot, counter_names):
        seen.update(kind=kind, triggered_by=triggered_by, snapshot=config_snapshot)
        seen["counters"] = tuple(counter_names)
        job = FakeJob()
        status = await body(job)
        return JobRunDetail(
            id=job.run_id,
            kind=kind,
            status=status,
            triggered_by=triggered_by,
            started_at=NOW,
            completed_at=NOW,
            counters=job.counters,
            config_snapshot=dict(config_snapshot),
        )

    detail = await run_agent_batch(
        start_job=start_job,
        backend=FakeBackend.ready([make_plan(0, confidence=0.9)]),
        store=FakeStore(),
        provider=None,
        policy=policy,
        config_snapshot=snapshot(policy),
        triggered_by="cli",
        source_page_ids=[SOURCE_ID],
    )
    assert seen["kind"] is JobKind.AGENT
    assert seen["counters"] == COUNTERS
    assert detail.status is RunStatus.SUCCEEDED
    assert detail.counters["plans_handed_off"] == 1


def test_config_snapshot_is_secret_free_json() -> None:
    policy = make_policy(planner=True)
    snap = snapshot(policy)
    text = json.dumps(snap)
    assert json.loads(text) == snap
    assert snap["graph_version"] == GRAPH_VERSION
    assert snap["agent_policy_version"] == 1
    assert snap["recommendation_policy_version"] == 1
    assert snap["ranking_policy_version"] == 1
    assert snap["model_roles"] == {"planner": "fast", "critic": "reasoning"}
    for word in ("password", "secret", "token", "postgres", "api_key", "url"):
        assert word not in text.lower()
