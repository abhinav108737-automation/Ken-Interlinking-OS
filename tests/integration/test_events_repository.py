"""append_event / list_events against a real PostgreSQL."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ken_contracts.events import EventEnvelope, EventName
from ken_core.db.models import Event
from ken_core.db.repositories.events import append_event, list_events

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("migrated")]


async def _count(sessionmaker: async_sessionmaker[AsyncSession]) -> int:
    async with sessionmaker() as session:
        return int((await session.execute(select(func.count()).select_from(Event))).scalar_one())


async def test_append_and_list_round_trip(sessionmaker: async_sessionmaker[AsyncSession]) -> None:
    envelope = EventEnvelope(
        event_name=EventName.PAGE_UPDATED,
        event_version=2,
        occurred_at=datetime(2026, 10, 13, 12, 0, tzinfo=UTC),
        emitted_by="ken-test",
        subject_type="page",
        subject_id="page-42",
        payload={"changed": ["title"], "nested": {"n": 1}},
        correlation_id=uuid4(),
    )
    async with sessionmaker() as session, session.begin():
        await append_event(session, envelope)

    async with sessionmaker() as session:
        (loaded,) = await list_events(session)
    assert loaded == envelope


async def test_duplicate_event_id_is_idempotent(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    envelope = EventEnvelope(event_name=EventName.WORKER_HEARTBEAT, emitted_by="ken-worker")
    for _ in range(3):
        async with sessionmaker() as session, session.begin():
            await append_event(session, envelope)

    # Even a replay with a different body must not create a second row.
    replay = envelope.model_copy(update={"payload": {"different": True}})
    async with sessionmaker() as session, session.begin():
        await append_event(session, replay)

    assert await _count(sessionmaker) == 1
    async with sessionmaker() as session:
        (stored,) = await list_events(session)
    assert stored.payload == {}


async def test_list_filters_by_name_newest_first(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    names = [EventName.WORKER_HEARTBEAT, EventName.LINK_PROPOSED, EventName.WORKER_HEARTBEAT]
    envelopes = [EventEnvelope(event_name=n, emitted_by="t") for n in names]
    async with sessionmaker() as session, session.begin():
        for envelope in envelopes:
            await append_event(session, envelope)

    async with sessionmaker() as session:
        heartbeats = await list_events(session, event_name=EventName.WORKER_HEARTBEAT)
        latest = await list_events(session, limit=1)
    assert [e.event_id for e in heartbeats] == [envelopes[2].event_id, envelopes[0].event_id]
    assert [e.event_id for e in latest] == [envelopes[2].event_id]


async def test_append_does_not_commit(sessionmaker: async_sessionmaker[AsyncSession]) -> None:
    """The caller owns the transaction, so an event can be atomic with its state change."""
    async with sessionmaker() as session:
        await append_event(
            session, EventEnvelope(event_name=EventName.PAGE_CREATED, emitted_by="t")
        )
        await session.rollback()
    assert await _count(sessionmaker) == 0


async def test_database_enforces_event_version_check(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    async with sessionmaker() as session:
        with pytest.raises(IntegrityError):
            await session.execute(
                text(
                    "INSERT INTO events (event_id, event_name, event_version, emitted_by) "
                    "VALUES (:id, 'page.created', 0, 't')"
                ),
                {"id": uuid4()},
            )


async def test_ungoverned_promotion_is_never_persisted(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """QA cleanup H1, reproduced against a real database.

    A promotion smuggled in via model_copy (which skips validation) was once
    written, after which list_events failed for the whole page -- permanently,
    since the log is append-only. Now: rejected, nothing written, listing works.
    """
    from pydantic import ValidationError

    governed = EventEnvelope(event_name=EventName.WORKER_HEARTBEAT, emitted_by="ken-test")
    smuggled = governed.model_copy(update={"event_name": EventName.POLICY_PROMOTED})

    async with sessionmaker() as session, session.begin():
        await append_event(session, governed)
    with pytest.raises(ValidationError):
        async with sessionmaker() as session, session.begin():
            await append_event(session, smuggled)

    assert await _count(sessionmaker) == 1
    async with sessionmaker() as session:
        listed = await list_events(session)
    assert [e.event_name for e in listed] == [EventName.WORKER_HEARTBEAT]
