"""The single plan transition service against the ``*_test`` database.

Every allowed edge moves the plan and appends one history row; every illegal edge raises
and changes nothing; actors are validated; concurrent movers serialise on FOR UPDATE.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest

from ken_contracts.recommendations import (
    ALLOWED_TRANSITIONS,
    OPEN_STATUSES,
    LinkPlanStatus,
    PlanNotFoundError,
)
from ken_services.recommendations.transitions import (
    IllegalTransitionError,
    InvalidActorError,
    transition,
)
from tests.integration.recommendation_support import (
    SM,
    import_records,
    insert_plans,
    make_plan,
    page_ids,
    record,
    rows,
    scalar,
)

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("migrated")]

S = LinkPlanStatus
T0 = datetime(2026, 10, 14, 9, 0, tzinfo=UTC)
ALLOWED = sorted(
    ((a, b) for a, targets in ALLOWED_TRANSITIONS.items() for b in targets),
    key=lambda e: (e[0].value, e[1].value),
)
ILLEGAL_BY_SOURCE = {
    a: sorted((b for b in S if b not in ALLOWED_TRANSITIONS[a]), key=lambda s: s.value) for a in S
}


async def _pages(sessionmaker: SM, tmp_path: Path, n: int = 2) -> list[uuid.UUID]:
    paths = [f"/reports/p{i}" for i in range(n)]
    await import_records(
        sessionmaker, tmp_path, [record(p, title=f"P{i}") for i, p in enumerate(paths)]
    )
    ids = await page_ids(sessionmaker)
    return [ids[p] for p in paths]


async def test_every_allowed_edge_moves_the_plan_and_appends_history(
    sessionmaker: SM, tmp_path: Path
) -> None:
    source, target = await _pages(sessionmaker, tmp_path)
    assert len(ALLOWED) == sum(len(v) for v in ALLOWED_TRANSITIONS.values())
    for current, to in ALLOWED:
        # Closed plans do not occupy the pair, open ones do: one plan at a time per pair.
        plan = make_plan(source, target, status=current, created_at=T0)
        await insert_plans(sessionmaker, plan)
        async with sessionmaker() as session, session.begin():
            view = await transition(
                session,
                plan_id=plan.id,
                to_status=to,
                actor="agent:critic@1.0.0",
                note=f"{current.value} to {to.value}",
                expected_from=current,
            )
        assert (view.plan_id, view.from_status, view.to_status) == (plan.id, current, to)
        assert view.actor == "agent:critic@1.0.0"
        state = (
            await rows(
                sessionmaker, "SELECT status, updated_at FROM link_plans WHERE id = :i", i=plan.id
            )
        )[0]
        assert state.status == to.value
        assert state.updated_at >= plan.updated_at
        history = await rows(
            sessionmaker,
            "SELECT from_status, to_status, note FROM link_plan_transitions "
            "WHERE plan_id = :i ORDER BY id",
            i=plan.id,
        )
        assert [(h.from_status, h.to_status) for h in history] == [
            (None, current.value),
            (current.value, to.value),
        ]
        if to in OPEN_STATUSES:
            # Still open: close it so the next iteration may open a plan on the pair.
            async with sessionmaker() as session, session.begin():
                await transition(session, plan_id=plan.id, to_status=S.EXPIRED, actor="system:test")


@pytest.mark.parametrize("current", list(S), ids=lambda s: s.value)
async def test_illegal_edges_raise_and_change_nothing(
    sessionmaker: SM, tmp_path: Path, current: LinkPlanStatus
) -> None:
    source, target = await _pages(sessionmaker, tmp_path)
    plan = make_plan(source, target, status=current)
    await insert_plans(sessionmaker, plan)
    for to in ILLEGAL_BY_SOURCE[current]:
        with pytest.raises(IllegalTransitionError) as info:
            async with sessionmaker() as session, session.begin():
                await transition(session, plan_id=plan.id, to_status=to, actor="system:test")
        assert (info.value.current, info.value.target) == (current, to)
    assert (
        await scalar(sessionmaker, "SELECT status FROM link_plans WHERE id = :i", i=plan.id)
        == current.value
    )
    assert (
        await scalar(
            sessionmaker, "SELECT count(*) FROM link_plan_transitions WHERE plan_id = :i", i=plan.id
        )
        == 1
    )


async def test_terminal_states_have_no_exit_and_approval_is_never_resurrected(
    sessionmaker: SM, tmp_path: Path
) -> None:
    for terminal in (S.REJECTED, S.SUPERSEDED, S.EXPIRED, S.ROLLED_BACK):
        assert ILLEGAL_BY_SOURCE[terminal] == sorted(S, key=lambda s: s.value)
    assert S.APPROVED not in ALLOWED_TRANSITIONS[S.REJECTED]
    assert S.APPROVED not in ALLOWED_TRANSITIONS[S.EXPIRED]


async def test_expected_from_must_match(sessionmaker: SM, tmp_path: Path) -> None:
    source, target = await _pages(sessionmaker, tmp_path)
    plan = make_plan(source, target, status=S.CRITIQUED)
    await insert_plans(sessionmaker, plan)
    with pytest.raises(IllegalTransitionError, match="not proposed"):
        async with sessionmaker() as session, session.begin():
            await transition(
                session,
                plan_id=plan.id,
                to_status=S.POLICY_CHECKED,
                actor="system:test",
                expected_from=S.PROPOSED,
            )
    assert await scalar(sessionmaker, "SELECT status FROM link_plans") == "critiqued"


@pytest.mark.parametrize(
    "actor",
    [
        "",
        "reviewer",
        "user:",
        "user:has space",
        "agent:Critic@1",
        "agent:critic",
        "agent:critic@",
        "system:Engine",
        "system:",
        "root:x",
        "user:x\nsystem:y",
    ],
)
async def test_invalid_actors_are_refused_before_anything_changes(
    sessionmaker: SM, tmp_path: Path, actor: str
) -> None:
    source, target = await _pages(sessionmaker, tmp_path)
    plan = make_plan(source, target)
    await insert_plans(sessionmaker, plan)
    with pytest.raises(InvalidActorError) as info:
        async with sessionmaker() as session, session.begin():
            await transition(session, plan_id=plan.id, to_status=S.CRITIQUED, actor=actor)
    assert str(info.value) == str(InvalidActorError())  # fixed text: never echoes the value
    assert await scalar(sessionmaker, "SELECT status FROM link_plans") == "proposed"


@pytest.mark.parametrize(
    "actor",
    ["user:42", "user:reviewer@example.org", "agent:critic@1.2.3", "system:recommendation-engine"],
)
async def test_valid_actor_forms(sessionmaker: SM, tmp_path: Path, actor: str) -> None:
    source, target = await _pages(sessionmaker, tmp_path)
    plan = make_plan(source, target)
    await insert_plans(sessionmaker, plan)
    async with sessionmaker() as session, session.begin():
        view = await transition(session, plan_id=plan.id, to_status=S.CRITIQUED, actor=actor)
    assert view.actor == actor


async def test_unknown_plan_and_long_note(sessionmaker: SM, tmp_path: Path) -> None:
    source, target = await _pages(sessionmaker, tmp_path)
    async with sessionmaker() as session, session.begin():
        with pytest.raises(PlanNotFoundError):
            await transition(
                session, plan_id=uuid.uuid4(), to_status=S.CRITIQUED, actor="system:test"
            )
    plan = make_plan(source, target)
    await insert_plans(sessionmaker, plan)
    with pytest.raises(ValueError, match="500"):
        async with sessionmaker() as session, session.begin():
            await transition(
                session, plan_id=plan.id, to_status=S.CRITIQUED, actor="system:t", note="x" * 501
            )
    assert await scalar(sessionmaker, "SELECT status FROM link_plans") == "proposed"


async def test_the_transition_commits_with_the_callers_transaction(
    sessionmaker: SM, tmp_path: Path
) -> None:
    source, target = await _pages(sessionmaker, tmp_path)
    plan = make_plan(source, target)
    await insert_plans(sessionmaker, plan)
    with pytest.raises(RuntimeError):
        async with sessionmaker() as session, session.begin():
            await transition(session, plan_id=plan.id, to_status=S.REJECTED, actor="user:1")
            raise RuntimeError("the caller's later write failed")
    assert await scalar(sessionmaker, "SELECT status FROM link_plans") == "proposed"
    assert await scalar(sessionmaker, "SELECT count(*) FROM link_plan_transitions") == 1


async def test_concurrent_movers_serialise_on_the_row_lock(
    sessionmaker: SM, tmp_path: Path
) -> None:
    """A rejects (holding the lock); B tries to critique meanwhile and must see 'rejected'."""
    source, target = await _pages(sessionmaker, tmp_path)
    plan = make_plan(source, target)
    await insert_plans(sessionmaker, plan)
    a_moved = asyncio.Event()
    release_a = asyncio.Event()
    b_started = asyncio.Event()

    async def mover_a() -> None:
        async with sessionmaker() as session, session.begin():
            await transition(session, plan_id=plan.id, to_status=S.REJECTED, actor="user:a")
            a_moved.set()
            await release_a.wait()

    async def mover_b() -> None:
        await a_moved.wait()
        async with sessionmaker() as session, session.begin():
            b_started.set()
            await transition(session, plan_id=plan.id, to_status=S.CRITIQUED, actor="user:b")

    task_a = asyncio.create_task(mover_a())
    task_b = asyncio.create_task(mover_b())
    await b_started.wait()
    await asyncio.sleep(0.3)
    assert not task_b.done()  # blocked on FOR UPDATE while A holds the row
    release_a.set()
    await task_a
    with pytest.raises(IllegalTransitionError) as info:
        await task_b
    assert info.value.current is S.REJECTED
    history = await rows(
        sessionmaker,
        "SELECT from_status, to_status, actor FROM link_plan_transitions ORDER BY id",
    )
    assert [(h.from_status, h.to_status, h.actor) for h in history] == [
        (None, "proposed", "system:test"),
        ("proposed", "rejected", "user:a"),
    ]


async def test_updated_at_never_moves_backwards(sessionmaker: SM, tmp_path: Path) -> None:
    source, target = await _pages(sessionmaker, tmp_path)
    future = datetime(2030, 1, 1, tzinfo=UTC)
    plan = make_plan(source, target, created_at=future)
    await insert_plans(sessionmaker, plan)
    async with sessionmaker() as session, session.begin():
        view = await transition(
            session,
            plan_id=plan.id,
            to_status=S.CRITIQUED,
            actor="system:test",
            clock=lambda: T0,
        )
    assert view.occurred_at == future
