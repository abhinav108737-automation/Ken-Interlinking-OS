"""Governed priority-page designations: ``page_priorities``.

Revision ID: 0013_page_priorities
Revises: 0012_page_modules
Create Date: 2026-11-19

Column-for-column with ``ken_core.db.models.recommendations.PagePriority``
(Business Phase 2A-1, knowledge base §5.1).

**Editorial input, not derived state.** Every other recommendation table added since 0004 is
either a lifecycle record the engine writes (``link_plans``) or state replaced wholesale per
run (``opportunities``, ``page_modules``). A designation is neither: a person writes it, owns
it, and revises it. So the row carries ``owner`` and ``reason`` alongside a created/updated
audit pair, and it is mutable -- unlike ``link_plans``, which a trigger freezes.

It carries no ``status`` and no transition history for the same reason ``page_modules`` does
not: the only question a reader asks -- "was this in force at instant X?" -- is answered by
arithmetic on the row's own timestamps. There is no lifecycle for a reviewer to advance.

**Validity is an INTERVAL, and retirement truncates it.** ``ken_priority_validity`` returns::

    [effective_from, min(effective_to, retired_at))      -- clamped, never inverted

Retirement records an INSTANT, not a boolean, so a designation retired today still resolves for
a date on which it was in force; a boolean could only ever answer "is it live now", which would
give every historical question today's answer. ``retired_at`` at or before ``effective_from``
clamps to an EMPTY interval: in force at no instant, and blocking nothing.

**The rule is non-overlap, not uniqueness.** A partial unique index ``WHERE active`` would
forbid a scheduled replacement (two rows of a type, disjoint in time) and would let an expired
row block its own successor. Instead ``ken_page_priority_no_overlap`` refuses two designations
of the same (page, type) whose VALIDITY INTERVALS overlap. Adjacent half-open intervals do not
overlap, so ``[Nov, Dec)`` then ``[Dec, ...)`` is accepted and needs no edit to the incumbent.

The guard takes ``pg_advisory_xact_lock`` on (page_id, priority_type) first: a cross-row check
in a trigger is otherwise open to two concurrent transactions each seeing the other's row as
absent. The repository already serialises plan generation per source this way
(``ken_services.recommendations.validity.SOURCE_LOCK_PREFIX``).

**No ``CREATE EXTENSION``.** A GiST exclusion constraint would express this declaratively but
needs ``btree_gist`` for the scalar equality columns. ``tstzrange`` itself is core PostgreSQL,
so the same guarantee is available with a trigger and no new deployment prerequisite -- every
migration in this chain states that it creates no extension, and ADR-0017 removed the only one
this project ever had.

The ``priority_type`` value list is a frozen copy of ``ken_core.priority.PriorityType`` at
this revision, following the convention 0012 set for ``ModuleKind``.

``page_id`` is ``ON DELETE RESTRICT``: a designation is an audit record of an editorial
decision, so removing the page must not silently remove the evidence that it was prioritised.

Downgrade drops the table, the trigger and both functions, returning exactly to the 0012
state. No existing table, column, index or trigger is touched in either direction, so no
stored row can become invalid.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Final

import sqlalchemy as sa
from alembic import op

revision: str = "0013_page_priorities"
down_revision: str | Sequence[str] | None = "0012_page_modules"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

PRIORITY_TYPES: Final[tuple[str, ...]] = (
    "high_priority_report",
    "high_revenue_report",
    "consulting_priority",
    "survey_priority",
    "expert_panel_priority",
    "country_or_region_priority",
    "trending_market",
    "new_or_refreshed_report",
    "lead_generation_page",
)
"""Frozen copy of ``ken_core.priority.PriorityType`` at this revision."""

MIN_TIER: Final = 1
MAX_TIER: Final = 5

_TYPES: Final = ", ".join(f"'{value}'" for value in PRIORITY_TYPES)


VALIDITY_FUNCTION: Final = """
CREATE FUNCTION ken_priority_validity(
    effective_from timestamptz, effective_to timestamptz, retired_at timestamptz
) RETURNS tstzrange LANGUAGE sql IMMUTABLE AS $fn$
    SELECT tstzrange(
        effective_from,
        CASE WHEN LEAST(effective_to, retired_at) IS NULL THEN NULL
             ELSE GREATEST(effective_from, LEAST(effective_to, retired_at)) END,
        '[)')
$fn$
"""
"""The validity interval of one designation. Mirrored in Python by
``PriorityDesignation.validity_end``; a test holds the two to each other."""

OVERLAP_GUARD: Final = """
CREATE FUNCTION ken_page_priority_no_overlap() RETURNS trigger LANGUAGE plpgsql AS $fn$
DECLARE
    clash uuid;
BEGIN
    -- Serialise per (page, type): two concurrent inserts would otherwise each find the
    -- other absent and both commit an overlap.
    PERFORM pg_advisory_xact_lock(
        hashtext(NEW.page_id::text || ':' || NEW.priority_type)
    );
    SELECT p.id INTO clash
    FROM page_priorities p
    WHERE p.page_id = NEW.page_id
      AND p.priority_type = NEW.priority_type
      AND p.id <> NEW.id
      AND ken_priority_validity(p.effective_from, p.effective_to, p.retired_at)
          && ken_priority_validity(NEW.effective_from, NEW.effective_to, NEW.retired_at)
    LIMIT 1;
    IF clash IS NOT NULL THEN
        RAISE EXCEPTION
            'page_priorities overlap: % already covers this interval for (page %, type %)',
            clash, NEW.page_id, NEW.priority_type
            USING ERRCODE = 'exclusion_violation',
                  CONSTRAINT = 'page_priorities_no_overlap';
    END IF;
    RETURN NEW;
END;
$fn$
"""
"""Two designations of one (page, type) may not be in force at the same instant."""


def _ts(name: str, *, nullable: bool = False, default_now: bool = False) -> sa.Column[Any]:
    return sa.Column(
        name,
        sa.DateTime(timezone=True),
        server_default=sa.func.now() if default_now else None,
        nullable=nullable,
    )


def upgrade() -> None:
    op.create_table(
        "page_priorities",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("page_id", sa.Uuid(), nullable=False),
        sa.Column("priority_type", sa.Text(), nullable=False),
        sa.Column("tier", sa.Integer(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("owner", sa.Text(), nullable=False),
        _ts("effective_from"),
        _ts("effective_to", nullable=True),
        _ts("retired_at", nullable=True),
        _ts("created_at", default_now=True),
        sa.Column("created_by", sa.Text(), nullable=False),
        _ts("updated_at", default_now=True),
        sa.Column("updated_by", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_page_priorities")),
        sa.ForeignKeyConstraint(
            ["page_id"],
            ["pages.id"],
            name=op.f("fk_page_priorities_page_id_pages"),
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            f"priority_type IN ({_TYPES})",
            name=op.f("ck_page_priorities_priority_type_valid"),
        ),
        sa.CheckConstraint(
            f"tier >= {MIN_TIER} AND tier <= {MAX_TIER}",
            name=op.f("ck_page_priorities_tier_range"),
        ),
        sa.CheckConstraint(
            "length(btrim(reason)) > 0", name=op.f("ck_page_priorities_reason_present")
        ),
        sa.CheckConstraint(
            "length(btrim(owner)) > 0", name=op.f("ck_page_priorities_owner_present")
        ),
        sa.CheckConstraint(
            "length(btrim(created_by)) > 0",
            name=op.f("ck_page_priorities_created_by_present"),
        ),
        sa.CheckConstraint(
            "length(btrim(updated_by)) > 0",
            name=op.f("ck_page_priorities_updated_by_present"),
        ),
        sa.CheckConstraint(
            "effective_to IS NULL OR effective_to > effective_from",
            name=op.f("ck_page_priorities_window_ordered"),
        ),
    )
    op.create_index("ix_page_priorities_page_id", "page_priorities", ["page_id"])
    op.create_index(
        "ix_page_priorities_page_id_type", "page_priorities", ["page_id", "priority_type"]
    )
    op.execute(VALIDITY_FUNCTION)
    op.execute(OVERLAP_GUARD)
    op.execute(
        "CREATE TRIGGER page_priorities_no_overlap "
        "BEFORE INSERT OR UPDATE ON page_priorities "
        "FOR EACH ROW EXECUTE FUNCTION ken_page_priority_no_overlap()"
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS page_priorities_no_overlap ON page_priorities")
    op.drop_index("ix_page_priorities_page_id_type", table_name="page_priorities")
    op.drop_index("ix_page_priorities_page_id", table_name="page_priorities")
    op.drop_table("page_priorities")
    op.execute("DROP FUNCTION IF EXISTS ken_page_priority_no_overlap()")
    op.execute(
        "DROP FUNCTION IF EXISTS ken_priority_validity(timestamptz, timestamptz, timestamptz)"
    )
