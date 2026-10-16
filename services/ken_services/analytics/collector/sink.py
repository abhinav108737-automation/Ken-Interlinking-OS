"""Where accepted first-party events are stored (``link_events``, migration 0008).

Pseudonymity is BINDING (ADR-0015 §1 and §12.3). A stored row is exactly:

    (event type, plan, match mode, synthetic, UTC date, minute-truncated ``received_at``,
     dedupe key)

and nothing else. No IP, User-Agent, page-view id, referrer, query string, cookie or user id
is written anywhere -- those exist only in memory while the request is being handled. The
``synthetic`` flag comes from :class:`~ken_services.analytics.protocol.CollectorConfig`
(every allowed origin is loopback), never from the client. ``received_at`` is truncated to
the minute, because full precision would allow a join with web-server logs; a CHECK
constraint enforces it, and ``event_date`` is its UTC date.

Insertion is idempotent: ``ON CONFLICT (dedupe_key) DO NOTHING``, where ``dedupe_key`` is
:func:`~ken_services.analytics.protocol.dedupe_key` -- ``sha256(pv|type|plan_id)``, so one
event per link per page view and type. The page-view id itself is never stored.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol
from uuid import UUID

from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ken_contracts.analytics import LinkEventType, MatchMode
from ken_core.db.models.analytics import LinkEvent

__all__ = ["LinkEventSink", "NewLinkEvent", "SqlLinkEventSink", "truncate_to_minute"]


def truncate_to_minute(moment: datetime) -> datetime:
    """``moment`` in UTC with seconds and microseconds dropped (the stored precision)."""
    return moment.astimezone(UTC).replace(second=0, microsecond=0)


@dataclass(frozen=True, slots=True)
class NewLinkEvent:
    """One row to insert. It carries no identifier of the visitor by construction."""

    event_type: LinkEventType
    plan_id: UUID
    match_mode: MatchMode
    synthetic: bool
    received_at: datetime
    """UTC, truncated to the minute (:func:`truncate_to_minute`)."""
    dedupe_key: str

    def row(self) -> dict[str, object]:
        received_at = truncate_to_minute(self.received_at)
        return {
            "event_type": self.event_type.value,
            "plan_id": self.plan_id,
            "match_mode": self.match_mode.value,
            "synthetic": self.synthetic,
            "event_date": received_at.date(),
            "received_at": received_at,
            "dedupe_key": self.dedupe_key,
        }


class LinkEventSink(Protocol):
    """What the collector writes through (a fake one is used by the unit tests)."""

    async def store(self, events: Sequence[NewLinkEvent]) -> int:
        """Insert ``events``; return how many rows were NEW (the rest were duplicates)."""
        ...


class SqlLinkEventSink:
    """``link_events`` over a read-write sessionmaker. One transaction per request."""

    def __init__(self, sessionmaker: async_sessionmaker[AsyncSession]) -> None:
        self._sessionmaker = sessionmaker

    async def store(self, events: Sequence[NewLinkEvent]) -> int:
        if not events:
            return 0
        statement = (
            insert(LinkEvent)
            .values([event.row() for event in events])
            .on_conflict_do_nothing(index_elements=["dedupe_key"])
            .returning(LinkEvent.id)
        )
        async with self._sessionmaker() as session, session.begin():
            inserted = (await session.execute(statement)).scalars().all()
        return len(inserted)
