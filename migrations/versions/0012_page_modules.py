"""Related-report modules: ``page_modules`` and ``page_module_items``.

Revision ID: 0012_page_modules
Revises: 0011_recommendation_gates
Create Date: 2026-11-09

Column-for-column with ``ken_core.db.models.recommendations.PageModule`` /
``PageModuleItem`` (ADR-0027 §1b/§1c). A module is the owner's Priority 1 -- the same market
in every other geography -- on the one surface that can carry it: the link text is the
target's own title, so it names the geography by construction and no sentence is rewritten.

**Derived, and replaced per run**, exactly like ``opportunities``:

* no ``status`` column, no transition history and no immutability trigger. A ``link_plan``
  has those because a reviewer approves one and the deployment service applies it; a module
  has nowhere to be applied yet (NEXT_STEPS item 12, the CMS surface), so a status would
  record a decision that cannot be carried out.
* ``page_module_items.module_id`` cascades on delete, because the items ARE the module.
  Half a module is not a smaller module, it is a wrong one. This is the only ``CASCADE`` in
  the recommendation schema, and it is deliberate: every other table there holds audit or
  lifecycle rows, which ``RESTRICT`` protects.

``UNIQUE (page_id, kind)`` is what makes a rebuild idempotent: one module per page per kind,
whichever run last wrote it.

The ``kind`` value list is a frozen copy of ``ModuleKind`` at this revision.

Downgrade drops both tables, returning exactly to the 0011 state. No existing table, column,
index or trigger is touched in either direction, so no stored row can become invalid.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Final

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0012_page_modules"
down_revision: str | Sequence[str] | None = "0011_recommendation_gates"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

MODULE_KINDS: Final[tuple[str, ...]] = ("regional_sibling",)
"""Frozen copy of ``ken_contracts.recommendations.ModuleKind`` at this revision."""

_KINDS: Final = ", ".join(f"'{kind}'" for kind in MODULE_KINDS)


def _ts(name: str, *, default_now: bool = False) -> sa.Column[Any]:
    return sa.Column(
        name,
        sa.DateTime(timezone=True),
        server_default=sa.func.now() if default_now else None,
        nullable=False,
    )


def upgrade() -> None:
    op.create_table(
        "page_modules",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("page_id", sa.Uuid(), nullable=False),
        sa.Column("page_version", sa.Integer(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("market_core", sa.Text(), nullable=False),
        sa.Column("geography_id", sa.Text(), nullable=True),
        sa.Column("geography_label", sa.Text(), nullable=True),
        sa.Column("analysis_version", sa.Text(), nullable=False),
        _ts("computed_at", default_now=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_page_modules")),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["job_runs.id"],
            name=op.f("fk_page_modules_run_id_job_runs"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["page_id"],
            ["pages.id"],
            name=op.f("fk_page_modules_page_id_pages"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["page_id", "page_version"],
            ["page_versions.page_id", "page_versions.version"],
            name=op.f("fk_page_modules_page_id_page_version_page_versions"),
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint("page_id", "kind", name=op.f("uq_page_modules_page_id_kind")),
        sa.CheckConstraint(f"kind IN ({_KINDS})", name=op.f("ck_page_modules_kind_valid")),
        sa.CheckConstraint(
            "length(market_core) > 0", name=op.f("ck_page_modules_market_core_present")
        ),
    )
    op.create_index("ix_page_modules_run_id", "page_modules", ["run_id"])

    op.create_table(
        "page_module_items",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("module_id", sa.Uuid(), nullable=False),
        sa.Column("rank", sa.Integer(), nullable=False),
        sa.Column("target_page_id", sa.Uuid(), nullable=False),
        sa.Column("target_url", sa.Text(), nullable=False),
        sa.Column("target_title", sa.Text(), nullable=True),
        sa.Column("geography_label", sa.Text(), nullable=True),
        sa.Column(
            "reason_codes",
            postgresql.JSONB(),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_page_module_items")),
        # CASCADE: the items ARE the module (see the module docstring).
        sa.ForeignKeyConstraint(
            ["module_id"],
            ["page_modules.id"],
            name=op.f("fk_page_module_items_module_id_page_modules"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["target_page_id"],
            ["pages.id"],
            name=op.f("fk_page_module_items_target_page_id_pages"),
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint("module_id", "rank", name=op.f("uq_page_module_items_module_id_rank")),
        sa.UniqueConstraint(
            "module_id",
            "target_page_id",
            name=op.f("uq_page_module_items_module_id_target_page_id"),
        ),
        sa.CheckConstraint("rank >= 1", name=op.f("ck_page_module_items_rank_positive")),
        sa.CheckConstraint(
            "jsonb_typeof(reason_codes) = 'array'",
            name=op.f("ck_page_module_items_reason_codes_array"),
        ),
    )
    op.create_index("ix_page_module_items_target_page_id", "page_module_items", ["target_page_id"])


def downgrade() -> None:
    op.drop_index("ix_page_module_items_target_page_id", table_name="page_module_items")
    op.drop_table("page_module_items")
    op.drop_index("ix_page_modules_run_id", table_name="page_modules")
    op.drop_table("page_modules")
