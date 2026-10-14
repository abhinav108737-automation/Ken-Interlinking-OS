"""Generic job-run repository (``job_runs``, ADR-0010 section 6).

The caller owns the transaction: nothing here commits, so a run's counters are
persisted atomically with the work they describe, and the finishing UPDATE is
atomic with its ``job.completed`` event (``ken_services.jobs.runner``).
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from datetime import datetime
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from ken_contracts.inventory import RunStatus
from ken_contracts.jobs import JobKind, JobRunDetail, JobRunSummary
from ken_core.db.models.intelligence import JobRun


async def insert_job_run(
    session: AsyncSession,
    *,
    run_id: uuid.UUID,
    kind: JobKind,
    triggered_by: str,
    config_snapshot: Mapping[str, Any],
    started_at: datetime,
) -> None:
    await session.execute(
        insert(JobRun).values(
            id=run_id,
            kind=JobKind(kind).value,
            status=RunStatus.RUNNING.value,
            triggered_by=triggered_by,
            started_at=started_at,
            counters={},
            config_snapshot=dict(config_snapshot),
            created_at=started_at,
            updated_at=started_at,
        )
    )


async def update_job_counters(
    session: AsyncSession, *, run_id: uuid.UUID, counters: Mapping[str, int], now: datetime
) -> None:
    await session.execute(
        update(JobRun).where(JobRun.id == run_id).values(counters=dict(counters), updated_at=now)
    )


async def finish_job_run(
    session: AsyncSession,
    *,
    run_id: uuid.UUID,
    status: RunStatus,
    counters: Mapping[str, int],
    completed_at: datetime,
    failure_reason: str | None,
) -> None:
    if status is RunStatus.RUNNING:
        raise ValueError("a finished run cannot be 'running'")
    await session.execute(
        update(JobRun)
        .where(JobRun.id == run_id)
        .values(
            status=status.value,
            counters=dict(counters),
            completed_at=completed_at,
            failure_reason=failure_reason,
            updated_at=completed_at,
        )
    )


# --------------------------------------------------------------------------- #
# Reads
# --------------------------------------------------------------------------- #
def _counters(raw: Mapping[str, Any]) -> dict[str, int]:
    return {str(key): int(value) for key, value in sorted(raw.items())}


def to_summary(row: JobRun) -> JobRunSummary:
    return JobRunSummary(
        id=row.id,
        kind=JobKind(row.kind),
        status=RunStatus(row.status),
        triggered_by=row.triggered_by,
        started_at=row.started_at,
        completed_at=row.completed_at,
        counters=_counters(row.counters),
    )


def to_detail(row: JobRun) -> JobRunDetail:
    return JobRunDetail(
        **to_summary(row).model_dump(),
        config_snapshot=dict(row.config_snapshot),
        failure_reason=row.failure_reason,
    )


async def get_job_run(session: AsyncSession, run_id: uuid.UUID) -> JobRun | None:
    return await session.get(JobRun, run_id)


async def list_job_runs(
    session: AsyncSession,
    *,
    limit: int,
    offset: int,
    kind: JobKind | None = None,
    status: RunStatus | None = None,
) -> tuple[list[JobRun], int]:
    """Newest first (``started_at`` DESC, then id for a total order) and the total count."""
    conditions = []
    if kind is not None:
        conditions.append(JobRun.kind == JobKind(kind).value)
    if status is not None:
        conditions.append(JobRun.status == RunStatus(status).value)
    total = int(
        await session.scalar(select(func.count()).select_from(JobRun).where(*conditions)) or 0
    )
    rows = (
        await session.scalars(
            select(JobRun)
            .where(*conditions)
            .order_by(JobRun.started_at.desc(), JobRun.id.desc())
            .limit(limit)
            .offset(offset)
        )
    ).all()
    return list(rows), total
