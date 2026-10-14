"""Phase 4 agent orchestration: agent runs, steps and plan reviews.

Revision ID: 0005_agent_runs
Revises: 0004_recommendations
Create Date: 2026-10-14

Column-for-column with ``ken_core.db.models.agents`` (docs/adr/ADR-0012-agent-orchestration.md).

* ``agent_runs`` is GUARDED by ``ken_agent_run_guard()``:

  - DELETE is always rejected;
  - an UPDATE of a row whose ``OLD.status <> 'running'`` is rejected (a finished run is
    frozen);
  - an UPDATE that changes any column outside ``AGENT_RUN_MUTABLE_COLUMNS`` is rejected. The
    comparison is GENERIC (``to_jsonb(row) - <mutable columns>``, like the 0004 link-plan
    guard), so a column added later is immutable automatically.

  Lifecycle CHECKs keep status / outcome / error code / finished_at consistent; a partial
  unique index allows at most one ``running`` run per source; ``trace_id`` is the
  correlation id's hex form.
* ``agent_steps`` and ``agent_run_plans`` are APPEND-ONLY: they join the Phase 1
  ``ken_reject_mutation()`` trigger (created by 0002). A step's ``output`` and
  ``tool_calls`` are bounded to 16 KiB of JSON text each (CHECK).
* Row triggers do not fire on TRUNCATE (test fixtures rely on it).

The value lists below are frozen copies of the contract enums at this revision (a migration
must not change meaning when the Python enums later grow).

Downgrade drops the triggers, the guard function and the three tables, returning exactly to
the 0004 state.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0005_agent_runs"
down_revision: str | Sequence[str] | None = "0004_recommendations"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

APPEND_ONLY_TABLES = ("agent_steps", "agent_run_plans")
MUTABLE_RUN_COLUMNS = (
    "status",
    "outcome",
    "triage_decision",
    "source_page_version",
    "review_notes",
    "error_code",
    "finished_at",
    "updated_at",
)
MAX_STEP_OUTPUT_BYTES = 16_384
MAX_EXPLANATION_CHARS = 300

_TRIGGERS = "'manual', 'batch'"
_RUN_STATUSES = "'running', 'awaiting_approval', 'completed', 'failed'"
_OUTCOMES = "'recommend', 'no_action', 'needs_human_review', 'failed'"
_TRIAGE = "'no_action', 'reindex', 'optimize', 'repair_link', 'review_performance'"
_ERROR_CODES = (
    "'page_not_found', 'tool_failed', 'timeout', 'invariant_violated', 'handoff_conflict', "
    "'orphaned', 'internal'"
)
_NODES = (
    "'load_page', 'triage', 'understand', 'discover', 'plan', 'critique', "
    "'check_invariants', 'policy_gate', 'finalize'"
)
_STEP_STATUSES = "'succeeded', 'skipped', 'failed'"
_FALLBACK_REASONS = "'model_disabled', 'model_unavailable', 'model_timeout', 'schema_violation'"
_VERDICTS = "'accept', 'reject', 'uncertain'"
_MODEL_ROLES = "'fast', 'reasoning', 'embedding'"


def _ts(name: str, *, nullable: bool = False, default_now: bool = False) -> sa.Column[Any]:
    return sa.Column(
        name,
        sa.DateTime(timezone=True),
        server_default=sa.func.now() if default_now else None,
        nullable=nullable,
    )


def _jsonb(name: str, default: str) -> sa.Column[Any]:
    return sa.Column(name, postgresql.JSONB(), server_default=sa.text(default), nullable=False)


def _check(expression: str, name: str) -> sa.CheckConstraint:
    return sa.CheckConstraint(expression, name=op.f(name))


def upgrade() -> None:
    # op.f(): names below are final (the naming convention must not be re-applied).
    op.create_table(
        "agent_runs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("job_run_id", sa.Uuid(), nullable=False),
        sa.Column("source_page_id", sa.Uuid(), nullable=False),
        sa.Column("source_page_version", sa.Integer(), nullable=True),
        sa.Column("source_url", sa.Text(), nullable=False),
        sa.Column("trigger", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("outcome", sa.Text(), nullable=True),
        sa.Column("triage_decision", sa.Text(), nullable=True),
        _jsonb("review_notes", "'[]'::jsonb"),
        sa.Column("correlation_id", sa.Uuid(), nullable=False),
        sa.Column("trace_id", sa.Text(), nullable=False),
        sa.Column("graph_version", sa.Text(), nullable=False),
        _jsonb("config_snapshot", "'{}'::jsonb"),
        sa.Column("error_code", sa.Text(), nullable=True),
        _ts("started_at", default_now=True),
        _ts("finished_at", nullable=True),
        _ts("created_at", default_now=True),
        _ts("updated_at", default_now=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_agent_runs")),
        sa.ForeignKeyConstraint(
            ["job_run_id"],
            ["job_runs.id"],
            name=op.f("fk_agent_runs_job_run_id_job_runs"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["source_page_id"],
            ["pages.id"],
            name=op.f("fk_agent_runs_source_page_id_pages"),
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint("correlation_id", name=op.f("uq_agent_runs_correlation_id")),
        _check(f"trigger IN ({_TRIGGERS})", "ck_agent_runs_trigger_valid"),
        _check(f"status IN ({_RUN_STATUSES})", "ck_agent_runs_status_valid"),
        _check(f"outcome IS NULL OR outcome IN ({_OUTCOMES})", "ck_agent_runs_outcome_valid"),
        _check(
            f"triage_decision IS NULL OR triage_decision IN ({_TRIAGE})",
            "ck_agent_runs_triage_decision_valid",
        ),
        _check(
            f"error_code IS NULL OR error_code IN ({_ERROR_CODES})",
            "ck_agent_runs_error_code_valid",
        ),
        _check(
            "(status = 'running') = (finished_at IS NULL)",
            "ck_agent_runs_finished_iff_not_running",
        ),
        _check("(status = 'running') = (outcome IS NULL)", "ck_agent_runs_outcome_iff_finished"),
        _check("(status = 'failed') = (outcome = 'failed')", "ck_agent_runs_failed_iff_failed"),
        _check(
            "outcome IS DISTINCT FROM 'recommend' OR status = 'awaiting_approval'",
            "ck_agent_runs_recommend_is_handed_off",
        ),
        _check(
            "outcome IS DISTINCT FROM 'no_action' OR status = 'completed'",
            "ck_agent_runs_no_action_is_completed",
        ),
        _check("status <> 'failed' OR error_code IS NOT NULL", "ck_agent_runs_failed_has_code"),
        _check(
            "finished_at IS NULL OR finished_at >= started_at",
            "ck_agent_runs_finished_after_started",
        ),
        _check("trace_id = replace(correlation_id::text, '-', '')", "ck_agent_runs_trace_id_hex"),
        _check(
            "jsonb_typeof(review_notes) = 'array' AND jsonb_array_length(review_notes) <= 20",
            "ck_agent_runs_review_notes_bounded",
        ),
    )
    op.create_index(op.f("ix_agent_runs_started_at"), "agent_runs", [sa.text("started_at DESC")])
    op.create_index(
        op.f("ix_agent_runs_source_page_id_started_at"),
        "agent_runs",
        ["source_page_id", sa.text("started_at DESC")],
    )
    op.create_index(op.f("ix_agent_runs_job_run_id"), "agent_runs", ["job_run_id"])
    op.create_index(op.f("ix_agent_runs_status_outcome"), "agent_runs", ["status", "outcome"])
    # At most one RUNNING run per source page.
    op.create_index(
        op.f("uq_agent_runs_running_source"),
        "agent_runs",
        ["source_page_id"],
        unique=True,
        postgresql_where=sa.text("status = 'running'"),
    )

    op.create_table(
        "agent_steps",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("node", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False),
        _ts("started_at"),
        _ts("finished_at"),
        sa.Column("duration_ms", sa.Integer(), nullable=False),
        sa.Column("model_role", sa.Text(), nullable=True),
        sa.Column("model", sa.Text(), nullable=True),
        sa.Column("fallback_used", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("fallback_reason", sa.Text(), nullable=True),
        sa.Column("error_code", sa.Text(), nullable=True),
        _jsonb("tool_calls", "'[]'::jsonb"),
        _jsonb("output", "'{}'::jsonb"),
        sa.Column("trace_id", sa.Text(), nullable=False),
        sa.Column("span_id", sa.Text(), nullable=False),
        _ts("created_at", default_now=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_agent_steps")),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["agent_runs.id"],
            name=op.f("fk_agent_steps_run_id_agent_runs"),
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint("run_id", "seq", name=op.f("uq_agent_steps_run_id_seq")),
        _check(f"node IN ({_NODES})", "ck_agent_steps_node_valid"),
        _check(f"status IN ({_STEP_STATUSES})", "ck_agent_steps_status_valid"),
        _check(
            f"model_role IS NULL OR model_role IN ({_MODEL_ROLES})",
            "ck_agent_steps_model_role_valid",
        ),
        _check("model IS NULL OR model_role IS NOT NULL", "ck_agent_steps_model_has_role"),
        _check(
            f"fallback_reason IS NULL OR fallback_reason IN ({_FALLBACK_REASONS})",
            "ck_agent_steps_fallback_reason_valid",
        ),
        _check(
            f"error_code IS NULL OR error_code IN ({_ERROR_CODES})",
            "ck_agent_steps_error_code_valid",
        ),
        _check(
            "fallback_used = (fallback_reason IS NOT NULL)", "ck_agent_steps_fallback_reason_iff"
        ),
        _check(
            "seq >= 0 AND attempt >= 1 AND duration_ms >= 0", "ck_agent_steps_counts_valid"
        ),
        _check("finished_at >= started_at", "ck_agent_steps_finished_after_started"),
        _check("trace_id ~ '^[0-9a-f]{32}$'", "ck_agent_steps_trace_id_format"),
        _check("span_id ~ '^[0-9a-f]{16}$'", "ck_agent_steps_span_id_format"),
        _check(
            f"octet_length(output::text) <= {MAX_STEP_OUTPUT_BYTES} "
            f"AND octet_length(tool_calls::text) <= {MAX_STEP_OUTPUT_BYTES}",
            "ck_agent_steps_payload_bounded",
        ),
    )

    op.create_table(
        "agent_run_plans",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("plan_id", sa.Uuid(), nullable=False),
        sa.Column("verdict", sa.Text(), nullable=False),
        _jsonb("reasons", "'[]'::jsonb"),
        sa.Column("explanation", sa.Text(), server_default="", nullable=False),
        sa.Column("handed_off", sa.Boolean(), nullable=False),
        sa.Column("fallback_used", sa.Boolean(), nullable=False),
        sa.Column("fallback_reason", sa.Text(), nullable=True),
        sa.Column("model", sa.Text(), nullable=True),
        _ts("reviewed_at"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_agent_run_plans")),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["agent_runs.id"],
            name=op.f("fk_agent_run_plans_run_id_agent_runs"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["plan_id"],
            ["link_plans.id"],
            name=op.f("fk_agent_run_plans_plan_id_link_plans"),
            ondelete="RESTRICT",
        ),
        # A plan is reviewed by agents at most once.
        sa.UniqueConstraint("plan_id", name=op.f("uq_agent_run_plans_plan_id")),
        _check(f"verdict IN ({_VERDICTS})", "ck_agent_run_plans_verdict_valid"),
        _check(
            f"fallback_reason IS NULL OR fallback_reason IN ({_FALLBACK_REASONS})",
            "ck_agent_run_plans_fallback_reason_valid",
        ),
        _check(
            "fallback_used = (fallback_reason IS NOT NULL)",
            "ck_agent_run_plans_fallback_reason_iff",
        ),
        # A rejected plan is never handed to the human queue.
        _check("verdict <> 'reject' OR NOT handed_off", "ck_agent_run_plans_reject_not_handed_off"),
        _check(
            f"length(explanation) <= {MAX_EXPLANATION_CHARS}",
            "ck_agent_run_plans_explanation_bounded",
        ),
        _check("jsonb_typeof(reasons) = 'array'", "ck_agent_run_plans_reasons_array"),
    )
    op.create_index(op.f("ix_agent_run_plans_run_id"), "agent_run_plans", ["run_id"])

    # agent_runs: no DELETE; only the mutable columns change, and only while running.
    mutable = ", ".join(f"'{c}'" for c in MUTABLE_RUN_COLUMNS)
    op.execute(
        f"""
        CREATE FUNCTION ken_agent_run_guard() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            IF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION 'table % rows are never deleted', TG_TABLE_NAME
                    USING ERRCODE = '55000';
            END IF;
            IF OLD.status <> 'running' THEN
                RAISE EXCEPTION 'table % row is frozen: the run has finished', TG_TABLE_NAME
                    USING ERRCODE = '55000';
            END IF;
            IF (to_jsonb(NEW) - ARRAY[{mutable}]::text[])
                IS DISTINCT FROM (to_jsonb(OLD) - ARRAY[{mutable}]::text[]) THEN
                RAISE EXCEPTION 'table % column is immutable', TG_TABLE_NAME
                    USING ERRCODE = '55000';
            END IF;
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        "CREATE TRIGGER agent_runs_guard BEFORE UPDATE ON agent_runs "
        "FOR EACH ROW EXECUTE FUNCTION ken_agent_run_guard()"
    )
    op.execute(
        "CREATE TRIGGER agent_runs_no_delete BEFORE DELETE ON agent_runs "
        "FOR EACH ROW EXECUTE FUNCTION ken_agent_run_guard()"
    )
    # Append-only steps and reviews: the Phase 1 function (migration 0002).
    for table in APPEND_ONLY_TABLES:
        op.execute(
            f"CREATE TRIGGER {table}_immutable BEFORE UPDATE OR DELETE ON {table} "
            "FOR EACH ROW EXECUTE FUNCTION ken_reject_mutation()"
        )


def downgrade() -> None:
    for table in APPEND_ONLY_TABLES:
        op.execute(f"DROP TRIGGER IF EXISTS {table}_immutable ON {table}")
    op.execute("DROP TRIGGER IF EXISTS agent_runs_no_delete ON agent_runs")
    op.execute("DROP TRIGGER IF EXISTS agent_runs_guard ON agent_runs")
    op.execute("DROP FUNCTION IF EXISTS ken_agent_run_guard()")
    # Dropping a table drops its indexes and constraints with it.
    op.drop_table("agent_run_plans")
    op.drop_table("agent_steps")
    op.drop_table("agent_runs")
