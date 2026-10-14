"""Agent repository: SQL primitives for agent runs, steps and plan reviews (ADR-0012).

The caller owns the transaction: nothing here commits. Migration 0005's triggers make
``agent_steps`` and ``agent_run_plans`` append-only and freeze a finished ``agent_runs`` row,
so there is deliberately no function that edits a step or a review, deletes anything, or
updates a run outside :data:`~ken_core.db.models.agents.AGENT_RUN_MUTABLE_COLUMNS`.

Rows are re-validated through the frozen ``ken_contracts.agents`` views on the way out.
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from collections.abc import Callable, Iterable, Mapping, Sequence
from datetime import datetime
from typing import Any, Final

from sqlalchemy import ColumnElement, and_, exists, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ken_contracts.agents import (
    AgentErrorCode,
    AgentNode,
    AgentOutcome,
    AgentPlanReview,
    AgentRunStatus,
    AgentRunSummary,
    AgentStepView,
    AgentTrigger,
    CriticReason,
    CriticVerdict,
    FallbackReason,
    StepStatus,
    ToolCallRecord,
    TriageDecision,
)
from ken_contracts.model import ModelRole
from ken_contracts.recommendations import LinkPlanStatus
from ken_core.db.models.agents import AgentRun, AgentRunPlan, AgentStep
from ken_core.db.models.intelligence import JobRun
from ken_core.db.models.inventory import Page
from ken_core.db.models.recommendations import LinkPlan as LinkPlanRow

_RUNNING: Final = AgentRunStatus.RUNNING.value
_PROPOSED: Final = LinkPlanStatus.PROPOSED.value
_JOB_RUNNING: Final = "running"


# --------------------------------------------------------------------------- #
# Row <-> contract mapping
# --------------------------------------------------------------------------- #
def _opt[E](make: Callable[[str], E], value: str | None) -> E | None:
    return None if value is None else make(value)


def summary_from_row(row: AgentRun, handed_off: Sequence[uuid.UUID]) -> AgentRunSummary:
    return AgentRunSummary(
        id=row.id,
        job_run_id=row.job_run_id,
        source_page_id=row.source_page_id,
        source_page_version=row.source_page_version,
        source_url=row.source_url,
        trigger=AgentTrigger(row.trigger),
        status=AgentRunStatus(row.status),
        outcome=_opt(AgentOutcome, row.outcome),
        triage_decision=_opt(TriageDecision, row.triage_decision),
        handed_off_plan_ids=list(handed_off),
        correlation_id=row.correlation_id,
        trace_id=row.trace_id,
        graph_version=row.graph_version,
        started_at=row.started_at,
        finished_at=row.finished_at,
        error_code=_opt(AgentErrorCode, row.error_code),
    )


def step_from_row(row: AgentStep) -> AgentStepView:
    return AgentStepView(
        run_id=row.run_id,
        seq=row.seq,
        node=AgentNode(row.node),
        status=StepStatus(row.status),
        attempt=row.attempt,
        started_at=row.started_at,
        finished_at=row.finished_at,
        duration_ms=row.duration_ms,
        model_role=_opt(ModelRole, row.model_role),
        model=row.model,
        fallback_used=row.fallback_used,
        fallback_reason=_opt(FallbackReason, row.fallback_reason),
        error_code=_opt(AgentErrorCode, row.error_code),
        tool_calls=[ToolCallRecord.model_validate(c) for c in row.tool_calls],
        output=dict(row.output),
        trace_id=row.trace_id,
        span_id=row.span_id,
    )


def review_from_row(row: AgentRunPlan) -> AgentPlanReview:
    return AgentPlanReview(
        plan_id=row.plan_id,
        verdict=CriticVerdict(row.verdict),
        reasons=[CriticReason(r) for r in row.reasons],
        explanation=row.explanation,
        handed_off=row.handed_off,
        fallback_used=row.fallback_used,
        fallback_reason=_opt(FallbackReason, row.fallback_reason),
        model=row.model,
        reviewed_at=row.reviewed_at,
    )


# --------------------------------------------------------------------------- #
# Runs
# --------------------------------------------------------------------------- #
async def insert_run(
    session: AsyncSession,
    *,
    run_id: uuid.UUID,
    job_run_id: uuid.UUID,
    source_page_id: uuid.UUID,
    source_url: str,
    trigger: AgentTrigger,
    correlation_id: uuid.UUID,
    graph_version: str,
    config_snapshot: Mapping[str, Any],
    started_at: datetime,
) -> None:
    """Insert a ``running`` run (the partial unique index refuses a second one per source)."""
    session.add(
        AgentRun(
            id=run_id,
            job_run_id=job_run_id,
            source_page_id=source_page_id,
            source_page_version=None,
            source_url=source_url,
            trigger=AgentTrigger(trigger).value,
            status=_RUNNING,
            outcome=None,
            triage_decision=None,
            review_notes=[],
            correlation_id=correlation_id,
            trace_id=correlation_id.hex,
            graph_version=graph_version,
            config_snapshot=dict(config_snapshot),
            error_code=None,
            started_at=started_at,
            finished_at=None,
            created_at=started_at,
            updated_at=started_at,
        )
    )
    await session.flush()


async def get_run(
    session: AsyncSession, *, run_id: uuid.UUID, lock: bool = False
) -> AgentRun | None:
    statement = select(AgentRun).where(AgentRun.id == run_id)
    if lock:
        statement = statement.with_for_update()
    return (await session.scalars(statement.execution_options(populate_existing=True))).first()


async def finish_run(
    session: AsyncSession,
    *,
    run_id: uuid.UUID,
    status: AgentRunStatus,
    outcome: AgentOutcome,
    finished_at: datetime,
    triage_decision: TriageDecision | None,
    source_page_version: int | None,
    review_notes: Sequence[str],
    error_code: AgentErrorCode | None,
) -> bool:
    """Finish a RUNNING run (mutable columns only). False if it is missing or finished."""
    result = await session.execute(
        update(AgentRun)
        .where(AgentRun.id == run_id, AgentRun.status == _RUNNING)
        .values(
            status=AgentRunStatus(status).value,
            outcome=AgentOutcome(outcome).value,
            finished_at=finished_at,
            triage_decision=None if triage_decision is None else triage_decision.value,
            source_page_version=source_page_version,
            review_notes=list(review_notes),
            error_code=None if error_code is None else error_code.value,
            updated_at=finished_at,
        )
        .returning(AgentRun.id)
        .execution_options(synchronize_session=False)
    )
    return result.first() is not None


async def reap_orphans(session: AsyncSession, *, now: datetime) -> int:
    """``running`` runs whose job run is no longer running -> failed / orphaned."""
    finished_job = (
        select(JobRun.id)
        .where(JobRun.id == AgentRun.job_run_id, JobRun.status != _JOB_RUNNING)
        .correlate(AgentRun)
    )
    result = await session.execute(
        update(AgentRun)
        .where(AgentRun.status == _RUNNING, exists(finished_job))
        .values(
            status=AgentRunStatus.FAILED.value,
            outcome=AgentOutcome.FAILED.value,
            error_code=AgentErrorCode.ORPHANED.value,
            finished_at=func.greatest(now, AgentRun.started_at),
            updated_at=func.greatest(now, AgentRun.updated_at),
        )
        .returning(AgentRun.id)
        .execution_options(synchronize_session=False)
    )
    return len(result.all())


def run_conditions(
    *,
    status: AgentRunStatus | None,
    outcome: AgentOutcome | None,
    source_page_id: uuid.UUID | None,
) -> list[ColumnElement[bool]]:
    conditions: list[ColumnElement[bool]] = []
    if status is not None:
        conditions.append(AgentRun.status == AgentRunStatus(status).value)
    if outcome is not None:
        conditions.append(AgentRun.outcome == AgentOutcome(outcome).value)
    if source_page_id is not None:
        conditions.append(AgentRun.source_page_id == source_page_id)
    return conditions


async def count_runs(session: AsyncSession, conditions: Sequence[ColumnElement[bool]]) -> int:
    return int(
        await session.scalar(select(func.count()).select_from(AgentRun).where(*conditions)) or 0
    )


async def list_runs(
    session: AsyncSession,
    conditions: Sequence[ColumnElement[bool]],
    *,
    limit: int,
    offset: int,
) -> list[AgentRun]:
    """Newest first (started_at desc, then id: a total order)."""
    return list(
        (
            await session.scalars(
                select(AgentRun)
                .where(*conditions)
                .order_by(AgentRun.started_at.desc(), AgentRun.id)
                .limit(limit)
                .offset(offset)
            )
        ).all()
    )


# --------------------------------------------------------------------------- #
# Steps
# --------------------------------------------------------------------------- #
async def insert_step(session: AsyncSession, *, step: AgentStepView) -> None:
    session.add(
        AgentStep(
            run_id=step.run_id,
            seq=step.seq,
            node=AgentNode(step.node).value,
            status=StepStatus(step.status).value,
            attempt=step.attempt,
            started_at=step.started_at,
            finished_at=step.finished_at,
            duration_ms=step.duration_ms,
            model_role=None if step.model_role is None else ModelRole(step.model_role).value,
            model=step.model,
            fallback_used=step.fallback_used,
            fallback_reason=None if step.fallback_reason is None else step.fallback_reason.value,
            error_code=None if step.error_code is None else step.error_code.value,
            tool_calls=[c.model_dump(mode="json") for c in step.tool_calls],
            output=step.model_dump(mode="json", include={"output"})["output"],
            trace_id=step.trace_id,
            span_id=step.span_id,
        )
    )
    await session.flush()


async def steps_for_run(session: AsyncSession, *, run_id: uuid.UUID) -> list[AgentStepView]:
    rows = (
        await session.scalars(
            select(AgentStep).where(AgentStep.run_id == run_id).order_by(AgentStep.seq)
        )
    ).all()
    return [step_from_row(r) for r in rows]


# --------------------------------------------------------------------------- #
# Reviews
# --------------------------------------------------------------------------- #
async def insert_review(
    session: AsyncSession,
    *,
    run_id: uuid.UUID,
    plan_id: uuid.UUID,
    verdict: CriticVerdict,
    reasons: Sequence[CriticReason],
    explanation: str,
    handed_off: bool,
    fallback_reason: FallbackReason | None,
    model: str | None,
    reviewed_at: datetime,
) -> None:
    session.add(
        AgentRunPlan(
            run_id=run_id,
            plan_id=plan_id,
            verdict=CriticVerdict(verdict).value,
            reasons=[CriticReason(r).value for r in reasons],
            explanation=explanation,
            handed_off=handed_off,
            fallback_used=fallback_reason is not None,
            fallback_reason=None if fallback_reason is None else fallback_reason.value,
            model=model,
            reviewed_at=reviewed_at,
        )
    )
    await session.flush()


async def reviews_for_run(session: AsyncSession, *, run_id: uuid.UUID) -> list[AgentPlanReview]:
    rows = (
        await session.scalars(
            select(AgentRunPlan).where(AgentRunPlan.run_id == run_id).order_by(AgentRunPlan.id)
        )
    ).all()
    return [review_from_row(r) for r in rows]


async def reviews_for_plan(session: AsyncSession, *, plan_id: uuid.UUID) -> list[AgentPlanReview]:
    rows = (
        await session.scalars(
            select(AgentRunPlan).where(AgentRunPlan.plan_id == plan_id).order_by(AgentRunPlan.id)
        )
    ).all()
    return [review_from_row(r) for r in rows]


async def handed_off_plan_ids(
    session: AsyncSession, *, run_ids: Iterable[uuid.UUID]
) -> dict[uuid.UUID, list[uuid.UUID]]:
    """run id -> the plans it handed off (in review order)."""
    ids = list(run_ids)
    out: dict[uuid.UUID, list[uuid.UUID]] = defaultdict(list)
    if not ids:
        return {}
    rows = (
        await session.execute(
            select(AgentRunPlan.run_id, AgentRunPlan.plan_id)
            .where(AgentRunPlan.run_id.in_(ids), AgentRunPlan.handed_off.is_(True))
            .order_by(AgentRunPlan.run_id, AgentRunPlan.id)
        )
    ).all()
    for run_id, plan_id in rows:
        out[run_id].append(plan_id)
    return dict(out)


# --------------------------------------------------------------------------- #
# What to review
# --------------------------------------------------------------------------- #
def _unreviewed() -> ColumnElement[bool]:
    return ~exists().where(AgentRunPlan.plan_id == LinkPlanRow.id)


async def unreviewed_proposed_plans(
    session: AsyncSession, *, source_page_id: uuid.UUID, limit: int
) -> list[LinkPlanRow]:
    """The source's ``proposed`` plans no agent has reviewed, in engine order."""
    return list(
        (
            await session.scalars(
                select(LinkPlanRow)
                .where(
                    LinkPlanRow.source_page_id == source_page_id,
                    LinkPlanRow.status == _PROPOSED,
                    _unreviewed(),
                )
                .order_by(LinkPlanRow.total_score.desc(), LinkPlanRow.created_at, LinkPlanRow.id)
                .limit(limit)
            )
        ).all()
    )


async def sources_to_review(session: AsyncSession, *, limit: int) -> list[uuid.UUID]:
    """Sources with >= 1 unreviewed proposed plan and no running agent run (URL, id order)."""
    has_plan = exists().where(
        LinkPlanRow.source_page_id == Page.id,
        LinkPlanRow.status == _PROPOSED,
        _unreviewed(),
    )
    running = exists().where(and_(AgentRun.source_page_id == Page.id, AgentRun.status == _RUNNING))
    rows = await session.scalars(
        select(Page.id).where(has_plan, ~running).order_by(Page.canonical_url, Page.id).limit(limit)
    )
    return list(rows.all())


__all__ = [
    "count_runs",
    "finish_run",
    "get_run",
    "handed_off_plan_ids",
    "insert_review",
    "insert_run",
    "insert_step",
    "list_runs",
    "reap_orphans",
    "review_from_row",
    "reviews_for_plan",
    "reviews_for_run",
    "run_conditions",
    "sources_to_review",
    "step_from_row",
    "steps_for_run",
    "summary_from_row",
    "unreviewed_proposed_plans",
]
