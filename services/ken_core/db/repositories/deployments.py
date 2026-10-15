"""Deployment repository: SQL primitives for ``deployments``, ``deployment_items`` and
``deployment_verifications`` (migration 0007, ADR-0014).

The caller owns the transaction: nothing here commits. ``deployments`` rows are updated only
through :func:`update_deployment` (the 0007 guard trigger enforces the state machine and the
mutable-column list); the other two tables are append-only.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ken_contracts.deployment import DeploymentStatus
from ken_core.db.models.deployment import (
    Deployment,
    DeploymentItem,
    DeploymentVerification,
    ItemStage,
)
from ken_core.db.models.inventory import Page
from ken_core.db.models.recommendations import LinkPlan as LinkPlanRow

__all__ = [
    "NewItem",
    "deployments_for_plan",
    "get_by_operation",
    "get_by_rollback_operation",
    "get_deployment",
    "insert_deployment",
    "insert_items",
    "insert_verification",
    "items_by_operation",
    "items_of",
    "list_deployments",
    "lock_deployment",
    "plan_exists",
    "plan_ids_of",
    "source_url_of",
    "stuck_deployments",
    "update_deployment",
    "verifications_of",
]


@dataclass(frozen=True, slots=True)
class NewItem:
    plan_id: uuid.UUID
    stage: str
    href: str
    anchor_text: str
    target_page_id: uuid.UUID
    codes: Sequence[str] = ()
    operation_id: uuid.UUID | None = None
    fingerprint: str | None = None


async def get_deployment(session: AsyncSession, *, deployment_id: uuid.UUID) -> Deployment | None:
    return (
        await session.scalars(
            select(Deployment)
            .where(Deployment.id == deployment_id)
            .execution_options(populate_existing=True)
        )
    ).first()


async def lock_deployment(session: AsyncSession, *, deployment_id: uuid.UUID) -> Deployment | None:
    return (
        await session.scalars(
            select(Deployment)
            .where(Deployment.id == deployment_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).first()


async def get_by_operation(session: AsyncSession, *, operation_id: uuid.UUID) -> Deployment | None:
    return (
        await session.scalars(
            select(Deployment)
            .where(Deployment.operation_id == operation_id)
            .execution_options(populate_existing=True)
        )
    ).first()


async def get_by_rollback_operation(
    session: AsyncSession, *, operation_id: uuid.UUID
) -> Deployment | None:
    return (
        await session.scalars(
            select(Deployment)
            .where(Deployment.rollback_operation_id == operation_id)
            .execution_options(populate_existing=True)
        )
    ).first()


async def items_by_operation(
    session: AsyncSession, *, operation_id: uuid.UUID
) -> list[DeploymentItem]:
    """Item rows written by a (per-plan) rollback operation."""
    return list(
        (
            await session.scalars(
                select(DeploymentItem)
                .where(DeploymentItem.operation_id == operation_id)
                .order_by(DeploymentItem.id)
            )
        ).all()
    )


async def insert_deployment(session: AsyncSession, **values: Any) -> Deployment:
    row = Deployment(**values)
    session.add(row)
    await session.flush()
    return row


async def update_deployment(
    session: AsyncSession, *, deployment_id: uuid.UUID, **values: Any
) -> None:
    await session.execute(
        update(Deployment)
        .where(Deployment.id == deployment_id)
        .values(**values)
        .execution_options(synchronize_session=False)
    )


async def insert_items(
    session: AsyncSession,
    *,
    deployment_id: uuid.UUID,
    items: Sequence[NewItem],
    recorded_at: datetime,
) -> None:
    for item in items:
        session.add(
            DeploymentItem(
                deployment_id=deployment_id,
                plan_id=item.plan_id,
                change_id=item.plan_id,
                stage=item.stage,
                operation_id=item.operation_id,
                fingerprint=item.fingerprint,
                href=item.href,
                anchor_text=item.anchor_text,
                target_page_id=item.target_page_id,
                codes=list(item.codes),
                recorded_at=recorded_at,
            )
        )
    await session.flush()


async def insert_verification(
    session: AsyncSession,
    *,
    deployment_id: uuid.UUID,
    run_at: datetime,
    passed: bool,
    inconclusive: bool,
    http_checked: bool,
    checks: list[dict[str, Any]],
) -> int:
    row = DeploymentVerification(
        deployment_id=deployment_id,
        run_at=run_at,
        passed=passed,
        inconclusive=inconclusive,
        http_checked=http_checked,
        checks=checks,
    )
    session.add(row)
    await session.flush()
    return int(row.id)


async def items_of(session: AsyncSession, *, deployment_id: uuid.UUID) -> list[DeploymentItem]:
    return list(
        (
            await session.scalars(
                select(DeploymentItem)
                .where(DeploymentItem.deployment_id == deployment_id)
                .order_by(DeploymentItem.id)
            )
        ).all()
    )


async def plan_ids_of(session: AsyncSession, *, deployment_id: uuid.UUID) -> list[uuid.UUID]:
    """The deployment's plans, in request order (the ``requested`` rows)."""
    rows = await session.scalars(
        select(DeploymentItem.plan_id)
        .where(
            DeploymentItem.deployment_id == deployment_id,
            DeploymentItem.stage == ItemStage.REQUESTED,
        )
        .order_by(DeploymentItem.id)
    )
    return list(rows.all())


async def verifications_of(
    session: AsyncSession, *, deployment_id: uuid.UUID
) -> list[DeploymentVerification]:
    return list(
        (
            await session.scalars(
                select(DeploymentVerification)
                .where(DeploymentVerification.deployment_id == deployment_id)
                .order_by(DeploymentVerification.id)
            )
        ).all()
    )


async def source_url_of(session: AsyncSession, *, page_id: uuid.UUID) -> str:
    return str(await session.scalar(select(Page.canonical_url).where(Page.id == page_id)) or "")


async def list_deployments(
    session: AsyncSession,
    *,
    limit: int,
    offset: int,
    status: DeploymentStatus | None,
    source_page_id: uuid.UUID | None,
) -> tuple[list[Deployment], int]:
    conditions = []
    if status is not None:
        conditions.append(Deployment.status == DeploymentStatus(status).value)
    if source_page_id is not None:
        conditions.append(Deployment.source_page_id == source_page_id)
    total = int(
        await session.scalar(select(func.count()).select_from(Deployment).where(*conditions)) or 0
    )
    rows = (
        await session.scalars(
            select(Deployment)
            .where(*conditions)
            .order_by(Deployment.created_at.desc(), Deployment.id)
            .limit(limit)
            .offset(offset)
        )
    ).all()
    return list(rows), total


async def deployments_for_plan(session: AsyncSession, *, plan_id: uuid.UUID) -> list[Deployment]:
    ids = select(DeploymentItem.deployment_id).where(
        DeploymentItem.plan_id == plan_id, DeploymentItem.stage == ItemStage.REQUESTED
    )
    rows = (
        await session.scalars(
            select(Deployment)
            .where(Deployment.id.in_(ids))
            .order_by(Deployment.created_at.desc(), Deployment.id)
        )
    ).all()
    return list(rows)


async def plan_exists(session: AsyncSession, *, plan_id: uuid.UUID) -> bool:
    return (
        await session.scalar(select(LinkPlanRow.id).where(LinkPlanRow.id == plan_id))
    ) is not None


async def stuck_deployments(
    session: AsyncSession, *, updated_before: datetime, limit: int = 500
) -> list[Deployment]:
    """``applying`` / ``rolling_back`` rows not touched since ``updated_before`` (oldest first)."""
    rows = (
        await session.scalars(
            select(Deployment)
            .where(
                or_(
                    Deployment.status == DeploymentStatus.APPLYING.value,
                    Deployment.status == DeploymentStatus.ROLLING_BACK.value,
                ),
                Deployment.updated_at < updated_before,
            )
            .order_by(Deployment.updated_at, Deployment.id)
            .limit(limit)
        )
    ).all()
    return list(rows)
