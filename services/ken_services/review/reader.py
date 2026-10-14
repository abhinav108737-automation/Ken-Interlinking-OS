"""SQL implementation of :class:`ken_contracts.review.ReviewReader` (read-only).

The API resolves this module by name (``ken_api.deps.REVIEW_READER``) and builds
``SqlReviewReader(sessionmaker=)`` per request. Nothing here writes.

* ``actions_for_plan`` -- the actions ON the plan and the edit that PRODUCED it, oldest
  first; an unknown plan is :class:`~ken_contracts.recommendations.PlanNotFoundError` (404).
* ``list_actions`` -- every action, newest first (by id), filtered by action and/or actor.
"""

from __future__ import annotations

import uuid
from typing import Final

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ken_contracts.errors import ListResponse, PageInfo
from ken_contracts.recommendations import PlanNotFoundError
from ken_contracts.review import ReviewAction, ReviewActionView
from ken_core.db.models.recommendations import LinkPlan as LinkPlanRow
from ken_core.db.repositories import review as repo

__all__ = ["MAX_LIMIT", "SqlReviewReader"]

MAX_LIMIT: Final = 200


class SqlReviewReader:
    def __init__(self, *, sessionmaker: async_sessionmaker[AsyncSession]) -> None:
        self._sessionmaker = sessionmaker

    async def actions_for_plan(self, *, plan_id: uuid.UUID) -> list[ReviewActionView]:
        async with self._sessionmaker() as session:
            exists = await session.scalar(select(LinkPlanRow.id).where(LinkPlanRow.id == plan_id))
            if exists is None:
                raise PlanNotFoundError
            return await repo.actions_for_plan(session, plan_id=plan_id)

    async def list_actions(
        self,
        *,
        limit: int,
        offset: int,
        action: ReviewAction | None = None,
        actor: str | None = None,
    ) -> ListResponse[ReviewActionView]:
        if not 1 <= limit <= MAX_LIMIT:
            raise ValueError(f"limit must be between 1 and {MAX_LIMIT}")
        if offset < 0:
            raise ValueError("offset must be >= 0")
        async with self._sessionmaker() as session:
            items, total = await repo.list_actions(
                session, limit=limit, offset=offset, action=action, actor=actor
            )
        return ListResponse[ReviewActionView](
            items=items, page=PageInfo(total=total, limit=limit, offset=offset)
        )
