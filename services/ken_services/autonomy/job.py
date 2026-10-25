"""The autonomy pass as a recorded job run (ADR-0018; ``make autonomy-run``).

One pass goes through the generic job runner, so it gets exactly what every other batch job
gets: a per-kind session-level advisory lock (one pass at a time), persisted counters, a
``job.completed`` event, and -- the reason this matters -- the REAPER. ``make jobs-reap``
fails a ``running`` row whose kind lock no live runner holds, so a publisher killed mid-pass
does not leave a run marked running for ever. Nothing extra had to be built for that: the
kinds are ``JobKind.AUTONOMY`` and ``JobKind.AUTONOMY_SHADOW``, both already in the enum and
in migration 0010's ``ck_job_runs_kind_valid``.

**Two kinds, deliberately.** A shadow pass and a publishing pass must never share a lock:
one writes nothing outside ``autonomy_decisions`` while the other can write to the CMS, and
a shadow run blocking a publishing run (or the reverse) would be a confusing way to lose
either. The kind is chosen from the EFFECTIVE mode, so ``make autonomy-shadow`` on a
``guarded`` installation records an ``autonomy_shadow`` run.

Counters are the :class:`~ken_services.autonomy.publish.PublishOutcome` values plus
``plans_considered`` and ``errors``, so one line of the job row says how a pass went:
how many were published, blocked, deferred, declined, rolled back, or refused.

**A shadow pass needs no operator identity** (it writes nothing to the CMS), so it runs with
no principal at all and cannot publish even if a bug tried: there is no publisher.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Sequence
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ken_contracts.auth import Principal
from ken_contracts.autonomy import AutonomyMode
from ken_contracts.cms import CMSProvider
from ken_contracts.inventory import RunStatus
from ken_contracts.jobs import JobKind, JobRunDetail
from ken_core.config import Settings
from ken_core.logging import get_logger
from ken_services.autonomy.compose import build_engine, build_publisher
from ken_services.autonomy.engine import CriticDeclinedError, DatabaseAutonomyEngine
from ken_services.autonomy.gate import require_publishing
from ken_services.autonomy.policy import effective_retry, load_configured_autonomy_policy
from ken_services.autonomy.publish import PUBLISH_COUNTERS, PublishOutcome
from ken_services.jobs.runner import JobContext, run_job, utc_now

__all__ = ["AUTONOMY_COUNTERS", "job_kind_for", "run_autonomy"]

logger = get_logger(__name__)

AUTONOMY_COUNTERS: tuple[str, ...] = PUBLISH_COUNTERS


def job_kind_for(mode: AutonomyMode) -> JobKind:
    """``autonomy_shadow`` in shadow mode, ``autonomy`` otherwise (module docstring)."""
    return JobKind.AUTONOMY_SHADOW if mode is AutonomyMode.SHADOW else JobKind.AUTONOMY


def _snapshot(
    *,
    settings: Settings,
    mode: AutonomyMode,
    plan_ids: Sequence[uuid.UUID] | None,
    source_page_ids: Sequence[uuid.UUID] | None,
    actor: str | None,
) -> dict[str, object]:
    """The run's configuration, SECRET-FREE: it is persisted and served by the API."""
    policy = load_configured_autonomy_policy(settings)
    retry = effective_retry(policy, settings)
    return {
        "mode": mode.value,
        "configured_mode": settings.autonomy_mode,
        "autonomous_acknowledged": settings.autonomy_autonomous_acknowledged,
        "allow_production": settings.autonomy_allow_production,
        "cms_provider": settings.cms_provider,
        "cms_writes_enabled": settings.cms_writes_enabled,
        "verification_profile": settings.autonomy_verification_profile,
        "policy_version": policy.version,
        "publish_max_attempts": retry.max_attempts,
        "publish_cooldown_s": retry.cooldown_s,
        "actor": actor,
        "plan_ids": None if plan_ids is None else [str(p) for p in plan_ids],
        "source_page_ids": (None if source_page_ids is None else [str(p) for p in source_page_ids]),
    }


async def run_autonomy(
    *,
    sessionmaker: async_sessionmaker[AsyncSession],
    settings: Settings,
    triggered_by: str,
    mode: AutonomyMode | None = None,
    plan_ids: Sequence[uuid.UUID] | None = None,
    source_page_ids: Sequence[uuid.UUID] | None = None,
    principal: Principal | None = None,
    cms: CMSProvider | None = None,
    engine: DatabaseAutonomyEngine | None = None,
    clock: Callable[[], datetime] = utc_now,
) -> JobRunDetail:
    """One autonomy pass. Returns the finished run; it never raises for a plan-level problem.

    Raises:
        JobAlreadyRunningError: another pass of the same kind is in progress (nothing
            written).
        ValueError: ``mode`` is more permissive than ``KEN_AUTONOMY_MODE``, or a publishing
            mode was asked for without an operator identity and a CMS provider.
    """
    built = engine or build_engine(sessionmaker=sessionmaker, settings=settings, clock=clock)
    resolved = built.resolve_mode(mode)
    publisher = None
    if resolved is not AutonomyMode.SHADOW:
        require_publishing(settings, resolved)  # before a job row, an engine or a CMS exists
        if principal is None or cms is None:
            raise ValueError(
                "a publishing autonomy pass needs an operator identity and a CMS provider; "
                "shadow mode needs neither because it writes nothing"
            )
        publisher = build_publisher(
            sessionmaker=sessionmaker,
            settings=settings,
            principal=principal,
            cms=cms,
            engine=built,
            clock=clock,
        )

    async def body(job: JobContext) -> RunStatus:
        ids = (
            list(plan_ids)
            if plan_ids is not None
            else await built.decidable_plans(
                source_page_ids=source_page_ids,
                include_shadow_only=resolved is AutonomyMode.SHADOW,
            )
        )
        for plan_id in ids:
            job.increment("plans_considered")
            try:
                if publisher is None:
                    outcome = await _shadow_one(built, plan_id=plan_id, job=job)
                else:
                    result = await publisher.publish_plan(
                        plan_id=plan_id, mode=resolved, job_run_id=job.run_id
                    )
                    outcome = result.outcome
            except Exception as exc:  # the CLASS only: never exception text in a run row
                logger.error(
                    "autonomy_pass_plan_failed",
                    plan_id=str(plan_id),
                    error_class=type(exc).__name__,
                )
                job.increment("errors")
                continue
            job.increment(f"outcome_{outcome.value}")
        await job.flush()
        return (
            RunStatus.COMPLETED_WITH_ERRORS
            if job.counters.get("errors", 0)
            else RunStatus.SUCCEEDED
        )

    return await run_job(
        kind=job_kind_for(resolved),
        sessionmaker=sessionmaker,
        triggered_by=triggered_by,
        body=body,
        config_snapshot=_snapshot(
            settings=settings,
            mode=resolved,
            plan_ids=plan_ids,
            source_page_ids=source_page_ids,
            actor=None if principal is None else principal.actor,
        ),
        counter_names=AUTONOMY_COUNTERS,
        clock=clock,
    )


async def _shadow_one(
    engine: DatabaseAutonomyEngine, *, plan_id: uuid.UUID, job: JobContext
) -> PublishOutcome:
    """Evaluate and record the counterfactual. Writes nothing else, by construction: there is
    no publisher in scope."""
    try:
        evaluated = await engine.evaluated(plan_id=plan_id, mode=AutonomyMode.SHADOW)
    except CriticDeclinedError:
        return PublishOutcome.DECLINED
    await engine.record(evaluated, job_run_id=job.run_id)
    return PublishOutcome.DEFERRED
