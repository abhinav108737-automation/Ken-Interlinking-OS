"""Typed review refusals with STABLE reason codes (ADR-0013 s4, s7).

Every refusal is a subclass of a frozen :mod:`ken_contracts.review` error, so the API maps
it by its ``code`` (an :class:`~ken_contracts.errors.ErrorCode` value) to a status:

======================  ======  =============================================================
``code``                status  raised for
======================  ======  =============================================================
``conflict``            409     status / ``expected_status`` mismatch, an outdated block
                                hash, a duplicate open pair, a reused ``operation_id``, a
                                plan that became invalid (not protected), a concurrent edit
``protected_page``      409     the plan's source or target is now protected
``stale_page_version``  409     the source changed and the plan is stale
``validation_failed``   422     the new span / target breaks a placement, anchor or pair rule
``permission_denied``   403     the principal may not review (defence in depth)
======================  ======  =============================================================

``reason`` is a machine code from :data:`REASON_MESSAGES` (the API puts it in
``ErrorResponse.details[0].code``); ``extra`` are further fixed-vocabulary codes (freshness or
ineligibility reasons). Messages are FIXED text from the table, never built from input.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Final

from ken_contracts.review import (
    InvalidEditError,
    OperationConflictError,
    PlanNotReviewableError,
    PlanStaleError,
    ReviewError,
)

__all__ = [
    "REASON_MESSAGES",
    "EditRejectedError",
    "InvalidatedError",
    "OperationReusedError",
    "PairConflictError",
    "ReviewPermissionError",
    "ReviewRefusal",
    "StaleError",
    "StatusConflictError",
]

REASON_MESSAGES: Final[dict[str, str]] = {
    # 409 conflict
    "expected_status_mismatch": "The plan's status is not the expected status.",
    "status_not_reviewable": "The plan's status does not allow this action.",
    "not_awaiting_approval": "Bulk review applies only to plans awaiting approval.",
    "context_hash_mismatch": "The block text changed since it was shown; reload the plan.",
    "concurrent_conflict": "A concurrent change conflicts with this action; reload the plan.",
    "pair_duplicate_recommendation": "Another open plan already links this source to that page.",
    "operation_id_reused": "This operation id was already used for a different request.",
    "source_ineligible": "The source page can no longer be linked from.",
    "target_ineligible": "The destination page can no longer be linked to.",
    "already_linked": "The source page already links to the destination.",
    # 409 stale_page_version
    "stale": "The source page changed and the plan is stale; it will be regenerated.",
    # 422 validation_failed
    "span_out_of_block": "The span lies outside the block text.",
    "span_not_word_aligned": "The span must start and end on word boundaries.",
    "span_unsafe": "The span is not inside one plain text run of an allowed block.",
    "span_overlaps_plan": "The span overlaps another open plan's anchor.",
    "span_unchanged": "The span is the plan's current anchor.",
    "anchor_rule": "The anchor text breaks the anchor rules (length, words or wording).",
    "block_kind_not_allowed": "Links may not be placed in this kind of block.",
    "target_unknown": "The destination page does not exist.",
    "target_unchanged": "The destination is the plan's current destination.",
    "pair_self_link": "A page cannot link to itself.",
    "pair_source_protected": "The source page is protected.",
    "pair_target_protected": "The destination page is protected.",
    "pair_experiment_locked": "An active experiment forbids this pair.",
    "pair_source_ineligible": "The source page is not eligible as a link source.",
    "pair_target_ineligible": "The destination page is not eligible as a link target.",
    "pair_already_linked": "The source page already links to that page.",
    "pair_previously_rejected": "A reviewer recently turned this pair down (cool-down).",
    "pair_target_excluded": "That page is never a link destination.",
    "pair_page_type_incompatible": "That page type is not a valid destination for the source.",
    # 403
    "permission_denied": "The principal may not review recommendations.",
}


class ReviewRefusal:
    """Mixin: a stable ``reason`` (key of :data:`REASON_MESSAGES`), an optional request
    ``location`` (e.g. ``body.start``) and ``extra`` fixed-vocabulary codes."""

    reason: str
    location: str | None
    extra: tuple[str, ...]

    def _init_refusal(
        self, reason: str, *, location: str | None = None, extra: Sequence[str] = ()
    ) -> str:
        if reason not in REASON_MESSAGES:  # pragma: no cover - programming error
            raise KeyError(reason)
        self.reason = reason
        self.location = location
        self.extra = tuple(extra)
        return REASON_MESSAGES[reason]


class StatusConflictError(ReviewRefusal, PlanNotReviewableError):
    def __init__(self, reason: str, *, location: str | None = None) -> None:
        super().__init__(self._init_refusal(reason, location=location))


class PairConflictError(ReviewRefusal, PlanNotReviewableError):
    def __init__(self, reason: str, *, location: str | None = "body.target_page_id") -> None:
        super().__init__(self._init_refusal(reason, location=location))


class InvalidatedError(ReviewRefusal, PlanNotReviewableError):
    """The plan's pair became DURABLY invalid (``validity.plan_invalidity``)."""

    def __init__(self, invalidity: str) -> None:
        kind, _, rest = invalidity.partition(" (")
        extra = [r for r in rest.rstrip(")").split(", ") if r] if rest else []
        reason = kind if kind in REASON_MESSAGES else "concurrent_conflict"
        super().__init__(self._init_refusal(reason, extra=extra))
        self.code = "protected_page" if "protected" in extra else "conflict"


class StaleError(ReviewRefusal, PlanStaleError):
    def __init__(self, freshness_reasons: Sequence[str] = ()) -> None:
        super().__init__(self._init_refusal("stale", extra=freshness_reasons))


class EditRejectedError(ReviewRefusal, InvalidEditError):
    def __init__(self, reason: str, *, location: str | None = None) -> None:
        super().__init__(self._init_refusal(reason, location=location))


class OperationReusedError(ReviewRefusal, OperationConflictError):
    def __init__(self) -> None:
        super().__init__(self._init_refusal("operation_id_reused", location="body.operation_id"))


class ReviewPermissionError(ReviewRefusal, ReviewError):
    code = "permission_denied"

    def __init__(self) -> None:
        super().__init__(self._init_refusal("permission_denied"))
