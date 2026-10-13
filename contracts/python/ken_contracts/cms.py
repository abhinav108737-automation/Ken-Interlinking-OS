"""CMSProvider protocol and its typed payloads.

FROZEN CONTRACT. Mirrors TRD.md section 13.

This is the ONLY route by which content may ever be mutated. CLAUDE.md
constraints 10-12 are encoded structurally rather than left to convention:

* ``operation_id`` is mandatory on every mutation  -> idempotency
* ``expected_version`` is mandatory                -> version awareness,
  with a mismatch raising :class:`CMSVersionConflictError` (TRD.md's 409)
* ``dry_run`` exists from day zero                 -> validate before mutate
* :meth:`CMSProvider.rollback` is part of the protocol itself -> reversibility
  is not optional

``CMSPage.version`` is a deliberately OPAQUE string. No caller may parse,
compare or increment it, so a real CMS can move from an integer to an ETag
without breaking this contract.

Phase 0 ships only MockCMSProvider. Real HTML mutation is a Phase 6
deterministic service (TRD.md section 11) with its own tests; the Phase 0 mock
validates the PROTOCOL, not anchor insertion.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any, Literal, Protocol, runtime_checkable
from uuid import UUID

from pydantic import Field

from ken_contracts.common import AsyncClosable, ContractModel, OperationId
from ken_contracts.model import ProviderHealth


class LinkOperation(StrEnum):
    INSERT = "insert"
    REMOVE = "remove"
    REPLACE = "replace"


class CMSPage(ContractModel):
    """A page as the CMS reports it."""

    page_id: str
    url: str
    title: str
    body_html: str
    version: str = Field(description="Opaque version token. Never parse, compare or increment it.")
    canonical_url: str | None = None
    protected: bool = Field(
        default=False,
        description="When true, no mutation may target this page regardless of approval.",
    )
    published_at: datetime | None = None
    updated_at: datetime | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class LinkLocator(ContractModel):
    """Where in the body a change applies.

    PRD.md section 7.7 requires paragraph identity plus deterministic offsets
    or another stable selector. ``context_hash`` lets the deterministic layer
    detect that the surrounding text moved since the plan was made, which is
    the stale-content failure mode the eval suite covers.
    """

    block_selector: str = Field(description="Stable selector for the containing block.")
    occurrence_index: int = Field(
        default=0, ge=0, description="Which occurrence within the block, zero-based."
    )
    context_hash: str | None = Field(
        default=None, description="Hash of surrounding text, to detect drift."
    )


class LinkChange(ContractModel):
    """One atomic link mutation.

    Field semantics per operation -- stated explicitly so the Phase 6
    deterministic mutation service has one source of truth:

    ``INSERT``   ``href`` + ``anchor_text`` describe the new link. The
                 ``previous_*`` fields must be null.
    ``REMOVE``   ``locator`` identifies the existing anchor; ``previous_href``
                 and ``previous_anchor_text`` record what is being removed, so
                 the change is reversible from this payload alone.
    ``REPLACE``  ``previous_*`` describe the old link, ``href`` +
                 ``anchor_text`` the new one.

    ``target_page_id`` is the source of truth for the destination;
    ``href`` is its rendered URL and must resolve to the same page. The
    deterministic validator, not the caller, checks they agree.
    """

    change_id: UUID
    operation: LinkOperation
    target_page_id: str
    href: str
    anchor_text: str
    locator: LinkLocator
    previous_href: str | None = Field(
        default=None, description="For REMOVE/REPLACE: the link being removed or replaced."
    )
    previous_anchor_text: str | None = Field(
        default=None, description="For REMOVE/REPLACE: its anchor text."
    )


class LinkChangeRequest(ContractModel):
    """A mutation request. The shape is identical for validate and apply."""

    operation_id: OperationId = Field(
        description="Idempotency key. Replaying it must not apply the change twice."
    )
    source_page_id: str
    expected_version: str = Field(
        description="Optimistic lock. A mismatch raises CMSVersionConflictError."
    )
    changes: list[LinkChange] = Field(min_length=1)
    dry_run: bool = False
    actor: str = Field(description="Who authorised this, for the audit trail.")


class ValidationIssue(ContractModel):
    code: str
    severity: Literal["error", "warning"]
    message: str
    change_id: UUID | None = None


class ValidationResult(ContractModel):
    valid: bool
    issues: list[ValidationIssue] = Field(default_factory=list)
    normalized_changes: list[LinkChange] = Field(default_factory=list)


class DeploymentResult(ContractModel):
    """Outcome of an applied mutation, carrying its own rollback handle."""

    deployment_id: str
    operation_id: OperationId
    source_page_id: str
    status: Literal["applied", "no_op", "rejected"]
    applied_change_ids: list[UUID] = Field(default_factory=list)
    previous_version: str = Field(
        description=(
            "Page version before this deployment. Persisted so every applied "
            "mutation carries its own rollback metadata (AUTONOMY_AND_PERMISSIONS.md "
            "BLOCKs irreversible publication without it)."
        )
    )
    new_version: str
    applied_at: datetime
    idempotent_replay: bool = Field(
        default=False,
        description="True when this response replays an earlier identical operation_id.",
    )


class RollbackResult(ContractModel):
    deployment_id: str
    status: Literal["rolled_back", "no_op"]
    restored_version: str
    rolled_back_at: datetime
    actor: str = Field(description="Who authorised the rollback, for the audit trail.")


# --------------------------------------------------------------------------- #
# Errors
# --------------------------------------------------------------------------- #
class CMSError(Exception):
    """Base class for every CMS failure."""


class CMSNotFoundError(CMSError):
    """No such page or deployment."""


class CMSValidationError(CMSError):
    """The request was structurally valid but semantically rejected."""

    def __init__(self, message: str, *, issues: list[ValidationIssue]) -> None:
        super().__init__(message)
        self.issues = issues


class CMSVersionConflictError(CMSError):
    """TRD.md section 13's 409: the page moved under us.

    The caller must regenerate from current content rather than retry, because
    a plan built against a stale body may no longer be correct.
    """

    def __init__(self, message: str, *, expected: str, current: str) -> None:
        super().__init__(message)
        self.expected = expected
        self.current = current


class CMSUnavailableError(CMSError):
    """The CMS could not be reached."""


# --------------------------------------------------------------------------- #
# Protocol
# --------------------------------------------------------------------------- #
@runtime_checkable
class CMSProvider(AsyncClosable, Protocol):
    """The only sanctioned route to content mutation.

    No agent and no LLM may hold an implementation of this protocol directly;
    access is mediated by narrow, permission-aware, audited tools
    (CLAUDE.md "Agent Design Rule").
    """

    name: str

    async def get_page(self, *, page_id: str) -> CMSPage:
        """Fetch one page.

        Raises:
            CMSNotFoundError: no such page.
        """
        ...

    async def validate_link_changes(self, *, request: LinkChangeRequest) -> ValidationResult:
        """Check a mutation without applying it. Never mutates."""
        ...

    async def apply_link_changes(self, *, request: LinkChangeRequest) -> DeploymentResult:
        """Apply a mutation idempotently.

        Raises:
            CMSVersionConflictError: ``expected_version`` no longer matches.
            CMSValidationError: the change was rejected.
        """
        ...

    async def rollback(
        self, *, deployment_id: str, operation_id: OperationId, actor: str
    ) -> RollbackResult:
        """Revert a deployment.

        ``actor`` is mandatory: ROLLBACK_PRODUCTION is an ASK-level permission
        (AUTONOMY_AND_PERMISSIONS.md), so the authoriser must be recorded.

        Raises:
            CMSNotFoundError: no such deployment.
            CMSValidationError: ``operation_id`` does not match the deployment, or
                ``actor`` is blank.
            CMSVersionConflictError: the page has changed since this deployment, so
                rolling back would silently discard a later change. The caller must
                resolve that explicitly rather than retry.
        """
        ...

    async def health(self) -> ProviderHealth:
        """Report reachability without raising."""
        ...
