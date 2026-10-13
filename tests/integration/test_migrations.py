"""Migrations 0001-0002 against a real PostgreSQL (the ``*_test`` database)."""

from __future__ import annotations

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import Connection, inspect, text

import ken_core.db.models  # noqa: F401 -- registers every model
from ken_core.db.base import Base
from tests.integration.conftest import SyncDb

pytestmark = pytest.mark.integration

APP_TABLES = frozenset(
    {
        "events",
        "ingestion_runs",
        "ingestion_issues",
        "pages",
        "page_versions",
        "existing_links",
        "known_urls",
    }
)


def _tables(db: SyncDb) -> set[str]:
    return db.run(lambda c: set(inspect(c).get_table_names()))


def _extensions(db: SyncDb) -> set[str]:
    return db.run(lambda c: set(c.execute(text("SELECT extname FROM pg_extension")).scalars()))


def _revision(db: SyncDb) -> str | None:
    return db.run(lambda c: MigrationContext.configure(c).get_current_revision())


def test_upgrade_downgrade_upgrade_round_trip(alembic_config: Config, sync_db: SyncDb) -> None:
    head = ScriptDirectory.from_config(alembic_config).get_current_head()
    assert head == "0002_content_inventory"

    command.upgrade(alembic_config, "head")
    assert _tables(sync_db) >= APP_TABLES
    assert _revision(sync_db) == head

    command.downgrade(alembic_config, "base")
    assert not APP_TABLES & _tables(sync_db)
    assert _revision(sync_db) is None

    command.upgrade(alembic_config, "head")
    assert _tables(sync_db) >= APP_TABLES
    assert _revision(sync_db) == head

    # ADR-0003: pgvector is provisioned by the image but never enabled in Phase 0.
    assert "vector" not in _extensions(sync_db)


def test_orm_matches_migration(alembic_config: Config, sync_db: SyncDb) -> None:
    command.upgrade(alembic_config, "head")

    def _diff(connection: Connection) -> list[object]:
        context = MigrationContext.configure(
            connection,
            opts={"compare_type": True, "compare_server_default": True},
        )
        return list(compare_metadata(context, Base.metadata))

    assert sync_db.run(_diff) == []


def test_constraint_names_match_naming_convention(alembic_config: Config, sync_db: SyncDb) -> None:
    """Autogenerate does not compare CHECK constraints, so assert them directly."""
    command.upgrade(alembic_config, "head")
    table = Base.metadata.tables["events"]

    def _reflect(
        connection: Connection,
    ) -> tuple[object, set[str], set[str], list[dict[str, object]]]:
        inspector = inspect(connection)
        return (
            inspector.get_pk_constraint("events")["name"],
            {str(c["name"]) for c in inspector.get_unique_constraints("events")},
            {str(c["name"]) for c in inspector.get_check_constraints("events")},
            [dict(i) for i in inspector.get_indexes("events")],
        )

    pk_name, uniques, checks, indexes = sync_db.run(_reflect)
    assert pk_name == table.primary_key.name
    assert uniques == {"uq_events_event_id"}
    assert checks == {"ck_events_event_version_positive"}
    # PostgreSQL backs a UNIQUE constraint with an index; that one is not ours.
    assert {i["name"] for i in indexes if not i.get("duplicates_constraint")} == {
        str(index.name) for index in table.indexes
    }


def test_identity_is_generated_always(alembic_config: Config, sync_db: SyncDb) -> None:
    command.upgrade(alembic_config, "head")
    identity = sync_db.run(
        lambda c: tuple(
            c.execute(
                text(
                    "SELECT is_identity, identity_generation FROM information_schema.columns "
                    "WHERE table_name = 'events' AND column_name = 'id'"
                )
            ).one()
        )
    )
    assert identity == ("YES", "ALWAYS")
