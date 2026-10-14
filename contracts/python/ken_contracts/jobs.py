"""Generic job runs (Phase 2 onward).

FROZEN CONTRACT.

Phase 1 recorded crawls and imports in ``ingestion_runs``, which stays as-is.
Every LATER batch job -- content analysis (Phase 2), recommendation generation
(3), agent runs that batch work (4), deployment verification (6), analytics
import (7), offline evaluation (8), autonomy shadow evaluation (9) -- records
itself as one ``job_runs`` row instead of each phase inventing its own table.

A run is auditable on its own: kind, status, timing, who triggered it, a
secret-free configuration snapshot, counters, and a safe failure reason. One
run of a given kind executes at a time (a PostgreSQL advisory lock per kind),
and every finished run emits ``job.completed``.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Protocol, runtime_checkable
from uuid import UUID

from pydantic import AwareDatetime, Field

from ken_contracts.common import FrozenModel
from ken_contracts.errors import ListResponse
from ken_contracts.inventory import RunStatus

SUBJECT_JOB_RUN = "job_run"
"""``subject_type`` of ``job.completed``; ``subject_id`` is ``str(run_id)``."""


class JobKind(StrEnum):
    """What a job run does. Adding a member is additive (one per phase)."""

    ANALYSIS = "analysis"
    """Phase 2: classification, summaries, embeddings, relationships, protections."""
    RECOMMENDATION = "recommendation"
    """Phase 3: candidate generation, ranking, placement."""
    AGENT = "agent"
    """Phase 4: a batch of supervised agent workflows."""
    VERIFICATION = "verification"
    """Phase 6: post-deployment verification sweep."""
    ANALYTICS_IMPORT = "analytics_import"
    """Phase 7: GA4 / GSC / first-party aggregation."""
    EVALUATION = "evaluation"
    """Phase 8: offline evaluation of a skill, policy or model."""
    AUTONOMY_SHADOW = "autonomy_shadow"
    """Phase 9: shadow-mode decision-class evaluation."""


class JobRunSummary(FrozenModel):
    id: UUID
    kind: JobKind
    status: RunStatus
    triggered_by: str = Field(description="Actor or component that started the run, e.g. 'cli'.")
    started_at: AwareDatetime
    completed_at: AwareDatetime | None = None
    counters: dict[str, int] = Field(
        default_factory=dict, description="Kind-specific counters; names are documented per kind."
    )


class JobRunDetail(JobRunSummary):
    config_snapshot: dict[str, Any] = Field(description="Secret-free run configuration.")
    failure_reason: str | None = Field(
        default=None, description="Safe summary when status is failed. Never exception text."
    )


class JobCompletedPayload(FrozenModel):
    """Payload of ``job.completed``: every finished job run, including failed ones."""

    run_id: UUID
    kind: JobKind
    status: RunStatus
    started_at: AwareDatetime
    completed_at: AwareDatetime
    counters: dict[str, int]


class JobRunNotFoundError(Exception):
    def __init__(self) -> None:
        super().__init__("job run not found")


@runtime_checkable
class JobReader(Protocol):
    async def list_runs(
        self,
        *,
        limit: int,
        offset: int,
        kind: JobKind | None = None,
        status: RunStatus | None = None,
    ) -> ListResponse[JobRunSummary]: ...

    async def get_run(self, *, run_id: UUID) -> JobRunDetail: ...
