"""Autonomy persistence (migration 0010; ADR-0018 s4 / s6).

Two append-only tables, and nothing else:

``autonomy_decisions``  one row per evaluation of one plan. INSERT only: there is no update
                        and no delete, because Phase 9 calibration and Phase 8 learning read
                        it as a record of what the system decided and why. The DEFERRED
                        ``ken_autonomy_decision_guard()`` requires the matching
                        ``link_plan_transitions`` row in the SAME transaction whenever the
                        decision moved the plan, so :func:`insert_decision` must be called
                        inside the caller's transaction, after the transition.
``publish_attempts``    the per-(source, target) retry budget. :func:`budget_counts` reads
                        it; :func:`insert_attempt` appends one attempt. The UNIQUE index on
                        (source, target, attempt) is what makes the budget unspendable twice:
                        a racing second INSERT of the same attempt number is REFUSED, which
                        the caller surfaces rather than retrying blindly.

Every function takes the caller's session and writes nothing of its own; the service layer
owns the transaction, the locks and the ordering.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import func, select, text
from sqlalchemy.dialects.postgresql import distinct_on
from sqlalchemy.ext.asyncio import AsyncSession

from ken_contracts.agents import CriticVerdict
from ken_contracts.autonomy import (
    AutonomyDecision,
    AutonomyDecisionView,
    AutonomyEvaluation,
    AutonomyMode,
    BlockCode,
    BlockedActionView,
    CriticOutcome,
    PublishAttemptView,
    RiskTier,
    VerificationProfile,
)
from ken_contracts.recommendations import LinkPlanStatus
from ken_core.db.models.autonomy import AutonomyDecisionRow, PublishAttempt
from ken_core.db.models.recommendations import LinkPlan as LinkPlanRow
from ken_core.db.models.recommendations import LinkPlanTransition

__all__ = [
    "BudgetCounts",
    "attempt_views",
    "block_code_counts",
    "blocked_actions",
    "budget_counts",
    "count_attempts",
    "count_blocked",
    "count_decisions",
    "decision_counts",
    "decision_views",
    "evaluation_of_row",
    "insert_attempt",
    "insert_decision",
    "latest_decision",
    "mode_counts",
    "plan_outcome_counts",
]


_BLOCK_COUNTS_SQL = (
    "SELECT code, count(*) AS n FROM autonomy_decisions, "
    "jsonb_array_elements_text(blocks) AS code GROUP BY code"
)
"""Written out because the ORM spelling of a set-returning function in the FROM list needs
``render_derived`` gymnastics to name its column, and this statement takes no parameters and
interpolates nothing: it is a constant."""


# --------------------------------------------------------------------------- #
# autonomy_decisions
# --------------------------------------------------------------------------- #
async def insert_decision(
    session: AsyncSession,
    *,
    evaluation: AutonomyEvaluation,
    actor: str,
    job_run_id: uuid.UUID | None = None,
    override_reason: str | None = None,
) -> int:
    """Append one decision row and return its id (the caller owns the transaction).

    ``blocks`` is stored as a jsonb array of the enum VALUES, in enum order, exactly as the
    contract validated them: a non-empty array plus the table's ``blocks_force_block`` CHECK
    is what makes "a fired block is never published" true in the database and not only in
    Python.
    """
    row = AutonomyDecisionRow(
        plan_id=evaluation.plan_id,
        decision=evaluation.decision.value,
        mode=evaluation.mode.value,
        risk_tier=evaluation.risk_tier.value,
        confidence=evaluation.confidence,
        calibration_version=evaluation.calibration_version,
        critic_outcome=evaluation.critic_outcome.value,
        critic_verdict=None
        if evaluation.critic_verdict is None
        else CriticVerdict(evaluation.critic_verdict).value,
        policy_version=evaluation.policy_version,
        blocks=[code.value for code in evaluation.blocks],
        rollback_available=evaluation.rollback_available,
        verification_profile=evaluation.verification_profile.value,
        would_have_decision=None
        if evaluation.would_have_decision is None
        else evaluation.would_have_decision.value,
        engine_version=evaluation.engine_version,
        job_run_id=job_run_id,
        actor=actor,
        override_reason=override_reason,
        evaluated_at=evaluation.evaluated_at,
    )
    session.add(row)
    await session.flush()
    return int(row.id)


def evaluation_of_row(row: AutonomyDecisionRow) -> AutonomyEvaluation:
    """Re-validate a stored decision through the frozen contract on the way out."""
    return AutonomyEvaluation(
        plan_id=row.plan_id,
        decision=AutonomyDecision(row.decision),
        mode=AutonomyMode(row.mode),
        risk_tier=RiskTier(row.risk_tier),
        confidence=row.confidence,
        calibration_version=row.calibration_version,
        critic_outcome=CriticOutcome(row.critic_outcome),
        critic_verdict=None if row.critic_verdict is None else CriticVerdict(row.critic_verdict),
        policy_version=row.policy_version,
        blocks=tuple(BlockCode(code) for code in row.blocks),
        rollback_available=row.rollback_available,
        verification_profile=VerificationProfile(row.verification_profile),
        would_have_decision=None
        if row.would_have_decision is None
        else AutonomyDecision(row.would_have_decision),
        evaluated_at=row.evaluated_at,
        engine_version=row.engine_version,
    )


async def latest_decision(
    session: AsyncSession, *, plan_id: uuid.UUID
) -> AutonomyDecisionRow | None:
    """The newest decision of one plan (``id`` descending: the table is append-only)."""
    return (
        await session.scalars(
            select(AutonomyDecisionRow)
            .where(AutonomyDecisionRow.plan_id == plan_id)
            .order_by(AutonomyDecisionRow.id.desc())
            .limit(1)
        )
    ).first()


def _decision_filter(
    statement: Any,
    *,
    decision: AutonomyDecision | None,
    mode: AutonomyMode | None,
    plan_id: uuid.UUID | None,
) -> Any:
    if decision is not None:
        statement = statement.where(AutonomyDecisionRow.decision == decision.value)
    if mode is not None:
        statement = statement.where(AutonomyDecisionRow.mode == mode.value)
    if plan_id is not None:
        statement = statement.where(AutonomyDecisionRow.plan_id == plan_id)
    return statement


async def count_decisions(
    session: AsyncSession,
    *,
    decision: AutonomyDecision | None = None,
    mode: AutonomyMode | None = None,
    plan_id: uuid.UUID | None = None,
) -> int:
    statement = _decision_filter(
        select(func.count()).select_from(AutonomyDecisionRow),
        decision=decision,
        mode=mode,
        plan_id=plan_id,
    )
    return int(await session.scalar(statement) or 0)


async def decision_views(
    session: AsyncSession,
    *,
    limit: int,
    offset: int,
    decision: AutonomyDecision | None = None,
    mode: AutonomyMode | None = None,
    plan_id: uuid.UUID | None = None,
) -> list[AutonomyDecisionView]:
    """Stored decisions, newest first, with the plan's CURRENT status and both URLs."""
    statement = _decision_filter(
        select(
            AutonomyDecisionRow,
            LinkPlanRow.status,
            LinkPlanRow.source_url,
            LinkPlanRow.target_url,
        )
        .join(LinkPlanRow, LinkPlanRow.id == AutonomyDecisionRow.plan_id)
        .order_by(AutonomyDecisionRow.id.desc()),
        decision=decision,
        mode=mode,
        plan_id=plan_id,
    )
    rows = (await session.execute(statement.limit(limit).offset(offset))).all()
    return [
        AutonomyDecisionView(
            id=int(row[0].id),
            plan_status=LinkPlanStatus(row.status),
            source_url=row.source_url,
            target_url=row.target_url,
            **evaluation_of_row(row[0]).model_dump(),
        )
        for row in rows
    ]


def _blocked_reason() -> Any:
    """The words recorded with the plan's most recent move to ``blocked``.

    ``autonomy_decisions`` has no column for a reason (migration 0010 is frozen), so the
    engine writes it as the ``note`` of the ``link_plan_transitions`` row it creates in the
    SAME transaction as the decision (``ken_services.autonomy.reasons``). That is what lets the
    investigation view say "blocked: deferred -- risk tier medium ..." instead of showing an
    empty block list for a deferral.
    """
    return (
        select(LinkPlanTransition.note)
        .where(
            LinkPlanTransition.plan_id == AutonomyDecisionRow.plan_id,
            LinkPlanTransition.to_status == LinkPlanStatus.BLOCKED.value,
        )
        .order_by(LinkPlanTransition.id.desc())
        .limit(1)
        .correlate(AutonomyDecisionRow)
        .scalar_subquery()
        .label("reason")
    )


def _blocked_statement(code: BlockCode | None) -> Any:
    """The newest BLOCK decision per plan, with the plan's CURRENT status beside it.

    ``DISTINCT ON (plan_id)`` ordered by ``id DESC`` is the newest row; the plan's current
    status is carried through as ``overridden``, which is how an overridden block leaves the
    screen without the decision row having to be edited (it cannot be: append-only).

    A BLOCK with NO fired codes is included on purpose: that is a ``guarded``-mode deferral
    or a critic rejection, and the operator screen that replaces the approval inbox is
    exactly where those belong.
    """
    statement = (
        select(
            AutonomyDecisionRow,
            LinkPlanRow.source_page_id,
            LinkPlanRow.source_url,
            LinkPlanRow.target_page_id,
            LinkPlanRow.target_url,
            LinkPlanRow.anchor_text,
            LinkPlanRow.status,
            _blocked_reason(),
        )
        .join(LinkPlanRow, LinkPlanRow.id == AutonomyDecisionRow.plan_id)
        .where(AutonomyDecisionRow.decision == AutonomyDecision.BLOCK.value)
        .ext(distinct_on(AutonomyDecisionRow.plan_id))
        .order_by(AutonomyDecisionRow.plan_id, AutonomyDecisionRow.id.desc())
    )
    if code is not None:
        statement = statement.where(AutonomyDecisionRow.blocks.contains([code.value]))
    return statement


async def count_blocked(session: AsyncSession, *, code: BlockCode | None = None) -> int:
    inner = _blocked_statement(code).subquery()
    return int(await session.scalar(select(func.count()).select_from(inner)) or 0)


async def blocked_actions(
    session: AsyncSession, *, limit: int, offset: int, code: BlockCode | None = None
) -> list[BlockedActionView]:
    inner = _blocked_statement(code).subquery()
    rows = (
        await session.execute(
            select(inner)
            .order_by(inner.c.evaluated_at.desc(), inner.c.id.desc())
            .limit(limit)
            .offset(offset)
        )
    ).all()
    return [
        BlockedActionView(
            plan_id=row.plan_id,
            source_page_id=row.source_page_id,
            source_url=row.source_url,
            target_page_id=row.target_page_id,
            target_url=row.target_url,
            anchor_text=row.anchor_text,
            blocks=tuple(BlockCode(value) for value in row.blocks),
            risk_tier=RiskTier(row.risk_tier),
            mode=AutonomyMode(row.mode),
            decided_at=row.evaluated_at,
            overridden=LinkPlanStatus(row.status) is not LinkPlanStatus.BLOCKED,
            reason=row.reason,
        )
        for row in rows
    ]


async def decision_counts(session: AsyncSession) -> dict[str, int]:
    rows = (
        await session.execute(
            select(AutonomyDecisionRow.decision, func.count()).group_by(
                AutonomyDecisionRow.decision
            )
        )
    ).all()
    return {str(row[0]): int(row[1]) for row in rows}


async def mode_counts(session: AsyncSession) -> dict[str, int]:
    rows = (
        await session.execute(
            select(AutonomyDecisionRow.mode, func.count()).group_by(AutonomyDecisionRow.mode)
        )
    ).all()
    return {str(row[0]): int(row[1]) for row in rows}


async def block_code_counts(session: AsyncSession) -> dict[str, int]:
    """How many decisions each block code appears on. One pass over the jsonb arrays.

    The set-returning function sits in the FROM list beside ``autonomy_decisions``, which
    PostgreSQL treats as an implicit LATERAL, so a decision with three fired codes counts
    once under each of them and the total is not the row count.
    """
    rows = (await session.execute(text(_BLOCK_COUNTS_SQL))).all()
    return {str(row[0]): int(row[1]) for row in rows}


async def plan_outcome_counts(session: AsyncSession) -> tuple[int, int, int]:
    """(blocked plans, auto-published plans, rolled-back plans) for the summary.

    ``auto_published`` counts PLANS with at least one successful publish attempt, not
    attempts: a pair retried twice and applied once published one link.
    """
    blocked = int(
        await session.scalar(
            select(func.count()).where(LinkPlanRow.status == LinkPlanStatus.BLOCKED.value)
        )
        or 0
    )
    published = int(
        await session.scalar(
            select(func.count(func.distinct(PublishAttempt.plan_id))).where(
                PublishAttempt.succeeded.is_(True)
            )
        )
        or 0
    )
    rolled_back = int(
        await session.scalar(
            select(func.count(func.distinct(PublishAttempt.plan_id))).where(
                PublishAttempt.rolled_back.is_(True)
            )
        )
        or 0
    )
    return blocked, published, rolled_back


# --------------------------------------------------------------------------- #
# publish_attempts (the retry budget)
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class BudgetCounts:
    """What the stored attempts of one (source, target) pair add up to."""

    attempts: int
    failures: int
    cooldown_until: datetime | None
    last_attempt_at: datetime | None


async def budget_counts(
    session: AsyncSession, *, source_page_id: uuid.UUID, target_page_id: uuid.UUID
) -> BudgetCounts:
    """The pair's spent budget. ``cooldown_until`` is the LATEST cool-down ever recorded for
    the pair, so a cool-down cannot be shortened by a later attempt that set none."""
    row = (
        await session.execute(
            select(
                func.count().label("attempts"),
                func.count().filter(PublishAttempt.succeeded.is_(False)).label("failures"),
                func.max(PublishAttempt.cooldown_until).label("cooldown_until"),
                func.max(PublishAttempt.occurred_at).label("last_attempt_at"),
            ).where(
                PublishAttempt.source_page_id == source_page_id,
                PublishAttempt.target_page_id == target_page_id,
            )
        )
    ).one()
    return BudgetCounts(
        attempts=int(row.attempts or 0),
        failures=int(row.failures or 0),
        cooldown_until=row.cooldown_until,
        last_attempt_at=row.last_attempt_at,
    )


async def insert_attempt(
    session: AsyncSession,
    *,
    plan_id: uuid.UUID,
    source_page_id: uuid.UUID,
    target_page_id: uuid.UUID,
    attempt: int,
    max_attempts: int,
    decision: AutonomyDecision,
    mode: AutonomyMode,
    profile: VerificationProfile,
    succeeded: bool,
    failure_code: str | None,
    rolled_back: bool,
    actor: str,
    cooldown_until: datetime | None,
    deployment_id: uuid.UUID | None,
    occurred_at: datetime,
) -> int:
    row = PublishAttempt(
        plan_id=plan_id,
        source_page_id=source_page_id,
        target_page_id=target_page_id,
        deployment_id=deployment_id,
        attempt=attempt,
        max_attempts=max_attempts,
        decision=decision.value,
        mode=mode.value,
        verification_profile=profile.value,
        succeeded=succeeded,
        rolled_back=rolled_back,
        failure_code=failure_code,
        cooldown_until=cooldown_until,
        actor=actor,
        occurred_at=occurred_at,
    )
    session.add(row)
    await session.flush()
    return int(row.id)


async def count_attempts(session: AsyncSession, *, plan_id: uuid.UUID | None = None) -> int:
    statement = select(func.count()).select_from(PublishAttempt)
    if plan_id is not None:
        statement = statement.where(PublishAttempt.plan_id == plan_id)
    return int(await session.scalar(statement) or 0)


async def attempt_views(
    session: AsyncSession, *, limit: int, offset: int, plan_id: uuid.UUID | None = None
) -> list[PublishAttemptView]:
    statement = select(PublishAttempt).order_by(PublishAttempt.id.desc())
    if plan_id is not None:
        statement = statement.where(PublishAttempt.plan_id == plan_id)
    rows: Sequence[PublishAttempt] = (
        await session.scalars(statement.limit(limit).offset(offset))
    ).all()
    return [
        PublishAttemptView(
            id=int(row.id),
            plan_id=row.plan_id,
            source_page_id=row.source_page_id,
            target_page_id=row.target_page_id,
            attempt=row.attempt,
            decision=AutonomyDecision(row.decision),
            mode=AutonomyMode(row.mode),
            succeeded=row.succeeded,
            verification_profile=VerificationProfile(row.verification_profile),
            failure_code=row.failure_code,
            rolled_back=row.rolled_back,
            cooldown_until=row.cooldown_until,
            occurred_at=row.occurred_at,
        )
        for row in rows
    ]
