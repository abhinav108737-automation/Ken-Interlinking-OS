"""Phase 1 content inventory: runs, issues, pages, immutable versions and links, known URLs.

Revision ID: 0002_content_inventory
Revises: 0001_initial_events
Create Date: 2026-10-13

Column-for-column with ``ken_core.db.models.inventory`` (docs/adr/ADR-0009-content-inventory.md).

* ``page_versions`` and ``existing_links`` are IMMUTABLE: a row trigger calling
  ``ken_reject_mutation()`` rejects every UPDATE and DELETE (SQLSTATE 55000).
  A row trigger does not stop TRUNCATE / DROP TABLE, which only the owner can run.
* ``pages(id, current_version)`` references ``page_versions(page_id, version)``
  through a DEFERRABLE INITIALLY DEFERRED foreign key, created after both tables
  exist because the reference is circular (``page_versions.page_id -> pages.id``).
* No ``CREATE EXTENSION``: plpgsql is built in. pgvector arrives in Phase 2.

Downgrade returns exactly to the 0001 state (only ``events``).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002_content_inventory"
down_revision: str | Sequence[str] | None = "0001_initial_events"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

IMMUTABLE_TABLES = ("page_versions", "existing_links")

_SOURCE_TYPES = "source_type IN ('public_web', 'file_export', 'cms')"
_PUBLICATION = "publication_state IN ('published', 'draft', 'unpublished', 'unknown')"
_SHA256 = "content_hash ~ '^[0-9a-f]{64}$'"


def _ts(name: str, *, nullable: bool = False, default_now: bool = False) -> sa.Column[object]:
    return sa.Column(
        name,
        sa.DateTime(timezone=True),
        server_default=sa.func.now() if default_now else None,
        nullable=nullable,
    )


def _counter(name: str) -> sa.Column[object]:
    return sa.Column(name, sa.Integer(), server_default="0", nullable=False)


def _jsonb(name: str, default: str) -> sa.Column[object]:
    return sa.Column(name, postgresql.JSONB(), server_default=sa.text(default), nullable=False)


def upgrade() -> None:
    # op.f(): names below are final (the naming convention must not be re-applied).
    op.create_table(
        "ingestion_runs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("source_type", sa.Text(), nullable=False),
        sa.Column("source_name", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        _ts("started_at", default_now=True),
        _ts("completed_at", nullable=True),
        _counter("pages_discovered"),
        _counter("pages_fetched"),
        _counter("pages_created"),
        _counter("pages_changed"),
        _counter("pages_unchanged"),
        _counter("pages_duplicate"),
        _counter("pages_failed"),
        _counter("pages_skipped"),
        _counter("pages_gone"),
        _counter("error_count"),
        _counter("warning_count"),
        _jsonb("config_snapshot", "'{}'::jsonb"),
        sa.Column("failure_reason", sa.Text(), nullable=True),
        _ts("created_at", default_now=True),
        _ts("updated_at", default_now=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_ingestion_runs")),
        sa.CheckConstraint(_SOURCE_TYPES, name=op.f("ck_ingestion_runs_source_type_valid")),
        sa.CheckConstraint(
            "status IN ('running', 'succeeded', 'completed_with_errors', 'failed')",
            name=op.f("ck_ingestion_runs_status_valid"),
        ),
        sa.CheckConstraint(
            "(status = 'running') = (completed_at IS NULL)",
            name=op.f("ck_ingestion_runs_completed_iff_not_running"),
        ),
        sa.CheckConstraint(
            "completed_at IS NULL OR completed_at >= started_at",
            name=op.f("ck_ingestion_runs_completed_after_started"),
        ),
        sa.CheckConstraint(
            "LEAST(pages_discovered, pages_fetched, pages_created, pages_changed, "
            "pages_unchanged, pages_duplicate, pages_failed, pages_skipped, pages_gone, "
            "error_count, warning_count) >= 0",
            name=op.f("ck_ingestion_runs_counters_non_negative"),
        ),
    )
    op.create_index(
        op.f("ix_ingestion_runs_started_at"), "ingestion_runs", [sa.text("started_at DESC")]
    )

    op.create_table(
        "ingestion_issues",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("code", sa.Text(), nullable=False),
        sa.Column("severity", sa.Text(), nullable=False),
        sa.Column("url", sa.Text(), nullable=True),
        sa.Column("related_url", sa.Text(), nullable=True),
        sa.Column("http_status", sa.Integer(), nullable=True),
        sa.Column("message", sa.Text(), nullable=False),
        _ts("occurred_at"),
        _ts("created_at", default_now=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_ingestion_issues")),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["ingestion_runs.id"],
            name=op.f("fk_ingestion_issues_run_id_ingestion_runs"),
            ondelete="CASCADE",
        ),
        sa.CheckConstraint(
            "severity IN ('info', 'warning', 'error')",
            name=op.f("ck_ingestion_issues_severity_valid"),
        ),
    )
    op.create_index(op.f("ix_ingestion_issues_run_id_id"), "ingestion_issues", ["run_id", "id"])

    op.create_table(
        "pages",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("canonical_url", sa.Text(), nullable=False),
        sa.Column("external_id", sa.Text(), nullable=True),
        sa.Column("current_version", sa.Integer(), nullable=False),
        sa.Column("content_hash", sa.Text(), nullable=False),
        sa.Column("title", sa.Text(), nullable=True),
        sa.Column("publication_state", sa.Text(), nullable=False),
        sa.Column("page_type", sa.Text(), nullable=True),
        sa.Column("language", sa.Text(), nullable=True),
        sa.Column("word_count", sa.Integer(), nullable=False),
        sa.Column("internal_link_count", sa.Integer(), nullable=False),
        sa.Column("external_link_count", sa.Integer(), nullable=False),
        _ts("first_seen_at"),
        _ts("last_seen_at"),
        _ts("last_changed_at"),
        _ts("gone_at", nullable=True),
        _ts("created_at", default_now=True),
        _ts("updated_at", default_now=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_pages")),
        sa.UniqueConstraint("canonical_url", name=op.f("uq_pages_canonical_url")),
        sa.CheckConstraint("current_version >= 1", name=op.f("ck_pages_current_version_positive")),
        sa.CheckConstraint(_PUBLICATION, name=op.f("ck_pages_publication_valid")),
        sa.CheckConstraint(_SHA256, name=op.f("ck_pages_content_hash_sha256")),
    )
    op.create_index(
        op.f("uq_pages_external_id"),
        "pages",
        ["external_id"],
        unique=True,
        postgresql_where=sa.text("external_id IS NOT NULL"),
    )
    op.create_index(op.f("ix_pages_last_changed_at"), "pages", [sa.text("last_changed_at DESC")])

    op.create_table(
        "page_versions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("page_id", sa.Uuid(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("content_hash", sa.Text(), nullable=False),
        sa.Column("hash_version", sa.Integer(), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=True),
        sa.Column("source_type", sa.Text(), nullable=False),
        sa.Column("source_id", sa.Text(), nullable=False),
        sa.Column("requested_url", sa.Text(), nullable=False),
        sa.Column("url", sa.Text(), nullable=False),
        _jsonb("redirect_chain", "'[]'::jsonb"),
        sa.Column("http_status", sa.Integer(), nullable=True),
        sa.Column("canonical_url", sa.Text(), nullable=False),
        sa.Column("declared_canonical_url", sa.Text(), nullable=True),
        sa.Column("canonical_status", sa.Text(), nullable=False),
        sa.Column("title", sa.Text(), nullable=True),
        sa.Column("meta_description", sa.Text(), nullable=True),
        sa.Column("language", sa.Text(), nullable=True),
        sa.Column("publication_state", sa.Text(), nullable=False),
        sa.Column("page_type", sa.Text(), nullable=True),
        _ts("published_at", nullable=True),
        _ts("modified_at", nullable=True),
        _jsonb("robots_directives", "'[]'::jsonb"),
        _jsonb("metadata", "'{}'::jsonb"),
        _jsonb("headings", "'[]'::jsonb"),
        sa.Column("body_html", sa.Text(), nullable=False),
        sa.Column("body_text", sa.Text(), nullable=False),
        sa.Column("word_count", sa.Integer(), nullable=False),
        sa.Column("internal_link_count", sa.Integer(), nullable=False),
        sa.Column("external_link_count", sa.Integer(), nullable=False),
        _ts("fetched_at"),
        _ts("created_at", default_now=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_page_versions")),
        sa.UniqueConstraint("page_id", "version", name=op.f("uq_page_versions_page_id_version")),
        sa.ForeignKeyConstraint(
            ["page_id"],
            ["pages.id"],
            name=op.f("fk_page_versions_page_id_pages"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["ingestion_runs.id"],
            name=op.f("fk_page_versions_run_id_ingestion_runs"),
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint("version >= 1", name=op.f("ck_page_versions_version_positive")),
        sa.CheckConstraint(
            "hash_version >= 1", name=op.f("ck_page_versions_hash_version_positive")
        ),
        sa.CheckConstraint(_SHA256, name=op.f("ck_page_versions_content_hash_sha256")),
        sa.CheckConstraint(_SOURCE_TYPES, name=op.f("ck_page_versions_source_type_valid")),
        sa.CheckConstraint(_PUBLICATION, name=op.f("ck_page_versions_publication_valid")),
        sa.CheckConstraint(
            "canonical_status IN ('self', 'declared', 'missing', 'out_of_scope', 'invalid')",
            name=op.f("ck_page_versions_canonical_status_valid"),
        ),
    )
    op.create_index(op.f("ix_page_versions_run_id"), "page_versions", ["run_id"])

    op.create_table(
        "existing_links",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("page_id", sa.Uuid(), nullable=False),
        sa.Column("page_version", sa.Integer(), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("href", sa.Text(), nullable=False),
        sa.Column("resolved_url", sa.Text(), nullable=True),
        sa.Column("link_type", sa.Text(), nullable=False),
        sa.Column("anchor_text", sa.Text(), nullable=False),
        sa.Column("anchor_source", sa.Text(), nullable=False),
        _jsonb("rel", "'[]'::jsonb"),
        sa.Column("block_id", sa.Text(), nullable=False),
        sa.Column("block_kind", sa.Text(), nullable=False),
        sa.Column("occurrence_index", sa.Integer(), nullable=False),
        sa.Column("context_text", sa.Text(), nullable=False),
        sa.Column("context_hash", sa.Text(), nullable=False),
        _ts("created_at", default_now=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_existing_links")),
        sa.ForeignKeyConstraint(
            ["page_id", "page_version"],
            ["page_versions.page_id", "page_versions.version"],
            name=op.f("fk_existing_links_page_id_page_version_page_versions"),
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint(
            "page_id",
            "page_version",
            "ordinal",
            name=op.f("uq_existing_links_page_id_page_version_ordinal"),
        ),
        sa.CheckConstraint(
            "link_type IN ('internal', 'external', 'fragment', 'non_http', 'invalid')",
            name=op.f("ck_existing_links_link_type_valid"),
        ),
        sa.CheckConstraint("ordinal >= 0", name=op.f("ck_existing_links_ordinal_non_negative")),
        sa.CheckConstraint(
            "occurrence_index >= 0", name=op.f("ck_existing_links_occurrence_non_negative")
        ),
    )
    op.create_index(op.f("ix_existing_links_resolved_url"), "existing_links", ["resolved_url"])

    op.create_table(
        "known_urls",
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("page_id", sa.Uuid(), nullable=True),
        sa.Column("outcome", sa.Text(), nullable=False),
        sa.Column("http_status", sa.Integer(), nullable=True),
        sa.Column("last_run_id", sa.Uuid(), nullable=True),
        _ts("first_seen_at"),
        _ts("last_seen_at"),
        _ts("updated_at", default_now=True),
        sa.PrimaryKeyConstraint("url", name=op.f("pk_known_urls")),
        sa.ForeignKeyConstraint(
            ["page_id"],
            ["pages.id"],
            name=op.f("fk_known_urls_page_id_pages"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["last_run_id"],
            ["ingestion_runs.id"],
            name=op.f("fk_known_urls_last_run_id_ingestion_runs"),
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "outcome IN ('page', 'redirect', 'not_found', 'gone', 'http_error', "
            "'fetch_error', 'blocked_by_robots', 'non_html')",
            name=op.f("ck_known_urls_outcome_valid"),
        ),
        # An implication, not "iff": a later failed observation keeps the last known page.
        sa.CheckConstraint(
            "outcome NOT IN ('page', 'redirect') OR page_id IS NOT NULL",
            name=op.f("ck_known_urls_resolved_has_page"),
        ),
    )
    op.create_index(op.f("ix_known_urls_page_id"), "known_urls", ["page_id"])

    # The circular current-version pointer, checked at COMMIT.
    op.create_foreign_key(
        op.f("fk_pages_id_current_version_page_versions"),
        "pages",
        "page_versions",
        ["id", "current_version"],
        ["page_id", "version"],
        ondelete="RESTRICT",
        deferrable=True,
        initially="DEFERRED",
    )

    # Immutability, enforced by the database rather than by application code.
    op.execute(
        """
        CREATE FUNCTION ken_reject_mutation() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION 'table % is append-only: % is not allowed',
                TG_TABLE_NAME, TG_OP
                USING ERRCODE = '55000';
        END;
        $$
        """
    )
    for table in IMMUTABLE_TABLES:
        op.execute(
            f"CREATE TRIGGER {table}_immutable BEFORE UPDATE OR DELETE ON {table} "
            "FOR EACH ROW EXECUTE FUNCTION ken_reject_mutation()"
        )


def downgrade() -> None:
    for table in IMMUTABLE_TABLES:
        op.execute(f"DROP TRIGGER IF EXISTS {table}_immutable ON {table}")
    op.execute("DROP FUNCTION IF EXISTS ken_reject_mutation()")
    op.drop_constraint(
        op.f("fk_pages_id_current_version_page_versions"), "pages", type_="foreignkey"
    )
    # Dropping a table drops its indexes and constraints with it.
    op.drop_table("known_urls")
    op.drop_table("existing_links")
    op.drop_table("page_versions")
    op.drop_table("pages")
    op.drop_table("ingestion_issues")
    op.drop_table("ingestion_runs")
