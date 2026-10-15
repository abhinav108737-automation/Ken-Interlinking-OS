"""Exception handlers. Every error body is the frozen ``ErrorResponse``.

Nothing derived from an exception's text reaches a response: ``str(exc)``
routinely contains connection strings, SQL or credentials. The real exception
is logged server-side, correlated by ``request_id``.
"""

from __future__ import annotations

import re
from http import HTTPStatus
from typing import Any, Final

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from ken_api.middleware.request_id import REQUEST_ID_HEADER
from ken_contracts.agents import AgentRunNotFoundError
from ken_contracts.deployment import (
    DeploymentError,
    DeploymentIssueCode,
    DeploymentNotFoundError,
)
from ken_contracts.errors import ErrorCode, ErrorDetail, ErrorResponse
from ken_contracts.intelligence import EmbeddingUnavailableError, IntelligenceNotFoundError
from ken_contracts.inventory import InventoryNotFoundError
from ken_contracts.jobs import JobRunNotFoundError
from ken_contracts.recommendations import PlanNotFoundError
from ken_contracts.review import ReviewError
from ken_core.logging import get_logger

logger = get_logger(__name__)

_STATUS_CODES: Final[dict[int, ErrorCode]] = {
    401: ErrorCode.UNAUTHENTICATED,
    403: ErrorCode.PERMISSION_DENIED,
    404: ErrorCode.NOT_FOUND,
    405: ErrorCode.METHOD_NOT_ALLOWED,
    409: ErrorCode.CONFLICT,
    422: ErrorCode.VALIDATION_FAILED,
    503: ErrorCode.SERVICE_UNAVAILABLE,
}


def _request_id(request: Request) -> str | None:
    value = getattr(request.state, "request_id", None)
    return value if isinstance(value, str) else None


def _respond(
    request: Request,
    status_code: int,
    body: ErrorResponse,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    merged = dict(headers or {})
    if body.request_id is not None:
        # The 500 handler runs outside RequestIdMiddleware, so set it here too.
        merged[REQUEST_ID_HEADER] = body.request_id
    return JSONResponse(
        status_code=status_code,
        content=body.model_dump(mode="json"),
        headers=merged,
    )


def _code_for_status(status_code: int) -> ErrorCode:
    if status_code in _STATUS_CODES:
        return _STATUS_CODES[status_code]
    return ErrorCode.INTERNAL_ERROR if status_code >= 500 else ErrorCode.VALIDATION_FAILED


async def http_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    if not isinstance(exc, StarletteHTTPException):  # registration guarantees it
        return await unhandled_exception_handler(request, exc)
    # The standard reason phrase, not exc.detail: detail is free text that a
    # future route might fill with something it should not.
    try:
        message = HTTPStatus(exc.status_code).phrase
    except ValueError:
        message = "HTTP error"
    body = ErrorResponse.of(
        _code_for_status(exc.status_code), message, request_id=_request_id(request)
    )
    return _respond(request, exc.status_code, body, dict(exc.headers or {}))


SAFE_VALIDATION_MESSAGE_TYPES: Final = frozenset(
    {
        "missing",
        "extra_forbidden",
        "string_type",
        "string_too_short",
        "string_too_long",
        "string_pattern_mismatch",
        "int_type",
        "int_parsing",
        "float_type",
        "float_parsing",
        "bool_type",
        "bool_parsing",
        "greater_than",
        "greater_than_equal",
        "less_than",
        "less_than_equal",
        "too_short",
        "too_long",
        "list_type",
        "dict_type",
        "model_type",
        "model_attributes_type",
        "enum",
        "literal_error",
        "uuid_type",
        "json_type",
        "value_error",
    }
)
"""Pydantic error types whose message never contains the submitted value (it names a
constraint or our own validator's fixed text). Every other type -- e.g. ``uuid_parsing``,
which quotes the offending character, or ``json_invalid`` -- gets :data:`GENERIC_INVALID`."""
GENERIC_INVALID: Final = "Invalid value."
_SAFE_LOCATION_PART: Final = re.compile(r"[A-Za-z0-9_.-]{1,64}")


def _location(parts: Any) -> str:
    """Dotted location; a part that is not a plain identifier (an extra key a client sent,
    say) is replaced by ``?`` rather than echoed."""
    return ".".join(
        str(part) if isinstance(part, int) or _SAFE_LOCATION_PART.fullmatch(str(part)) else "?"
        for part in parts
    )


def _validation_message(error: Any) -> str:
    if error.get("type") in SAFE_VALIDATION_MESSAGE_TYPES:
        return str(error.get("msg", GENERIC_INVALID))
    return GENERIC_INVALID


async def validation_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    if not isinstance(exc, RequestValidationError):  # registration guarantees it
        return await unhandled_exception_handler(request, exc)
    details = [
        ErrorDetail(
            location=_location(error.get("loc", ())),
            message=_validation_message(error),
            code=str(error["type"]) if "type" in error else None,
        )
        # Never error["input"] / error["ctx"], and only messages known not to quote the
        # submitted value (Phase 1 QA: a malformed UUID's character was echoed).
        for error in exc.errors()
    ]
    body = ErrorResponse.of(
        ErrorCode.VALIDATION_FAILED,
        "Request validation failed.",
        request_id=_request_id(request),
        details=details,
    )
    return _respond(request, 422, body)


_NOT_FOUND: Final[dict[str, tuple[ErrorCode, str]]] = {
    "page": (ErrorCode.PAGE_NOT_FOUND, "Page not found."),
    "page_version": (ErrorCode.NOT_FOUND, "Page version not found."),
    "run": (ErrorCode.NOT_FOUND, "Ingestion run not found."),
}


async def inventory_not_found_handler(request: Request, exc: Exception) -> JSONResponse:
    if not isinstance(exc, InventoryNotFoundError):  # registration guarantees it
        return await unhandled_exception_handler(request, exc)
    # A fixed message per resource kind: the requested id is never echoed.
    code, message = _NOT_FOUND.get(exc.resource, (ErrorCode.NOT_FOUND, "Not found."))
    body = ErrorResponse.of(code, message, request_id=_request_id(request))
    return _respond(request, 404, body)


async def intelligence_not_found_handler(request: Request, exc: Exception) -> JSONResponse:
    if isinstance(exc, IntelligenceNotFoundError):
        code, message = (
            (ErrorCode.PAGE_NOT_FOUND, "Page not found.")
            if exc.resource == "page"
            else (ErrorCode.NOT_FOUND, "Taxonomy node not found.")
        )
    elif isinstance(exc, JobRunNotFoundError):
        code, message = ErrorCode.NOT_FOUND, "Job run not found."
    elif isinstance(exc, PlanNotFoundError):
        code, message = ErrorCode.NOT_FOUND, "Link plan not found."
    elif isinstance(exc, AgentRunNotFoundError):
        code, message = ErrorCode.NOT_FOUND, "Agent run not found."
    else:  # registration guarantees one of the above
        return await unhandled_exception_handler(request, exc)
    return _respond(request, 404, ErrorResponse.of(code, message, request_id=_request_id(request)))


_REVIEW_STATUS: Final[dict[str, int]] = {
    ErrorCode.CONFLICT.value: 409,
    ErrorCode.STALE_PAGE_VERSION.value: 409,
    ErrorCode.PROTECTED_PAGE.value: 409,
    ErrorCode.VALIDATION_FAILED.value: 422,
    ErrorCode.PERMISSION_DENIED.value: 403,
}
_REVIEW_MESSAGES: Final[dict[str, str]] = {
    ErrorCode.CONFLICT.value: "The review action conflicts with the plan's current state.",
    ErrorCode.STALE_PAGE_VERSION.value: "The source page changed and the plan is stale.",
    ErrorCode.PROTECTED_PAGE.value: "A page of this plan is now protected.",
    ErrorCode.VALIDATION_FAILED.value: "The edit breaks a placement, anchor or pair rule.",
    ErrorCode.PERMISSION_DENIED.value: "The role lacks the permission.",
}


async def review_error_handler(request: Request, exc: Exception) -> JSONResponse:
    """Typed review refusals (``ken_services.review.errors``): 403 / 409 / 422 by ``code``.

    The message is fixed per code; ``details`` carry the refusal's stable ``reason`` (with
    its fixed message) and any further fixed-vocabulary codes. Exception text is never used.
    """
    if not isinstance(exc, ReviewError):  # registration guarantees it
        return await unhandled_exception_handler(request, exc)
    code = ErrorCode(exc.code) if exc.code in _REVIEW_STATUS else ErrorCode.CONFLICT
    details: list[ErrorDetail] = []
    reason = getattr(exc, "reason", None)
    if isinstance(reason, str):
        from ken_services.review.errors import REASON_MESSAGES

        details.append(
            ErrorDetail(
                location=getattr(exc, "location", None),
                message=REASON_MESSAGES.get(reason, _REVIEW_MESSAGES[code.value]),
                code=reason,
            )
        )
        for extra in getattr(exc, "extra", ()):
            if isinstance(extra, str) and _SAFE_LOCATION_PART.fullmatch(extra):
                details.append(ErrorDetail(message="Further reason.", code=extra))
    body = ErrorResponse.of(
        code, _REVIEW_MESSAGES[code.value], request_id=_request_id(request), details=details
    )
    return _respond(request, _REVIEW_STATUS[code.value], body)


# --------------------------------------------------------------------------- #
# Phase 6 controlled deployment (ADR-0014 sections 7 and 8)
# --------------------------------------------------------------------------- #
_I = DeploymentIssueCode
DEPLOYMENT_ISSUE_MESSAGES: Final[dict[str, str]] = {
    _I.PLAN_NOT_APPROVED.value: "The plan is not approved.",
    _I.PLAN_STALE.value: "The source page changed and the plan is stale.",
    _I.PLAN_INVALID.value: "The plan is no longer valid (protection, eligibility or anchor).",
    _I.MIXED_SOURCES.value: "The plans belong to more than one source page.",
    _I.CMS_PAGE_NOT_FOUND.value: "The CMS has no such page.",
    _I.CMS_PAGE_PROTECTED.value: "The CMS page is protected.",
    _I.CMS_UNAVAILABLE.value: "The CMS is unavailable.",
    _I.CMS_VALIDATION_FAILED.value: "The CMS refused the change.",
    _I.HREF_INVALID.value: "The link target URL breaks the href rules.",
    _I.BROKEN_TARGET.value: "The link target does not answer 200.",
    _I.BLOCK_NOT_FOUND.value: "No block of the CMS page has the planned block text.",
    _I.AMBIGUOUS_BLOCK.value: "Several blocks of the CMS page have the planned block text.",
    _I.ANCHOR_NOT_FOUND.value: "The anchor text is not at the planned occurrence.",
    _I.ANCHOR_NOT_IN_SINGLE_TEXT_NODE.value: "The anchor does not lie in one text node.",
    _I.UNMAPPABLE_SPAN.value: "The anchor span cannot be mapped to the page HTML.",
    _I.UNSAFE_PLACEMENT.value: "The anchor sits under an excluded, hidden or link ancestor.",
    _I.OVERLAPPING_CHANGES.value: "Two changes of the request overlap.",
    _I.ALREADY_APPLIED.value: "The link is already on the page.",
    _I.CONFLICTING_LINK.value: "A different link already occupies the anchor span.",
    _I.SELF_CHECK_FAILED.value: "The mutation self-check failed.",
    _I.DEPLOYMENT_IN_PROGRESS.value: "Another deployment of this source page is in progress.",
    _I.DENSITY_EXCEEDED.value: "The page would exceed the internal link density limit.",
    _I.ALREADY_LINKED.value: "The CMS page already links to the target.",
    _I.CMS_PAGE_UNMAPPED.value: "The page has no CMS page id.",
    _I.ROLLBACK_CONFLICT.value: "A deployed link was itself changed on the page.",
    _I.NOTHING_TO_ROLL_BACK.value: "Nothing of this deployment is on the page.",
}
"""One FIXED message per issue code (never an input value)."""
_DEPLOYMENT_STATUS: Final[dict[str, int]] = {
    ErrorCode.CONFLICT.value: 409,
    ErrorCode.CMS_VERSION_CONFLICT.value: 409,
    ErrorCode.CMS_UNAVAILABLE.value: 503,
}
_DEPLOYMENT_MESSAGES: Final[dict[str, str]] = {
    ErrorCode.CONFLICT.value: "The deployment request was refused.",
    ErrorCode.CMS_VERSION_CONFLICT.value: (
        "The CMS page changed since it was validated; the plans were expired."
    ),
    ErrorCode.CMS_UNAVAILABLE.value: "The CMS is unavailable; retry later.",
}


async def deployment_error_handler(request: Request, exc: Exception) -> JSONResponse:
    """Typed deployment refusals (``ken_contracts.deployment``): ``DeploymentRefusedError`` ->
    409 ``conflict``, ``DeploymentVersionConflictError`` -> 409 ``cms_version_conflict``,
    ``CMSUnavailableDeploymentError`` -> 503 ``cms_unavailable``. ``details`` = one entry per
    ``DeploymentIssue``: ``code`` = the issue code, ``location`` = the plan id (bare UUID) or
    null, a fixed message per code. A reused operation id has no issues (empty details)."""
    if not isinstance(exc, DeploymentError):  # registration guarantees it
        return await unhandled_exception_handler(request, exc)
    code = exc.code if exc.code in _DEPLOYMENT_STATUS else ErrorCode.CONFLICT.value
    details = [
        ErrorDetail(
            location=None if issue.plan_id is None else str(issue.plan_id),
            message=DEPLOYMENT_ISSUE_MESSAGES.get(
                DeploymentIssueCode(issue.code).value, "The deployment was refused."
            ),
            code=DeploymentIssueCode(issue.code).value,
        )
        for issue in exc.issues
    ]
    body = ErrorResponse.of(
        ErrorCode(code),
        _DEPLOYMENT_MESSAGES[code],
        request_id=_request_id(request),
        details=details,
    )
    status = _DEPLOYMENT_STATUS[code]
    headers = {"Retry-After": "30"} if status == 503 else None
    return _respond(request, status, body, headers)


async def deployment_not_found_handler(request: Request, exc: Exception) -> JSONResponse:
    if not isinstance(exc, DeploymentNotFoundError):  # registration guarantees it
        return await unhandled_exception_handler(request, exc)
    body = ErrorResponse.of(
        ErrorCode.NOT_FOUND, "Deployment not found.", request_id=_request_id(request)
    )
    return _respond(request, 404, body)


async def embedding_unavailable_handler(request: Request, exc: Exception) -> JSONResponse:
    # The class name only is logged; the provider's message may name its host.
    logger.warning("embedding_unavailable", error_class=type(exc).__name__)
    body = ErrorResponse.of(
        ErrorCode.MODEL_UNAVAILABLE,
        "The embedding model is unavailable.",
        request_id=_request_id(request),
    )
    return _respond(request, 503, body, {"Retry-After": "30"})


async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    request_id = _request_id(request)
    logger.error(
        "unhandled_exception",
        request_id=request_id,
        method=request.method,
        path=request.url.path,
        exc_info=exc,
    )
    body = ErrorResponse.of(
        ErrorCode.INTERNAL_ERROR, "Internal server error.", request_id=request_id
    )
    return _respond(request, 500, body)


def install_exception_handlers(app: FastAPI) -> None:
    app.add_exception_handler(StarletteHTTPException, http_exception_handler)
    app.add_exception_handler(RequestValidationError, validation_exception_handler)
    app.add_exception_handler(InventoryNotFoundError, inventory_not_found_handler)
    app.add_exception_handler(IntelligenceNotFoundError, intelligence_not_found_handler)
    app.add_exception_handler(JobRunNotFoundError, intelligence_not_found_handler)
    app.add_exception_handler(PlanNotFoundError, intelligence_not_found_handler)
    app.add_exception_handler(AgentRunNotFoundError, intelligence_not_found_handler)
    app.add_exception_handler(EmbeddingUnavailableError, embedding_unavailable_handler)
    app.add_exception_handler(ReviewError, review_error_handler)
    app.add_exception_handler(DeploymentError, deployment_error_handler)
    app.add_exception_handler(DeploymentNotFoundError, deployment_not_found_handler)
    app.add_exception_handler(Exception, unhandled_exception_handler)
