"""The review service on the ``*_test`` database, over REAL engine plans (ADR-0013 s4, s7).

The corpus is imported, analysed and recommended exactly as in production; plans are then
reviewed through :class:`DatabaseReviewService` as a real user row's principal.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from ken_contracts.events import EventName
from ken_contracts.intelligence import ProtectionSource
from ken_contracts.recommendations import (
    CandidateRejection,
    LinkPlanEvent,
    LinkPlanStatus,
    PlanNotFoundError,
)
from ken_contracts.review import (
    REVIEW_SERVICE_ACTOR,
    ApproveRequest,
    BulkAction,
    BulkReviewRequest,
    EditAnchorRequest,
    FeedbackReason,
    InvalidEditError,
    OperationConflictError,
    PlanNotReviewableError,
    PlanStaleError,
    RejectRequest,
    ReplaceTargetRequest,
    ReviewAction,
    ReviewService,
)
from ken_core.db.repositories import recommendations as rec_repo
from ken_core.db.repositories.intelligence import insert_protection
from ken_services.inventory.hashing import sha256_hex
from ken_services.inventory.normalize import extract_blocks
from ken_services.review.errors import ReviewPermissionError
from ken_services.review.service import (
    EDITED_PLACEMENT_QUALITY,
    EMITTED_BY,
    bulk_item_operation_id,
)
from tests.integration.recommendation_support import (
    CHARGING,
    EV,
    EV_MARKET_PARAS,
    LI,
    SM,
    SOLAR,
    STORAGE,
    _p,
    analyse,
    import_records,
    page_ids,
    recommend,
    record,
    rows,
    scalar,
)
from tests.integration.review_support import (
    open_plans,
    principal,
    queue,
    review_service,
    seeded,
    status_of,
)

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("migrated")]

S = LinkPlanStatus
LI_BLOCK = (
    "Vehicle makers depend on imported lithium-ion batteries, and local cell plants are "
    "planned to cut that dependence over the coming decade."
)


def _approve(status: S = S.PROPOSED, **kw: Any) -> ApproveRequest:
    return ApproveRequest(operation_id=kw.pop("op", uuid.uuid4()), expected_status=status, **kw)


def _reject(status: S = S.PROPOSED, **kw: Any) -> RejectRequest:
    return RejectRequest(
        operation_id=kw.pop("op", uuid.uuid4()),
        expected_status=status,
        reasons=kw.pop("reasons", [FeedbackReason.IRRELEVANT_DESTINATION]),
        **kw,
    )


def _span(phrase: str, block: str = LI_BLOCK) -> tuple[int, int]:
    start = block.index(phrase)
    return start, start + len(phrase)


def _edit(plan: Any, phrase: str, status: S = S.PROPOSED, **kw: Any) -> EditAnchorRequest:
    start, end = _span(phrase)
    return EditAnchorRequest(
        operation_id=kw.pop("op", uuid.uuid4()),
        expected_status=status,
        reasons=kw.pop("reasons", [FeedbackReason.UNNATURAL_ANCHOR]),
        block_context_hash=kw.pop("context_hash", plan.context_hash),
        start=kw.pop("start", start),
        end=kw.pop("end", end),
        **kw,
    )


def _replace(target: uuid.UUID, status: S = S.PROPOSED, **kw: Any) -> ReplaceTargetRequest:
    return ReplaceTargetRequest(
        operation_id=kw.pop("op", uuid.uuid4()),
        expected_status=status,
        reasons=kw.pop("reasons", [FeedbackReason.BETTER_DESTINATION_EXISTS]),
        target_page_id=target,
        **kw,
    )


async def _counts(sessionmaker: SM) -> tuple[int, int, int, int]:
    return (
        await scalar(sessionmaker, "SELECT count(*) FROM review_actions"),
        await scalar(sessionmaker, "SELECT count(*) FROM events"),
        await scalar(sessionmaker, "SELECT count(*) FROM link_plan_transitions"),
        await scalar(sessionmaker, "SELECT count(*) FROM link_plans"),
    )


async def _events(sessionmaker: SM, name: EventName) -> list[Any]:
    return await rows(
        sessionmaker, "SELECT * FROM events WHERE event_name = :n ORDER BY id", n=name.value
    )


async def _transitions(sessionmaker: SM, plan_id: uuid.UUID) -> list[tuple[Any, ...]]:
    found = await rows(
        sessionmaker,
        "SELECT from_status, to_status, actor, note FROM link_plan_transitions "
        "WHERE plan_id = :p ORDER BY id",
        p=plan_id,
    )
    return [tuple(r) for r in found]


@pytest.fixture
async def plans(sessionmaker: SM, tmp_path: Path) -> dict[tuple[str, str], Any]:
    found = await seeded(sessionmaker, tmp_path)
    assert found[(EV, LI)].anchor_text == "lithium-ion batteries"
    return found


@pytest.fixture
async def alice(sessionmaker: SM) -> Any:
    return await principal(sessionmaker, "alice")


# --------------------------------------------------------------------------- #
# Approve
# --------------------------------------------------------------------------- #
async def test_the_service_satisfies_the_protocol(sessionmaker: SM) -> None:
    assert isinstance(review_service(sessionmaker), ReviewService)


async def test_approve_a_proposed_plan_via_awaiting_approval(
    sessionmaker: SM, plans: Any, alice: Any
) -> None:
    plan = plans[(EV, LI)]
    request = _approve(reasons=[FeedbackReason.GOOD_AS_IS], note="looks right")
    result = await review_service(sessionmaker).approve(
        plan_id=plan.id, request=request, principal=alice
    )
    assert result.status is S.APPROVED and not result.idempotent_replay
    assert result.new_plan_id is None and result.new_plan_status is None
    assert await status_of(sessionmaker, plan.id) is S.APPROVED
    history = await _transitions(sessionmaker, plan.id)
    assert [(h[0], h[1], h[2]) for h in history[-2:]] == [
        ("proposed", "awaiting_approval", "user:alice"),
        ("awaiting_approval", "approved", "user:alice"),
    ]
    action = (await rows(sessionmaker, "SELECT * FROM review_actions"))[0]
    assert action.id == result.action_id
    assert (action.action, action.actor, action.user_id) == ("approve", "user:alice", alice.user_id)
    assert (action.from_status, action.to_status) == ("proposed", "approved")
    assert action.reasons == ["good_as_is"] and action.note == "looks right"
    assert (action.anchor_before, action.target_before) == (plan.anchor_text, plan.target_page_id)
    assert action.anchor_after is None and action.target_after is None
    events = await _events(sessionmaker, EventName.LINK_APPROVED)
    assert len(events) == 1
    event = events[0]
    assert event.correlation_id == request.operation_id
    assert event.emitted_by == EMITTED_BY and event.subject_id == str(plan.id)
    payload = LinkPlanEvent.model_validate(event.payload)
    assert payload.actor == "user:alice" and payload.status is S.APPROVED


async def test_a_replay_returns_the_stored_result_and_changes_nothing(
    sessionmaker: SM, plans: Any, alice: Any
) -> None:
    plan = plans[(EV, LI)]
    service = review_service(sessionmaker)
    request = _approve()
    first = await service.approve(plan_id=plan.id, request=request, principal=alice)
    before = await _counts(sessionmaker)
    again = await service.approve(plan_id=plan.id, request=request, principal=alice)
    assert again.idempotent_replay
    assert again.model_dump(exclude={"idempotent_replay"}) == first.model_dump(
        exclude={"idempotent_replay"}
    )
    assert await _counts(sessionmaker) == before


async def test_reusing_an_operation_id_for_another_request_is_409(
    sessionmaker: SM, plans: Any, alice: Any
) -> None:
    plan = plans[(EV, LI)]
    service = review_service(sessionmaker)
    op = uuid.uuid4()
    await service.approve(plan_id=plan.id, request=_approve(op=op), principal=alice)
    before = await _counts(sessionmaker)
    for call in (
        service.approve(plan_id=plan.id, request=_approve(op=op, note="other"), principal=alice),
        service.approve(plan_id=plans[(EV, CHARGING)].id, request=_approve(op=op), principal=alice),
        service.reject(plan_id=plan.id, request=_reject(S.APPROVED, op=op), principal=alice),
    ):
        with pytest.raises(OperationConflictError) as info:
            await call
        assert info.value.code == "conflict"
    bob = await principal(sessionmaker, "bob")
    with pytest.raises(OperationConflictError):  # another user: another fingerprint
        await service.approve(plan_id=plan.id, request=_approve(op=op), principal=bob)
    assert await _counts(sessionmaker) == before


async def test_refusals_write_nothing_and_a_retry_reexecutes(
    sessionmaker: SM, plans: Any, alice: Any
) -> None:
    plan = plans[(EV, LI)]
    service = review_service(sessionmaker)
    before = await _counts(sessionmaker)
    request = _approve(S.AWAITING_APPROVAL)  # the plan is still proposed
    with pytest.raises(PlanNotReviewableError) as info:
        await service.approve(plan_id=plan.id, request=request, principal=alice)
    assert info.value.reason == "expected_status_mismatch"  # type: ignore[attr-defined]
    assert await _counts(sessionmaker) == before
    await queue(sessionmaker, plan.id)
    result = await service.approve(plan_id=plan.id, request=request, principal=alice)
    assert result.status is S.APPROVED and not result.idempotent_replay


async def test_approve_refuses_unreviewable_statuses_and_unknown_plans(
    sessionmaker: SM, plans: Any, alice: Any
) -> None:
    plan = plans[(EV, LI)]
    service = review_service(sessionmaker)
    await service.approve(plan_id=plan.id, request=_approve(), principal=alice)
    with pytest.raises(PlanNotReviewableError) as info:
        await service.approve(plan_id=plan.id, request=_approve(S.APPROVED), principal=alice)
    assert info.value.reason == "status_not_reviewable"  # type: ignore[attr-defined]
    with pytest.raises(PlanNotFoundError):
        await service.approve(plan_id=uuid.uuid4(), request=_approve(), principal=alice)


async def test_a_principal_without_review_permission_is_refused(
    sessionmaker: SM, plans: Any
) -> None:
    from ken_contracts.auth import Role

    viewer = await principal(sessionmaker, "victor", role=Role.VIEWER)
    with pytest.raises(ReviewPermissionError) as info:
        await review_service(sessionmaker).approve(
            plan_id=plans[(EV, LI)].id, request=_approve(), principal=viewer
        )
    assert info.value.code == "permission_denied"
    assert await scalar(sessionmaker, "SELECT count(*) FROM review_actions") == 0


async def test_a_stale_plan_cannot_be_approved_or_edited(
    sessionmaker: SM, plans: Any, alice: Any, tmp_path: Path
) -> None:
    plan = plans[(EV, LI)]
    paragraphs = list(EV_MARKET_PARAS)
    paragraphs[1] = _p(
        "Vehicle makers still depend on imported lithium-ion batteries for most models."
    )
    await import_records(
        sessionmaker, tmp_path, [record(EV, paragraphs=paragraphs)], name="v2.jsonl"
    )
    service = review_service(sessionmaker)
    before = await _counts(sessionmaker)
    with pytest.raises(PlanStaleError) as info:
        await service.approve(plan_id=plan.id, request=_approve(), principal=alice)
    assert info.value.code == "stale_page_version"
    assert "context_changed" in info.value.extra or "block_missing" in info.value.extra  # type: ignore[attr-defined]
    with pytest.raises(PlanStaleError):
        await service.edit_anchor(
            plan_id=plan.id, request=_edit(plan, "imported lithium-ion batteries"), principal=alice
        )
    assert await _counts(sessionmaker) == before
    # Rejecting a stale plan is always possible (it only removes it from the queue).
    result = await service.reject(plan_id=plan.id, request=_reject(), principal=alice)
    assert result.status is S.REJECTED


async def test_an_appended_block_keeps_the_plan_rebasable_and_approvable(
    sessionmaker: SM, plans: Any, alice: Any, tmp_path: Path
) -> None:
    plan = plans[(EV, LI)]
    paragraphs = [*EV_MARKET_PARAS, _p("A new closing paragraph about something else entirely.")]
    await import_records(
        sessionmaker, tmp_path, [record(EV, paragraphs=paragraphs)], name="v2.jsonl"
    )
    result = await review_service(sessionmaker).approve(
        plan_id=plan.id, request=_approve(), principal=alice
    )
    assert result.status is S.APPROVED


async def test_a_page_protected_after_proposal_blocks_approval(
    sessionmaker: SM, plans: Any, alice: Any
) -> None:
    plan = plans[(EV, LI)]
    async with sessionmaker() as session, session.begin():
        await insert_protection(
            session,
            page_id=plan.target_page_id,
            source=ProtectionSource.MANUAL.value,
            rule_id=None,
            reason="legal hold",
            created_by="user:ops",
            created_at=datetime.now(UTC),
        )
    before = await _counts(sessionmaker)
    service = review_service(sessionmaker)
    with pytest.raises(PlanNotReviewableError) as info:
        await service.approve(plan_id=plan.id, request=_approve(), principal=alice)
    assert info.value.code == "protected_page"
    assert info.value.reason == "target_ineligible"  # type: ignore[attr-defined]
    assert "protected" in info.value.extra  # type: ignore[attr-defined]
    with pytest.raises(PlanNotReviewableError):
        await service.edit_anchor(
            plan_id=plan.id, request=_edit(plan, "imported lithium-ion batteries"), principal=alice
        )
    assert await _counts(sessionmaker) == before


# --------------------------------------------------------------------------- #
# Reject
# --------------------------------------------------------------------------- #
async def test_reject_and_reverse_an_approval(sessionmaker: SM, plans: Any, alice: Any) -> None:
    plan = plans[(EV, LI)]
    other = plans[(EV, CHARGING)]
    service = review_service(sessionmaker)
    rejected = await service.reject(
        plan_id=other.id,
        request=_reject(reasons=[FeedbackReason.OTHER], note="not for this audience"),
        principal=alice,
    )
    assert rejected.status is S.REJECTED
    await service.approve(plan_id=plan.id, request=_approve(), principal=alice)
    reversed_ = await service.reject(plan_id=plan.id, request=_reject(S.APPROVED), principal=alice)
    assert reversed_.status is S.REJECTED
    assert await status_of(sessionmaker, plan.id) is S.REJECTED
    events = await _events(sessionmaker, EventName.LINK_REJECTED)
    assert [e.subject_id for e in events] == [str(other.id), str(plan.id)]
    actions = await rows(sessionmaker, "SELECT * FROM review_actions ORDER BY id")
    assert [(a.action, a.from_status, a.to_status) for a in actions] == [
        ("reject", "proposed", "rejected"),
        ("approve", "proposed", "approved"),
        ("reject", "approved", "rejected"),
    ]
    assert actions[0].reasons == ["other"] and actions[0].note == "not for this audience"
    with pytest.raises(PlanNotReviewableError):  # terminal
        await service.reject(plan_id=plan.id, request=_reject(S.REJECTED), principal=alice)


# --------------------------------------------------------------------------- #
# Edit anchor
# --------------------------------------------------------------------------- #
async def test_edit_anchor_supersedes_with_a_fresh_verbatim_plan(
    sessionmaker: SM, plans: Any, alice: Any
) -> None:
    old = plans[(EV, LI)]
    request = _edit(old, "imported lithium-ion batteries")
    result = await review_service(sessionmaker).edit_anchor(
        plan_id=old.id, request=request, principal=alice
    )
    assert result.status is S.SUPERSEDED
    assert result.new_plan_status is S.AWAITING_APPROVAL and result.new_plan_id is not None
    assert await status_of(sessionmaker, old.id) is S.SUPERSEDED
    async with sessionmaker() as session:
        new = rec_repo.plan_from_row(await rec_repo.lock_plan(session, plan_id=result.new_plan_id))  # type: ignore[arg-type]
    assert new.anchor_text == "imported lithium-ion batteries"
    assert new.anchor_source == "edited" and new.supersedes == old.id and new.run_id is None
    assert (new.locator.start, new.locator.end) == (request.start, request.end)
    assert new.locator.context_hash == old.context_hash == sha256_hex(LI_BLOCK)
    assert new.locator.occurrence_index == 0
    assert new.target_page_id == old.target_page_id
    assert new.preview.anchor == new.anchor_text
    assert new.preview.before.endswith("depend on ")
    # Scoring: the pair's relevance, placement quality of an edited anchor.
    assert new.score.relevance == pytest.approx(
        old.total_score / old.score["factors"]["placement_quality"]
    )
    assert new.score.factors == {"placement_quality": EDITED_PLACEMENT_QUALITY}
    assert new.confidence == new.score.total and new.calibration_version is None
    assert "anchor_matches_target_topic" not in new.reason_codes
    assert '"imported lithium-ion batteries"' in new.rationale
    history = await _transitions(sessionmaker, new.id)
    assert history[0][:3] == (None, "proposed", REVIEW_SERVICE_ACTOR)
    assert "user:alice" in history[0][3] and str(old.id) in history[0][3]
    assert history[1][:3] == ("proposed", "awaiting_approval", "user:alice")
    old_history = await _transitions(sessionmaker, old.id)
    assert old_history[-1][:3] == ("proposed", "superseded", "user:alice")
    proposed = [
        e
        for e in await _events(sessionmaker, EventName.LINK_PROPOSED)
        if e.subject_id == str(new.id)
    ]
    assert len(proposed) == 1
    payload = LinkPlanEvent.model_validate(proposed[0].payload)
    assert payload.supersedes == old.id and payload.actor == "user:alice"
    assert proposed[0].correlation_id == request.operation_id
    action = (await rows(sessionmaker, "SELECT * FROM review_actions"))[0]
    assert (action.new_plan_id, action.new_plan_status) == (new.id, "awaiting_approval")
    assert (action.anchor_before, action.anchor_after) == (old.anchor_text, new.anchor_text)
    # The block text the reader shows is exactly where the new span points.
    body = await scalar(
        sessionmaker,
        "SELECT body_html FROM page_versions WHERE page_id = :p AND version = :v",
        p=new.source_page_id,
        v=new.source_page_version,
    )
    block = next(b for b in extract_blocks(body) if sha256_hex(b.text) == new.locator.context_hash)
    assert block.text[new.locator.start : new.locator.end] == new.anchor_text


async def test_edit_and_approve_in_one_step(sessionmaker: SM, plans: Any, alice: Any) -> None:
    old = plans[(EV, LI)]
    request = _edit(old, "imported lithium-ion batteries", approve=True)
    result = await review_service(sessionmaker).edit_anchor(
        plan_id=old.id, request=request, principal=alice
    )
    assert result.new_plan_status is S.APPROVED
    assert result.new_plan_id is not None
    assert await status_of(sessionmaker, result.new_plan_id) is S.APPROVED
    approved = await _events(sessionmaker, EventName.LINK_APPROVED)
    assert [e.subject_id for e in approved] == [str(result.new_plan_id)]


async def test_an_approved_plan_can_be_edited_without_a_reject(
    sessionmaker: SM, plans: Any, alice: Any
) -> None:
    old = plans[(EV, LI)]
    service = review_service(sessionmaker)
    await service.approve(plan_id=old.id, request=_approve(), principal=alice)
    result = await service.edit_anchor(
        plan_id=old.id,
        request=_edit(old, "imported lithium-ion batteries", S.APPROVED),
        principal=alice,
    )
    assert result.status is S.SUPERSEDED
    assert (await _transitions(sessionmaker, old.id))[-1][:2] == ("approved", "superseded")
    # A supersede is not a reject: no cool-down for the pair.
    assert await _events(sessionmaker, EventName.LINK_REJECTED) == []


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"start": 0, "end": len(LI_BLOCK) + 5}, "span_out_of_block"),
        (
            {"start": LI_BLOCK.index("imported") + 2, "end": LI_BLOCK.index(" lithium")},
            "span_not_word_aligned",
        ),
        (
            {"start": LI_BLOCK.index("imported") - 1, "end": LI_BLOCK.index(" lithium")},
            "span_not_word_aligned",
        ),
        ({"start": LI_BLOCK.index("imported"), "end": LI_BLOCK.index(" lithium")}, "anchor_rule"),
        (
            dict(zip(("start", "end"), _span("lithium-ion batteries"), strict=True)),
            "span_unchanged",
        ),
    ],
)
async def test_edit_span_validation(
    sessionmaker: SM, plans: Any, alice: Any, overrides: dict[str, Any], reason: str
) -> None:
    old = plans[(EV, LI)]
    before = await _counts(sessionmaker)
    with pytest.raises(InvalidEditError) as info:
        await review_service(sessionmaker).edit_anchor(
            plan_id=old.id,
            request=_edit(old, "imported lithium-ion batteries", **overrides),
            principal=alice,
        )
    assert info.value.code == "validation_failed"
    assert info.value.reason == reason  # type: ignore[attr-defined]
    assert await _counts(sessionmaker) == before


async def test_an_outdated_block_hash_is_a_409(sessionmaker: SM, plans: Any, alice: Any) -> None:
    old = plans[(EV, LI)]
    with pytest.raises(PlanNotReviewableError) as info:
        await review_service(sessionmaker).edit_anchor(
            plan_id=old.id,
            request=_edit(old, "imported lithium-ion batteries", context_hash=sha256_hex("other")),
            principal=alice,
        )
    assert info.value.reason == "context_hash_mismatch"  # type: ignore[attr-defined]


# --------------------------------------------------------------------------- #
# Replace target
# --------------------------------------------------------------------------- #
async def test_replace_target_builds_the_new_pair_from_live_facts(
    sessionmaker: SM, plans: Any, alice: Any
) -> None:
    old = plans[(EV, LI)]
    ids = await page_ids(sessionmaker)
    request = _replace(ids[SOLAR])
    result = await review_service(sessionmaker).replace_target(
        plan_id=old.id, request=request, principal=alice
    )
    assert result.status is S.SUPERSEDED and result.new_plan_status is S.AWAITING_APPROVAL
    async with sessionmaker() as session:
        row = await rec_repo.lock_plan(session, plan_id=result.new_plan_id)  # type: ignore[arg-type]
        assert row is not None
        new = rec_repo.plan_from_row(row)
    assert new.target_page_id == ids[SOLAR] and new.target_url.endswith(SOLAR)
    assert new.anchor_text == old.anchor_text and new.anchor_source == "edited"
    assert (new.locator.start, new.locator.end) == (old.start_offset, old.end_offset)
    assert new.channels == ()  # no channel retrieved this pair
    assert new.score.policy_name == "ranking"
    assert new.score.factors == {"placement_quality": EDITED_PLACEMENT_QUALITY}
    assert new.relationship_type is not None
    action = (await rows(sessionmaker, "SELECT * FROM review_actions"))[0]
    assert (action.target_before, action.target_after) == (old.target_page_id, ids[SOLAR])


async def test_replace_target_refuses_bad_destinations(
    sessionmaker: SM, plans: Any, alice: Any
) -> None:
    old = plans[(EV, LI)]
    ids = await page_ids(sessionmaker)
    service = review_service(sessionmaker)
    before = await _counts(sessionmaker)
    for target, reason in (
        (ids[LI], "target_unchanged"),
        (ids[EV], "pair_self_link"),
        (uuid.uuid4(), "target_unknown"),
    ):
        with pytest.raises(InvalidEditError) as info:
            await service.replace_target(plan_id=old.id, request=_replace(target), principal=alice)
        assert info.value.reason == reason  # type: ignore[attr-defined]
    # Another OPEN plan already links EV to CHARGING: a duplicate pair is a 409.
    with pytest.raises(PlanNotReviewableError) as conflict:
        await service.replace_target(
            plan_id=old.id, request=_replace(ids[CHARGING]), principal=alice
        )
    assert conflict.value.reason == "pair_duplicate_recommendation"  # type: ignore[attr-defined]
    async with sessionmaker() as session, session.begin():
        await insert_protection(
            session,
            page_id=ids[SOLAR],
            source=ProtectionSource.MANUAL.value,
            rule_id=None,
            reason="legal hold",
            created_by="user:ops",
            created_at=datetime.now(UTC),
        )
    with pytest.raises(InvalidEditError) as protected:
        await service.replace_target(plan_id=old.id, request=_replace(ids[SOLAR]), principal=alice)
    assert protected.value.reason == "pair_target_protected"  # type: ignore[attr-defined]
    assert await _counts(sessionmaker) == before


async def test_replace_target_into_an_already_linked_page_is_refused(
    sessionmaker: SM, plans: Any, alice: Any, tmp_path: Path
) -> None:
    old = plans[(EV, LI)]
    ids = await page_ids(sessionmaker)
    linked = _p(
        'See the <a href="https://site.test/reports/india-solar-power-market">solar report</a>.'
    )
    await import_records(
        sessionmaker, tmp_path, [record(EV, paragraphs=[*EV_MARKET_PARAS, linked])], name="v2.jsonl"
    )
    await analyse(sessionmaker)
    with pytest.raises(InvalidEditError) as info:
        await review_service(sessionmaker).replace_target(
            plan_id=old.id, request=_replace(ids[SOLAR]), principal=alice
        )
    assert info.value.reason == "pair_already_linked"  # type: ignore[attr-defined]


async def test_a_replaced_destination_is_not_reproposed_by_the_engine(
    sessionmaker: SM, plans: Any, alice: Any
) -> None:
    old = plans[(EV, LI)]
    ids = await page_ids(sessionmaker)
    await review_service(sessionmaker).replace_target(
        plan_id=old.id, request=_replace(ids[SOLAR]), principal=alice
    )
    detail = await recommend(sessionmaker)
    assert detail.counters["errors"] == 0
    assert (EV, LI) not in await open_plans(sessionmaker)
    found = await rows(
        sessionmaker,
        "SELECT rejection FROM link_candidates WHERE run_id = :r AND source_page_id = :s "
        "AND target_page_id = :t",
        r=detail.id,
        s=ids[EV],
        t=ids[LI],
    )
    assert [r.rejection for r in found] == [CandidateRejection.PREVIOUSLY_REJECTED.value]
    async with sessionmaker() as session:
        cooled = await rec_repo.rejected_targets_since(
            session, source_page_id=ids[EV], since=datetime(2000, 1, 1, tzinfo=UTC)
        )
    assert ids[LI] in cooled


# --------------------------------------------------------------------------- #
# Bulk
# --------------------------------------------------------------------------- #
async def test_bulk_partial_success_and_replay(sessionmaker: SM, plans: Any, alice: Any) -> None:
    queued = [plans[(EV, LI)].id, plans[(EV, CHARGING)].id]
    for plan_id in queued:
        await queue(sessionmaker, plan_id)
    proposed = plans[(STORAGE, SOLAR)].id
    missing = uuid.uuid4()
    request = BulkReviewRequest(
        operation_id=uuid.uuid4(),
        action="approve",
        plan_ids=[*queued, proposed, missing],
        reasons=[FeedbackReason.GOOD_AS_IS],
    )
    service = review_service(sessionmaker)
    result = await service.bulk(request=request, principal=alice)
    assert [(i.ok, i.status, i.error_code) for i in result.items] == [
        (True, S.APPROVED, None),
        (True, S.APPROVED, None),
        (False, None, "conflict"),
        (False, None, "not_found"),
    ]
    assert not result.idempotent_replay
    actions = await rows(sessionmaker, "SELECT * FROM review_actions ORDER BY id")
    assert len(actions) == 2
    for action, plan_id in zip(actions, queued, strict=True):
        assert action.operation_id == bulk_item_operation_id(request.operation_id, plan_id)
        assert action.bulk_operation_id == request.operation_id
        assert action.reasons == ["good_as_is"]
    events = await _events(sessionmaker, EventName.LINK_APPROVED)
    assert {e.correlation_id for e in events} == {request.operation_id}

    # Replay: done items replay, failed items are re-attempted (and now one succeeds).
    await queue(sessionmaker, proposed)
    again = await service.bulk(request=request, principal=alice)
    assert [(i.ok, i.error_code) for i in again.items] == [
        (True, None),
        (True, None),
        (True, None),
        (False, "not_found"),
    ]
    assert not again.idempotent_replay
    assert await scalar(sessionmaker, "SELECT count(*) FROM review_actions") == 3

    # The same bulk id with another action or plan set is a 409, and changes nothing.
    for other in (
        request.model_copy(
            update={"action": BulkAction.REJECT, "reasons": [FeedbackReason.OTHER], "note": "x"}
        ),
        request.model_copy(update={"plan_ids": queued}),
    ):
        with pytest.raises(OperationConflictError):
            await service.bulk(
                request=BulkReviewRequest.model_validate(other.model_dump()), principal=alice
            )
    assert await scalar(sessionmaker, "SELECT count(*) FROM review_actions") == 3


async def test_a_fully_replayed_bulk_is_an_idempotent_replay(
    sessionmaker: SM, plans: Any, alice: Any
) -> None:
    plan_ids = [plans[(EV, LI)].id, plans[(EV, CHARGING)].id]
    for plan_id in plan_ids:
        await queue(sessionmaker, plan_id)
    request = BulkReviewRequest(
        operation_id=uuid.uuid4(),
        action="reject",
        plan_ids=plan_ids,
        reasons=[FeedbackReason.OVER_LINKING],
    )
    service = review_service(sessionmaker)
    first = await service.bulk(request=request, principal=alice)
    assert all(i.ok and i.status is S.REJECTED for i in first.items)
    reordered = request.model_copy(update={"plan_ids": plan_ids[::-1]})
    again = await service.bulk(
        request=BulkReviewRequest.model_validate(reordered.model_dump()), principal=alice
    )
    assert again.idempotent_replay
    assert [i.plan_id for i in again.items] == plan_ids[::-1]
    rejected = await _events(sessionmaker, EventName.LINK_REJECTED)
    assert len(rejected) == 2


async def test_bulk_skips_stale_plans(
    sessionmaker: SM, plans: Any, alice: Any, tmp_path: Path
) -> None:
    plan = plans[(EV, LI)]
    await queue(sessionmaker, plan.id)
    paragraphs = list(EV_MARKET_PARAS)
    paragraphs[1] = _p("Vehicle makers buy imported lithium-ion batteries today.")
    await import_records(
        sessionmaker, tmp_path, [record(EV, paragraphs=paragraphs)], name="v2.jsonl"
    )
    request = BulkReviewRequest(
        operation_id=uuid.uuid4(),
        action="reject",
        plan_ids=[plan.id],
        reasons=[FeedbackReason.OUTDATED_OR_CHANGED_CONTENT],
    )
    result = await review_service(sessionmaker).bulk(request=request, principal=alice)
    assert [(i.ok, i.error_code) for i in result.items] == [(False, "stale_page_version")]


# --------------------------------------------------------------------------- #
# Target options
# --------------------------------------------------------------------------- #
async def test_target_options_pass_the_pair_filters_and_are_ranked(
    sessionmaker: SM, plans: Any
) -> None:
    old = plans[(EV, LI)]
    ids = await page_ids(sessionmaker)
    options = await review_service(sessionmaker).target_options(plan_id=old.id, limit=20)
    found = {o.page_id for o in options}
    assert ids[SOLAR] in found
    assert ids[EV] not in found and ids[LI] not in found
    assert ids[CHARGING] not in found  # an open plan already holds EV -> CHARGING
    relevances = [o.relevance or 0.0 for o in options]
    assert relevances == sorted(relevances, reverse=True)
    assert all(o.page_type == "report" for o in options)
    assert len(await review_service(sessionmaker).target_options(plan_id=old.id, limit=1)) == 1
    with pytest.raises(PlanNotFoundError):
        await review_service(sessionmaker).target_options(plan_id=uuid.uuid4(), limit=5)


async def test_review_rows_round_trip_through_the_contract(
    sessionmaker: SM, plans: Any, alice: Any
) -> None:
    from ken_services.review.reader import SqlReviewReader

    old = plans[(EV, LI)]
    service = review_service(sessionmaker)
    edited = await service.edit_anchor(
        plan_id=old.id, request=_edit(old, "imported lithium-ion batteries"), principal=alice
    )
    assert edited.new_plan_id is not None
    await service.approve(
        plan_id=edited.new_plan_id, request=_approve(S.AWAITING_APPROVAL), principal=alice
    )
    reader = SqlReviewReader(sessionmaker=sessionmaker)
    history = await reader.actions_for_plan(plan_id=edited.new_plan_id)
    assert [a.action.value for a in history] == ["edit_anchor", "approve"]
    assert [a.action.value for a in await reader.actions_for_plan(plan_id=old.id)] == [
        "edit_anchor"
    ]
    page = await reader.list_actions(limit=1, offset=0)
    assert page.page.total == 2 and page.items[0].action.value == "approve"
    filtered = await reader.list_actions(
        limit=10, offset=0, action=ReviewAction.EDIT_ANCHOR, actor="user:alice"
    )
    assert [a.id for a in filtered.items] == [history[0].id]
    assert (await reader.list_actions(limit=10, offset=0, actor="user:nobody")).items == []
    with pytest.raises(PlanNotFoundError):
        await reader.actions_for_plan(plan_id=uuid.uuid4())
    stored = await rows(sessionmaker, "SELECT result FROM review_actions ORDER BY id")
    assert "action_id" not in json.dumps(stored[0].result)
