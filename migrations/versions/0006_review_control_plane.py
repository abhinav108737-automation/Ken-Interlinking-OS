"""Phase 5 reviewer control plane: users, sessions, review actions; protection history guard.

Revision ID: 0006_review_control_plane
Revises: 0005_agent_runs
Create Date: 2026-10-14

Column-for-column with ``ken_core.db.models.review`` (docs/adr/ADR-0013-reviewer-control-plane.md).

* ``users`` is GUARDED by ``ken_user_guard()``: DELETE is always rejected (accounts are
  disabled, never deleted), and an UPDATE that changes a column outside
  ``USER_MUTABLE_COLUMNS`` (``id``, ``username``, ``created_at``) is rejected. The comparison
  is GENERIC (``to_jsonb(row) - <mutable columns>``), so a column added later is immutable
  automatically.
* ``auth_sessions`` is GUARDED by ``ken_auth_session_guard()``: an UPDATE may change only
  ``revoked_at`` and ``last_seen_at``, and a revocation is final (a set ``revoked_at`` never
  changes again); DELETE is allowed ONLY for a session that expired more than 30 days ago
  (the ``sessions-prune`` CLI). Only the SHA-256 of a token is stored (CHECK).
* ``review_actions`` is APPEND-ONLY: it joins the Phase 1 ``ken_reject_mutation()`` trigger
  (created by 0002). ``operation_id`` is UNIQUE (the idempotency key).
* ``page_protections`` (0003) gains ``ken_page_protection_guard()``: DELETE is rejected, and
  the only UPDATE allowed is a DEACTIVATION -- ``active`` true -> false, changing nothing but
  ``active``, ``deactivated_at`` and ``deactivated_by`` (the 0003 CHECK still ties
  ``active`` to ``deactivated_at``).
* Row triggers do not fire on TRUNCATE (test fixtures rely on it).

The value lists below are frozen copies of the contract enums at this revision (a migration
must not change meaning when the Python enums later grow).

Downgrade drops the triggers, the guard functions and the three tables, returning exactly to
the 0005 state.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0006_review_control_plane"
down_revision: str | Sequence[str] | None = "0005_agent_runs"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

APPEND_ONLY_TABLES = ("review_actions",)
USER_MUTABLE_COLUMNS = (
    "display_name",
    "role",
    "password_hash",
    "disabled",
    "failed_logins",
    "locked_until",
    "last_login_at",
    "updated_at",
)
AUTH_SESSION_MUTABLE_COLUMNS = ("revoked_at", "last_seen_at")
PROTECTION_DEACTIVATION_COLUMNS = ("active", "deactivated_at", "deactivated_by")
SESSION_PRUNE_AFTER = "30 days"
MAX_NOTE_CHARS = 500
USERNAME_PATTERN = r"^[a-z0-9][a-z0-9._-]{2,63}$"

_ROLES = "'viewer', 'reviewer', 'operator_admin'"
_ACTIONS = "'approve', 'reject', 'edit_anchor', 'replace_target'"
_STATUSES = (
    "'proposed', 'critiqued', 'policy_checked', 'awaiting_approval', 'approved', 'rejected', "
    "'superseded', 'validated', 'deployed', 'verified', 'failed', 'rolled_back', 'expired', "
    "'measured', 'learning_captured'"
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


def _check(expression: str, name: str) -> sa.CheckConstraint:
    return sa.CheckConstraint(expression, name=op.f(name))


def _array(columns: Sequence[str]) -> str:
    return "ARRAY[" + ", ".join(f"'{c}'" for c in columns) + "]::text[]"


def upgrade() -> None:
    # op.f(): names below are final (the naming convention must not be re-applied).
    op.create_table(
        "users",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("username", sa.Text(), nullable=False),
        sa.Column("display_name", sa.Text(), nullable=False),
        sa.Column("role", sa.Text(), nullable=False),
        sa.Column("password_hash", sa.Text(), nullable=False),
        sa.Column("disabled", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("failed_logins", sa.Integer(), server_default="0", nullable=False),
        _ts("locked_until", nullable=True),
        _ts("last_login_at", nullable=True),
        _ts("created_at", default_now=True),
        _ts("updated_at", default_now=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_users")),
        sa.UniqueConstraint("username", name=op.f("uq_users_username")),
        _check(f"username ~ '{USERNAME_PATTERN}'", "ck_users_username_format"),
        _check(f"role IN ({_ROLES})", "ck_users_role_valid"),
        _check("length(display_name) BETWEEN 1 AND 120", "ck_users_display_name_length"),
        _check("password_hash LIKE 'scrypt$%'", "ck_users_password_hash_scheme"),
        _check("failed_logins >= 0", "ck_users_failed_logins_non_negative"),
    )

    op.create_table(
        "auth_sessions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("token_hash", sa.Text(), nullable=False),
        _ts("created_at", default_now=True),
        _ts("expires_at"),
        _ts("revoked_at", nullable=True),
        _ts("last_seen_at", nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_auth_sessions")),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_auth_sessions_user_id_users"),
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint("token_hash", name=op.f("uq_auth_sessions_token_hash")),
        _check("token_hash ~ '^[0-9a-f]{64}$'", "ck_auth_sessions_token_hash_sha256"),
        _check("expires_at > created_at", "ck_auth_sessions_expires_after_created"),
    )
    op.create_index(op.f("ix_auth_sessions_user_id"), "auth_sessions", ["user_id"])

    op.create_table(
        "review_actions",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("operation_id", sa.Uuid(), nullable=False),
        sa.Column("request_fingerprint", sa.Text(), nullable=False),
        sa.Column("action", sa.Text(), nullable=False),
        sa.Column("actor", sa.Text(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("plan_id", sa.Uuid(), nullable=False),
        sa.Column("new_plan_id", sa.Uuid(), nullable=True),
        sa.Column("new_plan_status", sa.Text(), nullable=True),
        sa.Column("from_status", sa.Text(), nullable=False),
        sa.Column("to_status", sa.Text(), nullable=False),
        _jsonb("reasons", "'[]'::jsonb"),
        sa.Column("note", sa.Text(), server_default="", nullable=False),
        sa.Column("anchor_before", sa.Text(), nullable=False),
        sa.Column("anchor_after", sa.Text(), nullable=True),
        sa.Column("target_before", sa.Uuid(), nullable=False),
        sa.Column("target_after", sa.Uuid(), nullable=True),
        sa.Column("bulk_operation_id", sa.Uuid(), nullable=True),
        _jsonb("result", "'{}'::jsonb"),
        _ts("created_at", default_now=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_review_actions")),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_review_actions_user_id_users"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["plan_id"],
            ["link_plans.id"],
            name=op.f("fk_review_actions_plan_id_link_plans"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["new_plan_id"],
            ["link_plans.id"],
            name=op.f("fk_review_actions_new_plan_id_link_plans"),
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint("operation_id", name=op.f("uq_review_actions_operation_id")),
        _check(f"action IN ({_ACTIONS})", "ck_review_actions_action_valid"),
        _check(f"from_status IN ({_STATUSES})", "ck_review_actions_from_status_valid"),
        _check(f"to_status IN ({_STATUSES})", "ck_review_actions_to_status_valid"),
        _check(
            f"new_plan_status IS NULL OR new_plan_status IN ({_STATUSES})",
            "ck_review_actions_new_plan_status_valid",
        ),
        _check(
            "(new_plan_id IS NULL) = (new_plan_status IS NULL)",
            "ck_review_actions_new_plan_status_iff",
        ),
        _check("actor ~ '^user:'", "ck_review_actions_actor_is_user"),
        _check(f"length(note) <= {MAX_NOTE_CHARS}", "ck_review_actions_note_bounded"),
        _check("jsonb_typeof(reasons) = 'array'", "ck_review_actions_reasons_array"),
        _check(
            "request_fingerprint ~ '^[0-9a-f]{64}$'", "ck_review_actions_fingerprint_sha256"
        ),
        # Edits always produce a superseding plan; approve/reject never do.
        _check(
            "(action IN ('edit_anchor', 'replace_target')) = (new_plan_id IS NOT NULL)",
            "ck_review_actions_new_plan_iff_edit",
        ),
    )
    op.create_index(op.f("ix_review_actions_plan_id"), "review_actions", ["plan_id"])
    op.create_index(
        op.f("ix_review_actions_created_at"), "review_actions", [sa.text("created_at DESC")]
    )
    op.create_index(
        op.f("ix_review_actions_bulk_operation_id"), "review_actions", ["bulk_operation_id"]
    )
    op.create_index(
        op.f("ix_review_actions_user_id_created_at"),
        "review_actions",
        ["user_id", sa.text("created_at DESC")],
    )

    # users: never deleted; id / username / created_at never change.
    op.execute(
        f"""
        CREATE FUNCTION ken_user_guard() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            IF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION 'table % rows are never deleted (disable instead)', TG_TABLE_NAME
                    USING ERRCODE = '55000';
            END IF;
            IF (to_jsonb(NEW) - {_array(USER_MUTABLE_COLUMNS)})
                IS DISTINCT FROM (to_jsonb(OLD) - {_array(USER_MUTABLE_COLUMNS)}) THEN
                RAISE EXCEPTION 'table % column is immutable', TG_TABLE_NAME
                    USING ERRCODE = '55000';
            END IF;
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        "CREATE TRIGGER users_guard BEFORE UPDATE ON users "
        "FOR EACH ROW EXECUTE FUNCTION ken_user_guard()"
    )
    op.execute(
        "CREATE TRIGGER users_no_delete BEFORE DELETE ON users "
        "FOR EACH ROW EXECUTE FUNCTION ken_user_guard()"
    )

    # auth_sessions: only revoked_at / last_seen_at change; a revocation is final; DELETE
    # only once the session expired more than 30 days ago (the prune).
    op.execute(
        f"""
        CREATE FUNCTION ken_auth_session_guard() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            IF TG_OP = 'DELETE' THEN
                IF OLD.expires_at < now() - interval '{SESSION_PRUNE_AFTER}' THEN
                    RETURN OLD;
                END IF;
                RAISE EXCEPTION 'table % rows are deleted only after expiry + {SESSION_PRUNE_AFTER}',
                    TG_TABLE_NAME
                    USING ERRCODE = '55000';
            END IF;
            IF (to_jsonb(NEW) - {_array(AUTH_SESSION_MUTABLE_COLUMNS)})
                IS DISTINCT FROM (to_jsonb(OLD) - {_array(AUTH_SESSION_MUTABLE_COLUMNS)}) THEN
                RAISE EXCEPTION 'table % column is immutable', TG_TABLE_NAME
                    USING ERRCODE = '55000';
            END IF;
            IF OLD.revoked_at IS NOT NULL
                AND NEW.revoked_at IS DISTINCT FROM OLD.revoked_at THEN
                RAISE EXCEPTION 'table % revocation is final', TG_TABLE_NAME
                    USING ERRCODE = '55000';
            END IF;
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        "CREATE TRIGGER auth_sessions_guard BEFORE UPDATE ON auth_sessions "
        "FOR EACH ROW EXECUTE FUNCTION ken_auth_session_guard()"
    )
    op.execute(
        "CREATE TRIGGER auth_sessions_prune_only BEFORE DELETE ON auth_sessions "
        "FOR EACH ROW EXECUTE FUNCTION ken_auth_session_guard()"
    )

    # page_protections (0003): history is kept; the only UPDATE is a deactivation.
    op.execute(
        f"""
        CREATE FUNCTION ken_page_protection_guard() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            IF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION 'table % rows are never deleted (deactivate instead)',
                    TG_TABLE_NAME
                    USING ERRCODE = '55000';
            END IF;
            IF NOT (OLD.active AND NOT NEW.active) THEN
                RAISE EXCEPTION 'table % rows may only be deactivated', TG_TABLE_NAME
                    USING ERRCODE = '55000';
            END IF;
            IF (to_jsonb(NEW) - {_array(PROTECTION_DEACTIVATION_COLUMNS)})
                IS DISTINCT FROM (to_jsonb(OLD) - {_array(PROTECTION_DEACTIVATION_COLUMNS)}) THEN
                RAISE EXCEPTION 'table % column is immutable', TG_TABLE_NAME
                    USING ERRCODE = '55000';
            END IF;
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        "CREATE TRIGGER page_protections_guard BEFORE UPDATE ON page_protections "
        "FOR EACH ROW EXECUTE FUNCTION ken_page_protection_guard()"
    )
    op.execute(
        "CREATE TRIGGER page_protections_no_delete BEFORE DELETE ON page_protections "
        "FOR EACH ROW EXECUTE FUNCTION ken_page_protection_guard()"
    )

    # Append-only review record: the Phase 1 function (migration 0002).
    for table in APPEND_ONLY_TABLES:
        op.execute(
            f"CREATE TRIGGER {table}_immutable BEFORE UPDATE OR DELETE ON {table} "
            "FOR EACH ROW EXECUTE FUNCTION ken_reject_mutation()"
        )


def downgrade() -> None:
    for table in APPEND_ONLY_TABLES:
        op.execute(f"DROP TRIGGER IF EXISTS {table}_immutable ON {table}")
    op.execute("DROP TRIGGER IF EXISTS page_protections_no_delete ON page_protections")
    op.execute("DROP TRIGGER IF EXISTS page_protections_guard ON page_protections")
    op.execute("DROP FUNCTION IF EXISTS ken_page_protection_guard()")
    op.execute("DROP TRIGGER IF EXISTS auth_sessions_prune_only ON auth_sessions")
    op.execute("DROP TRIGGER IF EXISTS auth_sessions_guard ON auth_sessions")
    op.execute("DROP FUNCTION IF EXISTS ken_auth_session_guard()")
    op.execute("DROP TRIGGER IF EXISTS users_no_delete ON users")
    op.execute("DROP TRIGGER IF EXISTS users_guard ON users")
    op.execute("DROP FUNCTION IF EXISTS ken_user_guard()")
    # Dropping a table drops its indexes and constraints with it.
    op.drop_table("review_actions")
    op.drop_table("auth_sessions")
    op.drop_table("users")
