"""Initial schema: the append-only events table.

Revision ID: 0001_initial_events
Revises:
Create Date: 2026-10-13

Creates ONLY the ``events`` table (TRD.md section 15: a PostgreSQL-backed event
log). Column-for-column with ``ken_contracts.events.EventEnvelope``.

Deliberately absent, per docs/adr/ADR-0003.md:

* no ``CREATE EXTENSION`` of any kind;
* no vector column.

pgvector is enabled in Phase 2, in the same migration that creates the first
vector column. Keeping this revision extension-free means it runs on any
PostgreSQL 16, including one without pgvector installed.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001_initial_events"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # op.f(): names below are final. Without it, the ck_ naming convention on
    # Base.metadata would be applied a second time (ck_events_ck_events_...).
    op.create_table(
        "events",
        # Insertion order and pagination keyset. GENERATED ALWAYS: no caller
        # may choose its own id.
        sa.Column(
            "id",
            sa.BigInteger(),
            sa.Identity(always=True),
            nullable=False,
        ),
        sa.Column("event_id", sa.Uuid(), nullable=False),
        # TEXT, not a DB enum: the vocabulary is validated by Pydantic at the
        # boundary and can grow without a migration.
        sa.Column("event_name", sa.Text(), nullable=False),
        sa.Column("event_version", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.Column(
            "occurred_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("emitted_by", sa.Text(), nullable=False),
        sa.Column("subject_type", sa.Text(), nullable=True),
        sa.Column("subject_id", sa.Text(), nullable=True),
        sa.Column(
            "payload",
            postgresql.JSONB(),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("correlation_id", sa.Uuid(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_events")),
        # Idempotency: append_event relies on ON CONFLICT (event_id).
        sa.UniqueConstraint("event_id", name=op.f("uq_events_event_id")),
        sa.CheckConstraint("event_version >= 1", name=op.f("ck_events_event_version_positive")),
    )
    op.create_index(
        op.f("ix_events_event_name_occurred_at"),
        "events",
        ["event_name", sa.text("occurred_at DESC")],
    )
    op.create_index(op.f("ix_events_correlation_id"), "events", ["correlation_id"])
    op.create_index(
        op.f("ix_events_subject_type_subject_id"), "events", ["subject_type", "subject_id"]
    )


def downgrade() -> None:
    # Dropping the table drops its indexes and constraints with it.
    op.drop_table("events")
