"""``DatabaseAgentRunStore`` against the ``*_test`` database.

What to review; reaping orphaned runs; one running run per source; append-only, bounded
steps on running runs only; finishing exactly once with a consistent lifecycle.
"""

from __future__ import annotations

import uuid
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.exc import IntegrityError

from ken_contracts.agents import (
    MAX_REVIEW_NOTE_CHARS,
    MAX_REVIEW_NOTES,
    MAX_STEP_OUTPUT_BYTES,
    AgentErrorCode,
    AgentNode,
    AgentOutcome,
    AgentRunStatus,
    CriticVerdict,
    ToolCallRecord,
    ToolName,
    ToolPermission,
    TriageDecision,
)
from ken_services.agents.protocol import AgentRunStore, PlanReviewInput
from ken_services.agents.store import (
    AgentRunAlreadyRunningError,
    AgentRunStateError,
    json_text_bytes,
)
from tests.integration.agent_support import (
    CHARGING,
    EV,
    LI,
    SM,
    T0,
    backend,
    expire,
    job_run,
    new_run,
    plans_corpus,
    set_job_status,
    start,
    step,
    store,
)
from tests.integration.recommendation_support import SOLAR, rows, scalar

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("migrated")]

RS = AgentRunStatus
AO = AgentOutcome


async def _run_row(sessionmaker: SM, run_id: uuid.UUID) -> Any:
    found = await rows(sessionmaker, "SELECT * FROM agent_runs WHERE id = :id", id=run_id)
    return found[0]


def test_the_store_satisfies_the_protocol(sessionmaker: SM) -> None:
    assert isinstance(store(sessionmaker), AgentRunStore)


# --------------------------------------------------------------------------- #
# sources_to_review
# --------------------------------------------------------------------------- #
async def test_sources_to_review_by_url_without_running_or_fully_reviewed_sources(
    sessionmaker: SM, tmp_path: Path
) -> None:
    ids = await plans_corpus(sessionmaker, tmp_path)
    runs = store(sessionmaker)
    found = await rows(sessionmaker, "SELECT canonical_url, id FROM pages")
    everyone = [r.id for r in sorted(found, key=lambda r: (r.canonical_url, str(r.id)))]
    assert len(everyone) == len(ids)  # every corpus page has proposed plans
    assert await runs.sources_to_review(limit=100) == everyone
    assert await runs.sources_to_review(limit=2) == everyone[:2]

    # A running run hides its source; finishing it shows the source again.
    run = await start(sessionmaker, ids[EV])
    assert ids[EV] not in await runs.sources_to_review(limit=100)
    # Every EV plan reviewed (or no longer proposed): EV has nothing left to review.
    plans = await backend(sessionmaker).list_proposed_plans(source_page_id=ids[EV], limit=10)
    await backend(sessionmaker).hand_off(
        agent_run_id=run.id,
        reviews=[PlanReviewInput(plan_id=plans[0].id, verdict=CriticVerdict.REJECT)],
        reviewed_at=T0,
    )
    await runs.finish_run(run_id=run.id, status=RS.COMPLETED, outcome=AO.NO_ACTION,
                          finished_at=T0)  # fmt: skip
    assert ids[EV] in await runs.sources_to_review(limit=100)  # one plan still unreviewed
    await expire(sessionmaker, plans[1].id)
    assert await runs.sources_to_review(limit=100) == [i for i in everyone if i != ids[EV]]
    for bad in (0, 10_001):
        with pytest.raises(ValueError, match="limit"):
            await runs.sources_to_review(limit=bad)


# --------------------------------------------------------------------------- #
# start / reap
# --------------------------------------------------------------------------- #
async def test_start_run_writes_a_running_row(sessionmaker: SM, tmp_path: Path) -> None:
    ids = await plans_corpus(sessionmaker, tmp_path)
    run = await start(sessionmaker, ids[EV])
    row = await _run_row(sessionmaker, run.id)
    assert row.status == "running"
    assert row.trace_id == run.correlation_id.hex
    assert row.correlation_id == run.correlation_id
    assert row.trigger == "batch"
    assert row.config_snapshot == {
        "graph_version": "graph-v1",
        "agents_policy": {"version": 1},
    }
    assert row.outcome is None and row.finished_at is None
    assert row.started_at == T0


async def test_a_second_running_run_for_a_source_is_refused(
    sessionmaker: SM, tmp_path: Path
) -> None:
    ids = await plans_corpus(sessionmaker, tmp_path)
    first = await start(sessionmaker, ids[EV])
    with pytest.raises(AgentRunAlreadyRunningError):
        await start(sessionmaker, ids[EV], job_id=first.job_run_id)
    # Another source, but the first run's correlation id (unique).
    duplicate = replace(new_run(first.job_run_id, ids[LI]), correlation_id=first.correlation_id)
    with pytest.raises(IntegrityError):
        await store(sessionmaker).start_run(run=duplicate)
    assert await scalar(sessionmaker, "SELECT count(*) FROM agent_runs") == 1


async def test_reap_orphans_fails_running_runs_of_every_other_batch(
    sessionmaker: SM, tmp_path: Path
) -> None:
    """The caller holds the agent advisory lock, so a running run of ANY other batch is
    orphaned -- including a hard crash whose job row still says 'running' (QA H2)."""
    ids = await plans_corpus(sessionmaker, tmp_path)
    runs = store(sessionmaker)
    dead_job, crashed_job, current_job = (
        await job_run(sessionmaker),
        await job_run(sessionmaker),
        await job_run(sessionmaker),
    )
    orphan = await start(sessionmaker, ids[EV], job_id=dead_job)
    finished = await start(sessionmaker, ids[LI], job_id=dead_job)
    await runs.finish_run(run_id=finished.id, status=RS.COMPLETED, outcome=AO.NO_ACTION,
                          finished_at=T0 + timedelta(seconds=1))  # fmt: skip
    await set_job_status(sessionmaker, dead_job, "failed")
    crashed = await start(sessionmaker, ids[SOLAR], job_id=crashed_job)  # job row: running
    alive = await start(sessionmaker, ids[CHARGING], job_id=current_job)

    now = T0 + timedelta(minutes=10)
    assert await runs.reap_orphans(now=now, current_job_run_id=current_job) == 2
    for reaped in (orphan, crashed):
        row = await _run_row(sessionmaker, reaped.id)
        assert (row.status, row.outcome, row.error_code) == ("failed", "failed", "orphaned")
        assert row.finished_at == now
    assert (await _run_row(sessionmaker, finished.id)).outcome == "no_action"
    assert (await _run_row(sessionmaker, alive.id)).status == "running"
    assert await runs.reap_orphans(now=now, current_job_run_id=current_job) == 0  # idempotent
    # The reaped sources can be reviewed again, by the current batch.
    await start(sessionmaker, ids[SOLAR], job_id=current_job)
    # A clock behind started_at never produces finished_at < started_at.
    next_job = await job_run(sessionmaker)
    assert await runs.reap_orphans(now=T0 - timedelta(days=1), current_job_run_id=next_job) == 2
    assert not await rows(sessionmaker, "SELECT 1 FROM agent_runs WHERE finished_at < started_at")


# --------------------------------------------------------------------------- #
# append_step
# --------------------------------------------------------------------------- #
async def test_append_step_persists_the_view(sessionmaker: SM, tmp_path: Path) -> None:
    ids = await plans_corpus(sessionmaker, tmp_path)
    run = await start(sessionmaker, ids[EV])
    call = ToolCallRecord(
        tool=ToolName.GET_PAGE,
        permission=ToolPermission.READ,
        ok=True,
        duration_ms=3,
        summary={"found": True},
    )
    view = step(run, 0, tool_calls=[call], output={"current_version": 1})
    runs = store(sessionmaker)
    await runs.append_step(step=view)
    await runs.append_step(step=step(run, 1, node=AgentNode.TRIAGE))
    found = await rows(
        sessionmaker,
        "SELECT seq, node, tool_calls, output, trace_id, span_id FROM agent_steps ORDER BY seq",
    )
    assert [(r.seq, r.node) for r in found] == [(0, "load_page"), (1, "triage")]
    assert found[0].tool_calls == [call.model_dump(mode="json")]
    assert found[0].output == {"current_version": 1}
    assert {r.trace_id for r in found} == {run.trace_id}
    with pytest.raises(IntegrityError):  # (run_id, seq) is unique
        await runs.append_step(step=step(run, 1))


async def test_step_payloads_are_bounded_before_the_database(
    sessionmaker: SM, tmp_path: Path
) -> None:
    ids = await plans_corpus(sessionmaker, tmp_path)
    run = await start(sessionmaker, ids[EV])
    runs = store(sessionmaker)
    fits = {"x": "a" * (MAX_STEP_OUTPUT_BYTES - 9)}
    assert json_text_bytes(fits) == MAX_STEP_OUTPUT_BYTES
    await runs.append_step(step=step(run, 0, output=fits))
    stored = await scalar(sessionmaker, "SELECT octet_length(output::text) FROM agent_steps")
    assert stored == MAX_STEP_OUTPUT_BYTES  # the measure matches PostgreSQL's
    # Multi-byte text counts in bytes, not characters.
    wide = {"x": "é" * (MAX_STEP_OUTPUT_BYTES // 2)}
    big_call = ToolCallRecord(
        tool=ToolName.CHECK_PLAN,
        permission=ToolPermission.READ,
        ok=True,
        duration_ms=1,
        summary={"blob": "b" * MAX_STEP_OUTPUT_BYTES},
    )
    for overrides, name in (
        ({"output": {"x": "a" * (MAX_STEP_OUTPUT_BYTES - 8)}}, "output"),
        ({"output": wide}, "output"),
        ({"tool_calls": [big_call]}, "tool_calls"),
    ):
        with pytest.raises(ValueError, match=name):
            await runs.append_step(step=step(run, 1, **overrides))
    assert await scalar(sessionmaker, "SELECT count(*) FROM agent_steps") == 1


async def test_steps_need_a_running_run_and_its_trace(sessionmaker: SM, tmp_path: Path) -> None:
    ids = await plans_corpus(sessionmaker, tmp_path)
    run = await start(sessionmaker, ids[EV])
    runs = store(sessionmaker)
    with pytest.raises(ValueError, match="trace_id"):
        await runs.append_step(step=step(run, 0, trace_id=uuid.uuid4().hex))
    unknown = step(run, 0, run_id=uuid.uuid4())
    with pytest.raises(AgentRunStateError):
        await runs.append_step(step=unknown)
    await runs.append_step(step=step(run, 0))
    await runs.finish_run(run_id=run.id, status=RS.COMPLETED, outcome=AO.NO_ACTION,
                          finished_at=T0 + timedelta(seconds=2))  # fmt: skip
    with pytest.raises(AgentRunStateError, match="frozen"):
        await runs.append_step(step=step(run, 1, node=AgentNode.FINALIZE))
    assert await scalar(sessionmaker, "SELECT count(*) FROM agent_steps") == 1


# --------------------------------------------------------------------------- #
# finish_run
# --------------------------------------------------------------------------- #
async def test_finish_run_exactly_once(sessionmaker: SM, tmp_path: Path) -> None:
    ids = await plans_corpus(sessionmaker, tmp_path)
    run = await start(sessionmaker, ids[EV])
    runs = store(sessionmaker)
    done = T0 + timedelta(seconds=3)
    await runs.finish_run(
        run_id=run.id,
        status=RS.COMPLETED,
        outcome=AO.NEEDS_HUMAN_REVIEW,
        finished_at=done,
        triage_decision=TriageDecision.OPTIMIZE,
        source_page_version=1,
        review_notes=("plan c1: target_ineligible",),
        error_code=AgentErrorCode.HANDOFF_CONFLICT,
    )
    row = await _run_row(sessionmaker, run.id)
    assert (row.status, row.outcome, row.triage_decision) == (
        "completed",
        "needs_human_review",
        "optimize",
    )
    assert (row.source_page_version, row.error_code) == (1, "handoff_conflict")
    assert row.review_notes == ["plan c1: target_ineligible"]
    assert row.finished_at == done and row.updated_at == done
    with pytest.raises(AgentRunStateError):
        await runs.finish_run(
            run_id=run.id, status=RS.FAILED, outcome=AO.FAILED, finished_at=done,
            error_code=AgentErrorCode.INTERNAL,
        )  # fmt: skip
    with pytest.raises(AgentRunStateError):
        await runs.finish_run(
            run_id=uuid.uuid4(), status=RS.COMPLETED, outcome=AO.NO_ACTION, finished_at=done
        )
    assert (await _run_row(sessionmaker, run.id)).outcome == "needs_human_review"


@pytest.mark.parametrize(
    ("status", "outcome", "kwargs", "message"),
    [
        (RS.RUNNING, AO.NO_ACTION, {}, "not valid"),
        (RS.COMPLETED, AO.RECOMMEND, {}, "not valid"),
        (RS.AWAITING_APPROVAL, AO.NO_ACTION, {}, "not valid"),
        (RS.COMPLETED, AO.FAILED, {"error_code": AgentErrorCode.INTERNAL}, "not valid"),
        (RS.FAILED, AO.FAILED, {}, "error code"),
        (RS.COMPLETED, AO.NO_ACTION, {"review_notes": ["n"] * (MAX_REVIEW_NOTES + 1)}, "notes"),
        (RS.COMPLETED, AO.NO_ACTION, {"review_notes": ["n" * (MAX_REVIEW_NOTE_CHARS + 1)]},
         "characters"),
    ],
)  # fmt: skip
async def test_finish_run_refuses_inconsistent_lifecycles(
    sessionmaker: SM,
    tmp_path: Path,
    status: AgentRunStatus,
    outcome: AgentOutcome,
    kwargs: dict[str, object],
    message: str,
) -> None:
    ids = await plans_corpus(sessionmaker, tmp_path)
    run = await start(sessionmaker, ids[EV])
    with pytest.raises(ValueError, match=message):
        await store(sessionmaker).finish_run(
            run_id=run.id,
            status=status,
            outcome=outcome,
            finished_at=T0,
            **kwargs,  # type: ignore[arg-type]
        )
    assert (await _run_row(sessionmaker, run.id)).status == "running"


@pytest.mark.parametrize(
    ("status", "outcome", "error_code"),
    [
        (RS.AWAITING_APPROVAL, AO.RECOMMEND, None),
        (RS.AWAITING_APPROVAL, AO.NEEDS_HUMAN_REVIEW, None),
        (RS.COMPLETED, AO.NEEDS_HUMAN_REVIEW, None),
        (RS.COMPLETED, AO.NO_ACTION, None),
        (RS.FAILED, AO.FAILED, AgentErrorCode.PAGE_NOT_FOUND),
    ],
)
async def test_every_adr_outcome_can_be_recorded(
    sessionmaker: SM,
    tmp_path: Path,
    status: AgentRunStatus,
    outcome: AgentOutcome,
    error_code: AgentErrorCode | None,
) -> None:
    ids = await plans_corpus(sessionmaker, tmp_path)
    run = await start(sessionmaker, ids[EV])
    await store(sessionmaker).finish_run(
        run_id=run.id, status=status, outcome=outcome, finished_at=T0, error_code=error_code
    )
    row = await _run_row(sessionmaker, run.id)
    assert (row.status, row.outcome) == (status.value, outcome.value)
