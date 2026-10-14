"""Generic job-run lifecycle (ADR-0010 section 6). Reusable by every phase from 2 on.

Every batch job (analysis, recommendation, agent batches, verification,
analytics import, evaluation, autonomy shadow) runs through :func:`run_job`::

    async def body(job: JobContext) -> RunStatus:
        async with job.transaction() as tx:      # one unit of work
            await do_something(tx.session)
            tx.increment("things_done")           # counted only if it commits
        job.increment("errors")                   # or count outside a transaction
        return RunStatus.SUCCEEDED

    detail = await run_job(
        kind=JobKind.ANALYSIS, sessionmaker=sm, triggered_by="cli", body=body,
        config_snapshot={...}, counter_names=("things_done", "errors"),
    )

Guarantees:

* **One run per kind at a time.** A session-level ``pg_try_advisory_lock`` is
  taken on a DEDICATED connection with a per-kind key (:func:`job_lock_key`:
  the first 8 bytes of sha256("ken.jobs." + kind), signed -- the same
  derivation as Phase 1's ingest lock). If it is held,
  :class:`JobAlreadyRunningError` is raised and NO run row is written.
* **Counters never diverge.** :meth:`JobContext.transaction` persists the run
  counters in the same transaction as the work; the in-memory counters change
  only if it commits. :meth:`JobContext.increment` outside a transaction is
  persisted by the next transaction, :meth:`JobContext.flush` or the finish.
* **A failure never leaks text.** ``body`` raising an exception gives a
  ``failed`` run whose ``failure_reason`` is fixed text plus the exception
  CLASS name; the run is returned, not raised. :class:`JobFailedError` lets a
  body fail with its own fixed, safe reason. Cancellation / KeyboardInterrupt
  marks the run failed ("job interrupted") and is re-raised.
* **Every finished run emits ``job.completed``** (``JobCompletedPayload``,
  ``subject_type="job_run"``, ``subject_id=str(run_id)``,
  ``emitted_by="ken-jobs"``, ``correlation_id=run_id``) in the SAME transaction
  that marks it finished -- including failed runs.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Final

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, AsyncSession, async_sessionmaker

from ken_contracts.events import EventEnvelope, EventName
from ken_contracts.inventory import RunStatus
from ken_contracts.jobs import SUBJECT_JOB_RUN, JobCompletedPayload, JobKind, JobRunDetail
from ken_core.db.repositories import jobs as repo
from ken_core.db.repositories.events import append_event
from ken_core.logging import get_logger

__all__ = [
    "FAILURE_INTERRUPTED",
    "FAILURE_UNEXPECTED",
    "JOBS_EMITTED_BY",
    "JobAlreadyRunningError",
    "JobContext",
    "JobFailedError",
    "JobTransaction",
    "job_lock_key",
    "run_job",
    "utc_now",
]

logger = get_logger("ken_services.jobs.runner")

JOBS_EMITTED_BY: Final = "ken-jobs"
"""``emitted_by`` of every ``job.completed`` event."""
JOB_LOCK_PREFIX: Final = "ken.jobs."

FAILURE_UNEXPECTED: Final = "job aborted by an unexpected error"
FAILURE_INTERRUPTED: Final = "job interrupted"
FAILURE_REPORTED: Final = "job reported failure"

_FINISHED: Final = frozenset(
    {RunStatus.SUCCEEDED, RunStatus.COMPLETED_WITH_ERRORS, RunStatus.FAILED}
)


def utc_now() -> datetime:
    return datetime.now(UTC)


def job_lock_key(kind: JobKind) -> int:
    """``pg_advisory_lock`` key of ``kind``: first 8 bytes of sha256("ken.jobs." + kind), signed."""
    digest = hashlib.sha256((JOB_LOCK_PREFIX + JobKind(kind).value).encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big", signed=True)


class JobAlreadyRunningError(RuntimeError):
    """Another run of the same kind holds the advisory lock. No run row was created."""


class JobFailedError(Exception):
    """Raised by a job body to fail the run with its own reason.

    ``reason`` is stored and returned verbatim, so it MUST be fixed text (never
    exception text, a URL or a secret).
    """

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(slots=True)
class JobTransaction:
    """One unit of work: a session in a transaction and the TENTATIVE counters."""

    session: AsyncSession
    counters: dict[str, int] = field(default_factory=dict)

    def increment(self, name: str, by: int = 1) -> None:
        self.counters[name] = self.counters.get(name, 0) + by


class JobContext:
    """The running job, handed to the body. Create runs with :func:`run_job` only."""

    def __init__(
        self,
        *,
        kind: JobKind,
        sessionmaker: async_sessionmaker[AsyncSession],
        triggered_by: str,
        config_snapshot: dict[str, Any],
        counter_names: Iterable[str],
        clock: Callable[[], datetime],
    ) -> None:
        self.run_id = uuid.uuid4()
        self.kind = JobKind(kind)
        self.sessionmaker = sessionmaker
        self.triggered_by = triggered_by
        self.config_snapshot = config_snapshot
        self.clock = clock
        self.started_at = clock()
        self._counters: dict[str, int] = dict.fromkeys(counter_names, 0)

    @property
    def counters(self) -> dict[str, int]:
        """A copy of the committed (or pending, after :meth:`increment`) counters."""
        return dict(self._counters)

    def increment(self, name: str, by: int = 1) -> None:
        """Count outside a transaction; persisted by the next write of the run."""
        self._counters[name] = self._counters.get(name, 0) + by

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[JobTransaction]:
        """A transaction that also persists the counters; they change only if it commits."""
        tentative = dict(self._counters)
        async with self.sessionmaker() as session, session.begin():
            tx = JobTransaction(session=session, counters=tentative)
            yield tx
            await repo.update_job_counters(
                session, run_id=self.run_id, counters=tx.counters, now=self.clock()
            )
        self._counters = tx.counters

    async def flush(self) -> None:
        """Persist the counters now (e.g. after :meth:`increment` on a failure path)."""
        async with self.transaction():
            pass

    # ----------------------------------------------------------- lifecycle
    async def _start(self) -> None:
        async with self.sessionmaker() as session, session.begin():
            await repo.insert_job_run(
                session,
                run_id=self.run_id,
                kind=self.kind,
                triggered_by=self.triggered_by,
                config_snapshot=self.config_snapshot,
                started_at=self.started_at,
            )
            await repo.update_job_counters(
                session, run_id=self.run_id, counters=self._counters, now=self.started_at
            )
        logger.info("job_run_started", run_id=str(self.run_id), kind=self.kind.value)

    async def _finish(self, status: RunStatus, failure_reason: str | None) -> JobRunDetail:
        completed_at = max(self.clock(), self.started_at)
        counters = dict(sorted(self._counters.items()))
        async with self.sessionmaker() as session, session.begin():
            await repo.finish_job_run(
                session,
                run_id=self.run_id,
                status=status,
                counters=counters,
                completed_at=completed_at,
                failure_reason=failure_reason,
            )
            payload = JobCompletedPayload(
                run_id=self.run_id,
                kind=self.kind,
                status=status,
                started_at=self.started_at,
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
                    subject_id=str(self.run_id),
                    payload=payload.model_dump(mode="json"),
                    correlation_id=self.run_id,
                ),
            )
        logger.info(
            "job_run_finished",
            run_id=str(self.run_id),
            kind=self.kind.value,
            status=status.value,
            **counters,
        )
        return JobRunDetail(
            id=self.run_id,
            kind=self.kind,
            status=status,
            triggered_by=self.triggered_by,
            started_at=self.started_at,
            completed_at=completed_at,
            counters=counters,
            config_snapshot=self.config_snapshot,
            failure_reason=failure_reason,
        )


# --------------------------------------------------------------------------- #
# Advisory lock (dedicated connection, session level)
# --------------------------------------------------------------------------- #
def _engine_of(sessionmaker: async_sessionmaker[AsyncSession]) -> AsyncEngine:
    bind = sessionmaker.kw.get("bind")
    if not isinstance(bind, AsyncEngine):
        raise TypeError("the sessionmaker must be bound to an AsyncEngine")
    return bind


async def _acquire_lock(engine: AsyncEngine, key: int) -> AsyncConnection | None:
    connection = await engine.connect()
    try:
        acquired = await connection.scalar(text("SELECT pg_try_advisory_lock(:key)"), {"key": key})
        # Session-level: it outlives this transaction, which is ended so the
        # connection does not sit "idle in transaction" for the whole run.
        await connection.commit()
    except BaseException:
        await connection.close()
        raise
    if not acquired:
        await connection.close()
        return None
    return connection


async def _release_lock(connection: AsyncConnection, key: int) -> None:
    try:
        await connection.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": key})
        await connection.commit()
    except Exception as exc:
        # Never return a connection that may still hold the lock to the pool.
        logger.warning("job_lock_release_failed", error_class=type(exc).__name__)
        await connection.invalidate()
    finally:
        await connection.close()


def _json_object(snapshot: Mapping[str, Any] | None) -> dict[str, Any]:
    try:
        result: Any = json.loads(json.dumps(dict(snapshot or {}), allow_nan=False))
    except (TypeError, ValueError) as exc:
        raise ValueError("config_snapshot is not JSON-serialisable") from exc
    if not isinstance(result, dict):  # pragma: no cover - dict() above guarantees it
        raise ValueError("config_snapshot must be a JSON object")
    return result


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #
async def run_job(
    *,
    kind: JobKind,
    sessionmaker: async_sessionmaker[AsyncSession],
    triggered_by: str,
    body: Callable[[JobContext], Awaitable[RunStatus]],
    config_snapshot: Mapping[str, Any] | None = None,
    counter_names: Iterable[str] = (),
    clock: Callable[[], datetime] = utc_now,
) -> JobRunDetail:
    """Run ``body`` as one recorded job run of ``kind`` and return the finished run.

    ``body`` returns the final status (``succeeded`` / ``completed_with_errors``;
    ``failed`` is recorded with a generic reason). ``config_snapshot`` must be
    JSON-serialisable and SECRET-FREE: it is persisted and served by the API.

    Raises:
        JobAlreadyRunningError: another run of ``kind`` holds the lock (no row written).
        ValueError: ``triggered_by`` is empty or the snapshot is not JSON.
    """
    if not triggered_by.strip():
        raise ValueError("triggered_by must not be empty")
    snapshot = _json_object(config_snapshot)
    kind = JobKind(kind)
    key = job_lock_key(kind)
    lock = await _acquire_lock(_engine_of(sessionmaker), key)
    if lock is None:
        logger.warning("job_already_running", kind=kind.value)
        raise JobAlreadyRunningError(f"another {kind.value} run is in progress")
    try:
        job = JobContext(
            kind=kind,
            sessionmaker=sessionmaker,
            triggered_by=triggered_by,
            config_snapshot=snapshot,
            counter_names=counter_names,
            clock=clock,
        )
        await job._start()
        return await _drive(job, body)
    finally:
        await _release_lock(lock, key)


async def _drive(
    job: JobContext, body: Callable[[JobContext], Awaitable[RunStatus]]
) -> JobRunDetail:
    try:
        status = RunStatus(await body(job))
        if status not in _FINISHED:
            raise ValueError("a job body must return a finished status")
        return await job._finish(status, FAILURE_REPORTED if status is RunStatus.FAILED else None)
    except JobFailedError as exc:
        logger.error("job_run_failed", run_id=str(job.run_id), kind=job.kind.value)
        return await job._finish(RunStatus.FAILED, exc.reason)
    except (asyncio.CancelledError, KeyboardInterrupt):
        try:
            await job._finish(RunStatus.FAILED, FAILURE_INTERRUPTED)
        except Exception as exc:
            logger.error("job_run_not_finalised", error_class=type(exc).__name__)
        raise
    except Exception as exc:
        logger.error(
            "job_run_failed",
            run_id=str(job.run_id),
            kind=job.kind.value,
            error_class=type(exc).__name__,
        )
        return await job._finish(RunStatus.FAILED, f"{FAILURE_UNEXPECTED} ({type(exc).__name__})")
