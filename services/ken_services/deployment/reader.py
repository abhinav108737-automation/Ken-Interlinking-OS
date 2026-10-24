"""``SqlDeploymentReader``: :class:`ken_contracts.deployment.DeploymentReader` over PostgreSQL.

Read-only: the API hands it the READ ONLY session factory (ADR-0014 s8). The row -> view
mapping (:func:`view_of`, :func:`detail_of`) is shared with the deployment service, so a
replayed or freshly executed deployment returns exactly what the reader shows.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ken_contracts.autonomy import AutonomyDecision, AutonomyMode
from ken_contracts.deployment import (
    ChangePreview,
    DeploymentDetail,
    DeploymentItemView,
    DeploymentNotFoundError,
    DeploymentStatus,
    DeploymentView,
    RollbackReason,
    VerificationCheck,
    VerificationRun,
)
from ken_contracts.errors import ListResponse, PageInfo
from ken_contracts.recommendations import LinkPlanStatus, PlanNotFoundError
from ken_core.db.models.autonomy import AutonomyDecisionRow, PublishAttempt
from ken_core.db.models.deployment import Deployment, ItemStage
from ken_core.db.models.recommendations import LinkPlan as LinkPlanRow
from ken_core.db.repositories import deployments as repo

__all__ = ["SqlDeploymentReader", "detail_of", "view_of"]


_ENGINE_ACTOR = "system:autonomy-engine"
"""``ken_services.autonomy.protocol.AUTONOMY_ACTOR`` (not imported: services layering)."""


async def _autonomy_tag(session: AsyncSession, row: Deployment) -> dict[str, Any]:
    """The audit tag of an autonomous publish, from ``publish_attempts`` ONLY.

    ``deployments.actor`` is the operator account and the note is free text a human can type,
    so neither is evidence. An attempt row (actor ``system:autonomy-engine``, ``deployment_id``
    set) is written solely by the autonomous publisher.
    """
    attempt = (
        await session.execute(
            select(PublishAttempt)
            .where(PublishAttempt.deployment_id == row.id, PublishAttempt.actor == _ENGINE_ACTOR)
            .order_by(PublishAttempt.id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if attempt is None:
        return {}
    decision_id = await session.scalar(
        select(AutonomyDecisionRow.id)
        .where(
            AutonomyDecisionRow.plan_id == attempt.plan_id,
            AutonomyDecisionRow.decision == attempt.decision,
            AutonomyDecisionRow.mode == attempt.mode,
            AutonomyDecisionRow.evaluated_at <= attempt.occurred_at,
        )
        .order_by(AutonomyDecisionRow.id.desc())
        .limit(1)
    )
    return {
        "autonomous": True,
        "autonomy_decision_id": None if decision_id is None else int(decision_id),
        "autonomy_decision": AutonomyDecision(attempt.decision),
        "autonomy_mode": AutonomyMode(attempt.mode),
        "autonomy_rolled_back": bool(attempt.rolled_back),
    }


async def view_of(
    session: AsyncSession, row: Deployment, *, replay: bool = False
) -> DeploymentView:
    return DeploymentView(
        **await _autonomy_tag(session, row),
        id=row.id,
        operation_id=row.operation_id,
        source_page_id=row.source_page_id,
        source_url=await repo.source_url_of(session, page_id=row.source_page_id),
        cms_provider=row.cms_provider,
        cms_page_id=row.cms_page_id,
        status=DeploymentStatus(row.status),
        plan_ids=await repo.plan_ids_of(session, deployment_id=row.id),
        previous_version=row.previous_version,
        new_version=row.new_version,
        actor=row.actor,
        error_code=row.error_code,
        created_at=row.created_at,
        applied_at=row.applied_at,
        verified_at=row.verified_at,
        rolled_back_at=row.rolled_back_at,
        idempotent_replay=replay,
    )


async def detail_of(
    session: AsyncSession, row: Deployment, *, replay: bool = False
) -> DeploymentDetail:
    view = await view_of(session, row, replay=replay)
    items = await repo.items_of(session, deployment_id=row.id)
    requested = [i for i in items if i.stage == ItemStage.REQUESTED]
    statuses: dict[uuid.UUID, str] = {}
    if requested:
        found = await session.execute(
            select(LinkPlanRow.id, LinkPlanRow.status, LinkPlanRow.target_url).where(
                LinkPlanRow.id.in_([i.plan_id for i in requested])
            )
        )
        statuses = {r.id: r.status for r in found}
    verifications = await repo.verifications_of(session, deployment_id=row.id)
    preview: list[Any] = list(row.preview or [])
    return DeploymentDetail(
        **view.model_dump(),
        items=[
            DeploymentItemView(
                plan_id=i.plan_id,
                plan_status=LinkPlanStatus(statuses.get(i.plan_id, LinkPlanStatus.VALIDATED.value)),
                anchor_text=i.anchor_text,
                target_url=i.href,
            )
            for i in requested
        ],
        preview=[ChangePreview.model_validate(p) for p in preview],
        verifications=[
            VerificationRun(
                id=v.id,
                run_at=v.run_at,
                passed=v.passed,
                inconclusive=v.inconclusive,
                http_checked=v.http_checked,
                checks=[VerificationCheck.model_validate(c) for c in v.checks],
            )
            for v in verifications
        ],
        rollback_reason=None
        if row.rollback_reason is None
        else RollbackReason(row.rollback_reason),
        rollback_note=row.rollback_note,
    )


class SqlDeploymentReader:
    """Deployment history (newest first), one deployment, and a plan's deployments."""

    def __init__(self, *, sessionmaker: async_sessionmaker[AsyncSession]) -> None:
        self._sessionmaker = sessionmaker

    async def list_deployments(
        self,
        *,
        limit: int,
        offset: int,
        status: DeploymentStatus | None = None,
        source_page_id: uuid.UUID | None = None,
    ) -> ListResponse[DeploymentView]:
        async with self._sessionmaker() as session:
            rows, total = await repo.list_deployments(
                session, limit=limit, offset=offset, status=status, source_page_id=source_page_id
            )
            items = [await view_of(session, r) for r in rows]
        return ListResponse[DeploymentView](
            items=items, page=PageInfo(total=total, limit=limit, offset=offset)
        )

    async def get_deployment(self, *, deployment_id: uuid.UUID) -> DeploymentDetail:
        async with self._sessionmaker() as session:
            row = await repo.get_deployment(session, deployment_id=deployment_id)
            if row is None:
                raise DeploymentNotFoundError
            return await detail_of(session, row)

    async def deployments_for_plan(self, *, plan_id: uuid.UUID) -> list[DeploymentView]:
        async with self._sessionmaker() as session:
            if not await repo.plan_exists(session, plan_id=plan_id):
                raise PlanNotFoundError
            rows = await repo.deployments_for_plan(session, plan_id=plan_id)
            return [await view_of(session, r) for r in rows]
