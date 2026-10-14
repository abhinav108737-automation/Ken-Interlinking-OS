"""SQL implementation of :class:`ken_contracts.agents.AgentReader` (read-only).

The API resolves this module by name (``ken_api.deps.AGENT_READER``) and builds
``DatabaseAgentReader(sessionmaker=)`` per request. Nothing here writes.

* **Runs** are listed newest first (``started_at`` desc, then id: a total order), filtered by
  status, outcome and source page. ``handed_off_plan_ids`` is derived from the run's
  ``agent_run_plans`` rows with ``handed_off`` (in review order).
* **Run detail** adds the steps (by ``seq``), the reviews (in review order), the secret-free
  config snapshot and the review notes.
* **Reviews of a plan** (for the Phase 5 reviewer): empty when no agent reviewed it.
"""

from __future__ import annotations

import uuid
from typing import Final

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ken_contracts.agents import (
    AgentOutcome,
    AgentPlanReview,
    AgentRunDetail,
    AgentRunNotFoundError,
    AgentRunStatus,
    AgentRunSummary,
)
from ken_contracts.errors import ListResponse, PageInfo
from ken_core.db.repositories import agents as repo

__all__ = ["MAX_LIMIT", "DatabaseAgentReader"]

MAX_LIMIT: Final = 200


def _check_paging(limit: int, offset: int) -> None:
    if not 1 <= limit <= MAX_LIMIT:
        raise ValueError(f"limit must be between 1 and {MAX_LIMIT}")
    if offset < 0:
        raise ValueError("offset must be >= 0")


class DatabaseAgentReader:
    """Read-only agent run views."""

    def __init__(self, *, sessionmaker: async_sessionmaker[AsyncSession]) -> None:
        self._sessionmaker = sessionmaker

    async def list_runs(
        self,
        *,
        status: AgentRunStatus | None = None,
        outcome: AgentOutcome | None = None,
        source_page_id: uuid.UUID | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> ListResponse[AgentRunSummary]:
        _check_paging(limit, offset)
        conditions = repo.run_conditions(
            status=status, outcome=outcome, source_page_id=source_page_id
        )
        async with self._sessionmaker() as session:
            total = await repo.count_runs(session, conditions)
            rows = await repo.list_runs(session, conditions, limit=limit, offset=offset)
            handed = await repo.handed_off_plan_ids(session, run_ids=[r.id for r in rows])
        return ListResponse[AgentRunSummary](
            items=[repo.summary_from_row(r, handed.get(r.id, [])) for r in rows],
            page=PageInfo(total=total, limit=limit, offset=offset),
        )

    async def get_run(self, *, run_id: uuid.UUID) -> AgentRunDetail:
        async with self._sessionmaker() as session:
            row = await repo.get_run(session, run_id=run_id)
            if row is None:
                raise AgentRunNotFoundError
            steps = await repo.steps_for_run(session, run_id=run_id)
            reviews = await repo.reviews_for_run(session, run_id=run_id)
        summary = repo.summary_from_row(row, [r.plan_id for r in reviews if r.handed_off])
        return AgentRunDetail(
            **summary.model_dump(),
            steps=steps,
            reviews=reviews,
            config_snapshot=dict(row.config_snapshot),
            review_notes=[str(n) for n in row.review_notes],
        )

    async def reviews_for_plan(self, *, plan_id: uuid.UUID) -> list[AgentPlanReview]:
        async with self._sessionmaker() as session:
            return await repo.reviews_for_plan(session, plan_id=plan_id)
