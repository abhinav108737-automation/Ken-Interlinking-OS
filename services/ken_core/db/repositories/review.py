"""Review-action repository: the append-only ``review_actions`` record (ADR-0013 s4, s7).

The caller owns the transaction: nothing here commits. There is deliberately no update or
delete (migration 0006's trigger would refuse it anyway). Rows are re-validated through the
frozen :class:`~ken_contracts.review.ReviewActionView` on the way out.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import ColumnElement, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from ken_contracts.recommendations import LinkPlanStatus
from ken_contracts.review import FeedbackReason, ReviewAction, ReviewActionView
from ken_core.db.models.review import ReviewActionRow


@dataclass(frozen=True, slots=True)
class StoredAction:
    """What an idempotent replay needs of a stored action."""

    id: int
    operation_id: uuid.UUID
    request_fingerprint: str
    action: ReviewAction
    plan_id: uuid.UUID
    to_status: LinkPlanStatus
    new_plan_id: uuid.UUID | None
    new_plan_status: LinkPlanStatus | None
    bulk_operation_id: uuid.UUID | None
    result: dict[str, Any]


@dataclass(frozen=True, slots=True)
class NewAction:
    operation_id: uuid.UUID
    request_fingerprint: str
    action: ReviewAction
    actor: str
    user_id: uuid.UUID
    plan_id: uuid.UUID
    from_status: LinkPlanStatus
    to_status: LinkPlanStatus
    reasons: Sequence[FeedbackReason]
    note: str
    anchor_before: str
    target_before: uuid.UUID
    result: dict[str, Any]
    created_at: datetime
    new_plan_id: uuid.UUID | None = None
    new_plan_status: LinkPlanStatus | None = None
    anchor_after: str | None = None
    target_after: uuid.UUID | None = None
    bulk_operation_id: uuid.UUID | None = None


def _stored(row: ReviewActionRow) -> StoredAction:
    return StoredAction(
        id=row.id,
        operation_id=row.operation_id,
        request_fingerprint=row.request_fingerprint,
        action=ReviewAction(row.action),
        plan_id=row.plan_id,
        to_status=LinkPlanStatus(row.to_status),
        new_plan_id=row.new_plan_id,
        new_plan_status=None
        if row.new_plan_status is None
        else LinkPlanStatus(row.new_plan_status),
        bulk_operation_id=row.bulk_operation_id,
        result=dict(row.result),
    )


def view_from_row(row: ReviewActionRow) -> ReviewActionView:
    return ReviewActionView(
        id=row.id,
        operation_id=row.operation_id,
        action=ReviewAction(row.action),
        actor=row.actor,
        plan_id=row.plan_id,
        new_plan_id=row.new_plan_id,
        new_plan_status=None
        if row.new_plan_status is None
        else LinkPlanStatus(row.new_plan_status),
        from_status=LinkPlanStatus(row.from_status),
        to_status=LinkPlanStatus(row.to_status),
        reasons=[FeedbackReason(r) for r in row.reasons],
        note=row.note,
        anchor_before=row.anchor_before,
        anchor_after=row.anchor_after,
        target_before=row.target_before,
        target_after=row.target_after,
        bulk_operation_id=row.bulk_operation_id,
        created_at=row.created_at,
    )


async def action_by_operation(
    session: AsyncSession, *, operation_id: uuid.UUID
) -> StoredAction | None:
    row = (
        await session.scalars(
            select(ReviewActionRow).where(ReviewActionRow.operation_id == operation_id)
        )
    ).first()
    return None if row is None else _stored(row)


async def actions_by_operations(
    session: AsyncSession, *, operation_ids: Sequence[uuid.UUID]
) -> dict[uuid.UUID, StoredAction]:
    if not operation_ids:
        return {}
    rows = (
        await session.scalars(
            select(ReviewActionRow).where(ReviewActionRow.operation_id.in_(list(operation_ids)))
        )
    ).all()
    return {r.operation_id: _stored(r) for r in rows}


async def actions_for_bulk(
    session: AsyncSession, *, bulk_operation_id: uuid.UUID
) -> list[StoredAction]:
    rows = (
        await session.scalars(
            select(ReviewActionRow).where(ReviewActionRow.bulk_operation_id == bulk_operation_id)
        )
    ).all()
    return [_stored(r) for r in rows]


async def insert_action(session: AsyncSession, *, action: NewAction) -> int:
    """Append one action; its id. A reused ``operation_id`` raises ``IntegrityError``
    (``uq_review_actions_operation_id``)."""
    row = ReviewActionRow(
        operation_id=action.operation_id,
        request_fingerprint=action.request_fingerprint,
        action=ReviewAction(action.action).value,
        actor=action.actor,
        user_id=action.user_id,
        plan_id=action.plan_id,
        new_plan_id=action.new_plan_id,
        new_plan_status=None
        if action.new_plan_status is None
        else LinkPlanStatus(action.new_plan_status).value,
        from_status=LinkPlanStatus(action.from_status).value,
        to_status=LinkPlanStatus(action.to_status).value,
        reasons=[FeedbackReason(r).value for r in action.reasons],
        note=action.note,
        anchor_before=action.anchor_before,
        anchor_after=action.anchor_after,
        target_before=action.target_before,
        target_after=action.target_after,
        bulk_operation_id=action.bulk_operation_id,
        result=dict(action.result),
        created_at=action.created_at,
    )
    session.add(row)
    await session.flush()
    return row.id


async def actions_for_plan(session: AsyncSession, *, plan_id: uuid.UUID) -> list[ReviewActionView]:
    """Actions ON the plan and the edit that PRODUCED it, oldest first."""
    rows = (
        await session.scalars(
            select(ReviewActionRow)
            .where(or_(ReviewActionRow.plan_id == plan_id, ReviewActionRow.new_plan_id == plan_id))
            .order_by(ReviewActionRow.id)
        )
    ).all()
    return [view_from_row(r) for r in rows]


async def list_actions(
    session: AsyncSession,
    *,
    limit: int,
    offset: int,
    action: ReviewAction | None = None,
    actor: str | None = None,
) -> tuple[list[ReviewActionView], int]:
    """Newest first (by id: insertion order is total and stable)."""
    conditions: list[ColumnElement[bool]] = []
    if action is not None:
        conditions.append(ReviewActionRow.action == ReviewAction(action).value)
    if actor is not None:
        conditions.append(ReviewActionRow.actor == actor)
    total = int(
        await session.scalar(select(func.count()).select_from(ReviewActionRow).where(*conditions))
        or 0
    )
    rows = (
        await session.scalars(
            select(ReviewActionRow)
            .where(*conditions)
            .order_by(ReviewActionRow.id.desc())
            .limit(limit)
            .offset(offset)
        )
    ).all()
    return [view_from_row(r) for r in rows], total


async def action_counts(session: AsyncSession) -> dict[str, int]:
    rows = (
        await session.execute(
            select(ReviewActionRow.action, func.count())
            .group_by(ReviewActionRow.action)
            .order_by(ReviewActionRow.action)
        )
    ).all()
    return {str(r[0]): int(r[1]) for r in rows}
