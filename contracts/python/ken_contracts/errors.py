"""API error and collection envelopes.

FROZEN CONTRACT.

MULTI_AGENT_ORCHESTRATION.md freezes "API payload formats" before parallel
work. These two envelopes exist from Phase 0 because the backend and the
frontend would otherwise each invent an incompatible shape the moment Phase 1
adds its first non-health endpoint.

Phase 0 itself exposes only GET /health, /ready and /meta, none of which can
fail with a domain error -- so these models are frozen but barely exercised.
That is intentional: the cost of agreeing on them now is one file.
"""

from __future__ import annotations

from collections.abc import Sequence
from enum import StrEnum
from typing import Self

from pydantic import Field, computed_field

from ken_contracts.common import ContractModel


class ErrorCode(StrEnum):
    """Stable machine-readable error codes.

    Codes are ``snake_case``. A client may branch on a code but must never
    branch on ``message``, which is human-readable and may be reworded at any
    time. Adding a member in a later phase is additive and non-breaking.
    """

    # Generic
    VALIDATION_FAILED = "validation_failed"
    NOT_FOUND = "not_found"
    CONFLICT = "conflict"
    INTERNAL_ERROR = "internal_error"
    SERVICE_UNAVAILABLE = "service_unavailable"

    # Domain-specific (consumed from Phase 1 onward)
    PAGE_NOT_FOUND = "page_not_found"
    STALE_PAGE_VERSION = "stale_page_version"
    PROTECTED_PAGE = "protected_page"
    CMS_VERSION_CONFLICT = "cms_version_conflict"
    CMS_UNAVAILABLE = "cms_unavailable"
    MODEL_UNAVAILABLE = "model_unavailable"
    MODEL_SCHEMA_VIOLATION = "model_schema_violation"
    APPROVAL_REQUIRED = "approval_required"
    PERMISSION_DENIED = "permission_denied"


class ErrorDetail(ContractModel):
    """One field-level or item-level problem within a failed request."""

    location: str | None = Field(
        default=None, description="Dotted path to the offending field, when applicable."
    )
    message: str
    code: str | None = None


class ErrorResponse(ContractModel):
    """The ONLY error body any endpoint returns.

    Deliberately does not carry a stack trace, an exception class name or a
    database message: backend-engineer.md forbids exposing internal
    exceptions. ``request_id`` is how a caller correlates a failure with the
    server logs instead.
    """

    code: ErrorCode = Field(description="Stable machine-readable code.")
    message: str = Field(description="Human-readable summary. Never branch on this.")
    details: list[ErrorDetail] = Field(default_factory=list)
    request_id: str | None = Field(
        default=None, description="Correlates this failure with server logs."
    )

    @classmethod
    def of(
        cls,
        code: ErrorCode,
        message: str,
        *,
        request_id: str | None = None,
        details: Sequence[ErrorDetail] = (),
    ) -> Self:
        """Convenience constructor used by exception handlers.

        Takes already-constructed ``ErrorDetail`` objects only. It deliberately
        does NOT accept arbitrary values to stringify: exception handlers have
        exception objects in scope, and ``str(exc)`` routinely contains
        connection strings, SQL or credentials.
        """
        return cls(code=code, message=message, details=list(details), request_id=request_id)


class PageInfo(ContractModel):
    """Pagination metadata.

    Offset/limit rather than cursors: the Phase 5 review inbox needs jumpable
    pages and stable total counts more than it needs consistency under
    concurrent insertion.
    """

    total: int = Field(ge=0, description="Total matching items, ignoring pagination.")
    limit: int = Field(gt=0, le=200)
    offset: int = Field(ge=0)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def has_more(self) -> bool:
        return self.offset + self.limit < self.total


class ListResponse[T](ContractModel):
    """The ONLY collection body any endpoint returns.

    Generic so that ``ListResponse[LinkPlan]`` in Phase 5 and
    ``ListResponse[CMSPage]`` in Phase 1 share one shape, which in turn means
    the generated TypeScript client has one list type rather than several.
    """

    items: list[T]
    page: PageInfo
