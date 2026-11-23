"""Opt-in Search Console query-level daily metrics.

Revision ID: 0014_gsc_query_metrics
Revises: 0013_page_priorities
Create Date: 2026-11-22
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0014_gsc_query_metrics"
down_revision: str | Sequence[str] | None = "0013_page_priorities"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_constraint(
        op.f("ck_analytics_imports_dataset_valid"), "analytics_imports", type_="check"
    )
    op.create_check_constraint(
        op.f("ck_analytics_imports_dataset_valid"),
        "analytics_imports",
        "dataset IN ('links', 'pages', 'queries')",
    )
    op.create_check_constraint(
        op.f("ck_analytics_imports_query_dataset_source"),
        "analytics_imports",
        "dataset <> 'queries' OR source = 'search_console'",
    )
    op.create_table(
        "page_query_metrics_daily",
        sa.Column("metric_date", sa.Date(), nullable=False),
        sa.Column("page_id", sa.Uuid(), nullable=False),
        sa.Column("query_hash", sa.Text(), nullable=False),
        sa.Column("query", sa.Text(), nullable=False),
        sa.Column("impressions", sa.Integer(), nullable=False),
        sa.Column("clicks", sa.Integer(), nullable=False),
        sa.Column("position_sum", sa.Float(), nullable=False),
        sa.Column("import_id", sa.Uuid(), nullable=False),
        sa.Column("synthetic", sa.Boolean(), nullable=False),
        sa.PrimaryKeyConstraint(
            "metric_date", "page_id", "query_hash", name=op.f("pk_page_query_metrics_daily")
        ),
        sa.ForeignKeyConstraint(
            ["page_id"],
            ["pages.id"],
            name=op.f("fk_page_query_metrics_daily_page_id_pages"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["import_id"],
            ["analytics_imports.id"],
            name=op.f("fk_page_query_metrics_daily_import_id_analytics_imports"),
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "query_hash ~ '^[0-9a-f]{64}$'",
            name=op.f("ck_page_query_metrics_daily_query_hash_sha256"),
        ),
        sa.CheckConstraint(
            "length(query) BETWEEN 1 AND 500",
            name=op.f("ck_page_query_metrics_daily_query_bounded"),
        ),
        sa.CheckConstraint(
            "impressions >= 0 AND clicks >= 0",
            name=op.f("ck_page_query_metrics_daily_counts_non_negative"),
        ),
        sa.CheckConstraint(
            "position_sum >= 0 AND (impressions > 0 OR position_sum = 0)",
            name=op.f("ck_page_query_metrics_daily_position_sum_valid"),
        ),
    )
    op.create_index(
        op.f("ix_page_query_metrics_daily_page_id_metric_date"),
        "page_query_metrics_daily",
        ["page_id", "metric_date"],
    )
    op.create_index(
        op.f("ix_page_query_metrics_daily_import_id"),
        "page_query_metrics_daily",
        ["import_id"],
    )
    op.execute(
        "CREATE TRIGGER page_query_metrics_daily_writer_guard "
        "BEFORE UPDATE OR DELETE ON page_query_metrics_daily "
        "FOR EACH ROW EXECUTE FUNCTION ken_analytics_writer_guard()"
    )


def downgrade() -> None:
    op.execute(
        "DROP TRIGGER IF EXISTS page_query_metrics_daily_writer_guard "
        "ON page_query_metrics_daily"
    )
    op.drop_table("page_query_metrics_daily")
    op.drop_constraint(
        op.f("ck_analytics_imports_query_dataset_source"),
        "analytics_imports",
        type_="check",
    )
    op.drop_constraint(
        op.f("ck_analytics_imports_dataset_valid"), "analytics_imports", type_="check"
    )
    op.create_check_constraint(
        op.f("ck_analytics_imports_dataset_valid"),
        "analytics_imports",
        "dataset IN ('links', 'pages')",
    )
