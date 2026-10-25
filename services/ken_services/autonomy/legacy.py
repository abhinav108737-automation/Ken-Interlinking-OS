"""Reconcile plans stranded in the pre-ADR-0018 ``awaiting_approval`` status (hardening pass).

Why they are stranded. Before ADR-0018 the agent hand-off ended a reviewed plan at
``awaiting_approval``. The hand-off now ends at ``policy_checked``, and the autonomy engine
decides only ``proposed`` / ``critiqued`` / ``policy_checked`` / ``autonomy_decided``:
``ALLOWED_TRANSITIONS`` (and the database guard) has no edge from ``awaiting_approval`` onto the
autonomous ladder, so a plan left there can never be decided, let alone published.

What this does, deterministically and without a model call. Every ``awaiting_approval`` plan is
classified from the same stored facts the engine uses (``deterministic_blocks``) plus the
critic's recorded verdict and an open-duplicate check:

====================  ==========================================================  ===============
class                 meaning                                                     ``--apply``
====================  ==========================================================  ===============
``stale``             built against a source version that is no longer current    -> ``expired``
``duplicate``         an older open plan already covers the same source/target    -> ``expired``
``protected``         an active protection covers source or destination           -> ``blocked``
``invalid``           any other hard block code (broken/unpublished target, ...)  -> ``blocked``
``critic_not_accept`` no recorded ACCEPT from the critic                          left unchanged
``eligible``          no block, critic accepted                                   left unchanged
====================  ==========================================================  ===============

Only edges ``ALLOWED_TRANSITIONS`` already offers are used (``awaiting_approval`` ->
``expired`` / ``blocked``), by the ``system:legacy-reconcile`` actor through the one transition
service, so the append-only history keeps the original status as the row's ``from_status`` and
the note names the class and codes. **Nothing is ever published or approved here**: an
``eligible`` plan keeps its status and can be shadow-evaluated (``make autonomy-shadow``); moving
it further is an operator act. It is idempotent (a second run finds nothing in the status), each
plan moves in its own transaction guarded by ``expected_from`` (a concurrent change makes the
move fail rather than overwrite), and ``expired`` is terminal by design: the replacement is a new
plan from ``make recommend``.
"""

from __future__ import annotations

import enum
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final

from sqlalchemy import select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ken_contracts.agents import CriticVerdict
from ken_contracts.autonomy import BlockCode, CriticOutcome
from ken_contracts.recommendations import OPEN_STATUSES, LinkPlanStatus
from ken_core.db.models.agents import AgentRunPlan
from ken_core.db.models.recommendations import LinkPlan as LinkPlanRow
from ken_services.autonomy.engine import DatabaseAutonomyEngine, critic_outcome_of
from ken_services.recommendations.transitions import transition
from ken_services.recommendations.validity import lock_source_plans

__all__ = ["LEGACY_ACTOR", "LegacyClass", "LegacyFinding", "classify", "reconcile"]

LEGACY_ACTOR: Final = "system:legacy-reconcile"
_S = LinkPlanStatus


class LegacyClass(enum.StrEnum):
    STALE = "stale"
    DUPLICATE = "duplicate"
    PROTECTED = "protected"
    INVALID = "invalid"
    CRITIC_NOT_ACCEPT = "critic_not_accept"
    ELIGIBLE = "eligible"


_MOVES: Final[dict[LegacyClass, LinkPlanStatus]] = {
    LegacyClass.STALE: _S.EXPIRED,
    LegacyClass.DUPLICATE: _S.EXPIRED,
    LegacyClass.PROTECTED: _S.BLOCKED,
    LegacyClass.INVALID: _S.BLOCKED,
}


@dataclass(frozen=True)
class LegacyFinding:
    plan_id: uuid.UUID
    legacy_class: LegacyClass
    codes: tuple[str, ...]
    moved_to: str | None  # set only when --apply actually moved the plan


def classify(
    *, blocks: Sequence[BlockCode], critic: CriticOutcome, older_open_duplicate: bool
) -> LegacyClass:
    """The pure rule. Order matters: freshness first, then duplication, then blocks."""
    codes = set(blocks)
    if BlockCode.STALE_SOURCE_VERSION in codes:
        return LegacyClass.STALE
    if older_open_duplicate:
        return LegacyClass.DUPLICATE
    if BlockCode.PROTECTED_PAGE in codes:
        return LegacyClass.PROTECTED
    if codes:
        return LegacyClass.INVALID
    if critic is not CriticOutcome.ACCEPT:
        return LegacyClass.CRITIC_NOT_ACCEPT
    return LegacyClass.ELIGIBLE


async def _legacy_plans(session: AsyncSession) -> list[LinkPlanRow]:
    rows = await session.scalars(
        select(LinkPlanRow)
        .where(LinkPlanRow.status == _S.AWAITING_APPROVAL.value)
        .order_by(LinkPlanRow.created_at, LinkPlanRow.id)
    )
    return list(rows.all())


async def _older_open_duplicate(session: AsyncSession, plan: LinkPlanRow) -> bool:
    """Another OPEN plan for the same source and target that sorts before this one."""
    older = await session.scalar(
        select(LinkPlanRow.id)
        .where(
            LinkPlanRow.id != plan.id,
            LinkPlanRow.source_page_id == plan.source_page_id,
            LinkPlanRow.target_page_id == plan.target_page_id,
            LinkPlanRow.status.in_([s.value for s in OPEN_STATUSES]),
            tuple_(LinkPlanRow.created_at, LinkPlanRow.id) < tuple_(plan.created_at, plan.id),
        )
        .limit(1)
    )
    return older is not None


async def _critic_outcome(session: AsyncSession, plan_id: uuid.UUID) -> CriticOutcome:
    row = (
        await session.execute(
            select(AgentRunPlan.verdict, AgentRunPlan.handed_off)
            .where(AgentRunPlan.plan_id == plan_id)
            .order_by(AgentRunPlan.reviewed_at.desc())
            .limit(1)
        )
    ).first()
    if row is None:
        return CriticOutcome.NO_ACTION
    return critic_outcome_of(CriticVerdict(row.verdict), handed_off=bool(row.handed_off))


async def reconcile(
    *,
    sessionmaker: async_sessionmaker[AsyncSession],
    engine: DatabaseAutonomyEngine,
    apply: bool = False,
) -> list[LegacyFinding]:
    """Classify every ``awaiting_approval`` plan; with ``apply`` also make the safe moves."""
    async with sessionmaker() as session:
        plans = await _legacy_plans(session)
        pending: list[tuple[uuid.UUID, uuid.UUID, bool, CriticOutcome]] = []
        for plan in plans:
            pending.append(
                (
                    plan.id,
                    plan.source_page_id,
                    await _older_open_duplicate(session, plan),
                    await _critic_outcome(session, plan.id),
                )
            )
        await session.rollback()

    findings: list[LegacyFinding] = []
    for plan_id, source_page_id, duplicate, critic in pending:
        blocks = await engine.deterministic_blocks(plan_id=plan_id)
        legacy_class = classify(blocks=blocks, critic=critic, older_open_duplicate=duplicate)
        codes = tuple(sorted(code.value for code in blocks))
        moved: str | None = None
        target = _MOVES.get(legacy_class)
        if apply and target is not None:
            moved = await _move(sessionmaker, plan_id, source_page_id, target, legacy_class, codes)
        findings.append(LegacyFinding(plan_id, legacy_class, codes, moved))
    return findings


async def _move(
    sessionmaker: async_sessionmaker[AsyncSession],
    plan_id: uuid.UUID,
    source_page_id: uuid.UUID,
    target: LinkPlanStatus,
    legacy_class: LegacyClass,
    codes: tuple[str, ...],
) -> str | None:
    """One plan, one transaction. ``expected_from`` makes a concurrent change a no-op."""
    note = f"legacy reconciliation from awaiting_approval: {legacy_class.value}"
    if codes:
        note += f" [{','.join(codes)}]"
    async with sessionmaker() as session, session.begin():
        await lock_source_plans(session, source_page_id=source_page_id)
        current = await session.get(LinkPlanRow, plan_id)
        if current is None or current.status != _S.AWAITING_APPROVAL.value:
            return None  # already reconciled (idempotent) or moved by someone else
        await transition(
            session,
            plan_id=plan_id,
            to_status=target,
            actor=LEGACY_ACTOR,
            note=note[:500],
            expected_from=_S.AWAITING_APPROVAL,
        )
    return target.value
