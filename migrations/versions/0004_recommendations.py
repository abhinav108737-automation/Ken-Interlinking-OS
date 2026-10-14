"""Phase 3 recommendation engine: link plans, transitions, candidates, opportunities.

Revision ID: 0004_recommendations
Revises: 0003_content_intelligence
Create Date: 2026-10-14

Column-for-column with ``ken_core.db.models.recommendations`` and the new
``page_versions.search_vector`` of ``ken_core.db.models.inventory``
(docs/adr/ADR-0011-recommendation-engine.md).

* ``page_versions.search_vector`` is a STORED generated ``tsvector`` (title A,
  description B, first 100k characters of body text C) with a GIN index: the
  lexical retrieval channel uses built-in full-text search, no new
  infrastructure. Adding a stored generated column REWRITES the table; a table
  rewrite fires no row trigger, so the append-only rule of ``page_versions``
  (``ken_reject_mutation``) is unaffected, and new rows get the value on INSERT.
* ``link_plans`` content is IMMUTABLE. ``ken_link_plan_content_guard()`` is
  GENERIC: a BEFORE UPDATE row trigger rejects the update when
  ``to_jsonb(NEW) - 'status' - 'updated_at'`` differs from the same expression
  on OLD (so a column added later is immutable automatically), and a BEFORE
  DELETE trigger rejects every delete. Only ``status`` / ``updated_at`` move.
* ``link_plan_transitions`` and ``link_candidates`` are APPEND-ONLY: they join
  the Phase 1 ``ken_reject_mutation()`` trigger (created by 0002).
* ``opportunities`` is derived data, replaced per run (deliberately mutable).
* Row triggers do not fire on TRUNCATE (test fixtures rely on it).

Downgrade drops the triggers, the guard function, the four tables, the GIN
index and the column, returning exactly to the 0003 state.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0004_recommendations"
down_revision: str | Sequence[str] | None = "0003_content_intelligence"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

APPEND_ONLY_TABLES = ("link_plan_transitions", "link_candidates")

_STATUSES = (
    "'proposed', 'critiqued', 'policy_checked', 'awaiting_approval', 'approved', 'rejected', "
    "'superseded', 'validated', 'deployed', 'verified', 'failed', 'rolled_back', 'expired', "
    "'measured', 'learning_captured'"
)
_OPEN = "status IN ('proposed', 'critiqued', 'policy_checked', 'awaiting_approval', 'approved', 'validated')"  # noqa: E501
_REJECTIONS = (
    "rejection IN ('self_link', 'source_protected', 'target_protected', 'experiment_locked', "
    "'source_ineligible', 'target_ineligible', 'already_linked', 'duplicate_recommendation', "
    "'previously_rejected', 'target_excluded', 'page_type_incompatible', 'below_min_relevance', "
    "'no_natural_anchor', 'below_min_score', 'link_density_exceeded', 'per_source_limit', "
    "'diversity_limit')"
)
SEARCH_VECTOR = (
    "setweight(to_tsvector('english'::regconfig, left(coalesce(title, ''), 2000)), 'A') || "
    "setweight(to_tsvector('english'::regconfig, "
    "left(coalesce(meta_description, ''), 5000)), 'B') || "
    "setweight(to_tsvector('english'::regconfig, left(body_text, 100000)), 'C')"
)


def _ts(name: str, *, nullable: bool = False, default_now: bool = False) -> sa.Column[Any]:
    return sa.Column(
        name,
        sa.DateTime(timezone=True),
        server_default=sa.func.now() if default_now else None,
        nullable=nullable,
    )


def _jsonb(name: str, default: str) -> sa.Column[Any]:
    return sa.Column(name, postgresql.JSONB(), server_default=sa.text(default), nullable=False)


def upgrade() -> None:
    # ------------------------------------------------ lexical retrieval column
    op.add_column(
        "page_versions",
        sa.Column(
            "search_vector",
            postgresql.TSVECTOR(),
            sa.Computed(SEARCH_VECTOR, persisted=True),
            nullable=True,
        ),
    )
    op.create_index(
        op.f("ix_page_versions_search_vector"),
        "page_versions",
        ["search_vector"],
        postgresql_using="gin",
    )

    # op.f(): names below are final (the naming convention must not be re-applied).
    op.create_table(
        "link_plans",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=True),
        sa.Column("source_page_id", sa.Uuid(), nullable=False),
        sa.Column("source_page_version", sa.Integer(), nullable=False),
        sa.Column("source_url", sa.Text(), nullable=False),
        sa.Column("target_page_id", sa.Uuid(), nullable=False),
        sa.Column("target_page_version", sa.Integer(), nullable=False),
        sa.Column("target_url", sa.Text(), nullable=False),
        sa.Column("target_title", sa.Text(), nullable=True),
        sa.Column("relationship_type", sa.Text(), nullable=True),
        _jsonb("channels", "'[]'::jsonb"),
        sa.Column("anchor_text", sa.Text(), nullable=False),
        sa.Column("anchor_source", sa.Text(), nullable=False),
        sa.Column("block_id", sa.Text(), nullable=False),
        sa.Column("block_kind", sa.Text(), nullable=False),
        sa.Column("occurrence_index", sa.Integer(), nullable=False),
        sa.Column("start_offset", sa.Integer(), nullable=False),
        sa.Column("end_offset", sa.Integer(), nullable=False),
        sa.Column("context_hash", sa.Text(), nullable=False),
        _jsonb("preview", "'{}'::jsonb"),
        _jsonb("score", "'{}'::jsonb"),
        sa.Column("total_score", sa.Float(), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("calibration_version", sa.Text(), nullable=True),
        _jsonb("reason_codes", "'[]'::jsonb"),
        sa.Column("rationale", sa.Text(), nullable=False),
        sa.Column("engine_version", sa.Text(), nullable=False),
        sa.Column("policy_version", sa.Integer(), nullable=False),
        sa.Column("ranking_policy_version", sa.Integer(), nullable=False),
        sa.Column("embedding_model", sa.Text(), nullable=True),
        sa.Column("reasoning_model", sa.Text(), nullable=True),
        sa.Column("analysis_version", sa.Text(), nullable=False),
        sa.Column("supersedes", sa.Uuid(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False),
        _ts("created_at", default_now=True),
        _ts("updated_at", default_now=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_link_plans")),
        sa.ForeignKeyConstraint(
            ["source_page_id", "source_page_version"],
            ["page_versions.page_id", "page_versions.version"],
            name=op.f("fk_link_plans_source_page_id_source_page_version_page_versions"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["target_page_id", "target_page_version"],
            ["page_versions.page_id", "page_versions.version"],
            name=op.f("fk_link_plans_target_page_id_target_page_version_page_versions"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["job_runs.id"],
            name=op.f("fk_link_plans_run_id_job_runs"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["supersedes"],
            ["link_plans.id"],
            name=op.f("fk_link_plans_supersedes_link_plans"),
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(f"status IN ({_STATUSES})", name=op.f("ck_link_plans_status_valid")),
        sa.CheckConstraint("source_page_id <> target_page_id", name=op.f("ck_link_plans_not_self")),
        sa.CheckConstraint(
            "start_offset >= 0 AND end_offset > start_offset",
            name=op.f("ck_link_plans_offsets_valid"),
        ),
        sa.CheckConstraint(
            "occurrence_index >= 0", name=op.f("ck_link_plans_occurrence_non_negative")
        ),
        sa.CheckConstraint(
            "total_score >= 0 AND total_score <= 1", name=op.f("ck_link_plans_score_range")
        ),
        sa.CheckConstraint(
            "confidence >= 0 AND confidence <= 1", name=op.f("ck_link_plans_confidence_range")
        ),
        sa.CheckConstraint(
            "context_hash ~ '^[0-9a-f]{64}$'", name=op.f("ck_link_plans_context_hash_sha256")
        ),
        sa.CheckConstraint(
            "length(anchor_text) BETWEEN 1 AND 120", name=op.f("ck_link_plans_anchor_length")
        ),
    )
    # Never two OPEN plans for one pair, nor for one anchor start in one block TEXT.
    op.create_index(
        op.f("uq_link_plans_open_pair"),
        "link_plans",
        ["source_page_id", "target_page_id"],
        unique=True,
        postgresql_where=sa.text(_OPEN),
    )
    op.create_index(
        op.f("uq_link_plans_open_span"),
        "link_plans",
        ["source_page_id", "context_hash", "start_offset"],
        unique=True,
        postgresql_where=sa.text(_OPEN),
    )
    # A plan is superseded by at most one edit.
    op.create_index(
        op.f("uq_link_plans_supersedes"),
        "link_plans",
        ["supersedes"],
        unique=True,
        postgresql_where=sa.text("supersedes IS NOT NULL"),
    )
    op.create_index(
        op.f("ix_link_plans_status_total_score"),
        "link_plans",
        ["status", sa.text("total_score DESC")],
    )
    op.create_index(op.f("ix_link_plans_target_page_id"), "link_plans", ["target_page_id"])

    op.create_table(
        "link_plan_transitions",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("plan_id", sa.Uuid(), nullable=False),
        sa.Column("from_status", sa.Text(), nullable=True),
        sa.Column("to_status", sa.Text(), nullable=False),
        sa.Column("actor", sa.Text(), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        _ts("occurred_at", default_now=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_link_plan_transitions")),
        sa.ForeignKeyConstraint(
            ["plan_id"],
            ["link_plans.id"],
            name=op.f("fk_link_plan_transitions_plan_id_link_plans"),
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            f"to_status IN ({_STATUSES})", name=op.f("ck_link_plan_transitions_to_status_valid")
        ),
        sa.CheckConstraint(
            f"from_status IS NULL OR from_status IN ({_STATUSES})",
            name=op.f("ck_link_plan_transitions_from_status_valid"),
        ),
        sa.CheckConstraint(
            "length(actor) > 0", name=op.f("ck_link_plan_transitions_actor_present")
        ),
    )
    op.create_index(
        op.f("ix_link_plan_transitions_plan_id_id"), "link_plan_transitions", ["plan_id", "id"]
    )

    op.create_table(
        "link_candidates",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("source_page_id", sa.Uuid(), nullable=False),
        sa.Column("source_page_version", sa.Integer(), nullable=False),
        sa.Column("target_page_id", sa.Uuid(), nullable=False),
        sa.Column("target_page_version", sa.Integer(), nullable=False),
        _jsonb("channels", "'[]'::jsonb"),
        _jsonb("channel_scores", "'{}'::jsonb"),
        sa.Column("total_score", sa.Float(), nullable=True),
        sa.Column("rejection", sa.Text(), nullable=True),
        sa.Column("plan_id", sa.Uuid(), nullable=True),
        _ts("created_at", default_now=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_link_candidates")),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["job_runs.id"],
            name=op.f("fk_link_candidates_run_id_job_runs"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["source_page_id"],
            ["pages.id"],
            name=op.f("fk_link_candidates_source_page_id_pages"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["target_page_id"],
            ["pages.id"],
            name=op.f("fk_link_candidates_target_page_id_pages"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["plan_id"],
            ["link_plans.id"],
            name=op.f("fk_link_candidates_plan_id_link_plans"),
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint(
            "run_id",
            "source_page_id",
            "target_page_id",
            name=op.f("uq_link_candidates_run_id_source_page_id_target_page_id"),
        ),
        sa.CheckConstraint(
            f"rejection IS NULL OR {_REJECTIONS}", name=op.f("ck_link_candidates_rejection_valid")
        ),
        sa.CheckConstraint(
            "(rejection IS NULL) = (plan_id IS NOT NULL)",
            name=op.f("ck_link_candidates_kept_iff_planned"),
        ),
    )
    op.create_index(
        op.f("ix_link_candidates_source_page_id"), "link_candidates", ["source_page_id"]
    )

    op.create_table(
        "opportunities",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("page_id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("score", sa.Float(), nullable=False),
        _jsonb("evidence", "'{}'::jsonb"),
        _ts("computed_at"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_opportunities")),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["job_runs.id"],
            name=op.f("fk_opportunities_run_id_job_runs"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["page_id"],
            ["pages.id"],
            name=op.f("fk_opportunities_page_id_pages"),
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint("page_id", "kind", name=op.f("uq_opportunities_page_id_kind")),
        sa.CheckConstraint(
            "kind IN ('orphan', 'underlinked', 'link_capacity')",
            name=op.f("ck_opportunities_kind_valid"),
        ),
        sa.CheckConstraint("score >= 0 AND score <= 1", name=op.f("ck_opportunities_score_range")),
    )

    # Plan content immutability: GENERIC (every column except status / updated_at).
    op.execute(
        """
        CREATE FUNCTION ken_link_plan_content_guard() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            IF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION 'table % rows are never deleted', TG_TABLE_NAME
                    USING ERRCODE = '55000';
            END IF;
            IF (to_jsonb(NEW) - 'status' - 'updated_at')
                IS DISTINCT FROM (to_jsonb(OLD) - 'status' - 'updated_at') THEN
                RAISE EXCEPTION 'table % content is immutable: only status may change',
                    TG_TABLE_NAME
                    USING ERRCODE = '55000';
            END IF;
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        "CREATE TRIGGER link_plans_content_immutable BEFORE UPDATE ON link_plans "
        "FOR EACH ROW EXECUTE FUNCTION ken_link_plan_content_guard()"
    )
    op.execute(
        "CREATE TRIGGER link_plans_no_delete BEFORE DELETE ON link_plans "
        "FOR EACH ROW EXECUTE FUNCTION ken_link_plan_content_guard()"
    )
    # Append-only history and candidate audit: the Phase 1 function (migration 0002).
    for table in APPEND_ONLY_TABLES:
        op.execute(
            f"CREATE TRIGGER {table}_immutable BEFORE UPDATE OR DELETE ON {table} "
            "FOR EACH ROW EXECUTE FUNCTION ken_reject_mutation()"
        )


def downgrade() -> None:
    for table in APPEND_ONLY_TABLES:
        op.execute(f"DROP TRIGGER IF EXISTS {table}_immutable ON {table}")
    op.execute("DROP TRIGGER IF EXISTS link_plans_no_delete ON link_plans")
    op.execute("DROP TRIGGER IF EXISTS link_plans_content_immutable ON link_plans")
    op.execute("DROP FUNCTION IF EXISTS ken_link_plan_content_guard()")
    # Dropping a table drops its indexes and constraints with it.
    op.drop_table("opportunities")
    op.drop_table("link_candidates")
    op.drop_table("link_plan_transitions")
    op.drop_table("link_plans")
    op.drop_index(op.f("ix_page_versions_search_vector"), table_name="page_versions")
    op.drop_column("page_versions", "search_vector")
