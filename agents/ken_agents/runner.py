"""Run the supervisor: one source (:func:`run_one_source`) or a batch (:func:`run_agent_batch`).

ADR-0012 section 5. Every run belongs to a ``job_runs`` batch of kind ``agent`` (a manual
run is a batch of one): the batch holds the advisory lock and emits ``job.completed``. Per
run the runner:

1. reads the source snapshot (for the run's ``source_url``);
2. ``store.start_run`` with a fresh ``correlation_id`` (``trace_id = correlation_id.hex``);
3. executes the graph; each executed node appends ONE ``AgentStepView`` (live);
4. ``store.finish_run`` EXACTLY once, also when the graph raised (``failed`` / ``internal``).

Batch source selection: explicit ids (trigger ``manual``) or, for trigger ``batch``,
``store.sources_to_review(limit=policy.max_sources_per_batch)`` (sources with unreviewed
proposed plans and no running run). Orphaned ``running`` runs are reaped first.

The run store and the backend are protocols (``ken_services.agents.protocol``); this module
never touches a database session.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final, Protocol
from uuid import UUID

from ken_agents.graph import StepRecorder, build_graph, run_graph
from ken_agents.llm import RoleModel
from ken_agents.nodes import NodeContext
from ken_agents.state import AgentState
from ken_agents.tools import ToolBox
from ken_agents.tracing_guard import enforce_no_remote_tracing
from ken_contracts.agents import (
    GRAPH_VERSION,
    AgentErrorCode,
    AgentOutcome,
    AgentPolicy,
    AgentRunStatus,
    AgentStepView,
    AgentTrigger,
    FallbackReason,
    TriageDecision,
)
from ken_contracts.inventory import RunStatus
from ken_contracts.jobs import JobKind, JobRunDetail
from ken_contracts.model import ModelProvider, ModelRole
from ken_core.logging import get_logger
from ken_services.agents.protocol import AgentBackend, AgentRunStore, NewAgentRun

__all__ = [
    "COUNTERS",
    "AgentRunResult",
    "BatchJob",
    "JobStarter",
    "build_config_snapshot",
    "make_batch_body",
    "run_agent_batch",
    "run_one_source",
    "utc_now",
]

logger = get_logger("ken_agents.runner")

COUNTERS: Final = (
    "sources_considered",
    "runs_succeeded",
    "runs_failed",
    "plans_reviewed",
    "plans_handed_off",
    "fallbacks",
    "errors",
)


def utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True, slots=True)
class AgentRunResult:
    run_id: UUID
    source_page_id: UUID
    status: AgentRunStatus
    outcome: AgentOutcome
    error_code: AgentErrorCode | None
    plans_reviewed: int
    plans_handed_off: int
    fallbacks: int
    """Steps that fell back because of a MODEL PROBLEM (``model_disabled`` not counted)."""
    steps: tuple[AgentStepView, ...]
    state: AgentState | None
    """The re-validated final state; None when the graph raised."""


def build_config_snapshot(
    *,
    policy: AgentPolicy,
    recommendation_policy_version: int,
    ranking_policy_version: int,
    provider_configured: bool,
) -> dict[str, Any]:
    """Secret-free: graph and policy versions, model roles enabled, timeouts."""
    return {
        "graph_version": GRAPH_VERSION,
        "agent_policy_version": policy.version,
        "recommendation_policy_version": recommendation_policy_version,
        "ranking_policy_version": ranking_policy_version,
        "model_roles": {
            "planner": ModelRole.FAST.value if policy.planner.use_model else None,
            "critic": ModelRole.REASONING.value if policy.critic.use_model else None,
        },
        "provider_configured": provider_configured,
        "timeouts": {
            "node_seconds": policy.node_timeout_seconds,
            "planner_seconds": policy.planner.timeout_seconds,
            "critic_seconds": policy.critic.timeout_seconds,
        },
        "max_plans_per_run": policy.max_plans_per_run,
        "accept_min_confidence": policy.critic.accept_min_confidence,
        "tool_retries": policy.tool_retries,
        "max_sources_per_batch": policy.max_sources_per_batch,
    }


def _model_clients(
    policy: AgentPolicy, provider: ModelProvider | None
) -> tuple[RoleModel | None, RoleModel | None]:
    if provider is None:
        return None, None
    planner = (
        RoleModel(
            provider=provider, role=ModelRole.FAST, timeout_seconds=policy.planner.timeout_seconds
        )
        if policy.planner.use_model
        else None
    )
    critic = (
        RoleModel(
            provider=provider,
            role=ModelRole.REASONING,
            timeout_seconds=policy.critic.timeout_seconds,
        )
        if policy.critic.use_model
        else None
    )
    return planner, critic


async def run_one_source(
    *,
    source_page_id: UUID,
    job_run_id: UUID,
    trigger: AgentTrigger,
    backend: AgentBackend,
    store: AgentRunStore,
    provider: ModelProvider | None,
    policy: AgentPolicy,
    config_snapshot: Mapping[str, object],
    clock: Callable[[], datetime] = utc_now,
) -> AgentRunResult | None:
    """Run the supervisor over one source. ``None``: no run was started (unknown source or
    ``start_run`` refused, e.g. a run of this source is already ``running``).

    Raises:
        RemoteTracingConfiguredError: remote tracing is configured (nothing is started).
        Exception: only if ``finish_run`` itself fails (the row is then reaped as orphaned).
    """
    enforce_no_remote_tracing()
    page = await backend.get_page(page_id=source_page_id)
    if page is None:
        logger.warning("agent_source_not_found", source_page_id=str(source_page_id))
        return None
    run = NewAgentRun(
        id=uuid.uuid4(),
        job_run_id=job_run_id,
        source_page_id=source_page_id,
        source_url=page.canonical_url,
        trigger=trigger,
        correlation_id=uuid.uuid4(),
        graph_version=GRAPH_VERSION,
        config_snapshot=dict(config_snapshot),
        started_at=clock(),
    )
    try:
        await store.start_run(run=run)
    except Exception as exc:
        logger.warning(
            "agent_run_not_started",
            source_page_id=str(source_page_id),
            error_class=type(exc).__name__,
        )
        return None

    recorder = StepRecorder(
        run_id=run.id,
        trace_id=run.trace_id,
        sink=lambda step: store.append_step(step=step),
        clock=clock,
    )
    planner, critic = _model_clients(policy, provider)
    ctx = NodeContext(
        tools=ToolBox(backend=backend, policy=policy, agent_run_id=run.id, clock=clock),
        policy=policy,
        planner=planner,
        critic=critic,
    )
    initial = AgentState(run_id=run.id, source_page_id=source_page_id, trigger=trigger)
    final: AgentState | None = None
    try:
        final = await run_graph(build_graph(ctx=ctx, recorder=recorder), initial)
    except Exception as exc:
        logger.error("agent_run_crashed", run_id=str(run.id), error_class=type(exc).__name__)

    code: AgentErrorCode | None
    if final is None or final.status is None or final.outcome is None:
        status, outcome, code = AgentRunStatus.FAILED, AgentOutcome.FAILED, AgentErrorCode.INTERNAL
        triage: TriageDecision | None = final.triage_decision if final else None
        notes: Sequence[str] = final.review_notes if final else ()
    else:
        status, outcome, code = final.status, final.outcome, final.error_code
        triage, notes = final.triage_decision, final.review_notes
    await store.finish_run(
        run_id=run.id,
        status=status,
        outcome=outcome,
        finished_at=max(clock(), run.started_at),
        triage_decision=triage,
        source_page_version=page.current_version,
        review_notes=tuple(notes),
        error_code=code,
    )
    steps = tuple(recorder.steps)
    result = AgentRunResult(
        run_id=run.id,
        source_page_id=source_page_id,
        status=status,
        outcome=outcome,
        error_code=code,
        plans_reviewed=len(final.recorded_plans) if final else 0,
        plans_handed_off=len(final.link_plans) if final else 0,
        fallbacks=sum(
            1
            for s in steps
            if s.fallback_used and s.fallback_reason is not FallbackReason.MODEL_DISABLED
        ),
        steps=steps,
        state=final,
    )
    logger.info(
        "agent_run_finished",
        run_id=str(run.id),
        trace_id=run.trace_id,
        status=status.value,
        outcome=outcome.value,
        error_code=code.value if code else None,
        steps=len(steps),
    )
    return result


# --------------------------------------------------------------------------- #
# Batch (a job run of kind ``agent``)
# --------------------------------------------------------------------------- #
class BatchJob(Protocol):
    """What the batch body uses of ``ken_services.jobs.runner.JobContext``."""

    @property
    def run_id(self) -> UUID: ...

    @property
    def counters(self) -> dict[str, int]: ...

    def increment(self, name: str, by: int = 1) -> None: ...

    async def flush(self) -> None: ...


class JobStarter(Protocol):
    """``run_job`` with the session factory already bound (the CLI binds it)."""

    async def __call__(
        self,
        *,
        kind: JobKind,
        triggered_by: str,
        body: Callable[[BatchJob], Awaitable[RunStatus]],
        config_snapshot: Mapping[str, Any],
        counter_names: Iterable[str],
    ) -> JobRunDetail: ...


def _unique(ids: Iterable[UUID]) -> list[UUID]:
    return list(dict.fromkeys(ids))


def make_batch_body(
    *,
    backend: AgentBackend,
    store: AgentRunStore,
    provider: ModelProvider | None,
    policy: AgentPolicy,
    config_snapshot: Mapping[str, object],
    source_page_ids: Sequence[UUID] | None,
    clock: Callable[[], datetime] = utc_now,
    results: list[AgentRunResult] | None = None,
) -> Callable[[BatchJob], Awaitable[RunStatus]]:
    """The job body: reap orphans, select sources, run each, count. ``results`` (optional)
    receives every finished run, for the CLI summary and for tests."""
    trigger = AgentTrigger.MANUAL if source_page_ids else AgentTrigger.BATCH

    async def body(job: BatchJob) -> RunStatus:
        enforce_no_remote_tracing()
        reaped = await store.reap_orphans(now=clock(), current_job_run_id=job.run_id)
        if reaped:
            logger.warning("agent_runs_reaped", count=reaped)
        if source_page_ids:
            sources = _unique(source_page_ids)[: policy.max_sources_per_batch]
        else:
            sources = await store.sources_to_review(limit=policy.max_sources_per_batch)
        for source in sources:
            job.increment("sources_considered")
            try:
                result = await run_one_source(
                    source_page_id=source,
                    job_run_id=job.run_id,
                    trigger=trigger,
                    backend=backend,
                    store=store,
                    provider=provider,
                    policy=policy,
                    config_snapshot=config_snapshot,
                    clock=clock,
                )
            except Exception as exc:  # finish_run failed: the run is reaped later
                logger.error(
                    "agent_run_not_finished",
                    source_page_id=str(source),
                    error_class=type(exc).__name__,
                )
                job.increment("errors")
                await job.flush()
                continue
            if result is None:
                job.increment("errors")
            else:
                if results is not None:
                    results.append(result)
                failed = result.status is AgentRunStatus.FAILED
                job.increment("runs_failed" if failed else "runs_succeeded")
                job.increment("plans_reviewed", result.plans_reviewed)
                job.increment("plans_handed_off", result.plans_handed_off)
                job.increment("fallbacks", result.fallbacks)
            await job.flush()
        return RunStatus.SUCCEEDED if _clean(job) else RunStatus.COMPLETED_WITH_ERRORS

    return body


def _clean(job: BatchJob) -> bool:
    counters = job.counters
    return counters.get("runs_failed", 0) == 0 and counters.get("errors", 0) == 0


async def run_agent_batch(
    *,
    start_job: JobStarter,
    backend: AgentBackend,
    store: AgentRunStore,
    provider: ModelProvider | None,
    policy: AgentPolicy,
    config_snapshot: Mapping[str, Any],
    triggered_by: str,
    source_page_ids: Sequence[UUID] | None = None,
    clock: Callable[[], datetime] = utc_now,
    results: list[AgentRunResult] | None = None,
) -> JobRunDetail:
    """One ``agent`` job run over the explicit sources, or over ``sources_to_review``."""
    body = make_batch_body(
        backend=backend,
        store=store,
        provider=provider,
        policy=policy,
        config_snapshot=config_snapshot,
        source_page_ids=source_page_ids,
        clock=clock,
        results=results,
    )
    return await start_job(
        kind=JobKind.AGENT,
        triggered_by=triggered_by,
        body=body,
        config_snapshot=config_snapshot,
        counter_names=COUNTERS,
    )
