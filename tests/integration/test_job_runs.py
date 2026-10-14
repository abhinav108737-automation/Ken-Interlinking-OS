"""The generic job runner and SqlJobReader against the ``*_test`` database."""

from __future__ import annotations

import asyncio
import hashlib
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from ken_contracts.events import EventName
from ken_contracts.inventory import RunStatus
from ken_contracts.jobs import (
    SUBJECT_JOB_RUN,
    JobCompletedPayload,
    JobKind,
    JobRunNotFoundError,
)
from ken_core.db.repositories.events import list_events
from ken_services.jobs.reader import SqlJobReader
from ken_services.jobs.runner import (
    FAILURE_INTERRUPTED,
    JOBS_EMITTED_BY,
    JobAlreadyRunningError,
    JobContext,
    JobFailedError,
    job_lock_key,
    run_job,
)
from tests.integration.conftest import TickClock

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("migrated")]

SM = async_sessionmaker[AsyncSession]


def test_lock_key_derivation_is_per_kind_and_matches_phase_1_style() -> None:
    for kind in JobKind:
        digest = hashlib.sha256(f"ken.jobs.{kind.value}".encode()).digest()
        assert job_lock_key(kind) == int.from_bytes(digest[:8], "big", signed=True)
    assert len({job_lock_key(kind) for kind in JobKind}) == len(JobKind)


async def _ok(job: JobContext) -> RunStatus:
    async with job.transaction() as tx:
        await tx.session.execute(text("SELECT 1"))
        tx.increment("done", 2)
    return RunStatus.SUCCEEDED


async def _count_runs(sessionmaker: SM) -> int:
    async with sessionmaker() as session:
        return int(await session.scalar(text("SELECT count(*) FROM job_runs")) or 0)


async def _events(sessionmaker: SM) -> list:  # type: ignore[type-arg]
    async with sessionmaker() as session:
        return await list_events(session, event_name=EventName.JOB_COMPLETED)


async def test_a_run_is_recorded_and_emits_job_completed(sessionmaker: SM) -> None:
    detail = await run_job(
        kind=JobKind.EVALUATION,
        sessionmaker=sessionmaker,
        triggered_by="test",
        body=_ok,
        config_snapshot={"suite": "x", "n": 1},
        counter_names=("done", "errors"),
        clock=TickClock(),
    )
    assert detail.status is RunStatus.SUCCEEDED
    assert detail.counters == {"done": 2, "errors": 0}
    assert detail.failure_reason is None
    stored = await SqlJobReader(sessionmaker).get_run(run_id=detail.id)
    assert stored == detail
    [event] = await _events(sessionmaker)
    assert event.subject_type == SUBJECT_JOB_RUN
    assert event.subject_id == str(detail.id)
    assert event.emitted_by == JOBS_EMITTED_BY
    assert event.correlation_id == detail.id
    payload = JobCompletedPayload.model_validate(event.payload)
    assert (payload.run_id, payload.kind, payload.status) == (
        detail.id,
        JobKind.EVALUATION,
        RunStatus.SUCCEEDED,
    )
    assert payload.counters == detail.counters
    assert payload.completed_at == detail.completed_at


async def test_counters_change_only_when_the_transaction_commits(sessionmaker: SM) -> None:
    async def body(job: JobContext) -> RunStatus:
        with pytest.raises(RuntimeError):
            async with job.transaction() as tx:
                tx.increment("written")
                await tx.session.execute(
                    text(
                        "INSERT INTO job_runs (id, kind, status, triggered_by) "
                        "VALUES (:id, 'agent', 'running', 'inner')"
                    ),
                    {"id": uuid.uuid4()},
                )
                raise RuntimeError("roll back")
        assert job.counters["written"] == 0
        job.increment("errors")
        await job.flush()
        async with job.sessionmaker() as session:
            stored = await session.scalar(
                text("SELECT counters FROM job_runs WHERE id = :id"), {"id": job.run_id}
            )
        assert stored == {"written": 0, "errors": 1}
        return RunStatus.COMPLETED_WITH_ERRORS

    detail = await run_job(
        kind=JobKind.AGENT,
        sessionmaker=sessionmaker,
        triggered_by="test",
        body=body,
        counter_names=("written", "errors"),
    )
    assert detail.status is RunStatus.COMPLETED_WITH_ERRORS
    assert await _count_runs(sessionmaker) == 1  # the inner insert was rolled back


async def test_a_held_lock_refuses_the_run_and_writes_nothing(
    sessionmaker: SM, async_engine: AsyncEngine
) -> None:
    key = job_lock_key(JobKind.ANALYSIS)
    async with async_engine.connect() as holder:
        assert await holder.scalar(text("SELECT pg_try_advisory_lock(:k)"), {"k": key})
        try:
            with pytest.raises(JobAlreadyRunningError):
                await run_job(
                    kind=JobKind.ANALYSIS, sessionmaker=sessionmaker, triggered_by="t", body=_ok
                )
            # Another kind is not blocked by it.
            other = await run_job(
                kind=JobKind.VERIFICATION, sessionmaker=sessionmaker, triggered_by="t", body=_ok
            )
            assert other.status is RunStatus.SUCCEEDED
        finally:
            await holder.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": key})
    assert await _count_runs(sessionmaker) == 1
    # Released: the same kind runs now, and releases its own lock afterwards.
    await run_job(kind=JobKind.ANALYSIS, sessionmaker=sessionmaker, triggered_by="t", body=_ok)
    await run_job(kind=JobKind.ANALYSIS, sessionmaker=sessionmaker, triggered_by="t", body=_ok)
    assert await _count_runs(sessionmaker) == 3


async def test_a_concurrent_run_of_the_same_kind_is_refused(sessionmaker: SM) -> None:
    started, release = asyncio.Event(), asyncio.Event()

    async def slow(job: JobContext) -> RunStatus:
        started.set()
        await release.wait()
        return RunStatus.SUCCEEDED

    first = asyncio.create_task(
        run_job(kind=JobKind.RECOMMENDATION, sessionmaker=sessionmaker, triggered_by="a", body=slow)
    )
    await started.wait()
    with pytest.raises(JobAlreadyRunningError):
        await run_job(
            kind=JobKind.RECOMMENDATION, sessionmaker=sessionmaker, triggered_by="b", body=_ok
        )
    release.set()
    assert (await first).status is RunStatus.SUCCEEDED


async def test_an_exception_fails_the_run_with_a_safe_reason(sessionmaker: SM) -> None:
    async def boom(job: JobContext) -> RunStatus:
        job.increment("done")
        raise ValueError("postgresql://ken:secret@db/x exploded")

    detail = await run_job(
        kind=JobKind.ANALYTICS_IMPORT, sessionmaker=sessionmaker, triggered_by="t", body=boom
    )
    assert detail.status is RunStatus.FAILED
    assert detail.failure_reason == "job aborted by an unexpected error (ValueError)"
    assert detail.counters == {"done": 1}
    [event] = await _events(sessionmaker)
    assert "secret" not in str(event.payload)
    assert JobCompletedPayload.model_validate(event.payload).status is RunStatus.FAILED
    stored = await SqlJobReader(sessionmaker).get_run(run_id=detail.id)
    assert stored.failure_reason == detail.failure_reason
    assert stored.completed_at is not None


async def test_job_failed_error_carries_its_own_fixed_reason(sessionmaker: SM) -> None:
    async def refuse(job: JobContext) -> RunStatus:
        raise JobFailedError("no evaluation suite configured")

    detail = await run_job(
        kind=JobKind.EVALUATION, sessionmaker=sessionmaker, triggered_by="t", body=refuse
    )
    assert (detail.status, detail.failure_reason) == (
        RunStatus.FAILED,
        "no evaluation suite configured",
    )


async def test_cancellation_marks_the_run_failed_and_propagates(sessionmaker: SM) -> None:
    async def cancelled(job: JobContext) -> RunStatus:
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await run_job(
            kind=JobKind.AUTONOMY_SHADOW,
            sessionmaker=sessionmaker,
            triggered_by="t",
            body=cancelled,
        )
    page = await SqlJobReader(sessionmaker).list_runs(limit=5, offset=0)
    assert [(r.status, r.kind) for r in page.items] == [(RunStatus.FAILED, JobKind.AUTONOMY_SHADOW)]
    detail = await SqlJobReader(sessionmaker).get_run(run_id=page.items[0].id)
    assert detail.failure_reason == FAILURE_INTERRUPTED
    assert len(await _events(sessionmaker)) == 1


async def test_invalid_input_is_refused_before_anything_is_written(sessionmaker: SM) -> None:
    with pytest.raises(ValueError):
        await run_job(kind=JobKind.AGENT, sessionmaker=sessionmaker, triggered_by=" ", body=_ok)
    with pytest.raises(ValueError):
        await run_job(
            kind=JobKind.AGENT,
            sessionmaker=sessionmaker,
            triggered_by="t",
            body=_ok,
            config_snapshot={"bad": float("nan")},
        )
    assert await _count_runs(sessionmaker) == 0


async def test_reader_lists_newest_first_with_filters_and_paging(sessionmaker: SM) -> None:
    clock = TickClock()
    made = []
    for kind in (JobKind.ANALYSIS, JobKind.EVALUATION, JobKind.ANALYSIS):
        made.append(
            await run_job(
                kind=kind, sessionmaker=sessionmaker, triggered_by="t", body=_ok, clock=clock
            )
        )
    reader = SqlJobReader(sessionmaker)
    everything = await reader.list_runs(limit=10, offset=0)
    assert [r.id for r in everything.items] == [m.id for m in reversed(made)]
    assert everything.page.total == 3
    analysis = await reader.list_runs(limit=1, offset=1, kind=JobKind.ANALYSIS)
    assert [r.id for r in analysis.items] == [made[0].id]
    assert (analysis.page.total, analysis.page.has_more) == (2, False)
    assert (await reader.list_runs(limit=5, offset=0, status=RunStatus.FAILED)).page.total == 0
    with pytest.raises(JobRunNotFoundError):
        await reader.get_run(run_id=uuid.uuid4())
    with pytest.raises(ValueError):
        await reader.list_runs(limit=0, offset=0)
