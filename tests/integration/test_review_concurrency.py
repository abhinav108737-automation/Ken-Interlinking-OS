"""Concurrent review on the ``*_test`` database (ADR-0013 s7).

* two approvals of one plan: exactly one wins, the other is a 409;
* one operation id sent twice at once: one execution, one replay, one row;
* approve vs a concurrent engine expiry: the row lock serialises them; the approval fails;
* edit vs ``make recommend`` on the same source: the per-source advisory lock serialises
  them, in both directions, and running them together leaves the open-pair / open-span
  invariants intact.
"""

from __future__ import annotations

import asyncio
import uuid
from pathlib import Path
from typing import Any

import pytest

from ken_contracts.recommendations import LinkPlanStatus
from ken_contracts.review import (
    ApproveRequest,
    EditAnchorRequest,
    FeedbackReason,
    PlanNotReviewableError,
    ReviewResult,
)
from ken_services.recommendations.transitions import ENGINE_ACTOR, transition
from ken_services.recommendations.validity import lock_source_plans
from tests.integration.recommendation_support import EV, LI, SM, recommend, rows, scalar
from tests.integration.review_support import principal, review_service, seeded, status_of

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("migrated")]

S = LinkPlanStatus
LI_BLOCK = (
    "Vehicle makers buy cells from the lithium-ion batteries market, and local cell plants "
    "are planned to cut that dependence over the coming decade."
)
BLOCKED_FOR = 0.4


@pytest.fixture
async def plans(sessionmaker: SM, tmp_path: Path) -> dict[tuple[str, str], Any]:
    return await seeded(sessionmaker, tmp_path)


def _approve(op: uuid.UUID | None = None) -> ApproveRequest:
    return ApproveRequest(operation_id=op or uuid.uuid4(), expected_status=S.PROPOSED)


def _edit(plan: Any, phrase: str) -> EditAnchorRequest:
    start = LI_BLOCK.index(phrase)
    return EditAnchorRequest(
        operation_id=uuid.uuid4(),
        expected_status=S.PROPOSED,
        reasons=[FeedbackReason.UNNATURAL_ANCHOR],
        block_context_hash=plan.context_hash,
        start=start,
        end=start + len(phrase),
    )


async def _invariants(sessionmaker: SM) -> None:
    """At most one open plan per pair and per (source, block text, start)."""
    open_sql = (
        "status IN ('proposed', 'critiqued', 'policy_checked', 'awaiting_approval', "
        "'approved', 'validated')"
    )
    pairs = await rows(
        sessionmaker,
        f"SELECT source_page_id, target_page_id, count(*) FROM link_plans WHERE {open_sql} "  # noqa: S608
        "GROUP BY 1, 2 HAVING count(*) > 1",
    )
    spans = await rows(
        sessionmaker,
        f"SELECT source_page_id, context_hash, start_offset, count(*) FROM link_plans "  # noqa: S608
        f"WHERE {open_sql} GROUP BY 1, 2, 3 HAVING count(*) > 1",
    )
    assert pairs == [] and spans == []


async def test_two_approvals_of_one_plan_one_wins(sessionmaker: SM, plans: Any) -> None:
    plan = plans[(EV, LI)]
    alice = await principal(sessionmaker, "alice")
    bob = await principal(sessionmaker, "bob")
    service = review_service(sessionmaker)
    results = await asyncio.gather(
        service.approve(plan_id=plan.id, request=_approve(), principal=alice),
        service.approve(plan_id=plan.id, request=_approve(), principal=bob),
        return_exceptions=True,
    )
    wins = [r for r in results if isinstance(r, ReviewResult)]
    losses = [r for r in results if isinstance(r, PlanNotReviewableError)]
    assert len(wins) == 1 and len(losses) == 1, results
    assert await scalar(sessionmaker, "SELECT count(*) FROM review_actions") == 1
    assert await status_of(sessionmaker, plan.id) is S.APPROVED


async def test_a_duplicate_operation_id_at_once_executes_once(sessionmaker: SM, plans: Any) -> None:
    plan = plans[(EV, LI)]
    alice = await principal(sessionmaker, "alice")
    service = review_service(sessionmaker)
    request = _approve()
    results = await asyncio.gather(
        *(service.approve(plan_id=plan.id, request=request, principal=alice) for _ in range(3))
    )
    assert len({r.action_id for r in results}) == 1
    assert sorted(r.idempotent_replay for r in results) == [False, True, True]
    assert await scalar(sessionmaker, "SELECT count(*) FROM review_actions") == 1
    assert (
        await scalar(sessionmaker, "SELECT count(*) FROM events WHERE event_name = 'link.approved'")
        == 1
    )


async def test_approve_waits_for_a_concurrent_expiry_and_then_fails(
    sessionmaker: SM, plans: Any
) -> None:
    plan = plans[(EV, LI)]
    alice = await principal(sessionmaker, "alice")
    service = review_service(sessionmaker)
    async with sessionmaker() as engine_session:
        await engine_session.begin()
        await transition(  # the engine expires the plan and holds the row lock
            engine_session, plan_id=plan.id, to_status=S.EXPIRED, actor=ENGINE_ACTOR, note="stale"
        )
        approval = asyncio.create_task(
            service.approve(plan_id=plan.id, request=_approve(), principal=alice)
        )
        await asyncio.sleep(BLOCKED_FOR)
        assert not approval.done()  # blocked on the row lock
        await engine_session.commit()
    with pytest.raises(PlanNotReviewableError) as info:
        await approval
    assert info.value.reason == "expected_status_mismatch"  # type: ignore[attr-defined]
    assert await status_of(sessionmaker, plan.id) is S.EXPIRED
    assert await scalar(sessionmaker, "SELECT count(*) FROM review_actions") == 0


async def test_an_edit_waits_for_the_source_lock(sessionmaker: SM, plans: Any) -> None:
    plan = plans[(EV, LI)]
    alice = await principal(sessionmaker, "alice")
    service = review_service(sessionmaker)
    async with sessionmaker() as holder:
        await holder.begin()
        await lock_source_plans(holder, source_page_id=plan.source_page_id)
        edit = asyncio.create_task(
            service.edit_anchor(
                plan_id=plan.id, request=_edit(plan, "local cell plants"), principal=alice
            )
        )
        await asyncio.sleep(BLOCKED_FOR)
        assert not edit.done()  # the per-source writer lock is held
        await holder.rollback()
    result = await edit
    assert result.status is S.SUPERSEDED


async def test_the_engine_waits_for_an_edit_holding_the_source_lock(
    sessionmaker: SM, plans: Any
) -> None:
    plan = plans[(EV, LI)]
    async with sessionmaker() as holder:
        await holder.begin()
        await lock_source_plans(holder, source_page_id=plan.source_page_id)
        run = asyncio.create_task(recommend(sessionmaker, source_page_ids=[plan.source_page_id]))
        await asyncio.sleep(BLOCKED_FOR * 2)
        assert not run.done()  # the pipeline's per-source transaction waits
        await holder.rollback()
    detail = await run
    assert detail.counters["errors"] == 0


async def test_edit_and_recommend_together_keep_the_invariants(
    sessionmaker: SM, plans: Any
) -> None:
    plan = plans[(EV, LI)]
    alice = await principal(sessionmaker, "alice")
    service = review_service(sessionmaker)
    edit, run = await asyncio.gather(
        service.edit_anchor(
            plan_id=plan.id,
            request=_edit(plan, "cells from the lithium-ion batteries market"),
            principal=alice,
        ),
        recommend(sessionmaker, source_page_ids=[plan.source_page_id]),
        return_exceptions=True,
    )
    assert isinstance(edit, ReviewResult), edit
    assert not isinstance(run, BaseException) and run.counters["errors"] == 0
    await _invariants(sessionmaker)
    # The engine never re-proposes the pair the edit kept open.
    pairs = await rows(
        sessionmaker,
        "SELECT count(*) AS n FROM link_plans WHERE source_page_id = :s AND target_page_id = :t "
        "AND status = 'awaiting_approval'",
        s=plan.source_page_id,
        t=plan.target_page_id,
    )
    assert pairs[0].n == 1


async def test_a_bulk_id_reused_concurrently_for_another_plan_set_is_refused(
    sessionmaker: SM, plans: Any
) -> None:
    """QA (Phase 5) L2: two bulk calls with ONE operation id but DIFFERENT plan sets must not
    both execute: the second waits on the bulk-id lock, then sees the first's rows -> 409."""
    from ken_contracts.review import BulkAction, BulkReviewRequest, OperationConflictError
    from tests.integration.review_support import queue

    first, second = sorted(plans.values(), key=lambda p: str(p.id))[:2]
    for plan in (first, second):
        await queue(sessionmaker, plan.id)
    alice = await principal(sessionmaker, "alice")
    service = review_service(sessionmaker)
    bulk_id = uuid.uuid4()

    def request(plan_id: uuid.UUID) -> BulkReviewRequest:
        return BulkReviewRequest(
            operation_id=bulk_id,
            action=BulkAction.REJECT,
            plan_ids=[plan_id],
            reasons=[FeedbackReason.OVER_LINKING],
        )

    results = await asyncio.gather(
        service.bulk(request=request(first.id), principal=alice),
        service.bulk(request=request(second.id), principal=alice),
        return_exceptions=True,
    )
    refused = [r for r in results if isinstance(r, OperationConflictError)]
    done = [r for r in results if not isinstance(r, BaseException)]
    assert len(done) == 1 and len(refused) == 1, results
    assert await scalar(sessionmaker, "SELECT count(*) FROM review_actions") == 1
