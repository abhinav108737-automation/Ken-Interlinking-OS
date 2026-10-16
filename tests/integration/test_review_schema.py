"""Migration 0006 (reviewer control plane) against the ``*_test`` database.

Every CHECK, unique key and foreign key of ``users`` / ``auth_sessions`` /
``review_actions``; the guards (users never deleted, immutable identity columns; sessions
change only ``revoked_at`` / ``last_seen_at``, a revocation is final, DELETE only of sessions
expired > 30 days; append-only review actions; page protections only deactivated); an exact
downgrade to 0005.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable
from functools import partial
from hashlib import sha256
from typing import Any

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Connection, inspect, text
from sqlalchemy.exc import DBAPIError, IntegrityError

import ken_core.db.models  # noqa: F401 -- registers every model
from ken_core.db.models.review import (
    APPEND_ONLY_REVIEW_TABLES,
    AUTH_SESSION_MUTABLE_COLUMNS,
    USER_MUTABLE_COLUMNS,
)
from tests.integration.conftest import (
    AUTONOMY_FUNCTIONS,
    AUTONOMY_TABLES,
    AUTONOMY_TRIGGERS,
    HEAD_REVISION,
    ANALYTICS_FUNCTIONS,
    ANALYTICS_TABLES,
    ANALYTICS_TRIGGERS,
    DEPLOYMENT_FUNCTIONS,
    DEPLOYMENT_TABLES,
    DEPLOYMENT_TRIGGERS,
    LEARNING_FUNCTIONS,
    LEARNING_TABLES,
    LEARNING_TRIGGERS,
    SyncDb,
)

pytestmark = pytest.mark.integration

REVIEW_TABLES = ("users", "auth_sessions", "review_actions")
REVIEW_TRIGGERS = {
    ("users_guard", "UPDATE", "users"),
    ("users_no_delete", "DELETE", "users"),
    ("auth_sessions_guard", "UPDATE", "auth_sessions"),
    ("auth_sessions_prune_only", "DELETE", "auth_sessions"),
    ("page_protections_guard", "UPDATE", "page_protections"),
    ("page_protections_no_delete", "DELETE", "page_protections"),
    ("review_actions_immutable", "UPDATE", "review_actions"),
    ("review_actions_immutable", "DELETE", "review_actions"),
}
REVIEW_FUNCTIONS = {"ken_user_guard", "ken_auth_session_guard", "ken_page_protection_guard"}
HASH = sha256(b"x").hexdigest()
CTX = sha256(b"block").hexdigest()
SOURCE = uuid.UUID("00000000-0000-4000-8000-000000000301")
TARGET = uuid.UUID("00000000-0000-4000-8000-000000000302")
USER = uuid.UUID("00000000-0000-4000-8000-000000000303")
PLAN = uuid.UUID("00000000-0000-4000-8000-000000000304")
NEW_PLAN = uuid.UUID("00000000-0000-4000-8000-000000000305")
PWHASH = "scrypt$131072$8$1$c2FsdA==$aGFzaA=="


@pytest.fixture
def db(migrated: None, sync_db: SyncDb) -> SyncDb:
    sync_db.run(_seed)
    return sync_db


def _fails(db: SyncDb, fn: Callable[[Connection], Any]) -> DBAPIError:
    with pytest.raises(DBAPIError) as info:
        db.run(fn)
    return info.value


def _sqlstate(error: DBAPIError) -> str | None:
    origin: Any = error.orig
    for candidate in (origin, getattr(origin, "__cause__", None)):
        code = getattr(candidate, "sqlstate", None)
        if isinstance(code, str):
            return code
    return None


def _immutable(error: DBAPIError) -> bool:
    return _sqlstate(error) == "55000"


def _sql(sql: str, **params: Any) -> Callable[[Connection], Any]:
    return lambda c: c.execute(text(sql), params)


def _page(c: Connection, page_id: uuid.UUID, path: str) -> None:
    c.execute(
        text(
            "INSERT INTO pages (id, canonical_url, current_version, content_hash, "
            "publication_state, word_count, internal_link_count, external_link_count, "
            "first_seen_at, last_seen_at, last_changed_at) "
            "VALUES (:id, :u, 1, :h, 'published', 0, 0, 0, now(), now(), now())"
        ),
        {"id": page_id, "u": f"https://s.test/{path}", "h": HASH},
    )
    c.execute(
        text(
            "INSERT INTO page_versions (id, page_id, version, content_hash, hash_version, "
            "source_type, source_id, requested_url, url, canonical_url, canonical_status, "
            "publication_state, title, body_html, body_text, word_count, "
            "internal_link_count, external_link_count, fetched_at) VALUES (:id, :p, 1, :h, 1, "
            "'cms', 's', 'u', 'u', 'u', 'self', 'published', 't', '', '', 0, 0, 0, now())"
        ),
        {"id": uuid.uuid4(), "p": page_id, "h": HASH},
    )


def _plan(c: Connection, plan_id: uuid.UUID, *, start: int = 0) -> None:
    c.execute(
        text(
            "INSERT INTO link_plans (id, source_page_id, source_page_version, source_url, "
            "target_page_id, target_page_version, target_url, anchor_text, anchor_source, "
            "block_id, block_kind, occurrence_index, start_offset, end_offset, context_hash, "
            "total_score, confidence, rationale, engine_version, policy_version, "
            "ranking_policy_version, analysis_version, status) VALUES (:id, :s, 1, "
            "'https://s.test/s', :t, 1, 'https://s.test/t', 'anchor', 'target_topic', 'b0', "
            "'p', 0, :start, :end, :ctx, 0.5, 0.5, 'r', 'e', 1, 1, 'tax1-alg1', 'rejected')"
        ),
        {"id": plan_id, "s": SOURCE, "t": TARGET, "ctx": CTX, "start": start, "end": start + 6},
    )


def _user(c: Connection, user_id: uuid.UUID = USER, **overrides: Any) -> None:
    values: dict[str, Any] = {
        "id": user_id,
        "username": f"user-{user_id.hex[:8]}",
        "display_name": "Reviewer",
        "role": "reviewer",
        "password_hash": PWHASH,
        "failed_logins": 0,
    } | overrides
    c.execute(
        text(
            "INSERT INTO users (id, username, display_name, role, password_hash, failed_logins) "
            "VALUES (:id, :username, :display_name, :role, :password_hash, :failed_logins)"
        ),
        values,
    )


def _session(
    c: Connection,
    *,
    session_id: uuid.UUID | None = None,
    token_hash: str | None = None,
    created: str = "now()",
    expires: str = "now() + interval '12 hours'",
) -> uuid.UUID:
    sid = session_id or uuid.uuid4()
    c.execute(
        text(
            "INSERT INTO auth_sessions (id, user_id, token_hash, created_at, expires_at) "  # noqa: S608
            f"VALUES (:id, :u, :h, {created}, {expires})"
        ),
        {"id": sid, "u": USER, "h": token_hash or sha256(sid.bytes).hexdigest()},
    )
    return sid


def _action(c: Connection, **overrides: Any) -> None:
    values: dict[str, Any] = {
        "operation_id": uuid.uuid4(),
        "request_fingerprint": HASH,
        "action": "reject",
        "actor": "user:reviewer",
        "user_id": USER,
        "plan_id": PLAN,
        "new_plan_id": None,
        "new_plan_status": None,
        "from_status": "awaiting_approval",
        "to_status": "rejected",
        "reasons": json.dumps(["other"]),
        "note": "why",
        "anchor_before": "anchor",
        "target_before": TARGET,
    } | overrides
    c.execute(
        text(
            "INSERT INTO review_actions (operation_id, request_fingerprint, action, actor, "
            "user_id, plan_id, new_plan_id, new_plan_status, from_status, to_status, reasons, "
            "note, anchor_before, target_before) VALUES (:operation_id, :request_fingerprint, "
            ":action, :actor, :user_id, :plan_id, :new_plan_id, :new_plan_status, "
            ":from_status, :to_status, CAST(:reasons AS jsonb), :note, :anchor_before, "
            ":target_before)"
        ),
        values,
    )


def _protection(c: Connection) -> None:
    c.execute(
        text(
            "INSERT INTO page_protections (page_id, source, rule_id, reason, created_by) "
            "VALUES (:p, 'manual', 'legal', 'why', 'user:ops')"
        ),
        {"p": SOURCE},
    )


def _seed(c: Connection) -> None:
    _page(c, SOURCE, "s")
    _page(c, TARGET, "t")
    _plan(c, PLAN)
    _plan(c, NEW_PLAN, start=10)
    _user(c)


# --------------------------------------------------------------------------- #
# Structure
# --------------------------------------------------------------------------- #
def test_triggers_and_guard_functions_exist(db: SyncDb) -> None:
    def _triggers(c: Connection) -> set[tuple[str, str, str]]:
        rows = c.execute(
            text(
                "SELECT trigger_name, event_manipulation, event_object_table "
                "FROM information_schema.triggers WHERE event_object_table IN "
                "('users', 'auth_sessions', 'review_actions', 'page_protections')"
            )
        ).all()
        return {(str(r[0]), str(r[1]), str(r[2])) for r in rows}

    assert db.run(_triggers) == REVIEW_TRIGGERS
    assert set(APPEND_ONLY_REVIEW_TABLES) == {"review_actions"}


def test_indexes_are_declared(db: SyncDb) -> None:
    def _indexes(c: Connection) -> dict[str, str]:
        rows = c.execute(
            text(
                "SELECT indexname, indexdef FROM pg_indexes WHERE tablename IN "
                "('users', 'auth_sessions', 'review_actions')"
            )
        ).all()
        return {str(r[0]): str(r[1]) for r in rows}

    indexes = db.run(_indexes)
    assert {
        "pk_users",
        "uq_users_username",
        "pk_auth_sessions",
        "uq_auth_sessions_token_hash",
        "ix_auth_sessions_user_id",
        "pk_review_actions",
        "uq_review_actions_operation_id",
        "ix_review_actions_plan_id",
        "ix_review_actions_created_at",
        "ix_review_actions_bulk_operation_id",
        "ix_review_actions_user_id_created_at",
    } == set(indexes)
    assert "created_at DESC" in indexes["ix_review_actions_created_at"]
    assert "user_id, created_at DESC" in indexes["ix_review_actions_user_id_created_at"]


# --------------------------------------------------------------------------- #
# users
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("constraint", "overrides"),
    [
        ("ck_users_username_format", {"username": "Upper"}),
        ("ck_users_username_format", {"username": "ab"}),
        ("ck_users_username_format", {"username": "-leading"}),
        ("ck_users_username_format", {"username": "has space"}),
        ("ck_users_role_valid", {"role": "admin"}),
        ("ck_users_display_name_length", {"display_name": ""}),
        ("ck_users_display_name_length", {"display_name": "x" * 121}),
        ("ck_users_password_hash_scheme", {"password_hash": "plaintext"}),
        ("ck_users_failed_logins_non_negative", {"failed_logins": -1}),
    ],
)
def test_user_checks(db: SyncDb, constraint: str, overrides: dict[str, Any]) -> None:
    error = _fails(db, partial(_user, user_id=uuid.uuid4(), **overrides))
    assert isinstance(error, IntegrityError)
    assert constraint in str(error)


def test_usernames_are_unique(db: SyncDb) -> None:
    error = _fails(db, partial(_user, user_id=uuid.uuid4(), username=f"user-{USER.hex[:8]}"))
    assert isinstance(error, IntegrityError)
    assert "uq_users_username" in str(error)


@pytest.mark.parametrize("column", sorted(USER_MUTABLE_COLUMNS))
def test_user_mutable_columns_change(db: SyncDb, column: str) -> None:
    value = {
        "display_name": "'Renamed'",
        "role": "'viewer'",
        "password_hash": "'scrypt$1$2$3$a$b'",
        "disabled": "true",
        "failed_logins": "3",
        "locked_until": "now()",
        "last_login_at": "now()",
        "updated_at": "now()",
    }[column]
    db.run(_sql(f"UPDATE users SET {column} = {value} WHERE id = :u", u=USER))  # noqa: S608


@pytest.mark.parametrize(
    "assignment",
    [
        "id = gen_random_uuid()",
        "username = 'renamed-user'",
        "created_at = now() - interval '1 day'",
    ],
)
def test_user_identity_columns_are_immutable(db: SyncDb, assignment: str) -> None:
    error = _fails(db, _sql(f"UPDATE users SET {assignment} WHERE id = :u", u=USER))  # noqa: S608
    assert _immutable(error)


def test_users_are_never_deleted(db: SyncDb) -> None:
    error = _fails(db, _sql("DELETE FROM users WHERE id = :u", u=USER))
    assert _immutable(error)


# --------------------------------------------------------------------------- #
# auth_sessions
# --------------------------------------------------------------------------- #
def test_session_checks_and_keys(db: SyncDb) -> None:
    for bad in ("x" * 64, HASH.upper(), HASH[:63]):
        error = _fails(db, partial(_session, token_hash=bad))
        assert isinstance(error, IntegrityError)
        assert "ck_auth_sessions_token_hash_sha256" in str(error)
    error = _fails(db, partial(_session, expires="now() - interval '1 second'"))
    assert "ck_auth_sessions_expires_after_created" in str(error)
    db.run(partial(_session, token_hash=HASH))
    error = _fails(db, partial(_session, token_hash=HASH))
    assert "uq_auth_sessions_token_hash" in str(error)
    orphan = _fails(
        db,
        _sql(
            "INSERT INTO auth_sessions (id, user_id, token_hash, expires_at) "
            "VALUES (gen_random_uuid(), gen_random_uuid(), :h, now() + interval '1 hour')",
            h=CTX,
        ),
    )
    assert "fk_auth_sessions_user_id_users" in str(orphan)


def test_session_updates_only_revocation_and_last_seen(db: SyncDb) -> None:
    sid = db.run(_session)
    assert set(AUTH_SESSION_MUTABLE_COLUMNS) == {"revoked_at", "last_seen_at"}
    db.run(_sql("UPDATE auth_sessions SET last_seen_at = now() WHERE id = :s", s=sid))
    for assignment in (
        "expires_at = now() + interval '30 days'",
        "token_hash = :h",
        "user_id = gen_random_uuid()",
        "created_at = now() - interval '1 hour'",
    ):
        error = _fails(
            db,
            _sql(f"UPDATE auth_sessions SET {assignment} WHERE id = :s", s=sid, h=CTX),  # noqa: S608
        )
        assert _immutable(error), assignment
    db.run(_sql("UPDATE auth_sessions SET revoked_at = now() WHERE id = :s", s=sid))
    # A revocation is final: never cleared, never moved.
    for assignment in ("revoked_at = NULL", "revoked_at = now() + interval '1 hour'"):
        error = _fails(
            db,
            _sql(f"UPDATE auth_sessions SET {assignment} WHERE id = :s", s=sid),  # noqa: S608
        )
        assert _immutable(error), assignment
    db.run(_sql("UPDATE auth_sessions SET last_seen_at = now() WHERE id = :s", s=sid))


def test_session_delete_only_after_expiry_plus_30_days(db: SyncDb) -> None:
    live = db.run(_session)
    recent = db.run(
        partial(
            _session,
            created="now() - interval '40 days'",
            expires="now() - interval '29 days'",
        )
    )
    old = db.run(
        partial(
            _session,
            created="now() - interval '60 days'",
            expires="now() - interval '31 days'",
        )
    )
    for sid in (live, recent):
        error = _fails(db, _sql("DELETE FROM auth_sessions WHERE id = :s", s=sid))
        assert _immutable(error)
    db.run(_sql("DELETE FROM auth_sessions WHERE id = :s", s=old))
    remaining = db.run(lambda c: c.execute(text("SELECT count(*) FROM auth_sessions")).scalar())
    assert remaining == 2


# --------------------------------------------------------------------------- #
# review_actions
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("constraint", "overrides"),
    [
        ("ck_review_actions_action_valid", {"action": "delete"}),
        ("ck_review_actions_from_status_valid", {"from_status": "draft"}),
        ("ck_review_actions_to_status_valid", {"to_status": "gone"}),
        (
            "ck_review_actions_new_plan_status_valid",
            {"action": "edit_anchor", "new_plan_id": NEW_PLAN, "new_plan_status": "nope"},
        ),
        ("ck_review_actions_new_plan_status_iff", {"new_plan_status": "proposed"}),
        (
            "ck_review_actions_new_plan_status_iff",
            {"action": "edit_anchor", "new_plan_id": NEW_PLAN},
        ),
        ("ck_review_actions_actor_is_user", {"actor": "system:review-service"}),
        ("ck_review_actions_actor_is_user", {"actor": "agent:critic@graph-v1"}),
        ("ck_review_actions_note_bounded", {"note": "x" * 501}),
        ("ck_review_actions_reasons_array", {"reasons": json.dumps({"r": 1})}),
        ("ck_review_actions_fingerprint_sha256", {"request_fingerprint": "abc"}),
        (
            "ck_review_actions_new_plan_iff_edit",
            {"new_plan_id": NEW_PLAN, "new_plan_status": "proposed"},
        ),
        ("ck_review_actions_new_plan_iff_edit", {"action": "replace_target"}),
    ],
)
def test_review_action_checks(db: SyncDb, constraint: str, overrides: dict[str, Any]) -> None:
    error = _fails(db, partial(_action, **overrides))
    assert isinstance(error, IntegrityError)
    assert constraint in str(error)


def test_review_action_keys(db: SyncDb) -> None:
    op = uuid.uuid4()
    db.run(partial(_action, operation_id=op))
    db.run(
        partial(
            _action,
            action="edit_anchor",
            new_plan_id=NEW_PLAN,
            new_plan_status="awaiting_approval",
            to_status="superseded",
        )
    )
    duplicate = _fails(db, partial(_action, operation_id=op))
    assert "uq_review_actions_operation_id" in str(duplicate)
    for column, constraint in (
        ("user_id", "fk_review_actions_user_id_users"),
        ("plan_id", "fk_review_actions_plan_id_link_plans"),
    ):
        overrides: dict[str, Any] = {column: uuid.uuid4()}
        error = _fails(db, partial(_action, **overrides))
        assert constraint in str(error)
    error = _fails(
        db,
        partial(
            _action,
            action="replace_target",
            new_plan_id=uuid.uuid4(),
            new_plan_status="proposed",
        ),
    )
    assert "fk_review_actions_new_plan_id_link_plans" in str(error)


@pytest.mark.parametrize(
    "statement",
    ["UPDATE review_actions SET note = 'changed'", "DELETE FROM review_actions"],
)
def test_review_actions_are_append_only(db: SyncDb, statement: str) -> None:
    db.run(_action)
    error = _fails(db, _sql(statement))
    assert _immutable(error)


def test_users_with_review_history_cannot_be_removed_even_without_the_trigger(
    db: SyncDb,
) -> None:
    db.run(_action)
    error = _fails(
        db,
        lambda c: (
            c.execute(text("ALTER TABLE users DISABLE TRIGGER users_no_delete")),
            c.execute(text("DELETE FROM users WHERE id = :u"), {"u": USER}),
        ),
    )
    assert isinstance(error, IntegrityError)
    assert "fk_review_actions_user_id_users" in str(error)


# --------------------------------------------------------------------------- #
# page_protections (0003) guard
# --------------------------------------------------------------------------- #
def test_protections_are_only_deactivated(db: SyncDb) -> None:
    db.run(_protection)
    for statement in (
        "DELETE FROM page_protections",
        "UPDATE page_protections SET reason = 'changed'",
        "UPDATE page_protections SET rule_id = 'other'",
        "UPDATE page_protections SET created_by = 'user:mallory'",
        # a deactivation that also rewrites history
        "UPDATE page_protections SET active = false, deactivated_at = now(), "
        "deactivated_by = 'user:ops', reason = 'changed'",
    ):
        error = _fails(db, _sql(statement))
        assert _immutable(error), statement
    db.run(
        _sql(
            "UPDATE page_protections SET active = false, deactivated_at = now(), "
            "deactivated_by = 'user:ops'"
        )
    )
    # A deactivated row is history: never re-activated, never edited, never deleted.
    for statement in (
        "UPDATE page_protections SET active = true, deactivated_at = NULL, deactivated_by = NULL",
        "UPDATE page_protections SET deactivated_by = 'user:mallory'",
        "DELETE FROM page_protections",
    ):
        error = _fails(db, _sql(statement))
        assert _immutable(error), statement


# --------------------------------------------------------------------------- #
# Downgrade
# --------------------------------------------------------------------------- #
def _state(c: Connection) -> dict[str, Any]:
    return {
        "tables": set(inspect(c).get_table_names()),
        "triggers": set(
            c.execute(
                text("SELECT DISTINCT trigger_name FROM information_schema.triggers")
            ).scalars()
        ),
        "functions": set(
            c.execute(
                text(
                    "SELECT p.proname FROM pg_proc p JOIN pg_namespace n "
                    "ON n.oid = p.pronamespace WHERE n.nspname = 'public' "
                    "AND p.proname LIKE 'ken_%'"
                )
            ).scalars()
        ),
        "revision": c.execute(text("SELECT version_num FROM alembic_version")).scalar(),
    }


def test_downgrade_to_0005_restores_exactly_the_agents_schema(
    alembic_config: Config, migrated: None, sync_db: SyncDb
) -> None:
    head = sync_db.run(_state)
    command.downgrade(alembic_config, "0005_agent_runs")
    try:
        state = sync_db.run(_state)
        assert (
            state["tables"]
            == head["tables"]
            - set(REVIEW_TABLES)
            - DEPLOYMENT_TABLES
            - ANALYTICS_TABLES
            - LEARNING_TABLES - AUTONOMY_TABLES
        )
        assert state["functions"] == (
            head["functions"]
            - REVIEW_FUNCTIONS
            - DEPLOYMENT_FUNCTIONS
            - ANALYTICS_FUNCTIONS
            - LEARNING_FUNCTIONS - AUTONOMY_FUNCTIONS
        )
        assert state["functions"] == {
            "ken_reject_mutation",
            "ken_link_plan_content_guard",
            "ken_agent_run_guard",
        }
        assert (
            state["triggers"]
            == head["triggers"]
            - {t[0] for t in REVIEW_TRIGGERS}
            - DEPLOYMENT_TRIGGERS
            - ANALYTICS_TRIGGERS
            - LEARNING_TRIGGERS - AUTONOMY_TRIGGERS
        )
        assert state["revision"] == "0005_agent_runs"

        # 0005 semantics again: protections may be deleted (no guard below 0006).
        def _delete_protection(c: Connection) -> None:
            _page(c, SOURCE, "s")
            _protection(c)
            c.execute(text("DELETE FROM page_protections"))

        sync_db.run(_delete_protection)
    finally:
        sync_db.run(
            lambda c: c.execute(text("TRUNCATE page_protections, page_versions, pages CASCADE"))
        )
        command.upgrade(alembic_config, "head")
    state = sync_db.run(_state)
    assert state == head
    assert state["revision"] == HEAD_REVISION
