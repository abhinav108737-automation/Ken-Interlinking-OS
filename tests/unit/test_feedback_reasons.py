"""The structured feedback reasons a reviewer may file (PRD 7.9, ADR-0021).

``anchor_too_generic`` was added on 2026-10-29. Four rejections for exactly that fault had been
filed as ``other`` with a free-text note, and the learning step reads only structured codes, so
that feedback counted for nothing. These tests pin where the new code is valid, and that adding
it did not loosen any rule that already held.
"""

from __future__ import annotations

import uuid

import pytest
from pydantic import ValidationError

from ken_contracts.recommendations import LinkPlanStatus
from ken_contracts.review import (
    ANCHOR_REASONS,
    DESTINATION_REASONS,
    REASONS_BY_ACTION,
    ApproveRequest,
    FeedbackReason,
    RejectRequest,
    ReviewAction,
)

NEW = FeedbackReason.ANCHOR_TOO_GENERIC


def _reject(reasons: list[FeedbackReason], note: str = "") -> RejectRequest:
    return RejectRequest(
        operation_id=uuid.uuid4(),
        expected_status=LinkPlanStatus.POLICY_CHECKED,
        reasons=reasons,
        note=note,
    )


def test_the_code_is_the_string_the_learning_step_will_read() -> None:
    # Reasons are stored and aggregated as these exact strings, so the value is part of the
    # contract and must not drift from the name used in the ADR and the web console.
    assert NEW.value == "anchor_too_generic"


def test_a_reviewer_may_reject_for_it_without_a_note() -> None:
    # A structured reason exists so that a reviewer does not have to type an explanation.
    request = _reject([NEW])
    assert request.reasons == [NEW]


def test_it_is_valid_for_reject_and_for_editing_the_anchor() -> None:
    assert NEW in REASONS_BY_ACTION[ReviewAction.REJECT]
    assert NEW in REASONS_BY_ACTION[ReviewAction.EDIT_ANCHOR]
    assert NEW in ANCHOR_REASONS


def test_it_is_not_valid_for_approving_or_for_replacing_the_target() -> None:
    # Too-generic is a fault of the ANCHOR. Approving means there is no fault, and replacing the
    # target is about the destination, so neither may carry it.
    assert NEW not in REASONS_BY_ACTION[ReviewAction.APPROVE]
    assert NEW not in REASONS_BY_ACTION[ReviewAction.REPLACE_TARGET]
    assert NEW not in DESTINATION_REASONS
    with pytest.raises(ValidationError):
        ApproveRequest(
            operation_id=uuid.uuid4(),
            expected_status=LinkPlanStatus.POLICY_CHECKED,
            reasons=[NEW],
        )


def test_adding_it_did_not_loosen_the_existing_rules() -> None:
    # `other` still demands a note, and `good_as_is` is still an approval-only reason.
    with pytest.raises(ValidationError):
        _reject([FeedbackReason.OTHER], note="")
    with pytest.raises(ValidationError):
        _reject([FeedbackReason.GOOD_AS_IS])
    with pytest.raises(ValidationError):
        _reject([NEW, NEW])
    assert REASONS_BY_ACTION[ReviewAction.APPROVE] == frozenset(
        {FeedbackReason.GOOD_AS_IS, FeedbackReason.OTHER}
    )


def test_every_reason_has_a_distinct_value() -> None:
    values = [reason.value for reason in FeedbackReason]
    assert len(values) == len(set(values))
