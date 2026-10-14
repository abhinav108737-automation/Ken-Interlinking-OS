"""The interface between the agent runtime (``ken_agents``) and the services (Phase 4).

FROZEN (orchestrator, ADR-0012). ``ken_agents`` depends on these protocols only; the
implementations live in ``ken_services.agents`` and own every database session. Nodes never
see a session, an engine, credentials, a filesystem, a shell or a CMS client: they see a
``ToolBox`` built over :class:`AgentBackend`, and the runner records through
:class:`AgentRunStore`.

Rules every implementation must keep:

* Agents never INSERT plans: the engine (``make recommend``) is the only producer.
  ``list_proposed_plans`` returns the source's ``proposed`` plans that no agent has reviewed
  yet (no ``agent_run_plans`` row), in engine order (total score desc, created_at, id).
* ``check_plan`` re-checks deterministic invariants of one plan against LIVE state.
* ``hand_off`` is the policy gate, ONE transaction for the whole run: for each review it
  appends the ``agent_run_plans`` row and, if the verdict is ``accept`` or ``uncertain`` and
  the plan is still ``proposed``, moves it ``proposed -> critiqued`` (``CRITIC_ACTOR``) ->
  ``policy_checked`` -> ``awaiting_approval`` (``POLICY_GATE_ACTOR``) through the Phase 3
  transition service with ``expected_from``. Each plan runs under a SAVEPOINT: a plan that
  is no longer ``proposed`` (a concurrent ``make recommend`` expired it), already reviewed,
  or fails an invariant re-checked under its row lock is reported in ``conflicts`` and gets
  no row; the rest commit. Actors are bound inside the service; no
  caller chooses one. There is no edge to ``approved``.
* Every method is bounded and never returns secrets.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol, runtime_checkable
from uuid import UUID

from ken_contracts.agents import (
    AgentErrorCode,
    AgentOutcome,
    AgentRunStatus,
    AgentStepView,
    AgentTrigger,
    CriticReason,
    CriticVerdict,
    FallbackReason,
    TriageDecision,
)
from ken_contracts.intelligence import LinkEligibility, PageAnalysis
from ken_contracts.recommendations import LinkPlan

__all__ = [
    "AgentBackend",
    "AgentRunStore",
    "HandOffResult",
    "NewAgentRun",
    "PageSnapshot",
    "PlanCheck",
    "PlanReviewInput",
]


@dataclass(frozen=True, slots=True)
class PageSnapshot:
    id: UUID
    canonical_url: str
    title: str | None
    current_version: int
    publication_state: str
    gone: bool
    word_count: int
    internal_link_count: int


@dataclass(frozen=True, slots=True)
class PlanCheck:
    """``check_plan``: deterministic invariants re-checked right before the hand-off."""

    ok: bool
    problems: tuple[str, ...] = ()
    """Stable codes: ``not_proposed``, ``source_version_changed`` (stale, not rebasable),
    ``anchor_not_verbatim``, ``source_ineligible``, ``target_ineligible``, ``already_linked``."""


@dataclass(frozen=True, slots=True)
class PlanReviewInput:
    plan_id: UUID
    verdict: CriticVerdict
    reasons: tuple[CriticReason, ...] = ()
    explanation: str = ""
    fallback_reason: FallbackReason | None = None
    model: str | None = None
    """Set only when a model actually answered for this plan."""


@dataclass(frozen=True, slots=True)
class HandOffResult:
    handed_off: tuple[UUID, ...]
    """Now ``awaiting_approval``."""
    recorded: tuple[UUID, ...]
    """Every plan that got its ``agent_run_plans`` row (handed off or not)."""
    conflicts: Mapping[UUID, str] = field(default_factory=dict)
    """plan id -> stable code: nothing was written for it. ``not_proposed`` (a concurrent
    status change), ``already_reviewed`` (another run reviewed it first), or the first
    ``PlanCheck`` problem found when the invariants are RE-CHECKED under the plan's row lock
    inside the hand-off transaction (closing the window after ``check_plan``)."""


@runtime_checkable
class AgentBackend(Protocol):
    """What tools may do: five READ methods and the one REVIEW method."""

    async def get_page(self, *, page_id: UUID) -> PageSnapshot | None: ...

    async def get_analysis(self, *, page_id: UUID) -> PageAnalysis | None:
        """The CURRENT version's analysis at the CURRENT analysis version, else ``None``."""
        ...

    async def get_eligibility(self, *, page_id: UUID) -> LinkEligibility | None: ...

    async def list_proposed_plans(self, *, source_page_id: UUID, limit: int) -> list[LinkPlan]:
        """Unreviewed ``proposed`` plans of the source, engine order, at most ``limit``."""
        ...

    async def check_plan(self, *, plan_id: UUID) -> PlanCheck: ...

    async def hand_off(
        self, *, agent_run_id: UUID, reviews: Sequence[PlanReviewInput], reviewed_at: datetime
    ) -> HandOffResult: ...


@dataclass(frozen=True, slots=True)
class NewAgentRun:
    id: UUID
    job_run_id: UUID
    source_page_id: UUID
    source_url: str
    trigger: AgentTrigger
    correlation_id: UUID
    graph_version: str
    config_snapshot: Mapping[str, object]
    started_at: datetime

    @property
    def trace_id(self) -> str:
        return self.correlation_id.hex


@runtime_checkable
class AgentRunStore(Protocol):
    """Run/step persistence for the runner (never handed to nodes)."""

    async def sources_to_review(self, *, limit: int) -> list[UUID]:
        """Source pages that have >= 1 unreviewed ``proposed`` plan and no ``running`` agent
        run, ordered by canonical URL then id (deterministic), at most ``limit``."""
        ...

    async def reap_orphans(self, *, now: datetime) -> int:
        """Mark ``running`` runs whose job run has finished as failed/orphaned."""
        ...

    async def start_run(self, *, run: NewAgentRun) -> None:
        """Raises if the source already has a ``running`` run (partial unique index)."""
        ...

    async def append_step(self, *, step: AgentStepView) -> None:
        """Append-only; ``(run_id, seq)`` is unique; payloads must fit MAX_STEP_OUTPUT_BYTES."""
        ...

    async def finish_run(
        self,
        *,
        run_id: UUID,
        status: AgentRunStatus,
        outcome: AgentOutcome,
        finished_at: datetime,
        triage_decision: TriageDecision | None = None,
        source_page_version: int | None = None,
        review_notes: Sequence[str] = (),
        error_code: AgentErrorCode | None = None,
    ) -> None:
        """Exactly once per run (the row is frozen afterwards)."""
        ...
