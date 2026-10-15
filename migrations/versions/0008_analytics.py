"""Phase 7 analytics: import ledger, first-party link events, daily metrics, measurements.

Revision ID: 0008_analytics
Revises: 0007_controlled_deployment
Create Date: 2026-10-15

Column-for-column with ``ken_core.db.models.analytics`` (docs/adr/ADR-0015-analytics.md §2 as
amended by the binding §12).

* ``analytics_imports`` -- the import LEDGER. One row per SUCCEEDED import or aggregation,
  inserted once in the data transaction; APPEND-ONLY (the Phase 1 ``ken_reject_mutation()``
  trigger). There is no status / counters / error column: failures live in ``job_runs``.
* ``link_events`` -- first-party impressions and clicks. NO PII column (no IP, UA, page-view
  id, referrer, query string, cookie or user id). ``received_at`` is truncated to the minute
  and ``event_date`` is its UTC date (CHECKs). ``dedupe_key`` is UNIQUE.
* ``link_metrics_daily`` (``ga4`` | ``first_party``) and ``page_metrics_daily`` (``ga4`` |
  ``search_console``) -- daily rows, replaced per (source, dataset, date range) by imports.
* The three tables above are guarded by ONE shared function, ``ken_analytics_writer_guard()``:
  UPDATE is always rejected; DELETE is allowed only when the SAME transaction ran
  ``SELECT set_config('ken.analytics_writer', 'on', true)`` (transaction-local, so it never
  leaks to the next transaction on a pooled connection).
* ``link_measurements`` -- APPEND-ONLY (``ken_reject_mutation()``) snapshot written with the
  ``verified -> measured`` transition (which 0007's ``PLAN_TRANSITIONS`` already allows).

No experiment tables or columns: experiments are deferred to Phase 8 (ADR-0015 §12.8) and are
added there additively.

The value lists below are frozen copies of the contract enums at this revision.

Downgrade drops the triggers, the guard function and the five tables, returning exactly to
the 0007 state.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Final

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0008_analytics"
down_revision: str | Sequence[str] | None = "0007_controlled_deployment"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

WRITER_GATED_TABLES: Final = ("link_events", "link_metrics_daily", "page_metrics_daily")
APPEND_ONLY_TABLES: Final = ("analytics_imports", "link_measurements")
WRITER_SETTING: Final = "ken.analytics_writer"
MAX_RANGE_DAYS: Final = 400
MAX_UNRESOLVED_SAMPLE: Final = 100
MAX_FILE_NAME_CHARS: Final = 255

_SOURCES = "'ga4', 'search_console', 'first_party'"
_LINK_SOURCES = "source IN ('ga4', 'first_party')"
_PAGE_SOURCES = "source IN ('ga4', 'search_console')"
_DATASETS = "'links', 'pages'"
_VIA = "'api', 'csv', 'collector', 'synthetic'"
_EVENT_TYPES = "'internal_link_impression', 'internal_link_click'"
_MATCH_MODES = "'attr', 'href'"
_SHA256 = "~ '^[0-9a-f]{64}$'"


def _ts(name: str, *, nullable: bool = False, default_now: bool = False) -> sa.Column[Any]:
    return sa.Column(
        name,
        sa.DateTime(timezone=True),
        server_default=sa.func.now() if default_now else None,
        nullable=nullable,
    )


def _check(expression: str, name: str) -> sa.CheckConstraint:
    return sa.CheckConstraint(expression, name=op.f(name))


def _count(name: str) -> sa.Column[Any]:
    return sa.Column(name, sa.Integer(), nullable=False)


def upgrade() -> None:
    op.create_table(
        "analytics_imports",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("job_run_id", sa.Uuid(), nullable=False),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("dataset", sa.Text(), nullable=False),
        sa.Column("via", sa.Text(), nullable=False),
        sa.Column("synthetic", sa.Boolean(), nullable=False),
        sa.Column("replace_via", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("date_start", sa.Date(), nullable=False),
        sa.Column("date_end", sa.Date(), nullable=False),
        sa.Column("source_timezone", sa.Text(), nullable=False),
        sa.Column("file_name", sa.Text(), nullable=True),
        sa.Column("file_sha256", sa.Text(), nullable=True),
        sa.Column("sampled", sa.Boolean(), server_default="false", nullable=False),
        _count("rows_written"),
        sa.Column("analytics_policy_version", sa.Integer(), nullable=False),
        sa.Column(
            "unresolved_sample",
            postgresql.JSONB(),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("triggered_by", sa.Text(), nullable=False),
        _ts("created_at", default_now=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_analytics_imports")),
        sa.ForeignKeyConstraint(
            ["job_run_id"],
            ["job_runs.id"],
            name=op.f("fk_analytics_imports_job_run_id_job_runs"),
            ondelete="RESTRICT",
        ),
        _check(f"source IN ({_SOURCES})", "ck_analytics_imports_source_valid"),
        _check(f"dataset IN ({_DATASETS})", "ck_analytics_imports_dataset_valid"),
        _check(f"via IN ({_VIA})", "ck_analytics_imports_via_valid"),
        _check(f"dataset <> 'links' OR {_LINK_SOURCES}", "ck_analytics_imports_link_dataset_source"),
        _check(f"dataset <> 'pages' OR {_PAGE_SOURCES}", "ck_analytics_imports_page_dataset_source"),
        _check(
            "via <> 'collector' OR (source = 'first_party' AND dataset = 'links')",
            "ck_analytics_imports_collector_is_first_party_links",
        ),
        _check(
            "via <> 'api' OR source IN ('ga4', 'search_console')",
            "ck_analytics_imports_api_is_google",
        ),
        _check("via <> 'synthetic' OR synthetic", "ck_analytics_imports_synthetic_via_is_synthetic"),
        _check("date_end >= date_start", "ck_analytics_imports_date_range_ordered"),
        _check(
            f"date_end - date_start < {MAX_RANGE_DAYS}", "ck_analytics_imports_date_range_bounded"
        ),
        _check(
            "length(source_timezone) BETWEEN 1 AND 64",
            "ck_analytics_imports_source_timezone_bounded",
        ),
        _check("(via = 'csv') = (file_sha256 IS NOT NULL)", "ck_analytics_imports_file_sha256_iff_csv"),
        _check(
            "(file_name IS NULL) = (file_sha256 IS NULL)",
            "ck_analytics_imports_file_name_iff_sha256",
        ),
        _check(
            f"file_sha256 IS NULL OR file_sha256 {_SHA256}", "ck_analytics_imports_file_sha256_hex"
        ),
        _check(
            f"file_name IS NULL OR (length(file_name) BETWEEN 1 AND {MAX_FILE_NAME_CHARS} "
            "AND file_name !~ '[/\\\\]' AND file_name NOT IN ('.', '..'))",
            "ck_analytics_imports_file_name_is_basename",
        ),
        _check("rows_written >= 0", "ck_analytics_imports_rows_written_non_negative"),
        _check("analytics_policy_version >= 1", "ck_analytics_imports_policy_version_positive"),
        _check(
            "jsonb_typeof(unresolved_sample) = 'array' AND "
            f"jsonb_array_length(unresolved_sample) <= {MAX_UNRESOLVED_SAMPLE}",
            "ck_analytics_imports_unresolved_sample_bounded_array",
        ),
        _check("length(triggered_by) BETWEEN 1 AND 200", "ck_analytics_imports_triggered_by_bounded"),
    )
    op.create_index(
        op.f("ix_analytics_imports_source_dataset_date_end"),
        "analytics_imports",
        ["source", "dataset", sa.text("date_end DESC")],
    )
    op.create_index(
        op.f("ix_analytics_imports_job_run_id"), "analytics_imports", ["job_run_id"]
    )
    op.create_index(
        op.f("ix_analytics_imports_created_at"), "analytics_imports", [sa.text("created_at DESC")]
    )

    op.create_table(
        "link_events",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("event_type", sa.Text(), nullable=False),
        sa.Column("plan_id", sa.Uuid(), nullable=False),
        sa.Column("match_mode", sa.Text(), nullable=False),
        sa.Column("synthetic", sa.Boolean(), nullable=False),
        sa.Column("event_date", sa.Date(), nullable=False),
        _ts("received_at"),
        sa.Column("dedupe_key", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_link_events")),
        sa.UniqueConstraint("dedupe_key", name=op.f("uq_link_events_dedupe_key")),
        sa.ForeignKeyConstraint(
            ["plan_id"],
            ["link_plans.id"],
            name=op.f("fk_link_events_plan_id_link_plans"),
            ondelete="RESTRICT",
        ),
        _check(f"event_type IN ({_EVENT_TYPES})", "ck_link_events_event_type_valid"),
        _check(f"match_mode IN ({_MATCH_MODES})", "ck_link_events_match_mode_valid"),
        _check(f"dedupe_key {_SHA256}", "ck_link_events_dedupe_key_sha256"),
        _check(
            "date_trunc('minute', received_at AT TIME ZONE 'UTC') "
            "= (received_at AT TIME ZONE 'UTC')",
            "ck_link_events_received_at_minute",
        ),
        _check(
            "event_date = (received_at AT TIME ZONE 'UTC')::date",
            "ck_link_events_event_date_is_utc",
        ),
    )
    op.create_index(
        op.f("ix_link_events_plan_id_event_date"), "link_events", ["plan_id", "event_date"]
    )
    op.create_index(op.f("ix_link_events_event_date"), "link_events", ["event_date"])

    op.create_table(
        "link_metrics_daily",
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("metric_date", sa.Date(), nullable=False),
        sa.Column("plan_id", sa.Uuid(), nullable=False),
        _count("impressions"),
        _count("clicks"),
        sa.Column("import_id", sa.Uuid(), nullable=False),
        sa.Column("synthetic", sa.Boolean(), nullable=False),
        sa.PrimaryKeyConstraint(
            "source", "metric_date", "plan_id", name=op.f("pk_link_metrics_daily")
        ),
        sa.ForeignKeyConstraint(
            ["plan_id"],
            ["link_plans.id"],
            name=op.f("fk_link_metrics_daily_plan_id_link_plans"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["import_id"],
            ["analytics_imports.id"],
            name=op.f("fk_link_metrics_daily_import_id_analytics_imports"),
            ondelete="RESTRICT",
        ),
        _check(_LINK_SOURCES, "ck_link_metrics_daily_source_valid"),
        _check("impressions >= 0 AND clicks >= 0", "ck_link_metrics_daily_counts_non_negative"),
    )
    op.create_index(
        op.f("ix_link_metrics_daily_plan_id_metric_date"),
        "link_metrics_daily",
        ["plan_id", "metric_date"],
    )
    op.create_index(
        op.f("ix_link_metrics_daily_import_id"), "link_metrics_daily", ["import_id"]
    )

    op.create_table(
        "page_metrics_daily",
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("metric_date", sa.Date(), nullable=False),
        sa.Column("page_id", sa.Uuid(), nullable=False),
        _count("impressions"),
        _count("clicks"),
        sa.Column("position_sum", sa.Float(), nullable=False),
        _count("pageviews"),
        _count("sessions"),
        sa.Column("import_id", sa.Uuid(), nullable=False),
        sa.Column("synthetic", sa.Boolean(), nullable=False),
        sa.PrimaryKeyConstraint(
            "source", "metric_date", "page_id", name=op.f("pk_page_metrics_daily")
        ),
        sa.ForeignKeyConstraint(
            ["page_id"],
            ["pages.id"],
            name=op.f("fk_page_metrics_daily_page_id_pages"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["import_id"],
            ["analytics_imports.id"],
            name=op.f("fk_page_metrics_daily_import_id_analytics_imports"),
            ondelete="RESTRICT",
        ),
        _check(_PAGE_SOURCES, "ck_page_metrics_daily_source_valid"),
        _check(
            "impressions >= 0 AND clicks >= 0 AND pageviews >= 0 AND sessions >= 0",
            "ck_page_metrics_daily_counts_non_negative",
        ),
        _check(
            "position_sum >= 0 AND (impressions > 0 OR position_sum = 0)",
            "ck_page_metrics_daily_position_sum_valid",
        ),
    )
    op.create_index(
        op.f("ix_page_metrics_daily_page_id_metric_date"),
        "page_metrics_daily",
        ["page_id", "metric_date"],
    )
    op.create_index(
        op.f("ix_page_metrics_daily_import_id"), "page_metrics_daily", ["import_id"]
    )

    op.create_table(
        "link_measurements",
        sa.Column("plan_id", sa.Uuid(), nullable=False),
        _ts("measured_at", default_now=True),
        sa.Column("analytics_policy_version", sa.Integer(), nullable=False),
        sa.Column("window_start", sa.Date(), nullable=False),
        sa.Column("window_end", sa.Date(), nullable=False),
        sa.Column("link_totals", postgresql.JSONB(), nullable=False),
        sa.Column("target_comparison", postgresql.JSONB(), nullable=False),
        sa.Column("source_comparison", postgresql.JSONB(), nullable=False),
        sa.Column("flags", postgresql.JSONB(), nullable=False),
        sa.Column("import_ids", postgresql.JSONB(), nullable=False),
        sa.PrimaryKeyConstraint("plan_id", name=op.f("pk_link_measurements")),
        sa.ForeignKeyConstraint(
            ["plan_id"],
            ["link_plans.id"],
            name=op.f("fk_link_measurements_plan_id_link_plans"),
            ondelete="RESTRICT",
        ),
        _check("analytics_policy_version >= 1", "ck_link_measurements_policy_version_positive"),
        _check("window_end >= window_start", "ck_link_measurements_window_ordered"),
        _check("jsonb_typeof(link_totals) = 'object'", "ck_link_measurements_link_totals_object"),
        _check(
            "jsonb_typeof(target_comparison) = 'array'",
            "ck_link_measurements_target_comparison_array",
        ),
        _check(
            "jsonb_typeof(source_comparison) = 'array'",
            "ck_link_measurements_source_comparison_array",
        ),
        _check("jsonb_typeof(flags) = 'array'", "ck_link_measurements_flags_array"),
        _check(
            "jsonb_typeof(import_ids) = 'array' AND jsonb_array_length(import_ids) >= 1",
            "ck_link_measurements_import_ids_non_empty_array",
        ),
    )

    # One shared guard: UPDATE never; DELETE only under the transaction-local writer setting.
    op.execute(
        f"""
        CREATE FUNCTION ken_analytics_writer_guard() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            IF TG_OP = 'DELETE'
                AND coalesce(current_setting('{WRITER_SETTING}', true), '') = 'on' THEN
                RETURN OLD;
            END IF;
            RAISE EXCEPTION 'table % is append-only outside the analytics writer: % is not allowed',
                TG_TABLE_NAME, TG_OP
                USING ERRCODE = '55000';
        END;
        $$
        """
    )
    for table in WRITER_GATED_TABLES:
        op.execute(
            f"CREATE TRIGGER {table}_writer_guard BEFORE UPDATE OR DELETE ON {table} "
            "FOR EACH ROW EXECUTE FUNCTION ken_analytics_writer_guard()"
        )
    for table in APPEND_ONLY_TABLES:
        op.execute(
            f"CREATE TRIGGER {table}_immutable BEFORE UPDATE OR DELETE ON {table} "
            "FOR EACH ROW EXECUTE FUNCTION ken_reject_mutation()"
        )


def downgrade() -> None:
    for table in APPEND_ONLY_TABLES:
        op.execute(f"DROP TRIGGER IF EXISTS {table}_immutable ON {table}")
    for table in WRITER_GATED_TABLES:
        op.execute(f"DROP TRIGGER IF EXISTS {table}_writer_guard ON {table}")
    op.execute("DROP FUNCTION IF EXISTS ken_analytics_writer_guard()")
    op.drop_table("link_measurements")
    op.drop_table("page_metrics_daily")
    op.drop_table("link_metrics_daily")
    op.drop_table("link_events")
    op.drop_table("analytics_imports")
