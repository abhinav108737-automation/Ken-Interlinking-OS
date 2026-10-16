"""Control-plane read views (Phase 5, ADR-0013 section 7). FROZEN CONTRACT (orchestrator).

* ``GET /dashboard`` (``read``): one summary for the reviewer's home screen.
* ``GET /events`` (``read_audit``): the append-only event log, newest first, paged.
* ``GET /settings/policies`` (``read_settings``): the active, validated policy documents.

All are read-only. Nothing here exposes a secret (settings views go through the Phase 0
public-settings allowlist; policy documents are versioned data files from the repository).
"""

from __future__ import annotations

from typing import Any, Literal
from uuid import UUID

from pydantic import AwareDatetime, Field

from ken_contracts.common import FrozenModel
from ken_contracts.events import EventName
from ken_contracts.jobs import JobRunSummary
from ken_contracts.review import ReviewActionView


class DashboardSummary(FrozenModel):
    pages_total: int = Field(ge=0)
    pages_live: int = Field(ge=0, description="Not gone.")
    pages_analyzed: int = Field(ge=0, description="Current version has a current analysis.")
    pages_protected: int = Field(ge=0)
    plans_by_status: dict[str, int]
    awaiting_approval: int = Field(ge=0)
    opportunities_by_kind: dict[str, int]
    agent_runs_by_outcome: dict[str, int] = Field(description="All time, by outcome.")
    reviews_by_action: dict[str, int] = Field(description="All time, by review action.")
    recent_review_actions: list[ReviewActionView] = Field(max_length=10)
    latest_jobs: list[JobRunSummary] = Field(description="The latest run of each job kind.")
    auto_approve_enabled: Literal[False] = Field(
        default=False, description="Always false in v1 (TRD section 20)."
    )


class EventRecord(FrozenModel):
    """One stored event (``events`` row), for the audit log."""

    id: int
    event_id: UUID
    event_name: EventName
    event_version: int
    occurred_at: AwareDatetime
    emitted_by: str
    subject_type: str | None = None
    subject_id: str | None = None
    payload: dict[str, Any]
    correlation_id: UUID | None = None
    payload_status: Literal["ok", "unknown_version", "invalid"] = Field(
        default="ok",
        description=(
            "Phase 8, additive (ADR-0016 section 8): the verdict of "
            "``ken_contracts.events.validate_payload`` for THIS row's ``event_version``. "
            "``unknown_version``: no model is registered for that version (or the stored name "
            "is outside the vocabulary) and the payload is returned verbatim. ``invalid``: a "
            "model exists and the payload does not satisfy it. A stored row is never "
            "rewritten, so one bad row no longer makes the audit log unreadable."
        ),
    )


PolicyKind = Literal[
    "taxonomy", "recommendation", "ranking", "agents", "analytics", "learning", "autonomy"
]
"""The active policy documents ``GET /settings/policies`` serves. ``learning`` is Phase 8's
additive member (ADR-0016 section 10, the three-place edit ADR-0015 section 12.9.10 records).

A policy document is reserved to ``operator_admin`` through ``read_settings`` (ADR-0013
section 7), which is also why a learning PROPOSAL's candidate document is served behind
``read_settings`` and never behind ``read``."""


class PolicyDocument(FrozenModel):
    kind: PolicyKind
    version: int | str
    path: str = Field(description="Repository-relative path of the active file.")
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    content: dict[str, Any] = Field(description="The validated document (model dump).")


class PoliciesView(FrozenModel):
    policies: list[PolicyDocument]
    auto_approve_enabled: Literal[False] = False
