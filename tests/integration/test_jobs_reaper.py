"""``make jobs-reap`` against PostgreSQL (ADR-0014 s5 / s8).

A ``running`` job run whose kind's advisory lock nobody holds is failed with a fixed reason
(and ``job.completed`` is emitted); a kind whose lock IS held -- by another connection, or
by a genuinely live :func:`run_job` -- is untouched; deployments are NEVER touched.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from ken_contracts.deployment import DeploymentStatus
from ken_contracts.inventory import RunStatus
from ken_contracts.jobs import JobKind
from ken_core.db.repositories import deployments as deploy_repo
from ken_core.db.repositories import jobs as repo
from ken_services.jobs import reaper
from ken_services.jobs.runner import (
    JobAlreadyRunningError,
    JobContext,
    _acquire_lock,
    _release_lock,
    job_lock_key,
    run_job,
)
from tests.conftest import make_settings
from tests.integration.conftest import ingest, make_page
from tests.integration.review_support import create_user

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("migrated")]

SM = async_sessionmaker[AsyncSession]
STARTED = datetime(2026, 10, 15, 8, 0, tzinfo=UTC)
NOW = datetime(2026, 10, 15, 9, 0, tzinfo=UTC)


def clock() -> datetime:
    return NOW


async def _running(
    sessionmaker: SM, kind: JobKind, *, counters: dict[str, int] | None = None
) -> uuid.UUID:
    run_id = uuid.uuid4()
    async with sessionmaker() as session, session.begin():
        await repo.insert_job_run(
            session,
            run_id=run_id,
            kind=kind,
            triggered_by="test",
            config_snapshot={},
            started_at=STARTED,
        )
        if counters:
            await repo.update_job_counters(session, run_id=run_id, counters=counters, now=STARTED)
    return run_id


async def _run_row(sessionmaker: SM, run_id: uuid.UUID) -> Any:
    async with sessionmaker() as session:
        return (
            await session.execute(text("SELECT * FROM job_runs WHERE id = :id"), {"id": run_id})
        ).one()


async def _events(sessionmaker: SM, run_id: uuid.UUID) -> list[Any]:
    async with sessionmaker() as session:
        return list(
            (
                await session.execute(
                    text(
                        "SELECT event_name, emitted_by, payload, correlation_id FROM events "
                        "WHERE subject_type = 'job_run' AND subject_id = :s"
                    ),
                    {"s": str(run_id)},
                )
            ).all()
        )


async def _lock_is_free(engine: AsyncEngine, kind: JobKind) -> bool:
    lock = await _acquire_lock(engine, job_lock_key(kind))
    if lock is None:
        return False
    await _release_lock(lock, job_lock_key(kind))
    return True


async def test_a_stale_run_is_failed_with_a_fixed_reason(
    sessionmaker: SM, async_engine: AsyncEngine
) -> None:
    stale = await _running(sessionmaker, JobKind.RECOMMENDATION, counters={"plans_created": 4})
    other = await _running(sessionmaker, JobKind.RECOMMENDATION)

    summary = await reaper.reap_stale_runs(sessionmaker, clock=clock)

    assert summary.kinds_examined == 1
    assert summary.runs_reaped == 2
    assert summary.reaped == {"recommendation": 2}
    assert summary.skipped_live == []
    for run_id in (stale, other):
        row = await _run_row(sessionmaker, run_id)
        assert row.status == RunStatus.FAILED.value
        assert row.failure_reason == reaper.REAPED_REASON
        assert row.completed_at == NOW
        events = await _events(sessionmaker, run_id)
        assert len(events) == 1
        event = events[0]
        assert event.event_name == "job.completed" and event.emitted_by == "ken-jobs"
        assert event.correlation_id == run_id
        assert event.payload["status"] == "failed"
        assert event.payload["kind"] == "recommendation"
    assert (await _run_row(sessionmaker, stale)).counters == {"plans_created": 4}
    assert (await _events(sessionmaker, stale))[0].payload["counters"] == {"plans_created": 4}
    # The lock was released: a later run of the kind can start.
    assert await _lock_is_free(async_engine, JobKind.RECOMMENDATION)
    # A second sweep finds nothing.
    again = await reaper.reap_stale_runs(sessionmaker, clock=clock)
    assert again.kinds_examined == 0 and again.runs_reaped == 0


async def test_completed_at_never_precedes_started_at(sessionmaker: SM) -> None:
    run_id = await _running(sessionmaker, JobKind.ANALYSIS)
    early = STARTED - timedelta(hours=1)
    await reaper.reap_stale_runs(sessionmaker, clock=lambda: early)
    row = await _run_row(sessionmaker, run_id)
    assert row.status == "failed" and row.completed_at == STARTED


async def test_a_kind_whose_lock_is_held_is_untouched(
    sessionmaker: SM, async_engine: AsyncEngine
) -> None:
    held = await _running(sessionmaker, JobKind.AGENT)
    stale = await _running(sessionmaker, JobKind.ANALYSIS)
    key = job_lock_key(JobKind.AGENT)
    lock = await _acquire_lock(async_engine, key)  # another connection: a "live" runner
    assert lock is not None
    try:
        summary = await reaper.reap_stale_runs(sessionmaker, clock=clock)
    finally:
        await _release_lock(lock, key)

    assert summary.skipped_live == ["agent"]
    assert summary.reaped == {"analysis": 1}
    row = await _run_row(sessionmaker, held)
    assert row.status == "running" and row.completed_at is None and row.failure_reason is None
    assert await _events(sessionmaker, held) == []
    assert (await _run_row(sessionmaker, stale)).status == "failed"


async def test_a_live_run_job_is_never_reaped(sessionmaker: SM) -> None:
    started = asyncio.Event()
    release = asyncio.Event()
    seen: dict[str, uuid.UUID] = {}

    async def body(job: JobContext) -> RunStatus:
        seen["run_id"] = job.run_id
        started.set()
        await release.wait()
        return RunStatus.SUCCEEDED

    task = asyncio.create_task(
        run_job(kind=JobKind.EVALUATION, sessionmaker=sessionmaker, triggered_by="t", body=body)
    )
    await asyncio.wait_for(started.wait(), timeout=10)
    try:
        summary = await reaper.reap_stale_runs(sessionmaker, clock=clock)
        assert summary.skipped_live == ["evaluation"] and summary.runs_reaped == 0
        assert (await _run_row(sessionmaker, seen["run_id"])).status == "running"
    finally:
        release.set()
    detail = await asyncio.wait_for(task, timeout=10)
    assert detail.status is RunStatus.SUCCEEDED


async def test_a_run_cannot_start_while_the_reaper_holds_the_lock(
    sessionmaker: SM, async_engine: AsyncEngine
) -> None:
    """The reaper's lock is the runner's lock: a run starting mid-reap is refused, never
    double-booked (and no row is written for it)."""
    key = job_lock_key(JobKind.VERIFICATION)
    lock = await _acquire_lock(async_engine, key)  # as the reaper holds it while failing rows
    assert lock is not None
    try:

        async def body(job: JobContext) -> RunStatus:  # pragma: no cover - never runs
            return RunStatus.SUCCEEDED

        with pytest.raises(JobAlreadyRunningError):
            await run_job(
                kind=JobKind.VERIFICATION, sessionmaker=sessionmaker, triggered_by="t", body=body
            )
    finally:
        await _release_lock(lock, key)
    async with sessionmaker() as session:
        assert await session.scalar(text("SELECT count(*) FROM job_runs")) == 0


async def _insert_applying_deployment(sessionmaker: SM, tmp_user: uuid.UUID) -> uuid.UUID:
    await ingest(sessionmaker, make_page("/deployed"))
    async with sessionmaker() as session:
        page_id = await session.scalar(text("SELECT id FROM pages"))
    deployment_id = uuid.uuid4()
    old = datetime(2026, 10, 1, tzinfo=UTC)  # long stuck: a reaper by age would pick it
    async with sessionmaker() as session, session.begin():
        await deploy_repo.insert_deployment(
            session,
            id=deployment_id,
            operation_id=uuid.uuid4(),
            request_fingerprint="f" * 64,
            request_json=json.dumps({"stored": "request"}),
            cms_provider="mock",
            cms_page_id="cms-1",
            source_page_id=page_id,
            source_page_version=1,
            status=DeploymentStatus.APPLYING.value,
            actor="user:alice",
            user_id=tmp_user,
            note="",
            created_at=old,
            updated_at=old,
        )
    return deployment_id


async def test_deployments_are_never_touched(sessionmaker: SM) -> None:
    user_id = await create_user(sessionmaker, "operator")
    deployment_id = await _insert_applying_deployment(sessionmaker, user_id)
    await _running(sessionmaker, JobKind.VERIFICATION)  # the Phase 6 job kind
    await _running(sessionmaker, JobKind.ANALYSIS)

    async def snapshot() -> tuple[Any, ...]:
        async with sessionmaker() as session:
            row = (
                await session.execute(
                    text("SELECT to_jsonb(d) AS j FROM deployments d WHERE id = :id"),
                    {"id": deployment_id},
                )
            ).one()
            items = await session.scalar(text("SELECT count(*) FROM deployment_items"))
            verifications = await session.scalar(
                text("SELECT count(*) FROM deployment_verifications")
            )
            events = await session.scalar(
                text("SELECT count(*) FROM events WHERE subject_type <> 'job_run'")
            )
        return row.j, items, verifications, events

    before = await snapshot()
    summary = await reaper.reap_stale_runs(sessionmaker, clock=clock)
    assert summary.runs_reaped == 2
    after = await snapshot()
    assert after == before
    assert after[0]["status"] == "applying"


async def test_cli_reaps_through_the_real_database(
    sessionmaker: SM,
    test_database_url: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    run_id = await _running(sessionmaker, JobKind.ANALYTICS_IMPORT)
    settings = make_settings(database_url=test_database_url)
    monkeypatch.setattr(reaper, "get_settings", lambda: settings)

    exit_code = await asyncio.to_thread(reaper.main, [])

    assert exit_code == reaper.EXIT_OK
    out = capsys.readouterr()
    assert json.loads(out.out.strip().splitlines()[-1]) == {
        "command": "jobs-reap",
        "kinds_examined": 1,
        "runs_reaped": 1,
        "reaped": {"analytics_import": 1},
        "skipped_live": [],
    }
    assert "postgresql" not in out.out + out.err
    assert (await _run_row(sessionmaker, run_id)).status == "failed"
