"""Typed supervisor state (ADR-0012 sections 3 and 4).

``AgentState`` is the LangGraph state schema. Its fields map TRD section 8 as follows:

=====================  ============================================================
TRD section 8          here
=====================  ============================================================
``task_id``            ``run_id``
``source_page_id``     ``source_page_id``
``trigger``            ``trigger``
``source_page``        ``source_page`` (the snapshot)
``classification``     ``classification`` (the Phase 2 analysis)
``candidates``         ``candidates`` (the unreviewed proposed plans, engine order)
``ranked_candidates``  ``ranked_candidates`` (the planner's selection, plan ids)
``link_plans``         ``link_plans`` (ids of the plans handed off)
``critic_result``      ``critic_result`` (one :class:`PlanReview` per chosen plan)
``risk_level``         ``risk_level`` (deterministic)
``approval_required``  always ``True`` with :data:`APPROVAL_REASON`
``errors``             ``errors`` (typed codes, never exception text)
=====================  ============================================================

``approval_result``, ``deployment_result`` and ``verification_result`` do not apply until
Phases 5-6.

State integrity: LangGraph silently DROPS a key a node returns that the schema does not
declare. Every node therefore returns one of the partial-update models below, and
:func:`validate_update` (applied by the graph's node wrapper) rejects anything else, then
dumps it with ``exclude_unset`` (shallow, so nested models stay typed). The final state is
re-validated with ``AgentState.model_validate``.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from enum import StrEnum
from typing import Final, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from ken_agents.critic import critic_outcome
from ken_contracts.agents import (
    MAX_EXPLANATION_CHARS,
    MAX_REVIEW_NOTE_CHARS,
    MAX_REVIEW_NOTES,
    AgentErrorCode,
    AgentOutcome,
    AgentRunStatus,
    AgentTrigger,
    CriticReason,
    CriticVerdict,
    FallbackReason,
    PlanRef,
    TriageDecision,
)
from ken_contracts.autonomy import CriticOutcome
from ken_contracts.common import FrozenModel
from ken_contracts.intelligence import PageAnalysis
from ken_contracts.recommendations import LinkPlan
from ken_services.agents.protocol import PageSnapshot

__all__ = [
    "APPROVAL_REASON",
    "AgentState",
    "CheckInvariantsUpdate",
    "CritiqueUpdate",
    "DiscoverUpdate",
    "FailureUpdate",
    "FinalizeUpdate",
    "LoadPageUpdate",
    "NodeUpdate",
    "PlanReview",
    "PlanUpdate",
    "PolicyGateUpdate",
    "RiskLevel",
    "StateContractError",
    "TriageUpdate",
    "UnderstandUpdate",
    "add_notes",
    "validate_update",
]

APPROVAL_REASON: Final = "AUTO_APPROVE disabled (TRD §20)"


class RiskLevel(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class StateContractError(RuntimeError):
    """A node returned something other than its declared partial-update model."""


class PlanReview(FrozenModel):
    """The critic's (or the rule's) assessment of one chosen plan."""

    plan_id: UUID
    ref: PlanRef
    verdict: CriticVerdict
    reasons: tuple[CriticReason, ...] = ()
    explanation: str = Field(default="", max_length=MAX_EXPLANATION_CHARS)
    confidence: float = Field(ge=0.0, le=1.0, description="The plan's RAW confidence.")
    fallback_reason: FallbackReason | None = None
    model: str | None = Field(default=None, description="Set only when a model answered.")
    guard: str | None = Field(
        default=None, max_length=64, description="The deterministic guard that decided, if any."
    )

    @property
    def outcome(self) -> CriticOutcome:
        """The autonomy-level outcome: a pure function of the verdict (never more permissive)."""
        return critic_outcome(self.verdict)


class AgentState(BaseModel):
    """The whole supervisor state of one run (one source page)."""

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    run_id: UUID = Field(description="TRD section 8 task_id.")
    source_page_id: UUID
    trigger: AgentTrigger
    source_page: PageSnapshot | None = None
    triage_decision: TriageDecision | None = None
    classification: PageAnalysis | None = None
    candidates: list[LinkPlan] = Field(default_factory=list)
    ranked_candidates: list[UUID] = Field(default_factory=list)
    critic_result: list[PlanReview] = Field(default_factory=list)
    invariant_problems: dict[UUID, tuple[str, ...]] = Field(default_factory=dict)
    link_plans: list[UUID] = Field(default_factory=list, description="Handed off.")
    recorded_plans: list[UUID] = Field(default_factory=list, description="Got a review row.")
    conflicts: dict[UUID, str] = Field(default_factory=dict)
    risk_level: RiskLevel | None = None
    approval_required: Literal[True] = True
    approval_reason: Literal["AUTO_APPROVE disabled (TRD §20)"] = APPROVAL_REASON
    errors: list[AgentErrorCode] = Field(default_factory=list)
    error_code: AgentErrorCode | None = None
    outcome: AgentOutcome | None = None
    status: AgentRunStatus | None = None
    review_notes: list[str] = Field(default_factory=list, max_length=MAX_REVIEW_NOTES)
    step_seq: int = Field(default=0, ge=0, description="Steps executed so far (next seq).")


# --------------------------------------------------------------------------- #
# Partial updates: one model per node; anything undeclared is a contract error
# --------------------------------------------------------------------------- #
class NodeUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class LoadPageUpdate(NodeUpdate):
    source_page: PageSnapshot | None = None
    errors: list[AgentErrorCode] | None = None


class TriageUpdate(NodeUpdate):
    triage_decision: TriageDecision
    review_notes: list[str] | None = Field(default=None, max_length=MAX_REVIEW_NOTES)


class UnderstandUpdate(NodeUpdate):
    classification: PageAnalysis | None = None


class DiscoverUpdate(NodeUpdate):
    candidates: list[LinkPlan]


class PlanUpdate(NodeUpdate):
    ranked_candidates: list[UUID]


class CritiqueUpdate(NodeUpdate):
    critic_result: list[PlanReview]


class CheckInvariantsUpdate(NodeUpdate):
    invariant_problems: dict[UUID, tuple[str, ...]]
    review_notes: list[str] | None = Field(default=None, max_length=MAX_REVIEW_NOTES)


class PolicyGateUpdate(NodeUpdate):
    link_plans: list[UUID]
    recorded_plans: list[UUID]
    conflicts: dict[UUID, str]
    review_notes: list[str] | None = Field(default=None, max_length=MAX_REVIEW_NOTES)


class FinalizeUpdate(NodeUpdate):
    outcome: AgentOutcome
    status: AgentRunStatus
    risk_level: RiskLevel | None = None
    error_code: AgentErrorCode | None = None


class FailureUpdate(NodeUpdate):
    """Written by the node wrapper when a node fails recoverably (tool failure, timeout)."""

    errors: list[AgentErrorCode]


def validate_update(
    model: type[NodeUpdate], returned: BaseModel | Mapping[str, object]
) -> dict[str, object]:
    """Validate a node's return against its declared update model; dump ``exclude_unset``.

    Raises:
        StateContractError: another model type, an undeclared key or an invalid value.
    """
    if isinstance(returned, BaseModel):
        if type(returned) is not model:
            raise StateContractError(
                f"node returned {type(returned).__name__}, expected {model.__name__}"
            )
        update = returned
    else:
        try:
            update = model.model_validate(dict(returned))
        except ValidationError as exc:
            fields = sorted({".".join(str(p) for p in err["loc"]) for err in exc.errors()})
            raise StateContractError(
                f"node update violates {model.__name__}: {', '.join(fields)}"
            ) from None
    # ``exclude_unset``, shallow: nested models stay typed objects (no dict round trip).
    return {name: getattr(update, name) for name in update.model_fields_set}


def add_notes(existing: Iterable[str], new: Iterable[str]) -> list[str]:
    """Append review notes, each trimmed to MAX_REVIEW_NOTE_CHARS, at most MAX_REVIEW_NOTES."""
    notes = list(existing)
    for note in new:
        text = " ".join(note.split())
        if len(text) > MAX_REVIEW_NOTE_CHARS:
            text = text[: MAX_REVIEW_NOTE_CHARS - 3] + "..."
        if text and text not in notes:
            notes.append(text)
    return notes[:MAX_REVIEW_NOTES]
