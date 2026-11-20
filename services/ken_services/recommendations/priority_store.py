"""Persistence for governed priority designations, and the shadow report's read path.

Business Phase 2A-1. Two jobs, both local:

1. **Management** -- :func:`add_designation`, :func:`retire_designation`,
   :func:`list_designations`. Validation is the domain dataclass's
   (:class:`~ken_services.recommendations.priority.PriorityDesignation`), run BEFORE the
   insert, and the database's CHECK constraints (migration 0013) repeat it for any writer that
   bypasses this module.
2. **Shadow input** -- :func:`load_shadow_candidates` and :func:`build_shadow_report` read one
   recommendation run's QUALIFIED candidates (``priority_shadow`` module docstring) and the
   designations of their targets, and compute the report. Nothing is written.

**There is no edit.** A designation is added or retired, never changed in place. An in-place
edit would overwrite ``updated_by`` and lose the previous state; retire-and-add keeps both rows,
so the row set IS the audit history of every editorial decision. Retiring stamps ``retired_at``
and the audit pair, and nothing else.

**Retirement takes an instant, so it can be scheduled.** ``retire_designation(..., at=...)``
accepts a future instant: the incumbent's validity is truncated there, and a successor starting
at that same instant is accepted, because the intervals are half-open and therefore adjacent
rather than overlapping. An open-ended designation can thus be replaced on a date without ever
editing it. Two designations of one (page, type) that WOULD be in force together are refused by
the database (``page_priorities_no_overlap``).

**Eligibility is the engine's own.** Targets are judged by
:func:`ken_services.recommendations.validity.page_eligibility` over
:func:`ken_core.db.repositories.recommendations.page_facts` -- the same computation the live
pipeline uses, under the analysis version the shadowed run recorded -- so the shadow can never
disagree with the engine about who is eligible.
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from ken_contracts.intelligence import TaxonomyPolicy
from ken_contracts.inventory import RunStatus
from ken_contracts.jobs import JobKind
from ken_core.db.models.intelligence import JobRun
from ken_core.db.models.inventory import Page
from ken_core.db.models.recommendations import LinkCandidate, PagePriority
from ken_core.db.repositories.recommendations import page_facts
from ken_core.priority import PriorityType
from ken_services.recommendations.priority import PriorityDesignation, require_aware
from ken_services.recommendations.priority_shadow import (
    CAPACITY_REJECTIONS,
    ShadowCandidate,
    ShadowReport,
    shadow_report,
)
from ken_services.recommendations.validity import page_eligibility

__all__ = [
    "DesignationNotFoundError",
    "RunNotFoundError",
    "ShadowRunContext",
    "add_designation",
    "build_shadow_report",
    "latest_recommendation_run",
    "list_designations",
    "load_designations",
    "load_shadow_candidates",
    "retire_designation",
    "violated_constraint",
]


def violated_constraint(error: Exception) -> str:
    """The name of the database constraint an integrity error violated, or ``""``.

    Two layers have to be unwrapped. A trigger's ``RAISE ... USING CONSTRAINT`` sets the name
    on the asyncpg exception, but SQLAlchemy's asyncpg adapter re-wraps it and does NOT copy
    ``constraint_name`` across -- it keeps the original on ``__cause__``. Matching on the
    message text instead would be wrong in the other direction: a trigger's message does not
    repeat the constraint name at all.
    """
    for candidate in (error, getattr(error, "__cause__", None)):
        name = getattr(candidate, "constraint_name", None)
        if name:
            return str(name)
    return ""


class DesignationNotFoundError(LookupError):
    """No designation has that id."""


class RunNotFoundError(LookupError):
    """No recommendation run to shadow."""


def _to_domain(row: PagePriority) -> PriorityDesignation:
    return PriorityDesignation(
        id=row.id,
        page_id=row.page_id,
        priority_type=PriorityType(row.priority_type),
        tier=row.tier,
        reason=row.reason,
        owner=row.owner,
        effective_from=row.effective_from,
        effective_to=row.effective_to,
        retired_at=row.retired_at,
        created_at=row.created_at,
        created_by=row.created_by,
        updated_at=row.updated_at,
        updated_by=row.updated_by,
    )


# --------------------------------------------------------------------------- #
# Management
# --------------------------------------------------------------------------- #
async def add_designation(
    session: AsyncSession,
    *,
    page_id: uuid.UUID,
    priority_type: PriorityType,
    tier: int,
    reason: str,
    owner: str,
    effective_from: datetime,
    effective_to: datetime | None,
    actor: str,
    now: datetime,
) -> PriorityDesignation:
    """Insert one ACTIVE designation. The caller commits.

    The domain dataclass validates first, so a bad tier, blank reason/owner or inverted window
    is refused with a clear message before any SQL runs. A second ACTIVE designation of the
    same type on the same page is refused by the database (partial unique index): retire the
    old one first. That is deliberate -- replacing a designation is an editorial decision that
    must leave a trace.
    """
    for name, value in (
        ("effective_from", effective_from),
        ("effective_to", effective_to),
        ("now", now),
    ):
        require_aware(value, name)
    designation = PriorityDesignation(
        id=uuid.uuid4(),
        page_id=page_id,
        priority_type=priority_type,
        tier=tier,
        reason=reason,
        owner=owner,
        effective_from=effective_from,
        effective_to=effective_to,
        retired_at=None,
        created_at=now,
        created_by=actor,
        updated_at=now,
        updated_by=actor,
    )
    if not actor.strip():
        raise ValueError("a designation change must name its actor")
    session.add(
        PagePriority(
            id=designation.id,
            page_id=designation.page_id,
            priority_type=designation.priority_type.value,
            tier=designation.tier,
            reason=designation.reason,
            owner=designation.owner,
            effective_from=designation.effective_from,
            effective_to=designation.effective_to,
            retired_at=None,
            created_at=now,
            created_by=actor,
            updated_at=now,
            updated_by=actor,
        )
    )
    await session.flush()
    return designation


async def retire_designation(
    session: AsyncSession,
    *,
    designation_id: uuid.UUID,
    actor: str,
    now: datetime,
    at: datetime | None = None,
) -> PriorityDesignation:
    """Record the instant a designation stops being in force. The caller commits.

    ``at`` defaults to ``now``; a FUTURE ``at`` schedules the retirement, which is how an
    open-ended designation is replaced on a date without editing it. ``at`` at or before
    ``effective_from`` cancels it outright: the interval becomes empty, so it was in force at
    no instant and blocks no successor.

    Idempotent: retiring an already-retired designation changes nothing, including the audit
    pair -- re-running a command must not rewrite who retired it or when.
    """
    if not actor.strip():
        raise ValueError("a designation change must name its actor")
    require_aware(now, "now")
    require_aware(at, "at")
    row = await session.get(PagePriority, designation_id)
    if row is None:
        raise DesignationNotFoundError(str(designation_id))
    if row.retired_at is None:
        row.retired_at = at or now
        row.updated_at = now
        row.updated_by = actor
        await session.flush()
    return _to_domain(row)


async def list_designations(
    session: AsyncSession,
    *,
    page_id: uuid.UUID | None = None,
    include_retired: bool = False,
) -> list[PriorityDesignation]:
    """Designations in a stable order: page, then type, then creation, then id.

    ``include_retired`` shows designations whose retirement has been RECORDED, including ones
    scheduled to retire in the future and therefore still in force.
    """
    statement = select(PagePriority)
    if page_id is not None:
        statement = statement.where(PagePriority.page_id == page_id)
    if not include_retired:
        statement = statement.where(PagePriority.retired_at.is_(None))
    statement = statement.order_by(
        PagePriority.page_id,
        PagePriority.priority_type,
        PagePriority.created_at,
        PagePriority.id,
    )
    rows = (await session.execute(statement)).scalars().all()
    return [_to_domain(r) for r in rows]


async def load_designations(
    session: AsyncSession, *, page_ids: Iterable[uuid.UUID]
) -> dict[uuid.UUID, list[PriorityDesignation]]:
    """Every designation of ``page_ids``, retired or not, keyed by page.

    Retired and out-of-window rows are returned too, and resolved by the domain at a chosen
    instant. Filtering here would make a report for a past instant impossible -- a designation
    retired today was still in force last week, and the shadow must be able to say so.
    """
    ids = list(page_ids)
    out: dict[uuid.UUID, list[PriorityDesignation]] = defaultdict(list)
    if not ids:
        return dict(out)
    rows = (
        (
            await session.execute(
                select(PagePriority)
                .where(PagePriority.page_id.in_(ids))
                .order_by(PagePriority.page_id, PagePriority.id)
            )
        )
        .scalars()
        .all()
    )
    for row in rows:
        out[row.page_id].append(_to_domain(row))
    return dict(out)


# --------------------------------------------------------------------------- #
# Shadow input
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class ShadowRunContext:
    """Which engine output a shadow report shadowed, from the run's own record."""

    run_id: uuid.UUID
    analysis_version: str
    policy_version: int | None
    ranking_policy_version: int | None
    started_at: datetime


async def latest_recommendation_run(session: AsyncSession) -> uuid.UUID | None:
    """The most recent SUCCEEDED recommendation run, or ``None``."""
    return (
        await session.execute(
            select(JobRun.id)
            .where(
                JobRun.kind == JobKind.RECOMMENDATION.value,
                JobRun.status == RunStatus.SUCCEEDED.value,
            )
            .order_by(JobRun.started_at.desc(), JobRun.id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


async def _run_context(
    session: AsyncSession, run_id: uuid.UUID, *, fallback_analysis_version: str
) -> ShadowRunContext:
    run = await session.get(JobRun, run_id)
    if run is None or run.kind != JobKind.RECOMMENDATION.value:
        raise RunNotFoundError(str(run_id))
    snapshot: dict[str, Any] = run.config_snapshot or {}

    def _version(key: str) -> int | None:
        block = snapshot.get(key)
        value = block.get("version") if isinstance(block, dict) else None
        return value if isinstance(value, int) else None

    analysis = snapshot.get("analysis_version")
    return ShadowRunContext(
        run_id=run.id,
        analysis_version=analysis if isinstance(analysis, str) else fallback_analysis_version,
        policy_version=_version("policy"),
        ranking_policy_version=_version("ranking_policy"),
        started_at=run.started_at,
    )


async def load_shadow_candidates(
    session: AsyncSession, *, run_id: uuid.UUID
) -> list[ShadowCandidate]:
    """The run's QUALIFIED candidates: survivors, plus those cut only by a capacity cap.

    Everything refused by a gate, an eligibility rule, a floor (``min_relevance``,
    ``min_score``) or a safety guard (``link_density_exceeded``) is excluded here, in SQL, so
    it cannot reach the report (``priority_shadow`` module docstring). A candidate without a
    recorded final score cannot be ranked and is excluded too.
    """
    capacity = sorted(r.value for r in CAPACITY_REJECTIONS)
    rows = (
        await session.execute(
            select(
                LinkCandidate.source_page_id,
                LinkCandidate.target_page_id,
                Page.canonical_url,
                LinkCandidate.total_score,
                LinkCandidate.rejection,
            )
            .join(Page, Page.id == LinkCandidate.target_page_id)
            .where(
                LinkCandidate.run_id == run_id,
                LinkCandidate.total_score.is_not(None),
                or_(LinkCandidate.rejection.is_(None), LinkCandidate.rejection.in_(capacity)),
            )
            .order_by(LinkCandidate.source_page_id, LinkCandidate.target_page_id)
        )
    ).all()
    return [
        ShadowCandidate(
            source_page_id=source,
            target_page_id=target,
            target_url=url,
            live_score=min(1.0, max(0.0, float(score))),
            live_selected=rejection is None,
        )
        for source, target, url, score, rejection in rows
        if score is not None  # also excluded in SQL; repeated so the type is honest
    ]


async def build_shadow_report(
    session: AsyncSession,
    *,
    run_id: uuid.UUID,
    taxonomy: TaxonomyPolicy,
    fallback_analysis_version: str,
    at: datetime,
) -> tuple[ShadowRunContext, ShadowReport]:
    """Shadow one recommendation run. Read-only."""
    context = await _run_context(
        session, run_id, fallback_analysis_version=fallback_analysis_version
    )
    candidates = await load_shadow_candidates(session, run_id=run_id)
    targets: Sequence[uuid.UUID] = sorted({c.target_page_id for c in candidates}, key=str)
    facts = await page_facts(session, analysis_version=context.analysis_version, page_ids=targets)
    eligibility = {
        page_id: page_eligibility(
            fact, taxonomy=taxonomy, analysis_version=context.analysis_version
        )
        for page_id, fact in facts.items()
    }
    designations = await load_designations(session, page_ids=targets)
    report = shadow_report(candidates, designations=designations, eligibility=eligibility, at=at)
    return context, report
