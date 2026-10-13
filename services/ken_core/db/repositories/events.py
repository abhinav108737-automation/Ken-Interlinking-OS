"""Event log repository.

Validated envelope in, row out. The caller owns the transaction: these
functions never commit, so an event can be appended atomically with the state
change it describes.
"""

from __future__ import annotations

from typing import Final

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from ken_contracts.events import EventEnvelope, EventName
from ken_core.db.models.event import Event

MAX_LIST_LIMIT: Final = 500


async def append_event(session: AsyncSession, envelope: EventEnvelope) -> None:
    """Append one event. Idempotent on ``event_id``.

    A replayed envelope (same ``event_id``) is silently ignored by
    ``ON CONFLICT (event_id) DO NOTHING`` rather than creating a second row or
    raising. That is what makes at-least-once emitters (a worker that retries
    after a crash) safe without a read-before-write race.

    The envelope is **re-validated here**, at the persistence boundary, even
    though it is already an ``EventEnvelope``. Construction-time validation is
    not enough: ``model_copy(update=...)`` skips validation, and an in-place
    payload mutation (``envelope.payload["promoted_by"] = ""``) is invisible to
    the model. Without this, an ungoverned ``skill.promoted`` /
    ``policy.promoted`` could be written -- and because the log is append-only
    and ``list_events`` re-validates every row, that one bad row would then
    break event listing permanently. Raises ``pydantic.ValidationError`` and
    writes nothing.
    """
    envelope = EventEnvelope.model_validate(envelope.model_dump())
    statement = (
        insert(Event)
        .values(
            event_id=envelope.event_id,
            event_name=envelope.event_name.value,
            event_version=envelope.event_version,
            occurred_at=envelope.occurred_at,
            emitted_by=envelope.emitted_by,
            subject_type=envelope.subject_type,
            subject_id=envelope.subject_id,
            payload=envelope.payload,
            correlation_id=envelope.correlation_id,
        )
        .on_conflict_do_nothing(index_elements=[Event.event_id])
    )
    await session.execute(statement)


async def list_events(
    session: AsyncSession,
    *,
    event_name: EventName | None = None,
    limit: int = 50,
) -> list[EventEnvelope]:
    """Return the most recently appended events, newest first.

    Ordered by the identity column, i.e. insertion order, which is total and
    stable (``occurred_at`` is neither: emitters' clocks differ and ties are
    common).

    Raises:
        ValueError: ``limit`` is outside ``1..MAX_LIST_LIMIT``.
    """
    if not 1 <= limit <= MAX_LIST_LIMIT:
        raise ValueError(f"limit must be between 1 and {MAX_LIST_LIMIT}")

    statement = select(Event).order_by(Event.id.desc()).limit(limit)
    if event_name is not None:
        statement = statement.where(Event.event_name == EventName(event_name).value)

    rows = (await session.scalars(statement)).all()
    # Re-validate on the way out: a row written by anything other than
    # append_event (a manual fix, a future migration) still has to satisfy
    # the contract before it reaches application code.
    return [
        EventEnvelope(
            event_id=row.event_id,
            event_name=EventName(row.event_name),
            event_version=row.event_version,
            occurred_at=row.occurred_at,
            emitted_by=row.emitted_by,
            subject_type=row.subject_type,
            subject_id=row.subject_id,
            payload=row.payload,
            correlation_id=row.correlation_id,
        )
        for row in rows
    ]
