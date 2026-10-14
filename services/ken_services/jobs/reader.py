"""SQL implementation of :class:`ken_contracts.jobs.JobReader` (read-only).

The API resolves this module by name (``ken_api.deps.JOB_READER``) and builds
``SqlJobReader(sessionmaker)`` per request. Nothing here writes.
"""

from __future__ import annotations

import uuid
from typing import Final

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ken_contracts.errors import ListResponse, PageInfo
from ken_contracts.inventory import RunStatus
from ken_contracts.jobs import JobKind, JobRunDetail, JobRunNotFoundError, JobRunSummary
from ken_core.db.repositories import jobs as repo

MAX_LIMIT: Final = 200


class SqlJobReader:
    def __init__(self, sessionmaker: async_sessionmaker[AsyncSession]) -> None:
        self._sessionmaker = sessionmaker

    async def list_runs(
        self,
        *,
        limit: int,
        offset: int,
        kind: JobKind | None = None,
        status: RunStatus | None = None,
    ) -> ListResponse[JobRunSummary]:
        if not 1 <= limit <= MAX_LIMIT:
            raise ValueError(f"limit must be between 1 and {MAX_LIMIT}")
        if offset < 0:
            raise ValueError("offset must be >= 0")
        async with self._sessionmaker() as session:
            rows, total = await repo.list_job_runs(
                session, limit=limit, offset=offset, kind=kind, status=status
            )
        return ListResponse[JobRunSummary](
            items=[repo.to_summary(row) for row in rows],
            page=PageInfo(total=total, limit=limit, offset=offset),
        )

    async def get_run(self, *, run_id: uuid.UUID) -> JobRunDetail:
        async with self._sessionmaker() as session:
            row = await repo.get_job_run(session, run_id)
        if row is None:
            raise JobRunNotFoundError()
        return repo.to_detail(row)
