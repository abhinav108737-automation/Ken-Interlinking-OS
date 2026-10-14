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
    app.add_exception_handler(Exception, unhandled_exception_handler)
