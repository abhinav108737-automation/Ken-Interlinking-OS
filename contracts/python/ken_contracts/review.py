"""Human review of recommendations and editorial feedback (Phase 5, ADR-0013).

FROZEN CONTRACT (orchestrator).

PRD v1.0 section 7.9: a reviewer may approve, reject, edit the anchor, edit the destination
or edit the placement; rejections and edits require structured reason codes.

* **Every action is a deterministic service call** through the Phase 3 transition service,
  as ``user:<username>`` derived from the authenticated session (never from the body).
* **Idempotent.** Every request carries a client-generated ``operation_id``. A replay of the
  same operation returns the original result (``idempotent_replay = true``) and changes
  nothing; reusing an operation id for a DIFFERENT request is a 409.
* **Version-aware.** ``expected_status`` must equal the plan's current status (409
  otherwise), and a plan whose source changed so that it is STALE (not rebasable) cannot be
  approved (409 ``stale_page_version``): the engine regenerates it.
* **Auditable and reversible.** Every action appends one ``review_actions`` row (the
  editorial feedback record Phase 8 learns from) and emits ``link.approved`` /
  ``link.rejected`` (or ``link.proposed`` for an edited plan). An approval is reversible
  by a later reject (``approved -> rejected``) until deployment (Phase 6).
* **Two plan producers, one per purpose.** The engine produces plans; the review service
  produces ONLY the superseding plan of a human edit (ADR-0013 section 4, amending ADR-0012).
* **Edits never mutate a plan.** Editing the anchor/placement or replacing the destination
  SUPERSEDES the plan: the old one moves to ``superseded`` and a NEW plan
  (``anchor_source = "edited"``) is inserted in the same transaction, recorded by
  ``system:review-service`` (only system/agent actors may create plans) with the editing user
  in the transition note and the review action. The new anchor is ALWAYS a verbatim slice of
  the current block text and must pass the Phase 3 placement-safety and anchor rules; the
  new destination must pass the pair filters (eligible, unprotected, not already linked, not
  a duplicate open pair, not the source itself).
* **No auto-approval.** Nothing here approves without a human request.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Final, Protocol, Self, runtime_checkable
from uuid import UUID

from pydantic import AwareDatetime, Field, model_validator

from ken_contracts.auth import Principal
from ken_contracts.common import FrozenModel
from ken_contracts.errors import ListResponse
from ken_contracts.recommendations import LinkPlanStatus

REVIEW_SERVICE_ACTOR: Final = "system:review-service"
"""Records the initial ``proposed`` row of an EDITED plan (TRANSITION_AUTHORITY)."""
MAX_NOTE_CHARS: Final = 500
MAX_REASONS: Final = 5
MAX_BULK_PLANS: Final = 50

REVIEWABLE_STATUSES: Final = frozenset(
    {
        LinkPlanStatus.PROPOSED,
        LinkPlanStatus.CRITIQUED,
        LinkPlanStatus.POLICY_CHECKED,
        LinkPlanStatus.AWAITING_APPROVAL,
    }
)
"""A human may approve, reject or edit a plan in one of these (the human queue is
``awaiting_approval``; earlier statuses are reachable from the plan list)."""
REJECTABLE_STATUSES: Final = REVIEWABLE_STATUSES | {LinkPlanStatus.APPROVED}
"""``approved -> rejected`` reverses an approval before deployment."""
EDITABLE_STATUSES: Final = REVIEWABLE_STATUSES | {LinkPlanStatus.APPROVED}
"""``approved -> superseded`` exists: an approved plan may still be edited before deployment
(without a reject, which would start the cool-down)."""


class ReviewAction(StrEnum):
    APPROVE = "approve"
    REJECT = "reject"
    EDIT_ANCHOR = "edit_anchor"
    """New span in the same block (anchor text and/or placement)."""
    REPLACE_TARGET = "replace_target"
    """Same anchor span, a different destination page."""


class FeedbackReason(StrEnum):
    """Structured editorial reasons (PRD 7.9). Required for reject and edits."""

    GOOD_AS_IS = "good_as_is"
    IRRELEVANT_DESTINATION = "irrelevant_destination"
    BETTER_DESTINATION_EXISTS = "better_destination_exists"
    MISLEADING_ANCHOR = "misleading_anchor"
    UNNATURAL_ANCHOR = "unnatural_anchor"
    WRONG_PLACEMENT = "wrong_placement"
    OVER_LINKING = "over_linking"
    DUPLICATE_OF_EXISTING_LINK = "duplicate_of_existing_link"
    OUTDATED_OR_CHANGED_CONTENT = "outdated_or_changed_content"
    SENSITIVE_OR_PROTECTED = "sensitive_or_protected"
    READER_JOURNEY_MISMATCH = "reader_journey_mismatch"
    OTHER = "other"
    """Requires a note."""


APPROVE_REASONS: Final = frozenset({FeedbackReason.GOOD_AS_IS, FeedbackReason.OTHER})
DESTINATION_REASONS: Final = frozenset(
    {
        FeedbackReason.IRRELEVANT_DESTINATION,
        FeedbackReason.BETTER_DESTINATION_EXISTS,
        FeedbackReason.READER_JOURNEY_MISMATCH,
        FeedbackReason.DUPLICATE_OF_EXISTING_LINK,
        FeedbackReason.SENSITIVE_OR_PROTECTED,
        FeedbackReason.OTHER,
    }
)
ANCHOR_REASONS: Final = frozenset(
    {
        FeedbackReason.MISLEADING_ANCHOR,
        FeedbackReason.UNNATURAL_ANCHOR,
        FeedbackReason.WRONG_PLACEMENT,
        FeedbackReason.OVER_LINKING,
        FeedbackReason.OTHER,
    }
)
REASONS_BY_ACTION: Final = {
    ReviewAction.APPROVE: APPROVE_REASONS,
    ReviewAction.REJECT: frozenset(FeedbackReason) - {FeedbackReason.GOOD_AS_IS},
    ReviewAction.EDIT_ANCHOR: ANCHOR_REASONS,
    ReviewAction.REPLACE_TARGET: DESTINATION_REASONS,
}
"""Which structured reasons each action accepts (keeps the Phase 8 feedback data clean)."""


class _ReviewRequest(FrozenModel):
    operation_id: UUID = Field(description="Client-generated idempotency key.")
    expected_status: LinkPlanStatus
    reasons: list[FeedbackReason] = Field(default_factory=list, max_length=MAX_REASONS)
    note: str = Field(default="", max_length=MAX_NOTE_CHARS)

    @model_validator(mode="after")
    def _reasons_consistent(self) -> Self:
        if len(set(self.reasons)) != len(self.reasons):
            raise ValueError("reasons must be unique")
        if FeedbackReason.OTHER in self.reasons and not self.note.strip():
            raise ValueError("reason 'other' requires a note")
        if "\x00" in self.note:
            raise ValueError("note must not contain NUL")
        return self


class ApproveRequest(_ReviewRequest):
    @model_validator(mode="after")
    def _approve_reasons(self) -> Self:
        if not set(self.reasons) <= APPROVE_REASONS:
            raise ValueError("approve accepts only good_as_is / other")
        return self


class _RequiresReason(_ReviewRequest):
    @model_validator(mode="after")
    def _at_least_one_reason(self) -> Self:
        if not self.reasons:
            raise ValueError("at least one reason is required")
        if FeedbackReason.GOOD_AS_IS in self.reasons:
            raise ValueError("good_as_is is an approval reason")
        return self


class RejectRequest(_RequiresReason):
    pass


def _check_reasons(reasons: list[FeedbackReason], action: ReviewAction) -> None:
    if not set(reasons) <= REASONS_BY_ACTION[action]:
        raise ValueError(f"reasons not valid for {action.value}")


class EditAnchorRequest(_RequiresReason):
    """A new span ``[start, end)`` of the plan's block in the source's CURRENT version."""

    block_context_hash: str = Field(
        pattern=r"^[0-9a-f]{64}$",
        description="sha256 of the block text the reviewer saw (must still be current).",
    )
    start: int = Field(ge=0)
    end: int = Field(gt=0)
    approve: bool = Field(default=False, description="Approve the edited plan in the same step.")

    @model_validator(mode="after")
    def _span(self) -> Self:
        if self.end <= self.start:
            raise ValueError("end must be after start")
        _check_reasons(self.reasons, ReviewAction.EDIT_ANCHOR)
        return self


class ReplaceTargetRequest(_RequiresReason):
    target_page_id: UUID
    approve: bool = Field(default=False, description="Approve the edited plan in the same step.")

    @model_validator(mode="after")
    def _destination_reasons(self) -> Self:
        _check_reasons(self.reasons, ReviewAction.REPLACE_TARGET)
        return self


class BulkAction(StrEnum):
    APPROVE = "approve"
    REJECT = "reject"


class BulkReviewRequest(FrozenModel):
    """Bulk review where safe: only ``awaiting_approval`` plans that are not stale; every
    plan is its own transaction; edits are never bulk."""

    operation_id: UUID
    action: BulkAction
    plan_ids: list[UUID] = Field(min_length=1, max_length=MAX_BULK_PLANS)
    reasons: list[FeedbackReason] = Field(default_factory=list, max_length=MAX_REASONS)
    note: str = Field(default="", max_length=MAX_NOTE_CHARS)

    @model_validator(mode="after")
    def _valid(self) -> Self:
        if len(set(self.plan_ids)) != len(self.plan_ids):
            raise ValueError("plan_ids must be unique")
        if self.action is BulkAction.REJECT and not self.reasons:
            raise ValueError("a bulk reject requires at least one reason")
        allowed = REASONS_BY_ACTION[
            ReviewAction.APPROVE if self.action is BulkAction.APPROVE else ReviewAction.REJECT
        ]
        if not set(self.reasons) <= allowed:
            raise ValueError(f"reasons not valid for bulk {self.action.value}")
        if FeedbackReason.OTHER in self.reasons and not self.note.strip():
            raise ValueError("reason 'other' requires a note")
        if "\x00" in self.note:
            raise ValueError("note must not contain NUL")
        return self


class ReviewResult(FrozenModel):
    action_id: int
    operation_id: UUID
    action: ReviewAction
    plan_id: UUID
    status: LinkPlanStatus = Field(description="The reviewed plan's status afterwards.")
    new_plan_id: UUID | None = Field(default=None, description="The superseding plan (edits).")
    new_plan_status: LinkPlanStatus | None = None
    idempotent_replay: bool = False


class BulkItemResult(FrozenModel):
    plan_id: UUID
    ok: bool
    status: LinkPlanStatus | None = None
    error_code: str | None = Field(
        default=None, description="An ErrorCode value when not ok (e.g. conflict)."
    )


class BulkReviewResult(FrozenModel):
    operation_id: UUID
    action: BulkAction
    items: list[BulkItemResult]
    idempotent_replay: bool = False


class ReviewActionView(FrozenModel):
    """One ``review_actions`` row: the audit AND the editorial-feedback record."""

    id: int
    operation_id: UUID
    action: ReviewAction
    actor: str
    plan_id: UUID
    new_plan_id: UUID | None = None
    new_plan_status: LinkPlanStatus | None = None
    from_status: LinkPlanStatus
    to_status: LinkPlanStatus
    reasons: list[FeedbackReason]
    note: str
    anchor_before: str
    anchor_after: str | None = None
    target_before: UUID
    target_after: UUID | None = None
    bulk_operation_id: UUID | None = None
    created_at: AwareDatetime


class ReviewError(Exception):
    """Base of typed review refusals; ``code`` is an ErrorCode value."""

    code: str = "conflict"


class PlanNotReviewableError(ReviewError):
    """Wrong status for the action, or ``expected_status`` mismatch (409)."""

    code = "conflict"


class PlanStaleError(ReviewError):
    """The source changed and the plan is stale (409 ``stale_page_version``)."""

    code = "stale_page_version"


class InvalidEditError(ReviewError):
    """The new span / target breaks a placement, anchor or pair rule (422)."""

    code = "validation_failed"


class OperationConflictError(ReviewError):
    """``operation_id`` was already used for a DIFFERENT request (409)."""

    code = "conflict"


@runtime_checkable
class ReviewService(Protocol):
    async def approve(
        self, *, plan_id: UUID, request: ApproveRequest, principal: Principal
    ) -> ReviewResult: ...

    async def reject(
        self, *, plan_id: UUID, request: RejectRequest, principal: Principal
    ) -> ReviewResult: ...

    async def edit_anchor(
        self, *, plan_id: UUID, request: EditAnchorRequest, principal: Principal
    ) -> ReviewResult: ...

    async def replace_target(
        self, *, plan_id: UUID, request: ReplaceTargetRequest, principal: Principal
    ) -> ReviewResult: ...

    async def bulk(
        self, *, request: BulkReviewRequest, principal: Principal
    ) -> BulkReviewResult: ...


@runtime_checkable
class ReviewReader(Protocol):
    async def actions_for_plan(self, *, plan_id: UUID) -> list[ReviewActionView]: ...

    async def list_actions(
        self,
        *,
        limit: int,
        offset: int,
        action: ReviewAction | None = None,
        actor: str | None = None,
    ) -> ListResponse[ReviewActionView]: ...
