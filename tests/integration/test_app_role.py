"""The least-privilege application role (``ken_app``): what it cannot do, and that it works.

Connects AS ``ken_app`` (``KEN_TEST_APP_DATABASE_URL``, set up by ``make db-roles`` against the
test database) and asserts the real SQLSTATE of each refusal. Fixtures and migrations keep the
owner. Skipped when the application DSN is not configured, because creating a role is a
cluster-level operator action that a test run must not perform on a shared server.

What this does NOT prove: a client holding the OWNER credentials is unconstrained.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Connection, text
from sqlalchemy.exc import DBAPIError

from tests.integration.conftest import LEARNING_ACTIVATOR_ON, LEARNING_PROMOTER_ON, SyncDb
from tests.integration.test_autonomy_schema import (
    _attempt,
    _decision,
    _sqlstate,
    _with_transition,
)
from tests.integration.test_learning_schema import (
    _base,
    _promote,
    _status,
    _to_offline_test,
)

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "infrastructure" / "scripts"))
import db_roles

pytestmark = pytest.mark.integration

APP_URL = os.environ.get("KEN_TEST_APP_DATABASE_URL")
requires_app_role = pytest.mark.skipif(
    not APP_URL, reason="KEN_TEST_APP_DATABASE_URL not set (run `make db-roles-test`)"
)

INSUFFICIENT_PRIVILEGE = "42501"
OBJECT_NOT_IN_PREREQUISITE_STATE = "55000"  # the append-only / guard triggers


@pytest.fixture
def app_db() -> SyncDb:
    assert APP_URL
    return SyncDb(APP_URL)


@pytest.fixture
def db(migrated: None, sync_db: SyncDb) -> SyncDb:
    """Owner-side seeding: a proposal, an autonomy decision and one publish attempt."""

    def seed(c: Connection) -> None:
        _base(c)
        _with_transition("autonomy_decided")(c)
        _decision(c)
        _attempt(c, attempt=1)

    sync_db.run(seed)
    return sync_db


def _denied(app_db: SyncDb, sql: str) -> str | None:
    with pytest.raises(DBAPIError) as info:
        app_db.run(lambda c: c.execute(text(sql)))
    return _sqlstate(info.value)


@requires_app_role
def test_the_application_connects_as_a_least_privilege_role(db: SyncDb, app_db: SyncDb) -> None:
    row: Any = app_db.run(
        lambda c: c.execute(
            text(
                "SELECT current_user, r.rolsuper, r.rolcreatedb, r.rolcreaterole, "
                "r.rolreplication, r.rolbypassrls FROM pg_roles r WHERE r.rolname = current_user"
            )
        ).one()
    )
    assert row[0] == "ken_app"
    assert row[1:] == (False, False, False, False, False)


@requires_app_role
def test_the_role_audit_finds_no_violation(db: SyncDb) -> None:
    async def audit() -> list[str]:
        from sqlalchemy.ext.asyncio import create_async_engine
        from sqlalchemy.pool import NullPool

        engine = create_async_engine(db._url, poolclass=NullPool)
        try:
            async with engine.connect() as connection:
                return await db_roles.audit_role(connection, "ken_app")
        finally:
            await engine.dispose()

    import asyncio

    assert asyncio.run(audit()) == []


@requires_app_role
@pytest.mark.parametrize("table", ["publish_attempts", "autonomy_decisions", "events", "pages"])
def test_truncate_is_denied(db: SyncDb, app_db: SyncDb, table: str) -> None:
    assert _denied(app_db, f"TRUNCATE {table}") == INSUFFICIENT_PRIVILEGE
    # The retry budget (publish_attempts) is intact.
    assert db.run(lambda c: c.execute(text("SELECT count(*) FROM publish_attempts")).scalar_one())


@requires_app_role
@pytest.mark.parametrize(
    "statement",
    [
        "DROP TABLE publish_attempts",
        "ALTER TABLE publish_attempts ADD COLUMN sneaky int",
        "CREATE TABLE sneaky (id int)",
        "ALTER TABLE publish_attempts DISABLE TRIGGER ALL",
        "ALTER TABLE publish_attempts DISABLE TRIGGER publish_attempts_immutable",
        "DROP TRIGGER publish_attempts_immutable ON publish_attempts",
        "DROP FUNCTION ken_skill_guard() CASCADE",
        "CREATE SCHEMA sneaky",
        "SET session_replication_role = replica",
        "CREATE ROLE sneaky",
        "ALTER ROLE ken_app SUPERUSER",
    ],
)
def test_ddl_and_trigger_bypass_are_denied(db: SyncDb, app_db: SyncDb, statement: str) -> None:
    assert _denied(app_db, statement) == INSUFFICIENT_PRIVILEGE


@requires_app_role
@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE publish_attempts SET succeeded = false",
        "DELETE FROM publish_attempts",
        "UPDATE autonomy_decisions SET decision = 'auto_publish'",
        "DELETE FROM autonomy_decisions",
    ],
)
def test_the_append_only_triggers_still_refuse_update_and_delete(
    db: SyncDb, app_db: SyncDb, statement: str
) -> None:
    assert _denied(app_db, statement) == OBJECT_NOT_IN_PREREQUISITE_STATE


@requires_app_role
def test_the_migration_ledger_is_read_only(db: SyncDb, app_db: SyncDb) -> None:
    assert app_db.run(lambda c: c.execute(text("SELECT version_num FROM alembic_version")).one())
    assert _denied(app_db, "UPDATE alembic_version SET version_num = 'x'") == INSUFFICIENT_PRIVILEGE
    assert _denied(app_db, "DELETE FROM alembic_version") == INSUFFICIENT_PRIVILEGE


@requires_app_role
def test_the_legitimate_dml_advisory_lock_and_setting_paths_work(
    db: SyncDb, app_db: SyncDb
) -> None:
    def work(c: Connection) -> None:
        c.execute(text("SELECT pg_advisory_xact_lock(42)"))
        assert c.execute(text("SELECT pg_try_advisory_lock(43)")).scalar_one() is True
        c.execute(text("SELECT pg_advisory_unlock(43)"))
        c.execute(text(LEARNING_PROMOTER_ON))
        assert (
            c.execute(text("SELECT current_setting('ken.learning_promoter')")).scalar_one() == "on"
        )
        # an INSERT that draws a sequence value (identity/serial) as the application does
        _attempt(c, attempt=2)

    app_db.run(work)
    assert (
        db.run(lambda c: c.execute(text("SELECT count(*) FROM publish_attempts")).scalar_one()) == 2
    )


@requires_app_role
def test_the_promotion_guard_still_requires_the_setting_for_the_application_role(
    db: SyncDb, app_db: SyncDb
) -> None:
    db.run(_to_offline_test)
    # Without the transaction-local setting the guard refuses, exactly as for the owner.
    with pytest.raises(DBAPIError) as info:
        app_db.run(lambda c: _promote(c, guc=False))
    assert _sqlstate(info.value) == OBJECT_NOT_IN_PREREQUISITE_STATE
    assert _status(db) == "offline_test"
    # The service path sets it inside its own transaction.
    app_db.run(_promote)
    assert _status(db) == "promoted"


@requires_app_role
def test_a_setting_does_not_outlive_its_transaction(db: SyncDb, app_db: SyncDb) -> None:
    """is_local = true: one transaction's setting cannot leak into the next on a pooled
    connection (the pool-leak guard the promotion design relies on)."""
    app_db.run(lambda c: c.execute(text(LEARNING_PROMOTER_ON)))
    assert (
        app_db.run(
            lambda c: c.execute(
                text("SELECT coalesce(current_setting('ken.learning_promoter', true), '')")
            ).scalar_one()
        )
        == ""
    )
    assert LEARNING_ACTIVATOR_ON  # the activator setting is exercised by the lifecycle tests


@requires_app_role
async def test_the_shared_sessionmaker_fixture_really_connects_as_the_application_role(
    migrated: None, sessionmaker: Any
) -> None:
    """Guards the slice runs: the code under test is not silently still on the owner."""
    async with sessionmaker() as session:
        assert (await session.execute(text("SELECT current_user"))).scalar_one() == "ken_app"
