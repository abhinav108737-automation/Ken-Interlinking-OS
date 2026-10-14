"""The database implementation of :class:`~ken_services.agents.protocol.AgentRunStore`.

Run and step persistence for the runner (never handed to a node). Each method is its own
short transaction.

* ``sources_to_review`` -- sources with >= 1 unreviewed ``proposed`` plan (no
  ``agent_run_plans`` row) and no ``running`` agent run, by canonical URL then id.
* ``reap_orphans`` -- ``running`` runs whose ``job_runs`` batch is no longer ``running`` (the
  process died) become ``failed`` / ``orphaned``, with ``finished_at`` set. Called at each
  batch start; returns how many were reaped.
* ``start_run`` -- inserts the ``running`` row. A second running run for the same source
  raises :class:`AgentRunAlreadyRunningError` (partial unique index); a duplicate
  ``correlation_id`` or run id raises :class:`~sqlalchemy.exc.IntegrityError`.
* ``append_step`` -- append-only. The step must belong to a ``running`` run and carry its
  ``trace_id``; ``output`` and ``tool_calls`` must each serialise to at most
  ``MAX_STEP_OUTPUT_BYTES`` of JSON (as PostgreSQL renders ``jsonb::text``; the DB CHECK is
  the backstop) -- :class:`ValueError` otherwise, and nothing is written.
* ``finish_run`` -- exactly once: a finished (or unknown) run raises
  :class:`AgentRunStateError`. The status/outcome combination is validated here first so a
  bad call fails with a clear message (the DB CHECKs are the backstop).
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Sequence
from datetime import datetime
from typing import Any, Final

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ken_contracts.agents import (
    MAX_REVIEW_NOTE_CHARS,
    MAX_REVIEW_NOTES,
    MAX_STEP_OUTPUT_BYTES,
    AgentErrorCode,
    AgentOutcome,
    AgentRunStatus,
    AgentStepView,
    AgentTrigger,
    TriageDecision,
)
from ken_core.db.repositories import agents as repo
from ken_services.agents.protocol import NewAgentRun

__all__ = [
    "AgentRunAlreadyRunningError",
    "AgentRunStateError",
    "DatabaseAgentRunStore",
    "json_text_bytes",
]

MAX_SOURCES: Final = 10_000
_RUNNING_INDEX: Final = "uq_agent_runs_running_source"
_VALID_FINISH: Final = {
    AgentOutcome.RECOMMEND: frozenset({AgentRunStatus.AWAITING_APPROVAL}),
    AgentOutcome.NEEDS_HUMAN_REVIEW: frozenset(
        {AgentRunStatus.AWAITING_APPROVAL, AgentRunStatus.COMPLETED}
    ),
    AgentOutcome.NO_ACTION: frozenset({AgentRunStatus.COMPLETED}),
    AgentOutcome.FAILED: frozenset({AgentRunStatus.FAILED}),
}


class AgentRunAlreadyRunningError(RuntimeError):
    """The source page already has a ``running`` agent run. Nothing was written."""


class AgentRunStateError(RuntimeError):
    """The run does not exist, or has already finished (a finished run is frozen)."""


def json_text_bytes(value: Any) -> int:
    """UTF-8 size of ``value`` rendered like PostgreSQL's ``jsonb::text`` (``", "`` / ``": "``
    separators, non-ASCII unescaped). Key order does not change the size."""
    return len(
        json.dumps(value, ensure_ascii=False, separators=(", ", ": "), allow_nan=False).encode()
    )


def _constraint(exc: IntegrityError) -> str | None:
    """The violated constraint's name, from the driver error (never its message text)."""
    origin: Any = getattr(exc, "orig", None)
    for candidate in (origin, getattr(origin, "__cause__", None)):
        name = getattr(candidate, "constraint_name", None)
        if isinstance(name, str):
            return name
    return None


class DatabaseAgentRunStore:
    """``AgentRunStore`` over PostgreSQL (module docstring)."""

    def __init__(self, *, sessionmaker: async_sessionmaker[AsyncSession]) -> None:
        self._sessionmaker = sessionmaker

    async def sources_to_review(self, *, limit: int) -> list[uuid.UUID]:
        if not 1 <= limit <= MAX_SOURCES:
            raise ValueError(f"limit must be between 1 and {MAX_SOURCES}")
        async with self._sessionmaker() as session:
            return await repo.sources_to_review(session, limit=limit)

    async def reap_orphans(self, *, now: datetime) -> int:
        async with self._sessionmaker() as session, session.begin():
            return await repo.reap_orphans(session, now=now)

    async def start_run(self, *, run: NewAgentRun) -> None:
        try:
            async with self._sessionmaker() as session, session.begin():
                await repo.insert_run(
                    session,
                    run_id=run.id,
                    job_run_id=run.job_run_id,
                    source_page_id=run.source_page_id,
                    source_url=run.source_url,
                    trigger=AgentTrigger(run.trigger),
                    correlation_id=run.correlation_id,
                    graph_version=run.graph_version,
                    config_snapshot=dict(run.config_snapshot),
                    started_at=run.started_at,
                )
        except IntegrityError as exc:
            if _constraint(exc) == _RUNNING_INDEX:
                raise AgentRunAlreadyRunningError(
                    "the source page already has a running agent run"
                ) from exc
            raise

    async def append_step(self, *, step: AgentStepView) -> None:
        step = AgentStepView.model_validate(step.model_dump())  # re-validate at the boundary
        dumped = step.model_dump(mode="json", include={"output", "tool_calls"})
        for name in ("output", "tool_calls"):
            if json_text_bytes(dumped[name]) > MAX_STEP_OUTPUT_BYTES:
                raise ValueError(f"step {name} exceeds {MAX_STEP_OUTPUT_BYTES} bytes of JSON")
        async with self._sessionmaker() as session, session.begin():
            run = await repo.get_run(session, run_id=step.run_id, lock=True)
            if run is None:
                raise AgentRunStateError("unknown agent run")
            if run.status != AgentRunStatus.RUNNING.value:
                raise AgentRunStateError("the agent run has finished: its steps are frozen")
            if step.trace_id != run.trace_id:
                raise ValueError("the step's trace_id must be its run's trace_id")
            await repo.insert_step(session, step=step)

    async def finish_run(
        self,
        *,
        run_id: uuid.UUID,
        status: AgentRunStatus,
        outcome: AgentOutcome,
        finished_at: datetime,
        triage_decision: TriageDecision | None = None,
        source_page_version: int | None = None,
        review_notes: Sequence[str] = (),
        error_code: AgentErrorCode | None = None,
    ) -> None:
        status = AgentRunStatus(status)
        outcome = AgentOutcome(outcome)
        if status is AgentRunStatus.RUNNING or status not in _VALID_FINISH[outcome]:
            raise ValueError(f"status {status.value} is not valid for outcome {outcome.value}")
        if status is AgentRunStatus.FAILED and error_code is None:
            raise ValueError("a failed run needs an error code")
        notes = [str(n) for n in review_notes]
        if len(notes) > MAX_REVIEW_NOTES:
            raise ValueError(f"at most {MAX_REVIEW_NOTES} review notes")
        if any(len(n) > MAX_REVIEW_NOTE_CHARS for n in notes):
            raise ValueError(f"a review note is at most {MAX_REVIEW_NOTE_CHARS} characters")
        async with self._sessionmaker() as session, session.begin():
            finished = await repo.finish_run(
                session,
                run_id=run_id,
                status=status,
                outcome=outcome,
                finished_at=finished_at,
                triage_decision=None
                if triage_decision is None
                else TriageDecision(triage_decision),
                source_page_version=source_page_version,
                review_notes=notes,
                error_code=None if error_code is None else AgentErrorCode(error_code),
            )
            if not finished:
                raise AgentRunStateError("unknown agent run, or it has already finished")
