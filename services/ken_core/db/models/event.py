"""The append-only ``events`` table (TRD.md section 15).

Column-for-column with :class:`ken_contracts.events.EventEnvelope`, plus two
storage-only columns:

* ``id``         -- BIGINT identity: insertion order, and the keyset for
                    pagination. Never exposed in the envelope.
* ``created_at`` -- when the row was written, as distinct from ``occurred_at``
                    (when the fact happened). They differ for replayed or
                    backfilled events.

Append-only by design: there is no ``updated_at`` and no repository method
that updates or deletes a row.

``event_name`` is TEXT, not a database enum: the vocabulary is validated by the
Pydantic ``EventName`` enum at the application boundary, so it can grow
without a migration.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    Identity,
    Index,
    Integer,
    Text,
    Uuid,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from ken_core.db.base import Base


class Event(Base):
    """One row of the PostgreSQL-backed event log."""

    __tablename__ = "events"

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    event_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False, unique=True)
    event_name: Mapped[str] = mapped_column(Text, nullable=False)
    event_version: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    emitted_by: Mapped[str] = mapped_column(Text, nullable=False)
    subject_type: Mapped[str | None] = mapped_column(Text, nullable=True)
    subject_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    payload: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    correlation_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint("event_version >= 1", name="event_version_positive"),
        # "Latest N events of kind X" -- the dominant read pattern.
        Index("ix_events_event_name_occurred_at", "event_name", text("occurred_at DESC")),
        # "Everything caused by request/run Y".
        Index("ix_events_correlation_id", "correlation_id"),
        # "Every event about page Z" as an indexed query, not a JSON probe.
        Index("ix_events_subject_type_subject_id", "subject_type", "subject_id"),
    )
