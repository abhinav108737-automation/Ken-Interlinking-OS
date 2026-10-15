"""Phase 6 controlled deployment: deployments, items, verifications; link-plan status integrity.

Revision ID: 0007_controlled_deployment
Revises: 0006_review_control_plane
Create Date: 2026-10-15

Column-for-column with ``ken_core.db.models.deployment`` (docs/adr/ADR-0014-controlled-deployment.md
sections 5 and 8).

* ``deployments`` is GUARDED by ``ken_deployment_guard()``: DELETE is rejected; a row in
  ``failed`` / ``conflict`` / ``rolled_back`` is FROZEN; an UPDATE may change only
  ``DEPLOYMENT_MUTABLE_COLUMNS`` (generic ``to_jsonb`` comparison, so a later column is
  immutable automatically); a status change must be in :data:`DEPLOYMENT_TRANSITIONS`.
  ``operation_id`` is UNIQUE (the idempotency key).
* ``deployment_items`` and ``deployment_verifications`` are APPEND-ONLY (the Phase 1
  ``ken_reject_mutation()`` trigger).
* ``link_plan_transitions`` gains ``txid xid8 DEFAULT pg_current_xact_id()`` (the writing
  TOP-LEVEL transaction, savepoint-safe -- unlike ``xmin``). Existing rows get the id of this
  migration's transaction, which matches no later transaction.
* **Link-plan status integrity** (closes the Phase 3 QA M2 gate): the DEFERRABLE INITIALLY
  DEFERRED constraint trigger ``link_plans_status_integrity`` (``AFTER UPDATE OF status``,
  ``WHEN OLD.status IS DISTINCT FROM NEW.status``) rejects, at commit, a status change that is
  not in :data:`PLAN_TRANSITIONS` (a literal copy of ``ALLOWED_TRANSITIONS``, parity-tested)
  or that has no ``link_plan_transitions`` row (plan, from, to) written by the SAME
  transaction. A raw ``UPDATE link_plans SET status`` therefore fails. Known limitation: an
  INSERT at a non-initial status (test fixtures) is not covered.

The value lists below are frozen copies of the contract enums at this revision.

Downgrade drops the triggers, the functions, the ``txid`` column and the three tables,
returning exactly to the 0006 state.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Final

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0007_controlled_deployment"
down_revision: str | Sequence[str] | None = "0006_review_control_plane"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

APPEND_ONLY_TABLES: Final = ("deployment_items", "deployment_verifications")
MAX_NOTE_CHARS: Final = 500

PLAN_TRANSITIONS: Final[tuple[tuple[str, str], ...]] = (
    ("proposed", "critiqued"),
    ("proposed", "policy_checked"),
    ("proposed", "awaiting_approval"),
    ("proposed", "rejected"),
    ("proposed", "superseded"),
    ("proposed", "expired"),
    ("critiqued", "policy_checked"),
    ("critiqued", "awaiting_approval"),
    ("critiqued", "rejected"),
    ("critiqued", "superseded"),
    ("critiqued", "expired"),
    ("policy_checked", "awaiting_approval"),
    ("policy_checked", "rejected"),
    ("policy_checked", "superseded"),
    ("policy_checked", "expired"),
    ("awaiting_approval", "approved"),
    ("awaiting_approval", "rejected"),
    ("awaiting_approval", "superseded"),
    ("awaiting_approval", "expired"),
    ("approved", "validated"),
    ("approved", "rejected"),
    ("approved", "superseded"),
    ("approved", "expired"),
    ("validated", "deployed"),
    ("validated", "failed"),
    ("validated", "expired"),
    ("deployed", "verified"),
    ("deployed", "failed"),
    ("deployed", "rolled_back"),
    ("verified", "measured"),
    ("verified", "rolled_back"),
    ("failed", "rolled_back"),
    ("measured", "learning_captured"),
    ("measured", "rolled_back"),
    ("learning_captured", "rolled_back"),
)
"""Literal copy of ``ken_contracts.recommendations.ALLOWED_TRANSITIONS`` at this revision."""

DEPLOYMENT_TRANSITIONS: Final[tuple[tuple[str, str], ...]] = (
    ("applying", "applied"),
    ("applying", "failed"),
    ("applying", "conflict"),
    ("applied", "verified"),
    ("applied", "verification_failed"),
    ("applied", "rolling_back"),
    ("verified", "verification_failed"),
    ("verified", "rolling_back"),
    ("verification_failed", "verified"),
    ("verification_failed", "rolling_back"),
    ("rolling_back", "rolled_back"),
    ("rolling_back", "applied"),
    ("rolling_back", "verified"),
    ("rolling_back", "verification_failed"),
)
"""Literal copy of ``ken_core.db.models.deployment.DEPLOYMENT_TRANSITIONS``."""
FROZEN_STATUSES: Final = ("failed", "conflict", "rolled_back")
DEPLOYMENT_MUTABLE_COLUMNS: Final = (
    "status",
    "cms_deployment_id",
    "previous_version",
    "new_version",
    "error_code",
    "applied_at",
    "verified_at",
    "rolled_back_at",
    "updated_at",
    "attempts",
    "rollback_operation_id",
    "rollback_fingerprint",
    "rollback_reason",
    "rollback_note",
    "rollback_actor",
    "rollback_user_id",
    "rollback_from_status",
    "rollback_plan_ids",
    "rollback_request_json",
    "restored_version",
)

_DEPLOYMENT_STATUSES = (
    "'applying', 'applied', 'verified', 'verification_failed', 'failed', 'conflict', "
    "'rolling_back', 'rolled_back'"
)
_ROLLBACK_REASONS = (
    "'verification_failed', 'editorial_reversal', 'wrong_destination', 'broken_page', 'other'"
)
_STAGES = (
    "'requested', 'applied', 'already_applied', 'failed', 'conflict', 'verified', "
    "'verification_failed', 'rolled_back', 'rollback_conflict'"
)
_FINGERPRINT = "~ '^[0-9a-f]{64}$'"


def _ts(name: str, *, nullable: bool = False, default_now: bool = False) -> sa.Column[Any]:
    return sa.Column(
        name,
        sa.DateTime(timezone=True),
        server_default=sa.func.now() if default_now else None,
        nullable=nullable,
    )


def _check(expression: str, name: str) -> sa.CheckConstraint:
    return sa.CheckConstraint(expression, name=op.f(name))


def _array(columns: Sequence[str]) -> str:
    return "ARRAY[" + ", ".join(f"'{c}'" for c in columns) + "]::text[]"


def _pairs(pairs: Sequence[tuple[str, str]]) -> str:
    return "ARRAY[" + ", ".join(f"'{a}>{b}'" for a, b in pairs) + "]::text[]"


def upgrade() -> None:
    op.create_table(
        "deployments",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("operation_id", sa.Uuid(), nullable=False),
        sa.Column("request_fingerprint", sa.Text(), nullable=False),
        sa.Column("request_json", sa.Text(), nullable=False),
        sa.Column("cms_provider", sa.Text(), nullable=False),
        sa.Column("cms_page_id", sa.Text(), nullable=False),
        sa.Column("cms_deployment_id", sa.Text(), nullable=True),
        sa.Column("source_page_id", sa.Uuid(), nullable=False),
        sa.Column("source_page_version", sa.Integer(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("actor", sa.Text(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("note", sa.Text(), server_default="", nullable=False),
        sa.Column(
            "preview", postgresql.JSONB(), server_default=sa.text("'[]'::jsonb"), nullable=False
        ),
        sa.Column("previous_version", sa.Text(), nullable=True),
        sa.Column("new_version", sa.Text(), nullable=True),
        sa.Column("error_code", sa.Text(), nullable=True),
        sa.Column("attempts", sa.Integer(), server_default="1", nullable=False),
        _ts("created_at", default_now=True),
        _ts("updated_at", default_now=True),
        _ts("applied_at", nullable=True),
        _ts("verified_at", nullable=True),
        _ts("rolled_back_at", nullable=True),
        sa.Column("rollback_operation_id", sa.Uuid(), nullable=True),
        sa.Column("rollback_fingerprint", sa.Text(), nullable=True),
        sa.Column("rollback_reason", sa.Text(), nullable=True),
        sa.Column("rollback_note", sa.Text(), nullable=True),
        sa.Column("rollback_actor", sa.Text(), nullable=True),
        sa.Column("rollback_user_id", sa.Uuid(), nullable=True),
        sa.Column("rollback_from_status", sa.Text(), nullable=True),
        sa.Column("rollback_plan_ids", postgresql.JSONB(), nullable=True),
        sa.Column("rollback_request_json", sa.Text(), nullable=True),
        sa.Column("restored_version", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_deployments")),
        sa.UniqueConstraint("operation_id", name=op.f("uq_deployments_operation_id")),
        sa.ForeignKeyConstraint(
            ["source_page_id"],
            ["pages.id"],
            name=op.f("fk_deployments_source_page_id_pages"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_deployments_user_id_users"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["rollback_user_id"],
            ["users.id"],
            name=op.f("fk_deployments_rollback_user_id_users"),
            ondelete="RESTRICT",
        ),
        _check(f"status IN ({_DEPLOYMENT_STATUSES})", "ck_deployments_status_valid"),
        _check(f"request_fingerprint {_FINGERPRINT}", "ck_deployments_fingerprint_sha256"),
        _check(
            f"rollback_fingerprint IS NULL OR rollback_fingerprint {_FINGERPRINT}",
            "ck_deployments_rollback_fingerprint_sha256",
        ),
        _check("actor ~ '^user:'", "ck_deployments_actor_is_user"),
        _check(
            "rollback_actor IS NULL OR rollback_actor ~ '^user:'",
            "ck_deployments_rollback_actor_is_user",
        ),
        _check(f"length(note) <= {MAX_NOTE_CHARS}", "ck_deployments_note_bounded"),
        _check(
            f"rollback_note IS NULL OR length(rollback_note) <= {MAX_NOTE_CHARS}",
            "ck_deployments_rollback_note_bounded",
        ),
        _check(
            f"rollback_reason IS NULL OR rollback_reason IN ({_ROLLBACK_REASONS})",
            "ck_deployments_rollback_reason_valid",
        ),
        _check(
            f"rollback_from_status IS NULL OR rollback_from_status IN ({_DEPLOYMENT_STATUSES})",
            "ck_deployments_rollback_from_status_valid",
        ),
        _check(
            "error_code IS NULL OR error_code ~ '^[a-z_]{1,64}$'",
            "ck_deployments_error_code_vocabulary",
        ),
        _check("jsonb_typeof(preview) = 'array'", "ck_deployments_preview_array"),
        _check(
            "rollback_plan_ids IS NULL OR jsonb_typeof(rollback_plan_ids) = 'array'",
            "ck_deployments_rollback_plan_ids_array",
        ),
        _check("attempts >= 1", "ck_deployments_attempts_positive"),
        _check("source_page_version >= 1", "ck_deployments_source_page_version_positive"),
        _check(
            "(status IN ('applying', 'failed', 'conflict')) = (applied_at IS NULL)",
            "ck_deployments_applied_at_iff_applied",
        ),
        _check(
            "(status = 'rolling_back') = (rollback_from_status IS NOT NULL)",
            "ck_deployments_rolling_back_iff_from_status",
        ),
    )
    op.create_index(
        op.f("uq_deployments_rollback_operation_id"),
        "deployments",
        ["rollback_operation_id"],
        unique=True,
        postgresql_where=sa.text("rollback_operation_id IS NOT NULL"),
    )
    op.create_index(
        op.f("ix_deployments_source_page_id_created_at"),
        "deployments",
        ["source_page_id", sa.text("created_at DESC")],
    )
    op.create_index(
        op.f("ix_deployments_status_updated_at"), "deployments", ["status", "updated_at"]
    )
    op.create_index(op.f("ix_deployments_created_at"), "deployments", [sa.text("created_at DESC")])

    op.create_table(
        "deployment_items",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("deployment_id", sa.Uuid(), nullable=False),
        sa.Column("plan_id", sa.Uuid(), nullable=False),
        sa.Column("change_id", sa.Uuid(), nullable=False),
        sa.Column("stage", sa.Text(), nullable=False),
        sa.Column("operation_id", sa.Uuid(), nullable=True),
        sa.Column("fingerprint", sa.Text(), nullable=True),
        sa.Column("href", sa.Text(), nullable=False),
        sa.Column("anchor_text", sa.Text(), nullable=False),
        sa.Column("target_page_id", sa.Uuid(), nullable=False),
        sa.Column(
            "codes", postgresql.JSONB(), server_default=sa.text("'[]'::jsonb"), nullable=False
        ),
        _ts("recorded_at", default_now=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_deployment_items")),
        sa.ForeignKeyConstraint(
            ["deployment_id"],
            ["deployments.id"],
            name=op.f("fk_deployment_items_deployment_id_deployments"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["plan_id"],
            ["link_plans.id"],
            name=op.f("fk_deployment_items_plan_id_link_plans"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["target_page_id"],
            ["pages.id"],
            name=op.f("fk_deployment_items_target_page_id_pages"),
            ondelete="RESTRICT",
        ),
        _check(f"stage IN ({_STAGES})", "ck_deployment_items_stage_valid"),
        _check("jsonb_typeof(codes) = 'array'", "ck_deployment_items_codes_array"),
        _check(
            f"fingerprint IS NULL OR fingerprint {_FINGERPRINT}",
            "ck_deployment_items_fingerprint_sha256",
        ),
        _check("change_id = plan_id", "ck_deployment_items_change_id_is_plan_id"),
    )
    op.create_index(
        op.f("ix_deployment_items_deployment_id_id"), "deployment_items", ["deployment_id", "id"]
    )
    op.create_index(op.f("ix_deployment_items_plan_id"), "deployment_items", ["plan_id"])
    op.create_index(
        op.f("ix_deployment_items_operation_id"),
        "deployment_items",
        ["operation_id"],
        postgresql_where=sa.text("operation_id IS NOT NULL"),
    )

    op.create_table(
        "deployment_verifications",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("deployment_id", sa.Uuid(), nullable=False),
        _ts("run_at", default_now=True),
        sa.Column("passed", sa.Boolean(), nullable=False),
        sa.Column("inconclusive", sa.Boolean(), nullable=False),
        sa.Column("http_checked", sa.Boolean(), nullable=False),
        sa.Column(
            "checks", postgresql.JSONB(), server_default=sa.text("'[]'::jsonb"), nullable=False
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_deployment_verifications")),
        sa.ForeignKeyConstraint(
            ["deployment_id"],
            ["deployments.id"],
            name=op.f("fk_deployment_verifications_deployment_id_deployments"),
            ondelete="RESTRICT",
        ),
        _check("jsonb_typeof(checks) = 'array'", "ck_deployment_verifications_checks_array"),
        _check(
            "NOT (passed AND inconclusive)", "ck_deployment_verifications_passed_xor_inconclusive"
        ),
    )
    op.create_index(
        op.f("ix_deployment_verifications_deployment_id_id"),
        "deployment_verifications",
        ["deployment_id", "id"],
    )

    # deployments: never deleted; frozen once finished; only mutable columns change; the
    # status moves only along the state machine.
    op.execute(
        f"""
        CREATE FUNCTION ken_deployment_guard() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            IF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION 'table % rows are never deleted', TG_TABLE_NAME
                    USING ERRCODE = '55000';
            END IF;
            IF OLD.status = ANY ({_array(FROZEN_STATUSES)}) THEN
                RAISE EXCEPTION 'table % row is frozen in status %', TG_TABLE_NAME, OLD.status
                    USING ERRCODE = '55000';
            END IF;
            IF (to_jsonb(NEW) - {_array(DEPLOYMENT_MUTABLE_COLUMNS)})
                IS DISTINCT FROM (to_jsonb(OLD) - {_array(DEPLOYMENT_MUTABLE_COLUMNS)}) THEN
                RAISE EXCEPTION 'table % column is immutable', TG_TABLE_NAME
                    USING ERRCODE = '55000';
            END IF;
            IF NEW.status IS DISTINCT FROM OLD.status
                AND NOT ((OLD.status || '>' || NEW.status) = ANY ({_pairs(DEPLOYMENT_TRANSITIONS)}))
            THEN
                RAISE EXCEPTION 'table % status % -> % is not allowed', TG_TABLE_NAME,
                    OLD.status, NEW.status
                    USING ERRCODE = '55000';
            END IF;
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        "CREATE TRIGGER deployments_guard BEFORE UPDATE ON deployments "
        "FOR EACH ROW EXECUTE FUNCTION ken_deployment_guard()"
    )
    op.execute(
        "CREATE TRIGGER deployments_no_delete BEFORE DELETE ON deployments "
        "FOR EACH ROW EXECUTE FUNCTION ken_deployment_guard()"
    )
    for table in APPEND_ONLY_TABLES:
        op.execute(
            f"CREATE TRIGGER {table}_immutable BEFORE UPDATE OR DELETE ON {table} "
            "FOR EACH ROW EXECUTE FUNCTION ken_reject_mutation()"
        )

    # link-plan status integrity. Adding a column with a volatile default rewrites the table;
    # row triggers (the append-only guard) do not fire on a rewrite.
    op.execute(
        "ALTER TABLE link_plan_transitions "
        "ADD COLUMN txid xid8 NOT NULL DEFAULT pg_current_xact_id()"
    )
    op.execute(
        f"""
        CREATE FUNCTION ken_link_plan_status_guard() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            IF NOT ((OLD.status || '>' || NEW.status) = ANY ({_pairs(PLAN_TRANSITIONS)})) THEN
                RAISE EXCEPTION 'link_plans status % -> % is not an allowed transition',
                    OLD.status, NEW.status
                    USING ERRCODE = '55000';
            END IF;
            IF NOT EXISTS (
                SELECT 1 FROM link_plan_transitions t
                WHERE t.plan_id = NEW.id
                  AND t.from_status = OLD.status
                  AND t.to_status = NEW.status
                  AND t.txid = pg_current_xact_id()
            ) THEN
                RAISE EXCEPTION
                    'link_plans status % -> % has no transition record in this transaction',
                    OLD.status, NEW.status
                    USING ERRCODE = '55000';
            END IF;
            RETURN NULL;
        END;
        $$
        """
    )
    op.execute(
        "CREATE CONSTRAINT TRIGGER link_plans_status_integrity "
        "AFTER UPDATE OF status ON link_plans "
        "DEFERRABLE INITIALLY DEFERRED FOR EACH ROW "
        "WHEN (OLD.status IS DISTINCT FROM NEW.status) "
        "EXECUTE FUNCTION ken_link_plan_status_guard()"
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS link_plans_status_integrity ON link_plans")
    op.execute("DROP FUNCTION IF EXISTS ken_link_plan_status_guard()")
    op.drop_column("link_plan_transitions", "txid")
    for table in APPEND_ONLY_TABLES:
        op.execute(f"DROP TRIGGER IF EXISTS {table}_immutable ON {table}")
    op.execute("DROP TRIGGER IF EXISTS deployments_no_delete ON deployments")
    op.execute("DROP TRIGGER IF EXISTS deployments_guard ON deployments")
    op.execute("DROP FUNCTION IF EXISTS ken_deployment_guard()")
    op.drop_table("deployment_verifications")
    op.drop_table("deployment_items")
    op.drop_table("deployments")
