"""Controlled deployment of approved plans (Phase 6, ADR-0014). FROZEN CONTRACT (orchestrator).

One deployment = up to :data:`MAX_PLANS_PER_DEPLOYMENT` APPROVED plans of ONE source page,
applied in ONE CMS request:

    validate -> permission (``deploy``) -> dry-run -> expected-version -> apply -> verify
        -> events/audit -> (rollback)

Idempotent (client ``operation_id`` + request fingerprint, as ADR-0013), version-aware
(the CMS page's opaque version is the optimistic lock), auditable (append-only items and
verification runs, ``link.*`` events) and reversible (``rollback``). No automatic deploy:
only an operator request reaches :class:`DeploymentService`.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Final, Protocol, Self, runtime_checkable
from uuid import UUID

from pydantic import AwareDatetime, Field, model_validator

from ken_contracts.auth import Principal
from ken_contracts.common import FrozenModel, reject_unstorable_text
from ken_contracts.errors import ListResponse
from ken_contracts.recommendations import LinkPlanStatus

DEPLOYMENT_ACTOR: Final = "system:deployment-service"
"""Moves plans approved -> validated -> deployed -> verified | failed | rolled_back."""
MAX_PLANS_PER_DEPLOYMENT: Final = 20
MAX_NOTE_CHARS: Final = 500
SNIPPET_CHARS: Final = 400


class DeploymentStatus(StrEnum):
    APPLIED = "applied"
    """The CMS accepted the change; verification pending or not yet run."""
    VERIFIED = "verified"
    VERIFICATION_FAILED = "verification_failed"
    FAILED = "failed"
    """The CMS refused the change (validation), or the apply errored: nothing applied."""
    CONFLICT = "conflict"
    """``expected_version`` mismatch: nothing applied; the plans were expired."""
    ROLLED_BACK = "rolled_back"


class DeploymentIssueCode(StrEnum):
    """Why a plan (or the whole request) cannot be deployed. Fixed vocabulary."""

    PLAN_NOT_APPROVED = "plan_not_approved"
    PLAN_STALE = "plan_stale"
    PLAN_INVALID = "plan_invalid"
    """Durable invalidity (protected, ineligible, already linked) -- ``validity.py``."""
    MIXED_SOURCES = "mixed_sources"
    CMS_PAGE_NOT_FOUND = "cms_page_not_found"
    CMS_PAGE_PROTECTED = "cms_page_protected"
    CMS_UNAVAILABLE = "cms_unavailable"
    CMS_VALIDATION_FAILED = "cms_validation_failed"
    HREF_INVALID = "href_invalid"
    BROKEN_TARGET = "broken_target"
    BLOCK_NOT_FOUND = "block_not_found"
    AMBIGUOUS_BLOCK = "ambiguous_block"
    ANCHOR_NOT_FOUND = "anchor_not_found"
    ANCHOR_NOT_IN_SINGLE_TEXT_NODE = "anchor_not_in_single_text_node"
    UNMAPPABLE_SPAN = "unmappable_span"
    UNSAFE_PLACEMENT = "unsafe_placement"
    """Excluded ancestor, inside an existing link, hidden ancestor."""
    OVERLAPPING_CHANGES = "overlapping_changes"
    ALREADY_APPLIED = "already_applied"
    CONFLICTING_LINK = "conflicting_link"
    SELF_CHECK_FAILED = "self_check_failed"


class DeploymentIssue(FrozenModel):
    code: DeploymentIssueCode
    plan_id: UUID | None = None


class VerificationCheckName(StrEnum):
    LINK_PRESENT_ONCE = "link_present_once"
    HREF_MATCHES = "href_matches"
    ANCHOR_MATCHES = "anchor_matches"
    IN_EXPECTED_BLOCK = "in_expected_block"
    NOT_HIDDEN = "not_hidden"
    NOT_NESTED = "not_nested"
    TARGET_REACHABLE = "target_reachable"
    """Only when HTTP verification is configured; otherwise ``skipped``."""


class VerificationCheck(FrozenModel):
    plan_id: UUID
    check: VerificationCheckName
    outcome: str = Field(pattern=r"^(passed|failed|skipped)$")
    detail: str | None = Field(default=None, max_length=200, description="Fixed-vocabulary code.")


class ChangePreview(FrozenModel):
    """Dry-run preview of one plan. Snippets are TEXT for display, never rendered as HTML."""

    plan_id: UUID
    anchor_text: str
    href: str
    block_text: str = Field(
        description="The block's text; the UI marks [anchor_start, anchor_end)."
    )
    anchor_start: int = Field(ge=0)
    anchor_end: int = Field(gt=0)
    html_before: str = Field(max_length=SNIPPET_CHARS * 2 + 200)
    html_after: str = Field(max_length=SNIPPET_CHARS * 2 + 400)


class _DeployBase(FrozenModel):
    operation_id: UUID
    plan_ids: list[UUID] = Field(min_length=1, max_length=MAX_PLANS_PER_DEPLOYMENT)
    note: str = Field(default="", max_length=MAX_NOTE_CHARS)

    @model_validator(mode="after")
    def _valid(self) -> Self:
        if len(set(self.plan_ids)) != len(self.plan_ids):
            raise ValueError("plan_ids must be unique")
        reject_unstorable_text(self.note)
        return self


class DryRunRequest(_DeployBase):
    pass


class DeployRequest(_DeployBase):
    pass


class DryRunResult(FrozenModel):
    source_page_id: UUID | None = None
    cms_page_id: str | None = None
    cms_version: str | None = None
    valid: bool
    changes: list[ChangePreview]
    issues: list[DeploymentIssue]


class RollbackReason(StrEnum):
    VERIFICATION_FAILED = "verification_failed"
    EDITORIAL_REVERSAL = "editorial_reversal"
    WRONG_DESTINATION = "wrong_destination"
    BROKEN_PAGE = "broken_page"
    OTHER = "other"
    """Requires a note."""


class RollbackRequest(FrozenModel):
    operation_id: UUID
    reason: RollbackReason
    note: str = Field(default="", max_length=MAX_NOTE_CHARS)

    @model_validator(mode="after")
    def _valid(self) -> Self:
        if self.reason is RollbackReason.OTHER and not self.note.strip():
            raise ValueError("reason 'other' requires a note")
        reject_unstorable_text(self.note)
        return self


class DeploymentItemView(FrozenModel):
    plan_id: UUID
    plan_status: LinkPlanStatus
    anchor_text: str
    target_url: str


class DeploymentView(FrozenModel):
    id: UUID
    operation_id: UUID
    source_page_id: UUID
    source_url: str
    cms_provider: str
    cms_page_id: str
    status: DeploymentStatus
    plan_ids: list[UUID]
    previous_version: str | None = None
    new_version: str | None = None
    actor: str
    error_code: str | None = None
    created_at: AwareDatetime
    applied_at: AwareDatetime | None = None
    verified_at: AwareDatetime | None = None
    rolled_back_at: AwareDatetime | None = None
    idempotent_replay: bool = False


class VerificationRun(FrozenModel):
    id: int
    run_at: AwareDatetime
    passed: bool
    http_checked: bool
    checks: list[VerificationCheck]


class DeploymentDetail(DeploymentView):
    items: list[DeploymentItemView]
    preview: list[ChangePreview]
    verifications: list[VerificationRun]
    rollback_reason: RollbackReason | None = None
    rollback_note: str | None = None


class DeploymentNotFoundError(LookupError):
    pass


class DeploymentError(Exception):
    """Base of typed refusals. ``code`` is an ErrorCode value; ``issues`` explain why."""

    code: str = "conflict"

    def __init__(self, issues: list[DeploymentIssue] | None = None) -> None:
        super().__init__(self.code)
        self.issues = issues or []


class DeploymentRefusedError(DeploymentError):
    """Validation found issues (409): nothing was written to the CMS."""

    code = "conflict"


class DeploymentVersionConflictError(DeploymentError):
    """The CMS page changed (409 ``cms_version_conflict``): plans were expired."""

    code = "cms_version_conflict"


class CMSUnavailableDeploymentError(DeploymentError):
    code = "cms_unavailable"


@runtime_checkable
class DeploymentService(Protocol):
    async def dry_run(self, *, request: DryRunRequest, principal: Principal) -> DryRunResult: ...

    async def deploy(self, *, request: DeployRequest, principal: Principal) -> DeploymentDetail: ...

    async def verify(self, *, deployment_id: UUID, principal: Principal) -> DeploymentDetail: ...

    async def rollback(
        self, *, deployment_id: UUID, request: RollbackRequest, principal: Principal
    ) -> DeploymentDetail: ...


@runtime_checkable
class DeploymentReader(Protocol):
    async def list_deployments(
        self,
        *,
        limit: int,
        offset: int,
        status: DeploymentStatus | None = None,
        source_page_id: UUID | None = None,
    ) -> ListResponse[DeploymentView]: ...

    async def get_deployment(self, *, deployment_id: UUID) -> DeploymentDetail: ...

    async def deployments_for_plan(self, *, plan_id: UUID) -> list[DeploymentView]: ...
