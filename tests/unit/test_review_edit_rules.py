"""Deterministic pieces of the review service (ADR-0013 s7). No database.

* the edited-span rules over REAL extracted blocks: verbatim whole-phrase occurrence, one
  safe text run (no excluded ancestor, never inside or across a link), no overlap with
  another open plan's span;
* the placement quality and anchor-rule kind of an edited anchor;
* the idempotency fingerprints.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from ken_contracts.recommendations import LinkPlanStatus
from ken_contracts.review import (
    ApproveRequest,
    BulkAction,
    BulkReviewRequest,
    FeedbackReason,
    InvalidEditError,
    ReviewAction,
)
from ken_services.inventory.normalize import extract_blocks
from ken_services.recommendations.placement import AnchorPhrase
from ken_services.review.service import (
    EDITED_PLACEMENT_QUALITY,
    DatabaseReviewService,
    bulk_fingerprint,
    bulk_item_operation_id,
    request_fingerprint,
)
from tests.conftest import make_settings

HTML = (
    "<main><h1>Title</h1>"
    "<p>An opening paragraph about electric vehicles and grid storage.</p>"
    "<p>Makers buy <a href='/x'>lithium-ion batteries</a> while <code>cell plants</code> "
    "grow, and solar power parks expand quickly.</p>"
    "<table><tr><td><p>Solar power in a table cell.</p></td></tr></table>"
    "</main>"
)


@pytest.fixture(scope="module")
def service() -> DatabaseReviewService:
    return DatabaseReviewService(sessionmaker=None, settings=make_settings())  # type: ignore[arg-type]


@pytest.fixture(scope="module")
def blocks() -> list[Any]:
    return list(extract_blocks(HTML))


def _block(blocks: list[Any], needle: str) -> Any:
    return next(b for b in blocks if needle in b.text)


def _span(text: str, phrase: str) -> tuple[int, int]:
    start = text.index(phrase)
    return start, start + len(phrase)


def _reason(
    service: DatabaseReviewService,
    block: Any,
    span: tuple[int, int],
    other: list[tuple[int, int]] | None = None,
) -> str | None:
    try:
        service._validate_span(block, *span, other_spans=other or [])
    except InvalidEditError as exc:
        return str(exc.reason)  # type: ignore[attr-defined]
    return None


def test_a_plain_whole_phrase_is_accepted(service: DatabaseReviewService, blocks: Any) -> None:
    block = _block(blocks, "Makers buy")
    start, end = _span(block.text, "solar power parks")
    anchor, occurrence = service._validate_span(block, start, end, other_spans=[])
    assert anchor == "solar power parks" == block.text[start:end]
    assert occurrence == 0


@pytest.mark.parametrize(
    ("phrase", "reason"),
    [
        ("lithium-ion batteries", "span_unsafe"),  # inside a link
        ("buy lithium-ion", "span_unsafe"),  # across into a link
        ("cell plants", "span_unsafe"),  # under <code>
        ("olar power", "span_not_word_aligned"),
        ("solar power ", "span_not_word_aligned"),
    ],
)
def test_unsafe_or_misaligned_spans_are_refused(
    service: DatabaseReviewService, blocks: Any, phrase: str, reason: str
) -> None:
    block = _block(blocks, "Makers buy")
    assert _reason(service, block, _span(block.text, phrase)) == reason


def test_a_span_outside_the_block_is_refused(service: DatabaseReviewService, blocks: Any) -> None:
    block = _block(blocks, "Makers buy")
    assert _reason(service, block, (5, len(block.text) + 1)) == "span_out_of_block"


def test_an_overlap_with_another_open_plans_span_is_refused(
    service: DatabaseReviewService, blocks: Any
) -> None:
    block = _block(blocks, "Makers buy")
    taken = _span(block.text, "power parks")
    assert _reason(service, block, _span(block.text, "solar power"), [taken]) == (
        "span_overlaps_plan"
    )
    assert _reason(service, block, _span(block.text, "solar power"), [(0, 5)]) is None


def test_a_block_inside_a_table_is_never_a_placement(
    service: DatabaseReviewService, blocks: Any
) -> None:
    block = _block(blocks, "table cell")
    assert _reason(service, block, _span(block.text, "Solar power")) == "span_unsafe"


def test_edited_anchor_quality_and_rule_kind(service: DatabaseReviewService, blocks: Any) -> None:
    body = _block(blocks, "Makers buy")
    opening = _block(blocks, "opening paragraph")
    topic = [AnchorPhrase(text="Solar Power", source="target_topic", rank=0)]
    kind, quality = service._anchor_quality("solar power", body, blocks, topic)
    assert (kind, quality) == ("target_topic", 1.0)
    kind, quality = service._anchor_quality("solar power parks", body, blocks, topic)
    assert (kind, quality) == ("edited", EDITED_PLACEMENT_QUALITY)
    kind, quality = service._anchor_quality("grid storage", opening, blocks, [])
    assert quality == pytest.approx(EDITED_PLACEMENT_QUALITY - 0.1)
    # A one-word anchor is allowed only when it IS a topic alias of the target.
    with pytest.raises(InvalidEditError):
        service._anchor_quality("solar", body, blocks, [])
    one_word = [AnchorPhrase(text="Solar", source="target_topic", rank=0)]
    assert service._anchor_quality("solar", body, blocks, one_word)[0] == "target_topic"
    with pytest.raises(InvalidEditError):
        service._anchor_quality("click here", body, blocks, [])


def test_fingerprints_are_canonical_and_cover_the_user() -> None:
    plan, user = uuid.uuid4(), uuid.uuid4()
    request = ApproveRequest(operation_id=uuid.uuid4(), expected_status=LinkPlanStatus.PROPOSED)
    approve, reject = ReviewAction.APPROVE, ReviewAction.REJECT
    base = request_fingerprint(action=approve, plan_id=plan, user_id=user, body=request)
    assert len(base) == 64
    assert base == request_fingerprint(action=approve, plan_id=plan, user_id=user, body=request)
    other_user = uuid.uuid4()
    assert base != request_fingerprint(
        action=approve, plan_id=plan, user_id=other_user, body=request
    )
    assert base != request_fingerprint(action=reject, plan_id=plan, user_id=user, body=request)
    noted = request.model_copy(update={"note": "x"})
    assert base != request_fingerprint(action=approve, plan_id=plan, user_id=user, body=noted)


def test_bulk_fingerprints_ignore_plan_order_but_not_the_set() -> None:
    user, a, b = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    op = uuid.uuid4()

    def bulk(*plan_ids: uuid.UUID, action: BulkAction = BulkAction.APPROVE) -> BulkReviewRequest:
        reasons = [] if action is BulkAction.APPROVE else [FeedbackReason.OVER_LINKING]
        return BulkReviewRequest(
            operation_id=op, action=action, plan_ids=list(plan_ids), reasons=reasons
        )

    first = bulk_fingerprint(request=bulk(a, b), user_id=user)
    assert first == bulk_fingerprint(request=bulk(b, a), user_id=user)
    assert first != bulk_fingerprint(request=bulk(a), user_id=user)
    assert first != bulk_fingerprint(request=bulk(a, b, action=BulkAction.REJECT), user_id=user)
    assert bulk_item_operation_id(op, a) == uuid.uuid5(op, str(a))
    assert bulk_item_operation_id(op, a) != bulk_item_operation_id(op, b)
