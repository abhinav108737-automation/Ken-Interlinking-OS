"""Phase 2 content intelligence: job runs, analyses, embeddings, relationships, protections.

Revision ID: 0003_content_intelligence
Revises: 0002_content_inventory
Create Date: 2026-10-14

Column-for-column with ``ken_core.db.models.intelligence``
(docs/adr/ADR-0010-content-intelligence.md).

* ``CREATE EXTENSION IF NOT EXISTS vector`` -- the first vector column,
  ``page_embeddings.embedding``, arrives here (ADR-0003 deferred the extension
  to exactly this migration). On a server without pgvector this migration fails
  loudly with pgvector's own error; it never silently skips (ADR-0010 section 1).
* ``page_embeddings.embedding`` is a DIMENSION-LESS ``vector``. The dimension is
  the provider's, stored per row in ``dimensions`` and tied to the vector by the
  CHECK ``vector_dims(embedding) = dimensions``. Nothing here names a dimension.
* ``page_analyses`` and ``page_embeddings`` are IMMUTABLE: they join the Phase 1
  append-only trigger (``ken_reject_mutation()``, created by 0002).
* ``page_relationships`` is derived data and deliberately mutable (replaced per
  analysis run); ``page_protections`` rows are deactivated, never deleted (an
  application rule; at most one ACTIVE row per (page, source, rule) is enforced
  by a partial unique index).

Downgrade drops the triggers, the five tables in reverse order and then the
extension, returning exactly to the 0002 state.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql
from sqlalchemy.types import UserDefinedType

revision: str = "0003_content_intelligence"
down_revision: str | Sequence[str] | None = "0002_content_inventory"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

IMMUTABLE_TABLES = ("page_analyses", "page_embeddings")

_RUN_STATUS = "status IN ('running', 'succeeded', 'completed_with_errors', 'failed')"
_JOB_KINDS = (
    "kind IN ('analysis', 'recommendation', 'agent', 'verification', 'analytics_import', "
    "'evaluation', 'autonomy_shadow')"
)
_INTENTS = (
    "intent IN ('commercial_research', 'informational', 'navigational', 'transactional', "
    "'corporate', 'unknown')"
)
_RELATIONSHIP_TYPES = (
    "relationship_type IN ('same_market', 'parent_market', 'submarket', 'related_market', "
    "'same_geography', 'regional_parent', 'supply_chain_upstream', 'supply_chain_downstream', "
    "'complementary_market', 'industry_adjacency', 'semantic_similar')"
)


class _Vector(UserDefinedType[Any]):
    """pgvector ``vector`` WITHOUT a dimension. Local to this migration on purpose:

    a migration is a frozen snapshot and must not change when application types do.
    """

    cache_ok = True

    def get_col_spec(self, **kw: Any) -> str:
        return "vector"


def _ts(name: str, *, nullable: bool = False, default_now: bool = False) -> sa.Column[Any]:
    return sa.Column(
        name,
        sa.DateTime(timezone=True),
        server_default=sa.func.now() if default_now else None,
        nullable=nullable,
    )


def _jsonb(name: str, default: str) -> sa.Column[Any]:
    return sa.Column(name, postgresql.JSONB(), server_default=sa.text(default), nullable=False)


def _gin(name: str, column: str) -> None:
    op.create_index(
        op.f(name),
        "page_analyses",
        [column],
        postgresql_using="gin",
        postgresql_ops={column: "jsonb_path_ops"},
    )


def upgrade() -> None:
    # pgvector's own error if the extension is not available: never a silent skip.
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    # op.f(): names below are final (the naming convention must not be re-applied).
    op.create_table(
        "job_runs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("triggered_by", sa.Text(), nullable=False),
        _ts("started_at", default_now=True),
        _ts("completed_at", nullable=True),
        _jsonb("counters", "'{}'::jsonb"),
        _jsonb("config_snapshot", "'{}'::jsonb"),
        sa.Column("failure_reason", sa.Text(), nullable=True),
        _ts("created_at", default_now=True),
        _ts("updated_at", default_now=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_job_runs")),
        sa.CheckConstraint(_JOB_KINDS, name=op.f("ck_job_runs_kind_valid")),
        sa.CheckConstraint(_RUN_STATUS, name=op.f("ck_job_runs_status_valid")),
        sa.CheckConstraint(
            "(status = 'running') = (completed_at IS NULL)",
            name=op.f("ck_job_runs_completed_iff_not_running"),
        ),
        sa.CheckConstraint(
            "completed_at IS NULL OR completed_at >= started_at",
            name=op.f("ck_job_runs_completed_after_started"),
        ),
    )
    op.create_index(
        op.f("ix_job_runs_kind_started_at"), "job_runs", ["kind", sa.text("started_at DESC")]
    )

    op.create_table(
        "page_analyses",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("page_id", sa.Uuid(), nullable=False),
        sa.Column("page_version", sa.Integer(), nullable=False),
        sa.Column("analysis_version", sa.Text(), nullable=False),
        sa.Column("extractor", sa.Text(), nullable=False),
        sa.Column("page_type", sa.Text(), nullable=False),
        sa.Column("intent", sa.Text(), nullable=False),
        sa.Column("commercial_importance", sa.Integer(), nullable=False),
        sa.Column("industry_id", sa.Text(), nullable=True),
        sa.Column("market_id", sa.Text(), nullable=True),
        _jsonb("topics", "'[]'::jsonb"),
        _jsonb("geographies", "'[]'::jsonb"),
        _jsonb("entities", "'[]'::jsonb"),
        _jsonb("keywords", "'[]'::jsonb"),
        sa.Column("summary", sa.Text(), server_default="", nullable=False),
        _jsonb("salient_blocks", "'[]'::jsonb"),
        sa.Column("run_id", sa.Uuid(), nullable=True),
        _ts("created_at", default_now=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_page_analyses")),
        sa.ForeignKeyConstraint(
            ["page_id", "page_version"],
            ["page_versions.page_id", "page_versions.version"],
            name=op.f("fk_page_analyses_page_id_page_version_page_versions"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["job_runs.id"],
            name=op.f("fk_page_analyses_run_id_job_runs"),
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint(
            "page_id",
            "page_version",
            "analysis_version",
            name=op.f("uq_page_analyses_page_id_page_version_analysis_version"),
        ),
        sa.CheckConstraint(_INTENTS, name=op.f("ck_page_analyses_intent_valid")),
        sa.CheckConstraint(
            "commercial_importance BETWEEN 0 AND 3", name=op.f("ck_page_analyses_importance_range")
        ),
        sa.CheckConstraint(
            "extractor IN ('deterministic', 'llm')", name=op.f("ck_page_analyses_extractor_valid")
        ),
    )
    op.create_index(op.f("ix_page_analyses_market_id"), "page_analyses", ["market_id"])
    # Membership probes: topics @> '[{"node_id": "x"}]' (likewise geographies / entities).
    _gin("ix_page_analyses_topics", "topics")
    _gin("ix_page_analyses_geographies", "geographies")
    _gin("ix_page_analyses_entities", "entities")

    op.create_table(
        "page_embeddings",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("page_id", sa.Uuid(), nullable=False),
        sa.Column("page_version", sa.Integer(), nullable=False),
        sa.Column("model", sa.Text(), nullable=False),
        sa.Column("provider", sa.Text(), nullable=False),
        sa.Column("dimensions", sa.Integer(), nullable=False),
        sa.Column("recipe_version", sa.Integer(), nullable=False),
        sa.Column("input_hash", sa.Text(), nullable=False),
        sa.Column("embedding", _Vector(), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=True),
        _ts("created_at", default_now=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_page_embeddings")),
        sa.ForeignKeyConstraint(
            ["page_id", "page_version"],
            ["page_versions.page_id", "page_versions.version"],
            name=op.f("fk_page_embeddings_page_id_page_version_page_versions"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["job_runs.id"],
            name=op.f("fk_page_embeddings_run_id_job_runs"),
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint(
            "page_id",
            "page_version",
            "model",
            "recipe_version",
            name=op.f("uq_page_embeddings_page_id_page_version_model_recipe_version"),
        ),
        sa.CheckConstraint("dimensions > 0", name=op.f("ck_page_embeddings_dimensions_positive")),
        sa.CheckConstraint(
            "vector_dims(embedding) = dimensions",
            name=op.f("ck_page_embeddings_dimensions_match"),
        ),
        sa.CheckConstraint(
            "input_hash ~ '^[0-9a-f]{64}$'", name=op.f("ck_page_embeddings_input_hash_sha256")
        ),
    )
    op.create_index(
        op.f("ix_page_embeddings_model_dimensions"), "page_embeddings", ["model", "dimensions"]
    )

    op.create_table(
        "page_relationships",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("source_page_id", sa.Uuid(), nullable=False),
        sa.Column("source_page_version", sa.Integer(), nullable=False),
        sa.Column("target_page_id", sa.Uuid(), nullable=False),
        sa.Column("target_page_version", sa.Integer(), nullable=False),
        sa.Column("relationship_type", sa.Text(), nullable=False),
        sa.Column("weight", sa.Float(), nullable=False),
        _jsonb("evidence", "'{}'::jsonb"),
        sa.Column("analysis_version", sa.Text(), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=True),
        _ts("computed_at"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_page_relationships")),
        sa.ForeignKeyConstraint(
            ["source_page_id"],
            ["pages.id"],
            name=op.f("fk_page_relationships_source_page_id_pages"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["target_page_id"],
            ["pages.id"],
            name=op.f("fk_page_relationships_target_page_id_pages"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["job_runs.id"],
            name=op.f("fk_page_relationships_run_id_job_runs"),
            ondelete="RESTRICT",
        ),
        # The naming convention's name exceeds 63 characters; this is SQLAlchemy's
        # deterministic truncation of it, exactly as the ORM metadata renders it.
        sa.UniqueConstraint(
            "source_page_id",
            "target_page_id",
            "relationship_type",
            name=op.f("uq_page_relationships_source_page_id_target_page_id_rel_38f9"),
        ),
        sa.CheckConstraint(_RELATIONSHIP_TYPES, name=op.f("ck_page_relationships_type_valid")),
        sa.CheckConstraint(
            "weight >= 0 AND weight <= 1", name=op.f("ck_page_relationships_weight_range")
        ),
        sa.CheckConstraint(
            "source_page_id <> target_page_id", name=op.f("ck_page_relationships_not_self")
        ),
    )
    op.create_index(
        op.f("ix_page_relationships_target_page_id"), "page_relationships", ["target_page_id"]
    )

    op.create_table(
        "page_protections",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("page_id", sa.Uuid(), nullable=False),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("rule_id", sa.Text(), nullable=True),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("created_by", sa.Text(), nullable=False),
        _ts("created_at", default_now=True),
        _ts("deactivated_at", nullable=True),
        sa.Column("deactivated_by", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_page_protections")),
        sa.ForeignKeyConstraint(
            ["page_id"],
            ["pages.id"],
            name=op.f("fk_page_protections_page_id_pages"),
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "source IN ('policy', 'cms', 'manual')", name=op.f("ck_page_protections_source_valid")
        ),
        sa.CheckConstraint(
            "active = (deactivated_at IS NULL)",
            name=op.f("ck_page_protections_active_iff_not_deactivated"),
        ),
    )
    # At most one ACTIVE protection per (page, source, rule); history rows are unlimited.
    op.create_index(
        op.f("uq_page_protections_active"),
        "page_protections",
        ["page_id", "source", sa.text("coalesce(rule_id, '')")],
        unique=True,
        postgresql_where=sa.text("active"),
    )

    # Immutability: the Phase 1 function, now also guarding analyses and embeddings.
    for table in IMMUTABLE_TABLES:
        op.execute(
            f"CREATE TRIGGER {table}_immutable BEFORE UPDATE OR DELETE ON {table} "
            "FOR EACH ROW EXECUTE FUNCTION ken_reject_mutation()"
        )


def downgrade() -> None:
    for table in IMMUTABLE_TABLES:
        op.execute(f"DROP TRIGGER IF EXISTS {table}_immutable ON {table}")
    # Dropping a table drops its indexes and constraints with it.
    op.drop_table("page_protections")
    op.drop_table("page_relationships")
    op.drop_table("page_embeddings")
    op.drop_table("page_analyses")
    op.drop_table("job_runs")
    # Only this migration created the extension, and nothing at 0002 uses it.
    op.execute("DROP EXTENSION IF EXISTS vector")
