"""Job reaper: ``python -m ken_services.jobs.reaper`` (``make jobs-reap``, ADR-0014 s5 / s8).

A run whose process died (OOM, ``kill -9``, a host reboot) stays ``running`` forever: its
``finally`` never executed. The ONLY authority on whether a run is alive is the per-kind
session-level advisory lock that :func:`~ken_services.jobs.runner.run_job` holds on a
dedicated connection for the whole run (PostgreSQL releases it when that connection dies).
So, for each kind that has ``running`` rows:

1. ``pg_try_advisory_lock(job_lock_key(kind))`` on a DEDICATED connection (the runner's
   own helpers, so the key and the connection handling are identical);
2. lock NOT acquired -> a live run of that kind exists: the kind is skipped, nothing is
   written;
3. lock acquired -> no live runner can exist (a runner inserts its row only after taking
   this lock), so every ``running`` row of that kind is stale: each is finished as
   ``failed`` with the fixed reason :data:`REAPED_REASON` through the job-run repository,
   and ``job.completed`` is emitted in the SAME transaction (the runner's guarantee that
   every finished run emits it, failed ones included);
4. the lock is ALWAYS released in ``finally``; a connection whose unlock failed is
   invalidated, never returned to the pool still holding the lock.

While the reaper holds a kind's lock, a run of that kind trying to start gets
:class:`~ken_services.jobs.runner.JobAlreadyRunningError` (nothing written), exactly as if
another run were in progress; the window is one short transaction.

The reaper touches ``job_runs`` and ``events`` ONLY. Deployments and deployment items are
never reaped: an ``applying`` deployment is RECONCILED (``make deployments-reconcile``),
because its CMS outcome is unknown, not failed (ADR-0014 s8).

Prints one JSON line. Exit codes: 0 swept (kinds with a live run are skipped, not errors);
1 the sweep could not complete; 2 usage or configuration error. Never prints the database
URL or exception text.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Final

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ken_contracts.events import EventEnvelope, EventName
from ken_contracts.inventory import RunStatus
from ken_contracts.jobs import SUBJECT_JOB_RUN, JobCompletedPayload, JobKind
from ken_core import logging as ken_logging
from ken_core.config import ConfigurationError, Settings, get_settings
from ken_core.db.engine import create_engine
from ken_core.db.models.intelligence import JobRun
from ken_core.db.repositories import jobs as repo
from ken_core.db.repositories.events import append_event
from ken_core.db.session import create_sessionmaker
from ken_core.logging import get_logger
from ken_services.jobs.runner import (
    JOBS_EMITTED_BY,
    _acquire_lock,
    _engine_of,
    _release_lock,
    job_lock_key,
    utc_now,
)

__all__ = ["REAPED_REASON", "ReapSummary", "main", "reap_stale_runs", "summary_line"]

logger = get_logger("ken_services.jobs.reaper")

REAPED_REASON: Final = "reaped: no live runner held the lock"
"""``failure_reason`` of every reaped run (fixed text: never exception text or a URL)."""

EXIT_OK: Final = 0
EXIT_RUN_ERRORS: Final = 1
EXIT_USAGE: Final = 2


@dataclass(slots=True)
class ReapSummary:
    kinds_examined: int = 0
    runs_reaped: int = 0
    reaped: dict[str, int] = field(default_factory=dict)
    """kind -> number of runs marked failed."""
    skipped_live: list[str] = field(default_factory=list)
    """Kinds whose lock is held by a live runner (left untouched)."""


async def _running_kinds(sessionmaker: async_sessionmaker[AsyncSession]) -> list[JobKind]:
    async with sessionmaker() as session:
        rows = await session.scalars(
            select(JobRun.kind).where(JobRun.status == RunStatus.RUNNING.value).distinct()
        )
        return sorted(JobKind(value) for value in rows.all())


async def _reap_kind(
    sessionmaker: async_sessionmaker[AsyncSession],
    kind: JobKind,
    clock: Callable[[], datetime],
) -> int:
    """Fail every ``running`` row of ``kind``. The caller HOLDS the kind's lock."""
    reaped = 0
    async with sessionmaker() as session, session.begin():
        rows = (
            await session.scalars(
                select(JobRun)
                .where(JobRun.kind == kind.value, JobRun.status == RunStatus.RUNNING.value)
                .order_by(JobRun.started_at, JobRun.id)
                .with_for_update()
            )
        ).all()
        for row in rows:
            completed_at = max(clock(), row.started_at)
            counters = {str(k): int(v) for k, v in sorted((row.counters or {}).items())}
            await repo.finish_job_run(
                session,
                run_id=row.id,
                status=RunStatus.FAILED,
                counters=counters,
                completed_at=completed_at,
                failure_reason=REAPED_REASON,
            )
            payload = JobCompletedPayload(
                run_id=row.id,
                kind=kind,
                status=RunStatus.FAILED,
                started_at=row.started_at,
                completed_at=completed_at,
                counters=counters,
            )
            await append_event(
                session,
                EventEnvelope(
                    event_name=EventName.JOB_COMPLETED,
                    occurred_at=completed_at,
                    emitted_by=JOBS_EMITTED_BY,
                    subject_type=SUBJECT_JOB_RUN,
                    subject_id=str(row.id),
                    payload=payload.model_dump(mode="json"),
                    correlation_id=row.id,
                ),
            )
            reaped += 1
            logger.warning("job_run_reaped", run_id=str(row.id), kind=kind.value)
    return reaped


async def reap_stale_runs(
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    clock: Callable[[], datetime] = utc_now,
) -> ReapSummary:
    """Fail the ``running`` job runs whose kind's advisory lock no live runner holds.

    See the module docstring. Touches ``job_runs`` and ``events`` only."""
    engine = _engine_of(sessionmaker)
    summary = ReapSummary()
    for kind in await _running_kinds(sessionmaker):
        summary.kinds_examined += 1
        key = job_lock_key(kind)
        lock = await _acquire_lock(engine, key)
        if lock is None:
            summary.skipped_live.append(kind.value)
            logger.info("job_reap_skipped_live", kind=kind.value)
            continue
        try:
            count = await _reap_kind(sessionmaker, kind, clock)
        finally:
            await _release_lock(lock, key)
        if count:
            summary.reaped[kind.value] = count
            summary.runs_reaped += count
    logger.info(
        "job_reap_finished",
        kinds_examined=summary.kinds_examined,
        runs_reaped=summary.runs_reaped,
        skipped_live=len(summary.skipped_live),
    )
    return summary


# --------------------------------------------------------------------------- #
# Command line
# --------------------------------------------------------------------------- #
def summary_line(summary: ReapSummary) -> str:
    return json.dumps(
        {
            "command": "jobs-reap",
            "kinds_examined": summary.kinds_examined,
            "runs_reaped": summary.runs_reaped,
            "reaped": dict(sorted(summary.reaped.items())),
            "skipped_live": sorted(summary.skipped_live),
        },
        sort_keys=True,
    )


def _parser() -> argparse.ArgumentParser:
    return argparse.ArgumentParser(
        prog="python -m ken_services.jobs.reaper",
        description=(
            "Fail 'running' job runs whose kind's advisory lock no live runner holds. "
            "Never touches deployments (those are reconciled)."
        ),
    )


async def _run(settings: Settings) -> int:
    engine = create_engine(settings)  # no I/O until first use
    try:
        summary = await reap_stale_runs(create_sessionmaker(engine))
    except Exception as exc:
        print(f"the job reaper could not complete ({type(exc).__name__})", file=sys.stderr)
        return EXIT_RUN_ERRORS
    finally:
        await engine.dispose()
    print(summary_line(summary))
    return EXIT_OK


def main(argv: Sequence[str] | None = None) -> int:
    try:
        _parser().parse_args(argv)
    except SystemExit as exc:  # argparse has already printed its usage message
        return EXIT_USAGE if exc.code not in (0, None) else EXIT_OK
    try:
        settings = get_settings()
    except (ValidationError, ConfigurationError):
        print("configuration error: invalid KEN_* settings", file=sys.stderr)
        return EXIT_USAGE
    ken_logging.configure_logging(level=settings.log_level, fmt=settings.log_format)
    return asyncio.run(_run(settings))


if __name__ == "__main__":
    raise SystemExit(main())
